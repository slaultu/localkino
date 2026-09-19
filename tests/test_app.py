# -*- coding: utf-8 -*-
"""Test suite for KinoPub Offline.

    python3 -m unittest discover -s tests -v      (or ./run_tests.sh)

Every test runs against a throwaway config profile and its own stub servers,
so nothing here touches the real settings, library or kino.pub.
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_PROFILE = tempfile.mkdtemp(prefix="kp-tests-")
os.environ.setdefault("KP_CONFIG_DIR", os.path.join(_PROFILE, "config"))
os.environ.setdefault("KP_API_BASE", "http://127.0.0.1:1")   # unreachable by default

from kinopub import api, config, downloader, routes, store  # noqa: E402


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(handler_cls):
    """Start a throwaway HTTP server, return (port, shutdown)."""
    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def stop():
        httpd.shutdown()
        httpd.server_close()

    return port, stop


def http(url, method="GET", headers=None, body=None, timeout=5):
    """Return (status, body_bytes) without raising on 4xx/5xx."""
    request = urllib.request.Request(url, method=method, data=body,
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# --------------------------------------------------------------------------- #
class TestFileNaming(unittest.TestCase):
    """Downloads must land on predictable, non-colliding paths."""

    def test_unsafe_characters_are_stripped(self):
        self.assertNotIn("/", downloader.safe_name("Some/Title: Part?"))
        self.assertTrue(downloader.safe_name(""))            # never empty

    def test_extension_follows_the_source(self):
        self.assertEqual(routes._extension_for("http://x/a.mkv", "http"), ".mkv")
        self.assertEqual(routes._extension_for("http://x/a.mp4", "http"), ".mp4")
        # HLS is always remuxed to mp4, whatever the playlist is called
        self.assertEqual(routes._extension_for("http://x/a.m3u8", "http"), ".mp4")
        self.assertEqual(routes._extension_for("http://x/a.mkv", "hls"), ".mp4")
        # an unknown extension must not end up on disk
        self.assertEqual(routes._extension_for("http://x/a.exe", "http"), ".mp4")

    def test_episode_and_movie_layout(self):
        episode = routes._build_rel_path(
            {"show_title": "Show", "title": "Show", "year": 2024,
             "season": 1, "episode": 2, "episode_title": "Pilot"}, "1080p")
        self.assertIn("S01E02", episode)
        self.assertTrue(episode.endswith(".mp4"))
        movie = routes._build_rel_path({"title": "Film", "year": 2024}, "720p")
        self.assertIn("Film", movie)
        self.assertIn("720p", movie)

    def test_two_videos_never_share_one_file(self):
        first = routes._build_rel_path({"title": "Same", "year": 2024}, "1080p")
        store.add({"rel_path": first, "media_id": "a", "status": "done"})
        second = routes._unique_rel_path(first, "b")
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith(".mp4"))


# --------------------------------------------------------------------------- #
class TestRestartBehaviour(unittest.TestCase):
    """What survives quitting the app mid-download."""

    def _reload_store(self, entries):
        config.write_json(config.LIBRARY_FILE, {"entries": entries})
        store._cache = None                    # force a re-read from disk
        return {e["media_id"]: e for e in store.all_entries()}

    def test_interrupted_download_resumes_but_manual_pause_does_not(self):
        entries = self._reload_store([
            {"id": "a1", "media_id": "crashed", "status": "downloading",
             "rel_path": "a.mp4"},
            {"id": "b1", "media_id": "paused", "status": "downloading",
             "paused_by_user": True, "rel_path": "b.mp4"},
            {"id": "c1", "media_id": "finished", "status": "done",
             "rel_path": "c.mp4"},
        ])
        self.assertEqual(entries["crashed"]["status"], "queued")
        self.assertEqual(entries["paused"]["status"], "paused")
        self.assertEqual(entries["finished"]["status"], "done")


# --------------------------------------------------------------------------- #
class TestDownloadGuards(unittest.TestCase):
    """Rules that stop a broken file being presented as a good one."""

    def test_disk_space_is_checked_with_a_margin(self):
        real = shutil.disk_usage
        shutil.disk_usage = lambda p: type("U", (), {"free": 2 * 1024 ** 3})()
        try:
            downloader._check_space(1 * 1024 ** 3)            # fits
            with self.assertRaises(downloader.NotEnoughSpace):
                downloader._check_space(5 * 1024 ** 3)        # far too big
            with self.assertRaises(downloader.NotEnoughSpace):
                downloader._check_space(int(1.8 * 1024 ** 3))  # inside the margin
        finally:
            shutil.disk_usage = real

    def test_only_transient_failures_are_retried(self):
        self.assertTrue(downloader._is_retryable(downloader.TransferTruncated("x")))
        self.assertTrue(downloader._is_retryable(urllib.error.URLError("boom")))
        self.assertTrue(downloader._is_retryable(
            urllib.error.HTTPError("u", 503, "busy", {}, None)))
        # a dead link or a full disk will not fix itself
        self.assertFalse(downloader._is_retryable(
            urllib.error.HTTPError("u", 404, "gone", {}, None)))
        self.assertFalse(downloader._is_retryable(downloader.NotEnoughSpace("x")))

    def test_errors_are_explained_in_plain_language(self):
        message = downloader._friendly_error(urllib.error.URLError("boom"))
        self.assertNotIn("URLError", message)
        self.assertIn("connection", message.lower())


# --------------------------------------------------------------------------- #
class TestConcurrency(unittest.TestCase):
    """Two things happening at once must not corrupt one download."""

    def test_only_one_worker_may_claim_an_entry(self):
        downloader._controls.clear()
        granted = []
        barrier = threading.Barrier(24)

        def contend():
            barrier.wait()
            if downloader._claim("same-entry") is not None:
                granted.append(1)

        threads = [threading.Thread(target=contend) for _ in range(24)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(granted), 1, "a second worker would corrupt the .part file")
        downloader._controls.clear()

    def test_concurrent_401s_trigger_exactly_one_refresh(self):
        """kino.pub invalidates the old pair on refresh, so two at once lock us out."""
        refreshes = []
        lock = threading.Lock()

        class Stub(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with lock:
                    refreshes.append(1)
                    issued = len(refreshes)
                time.sleep(0.2)                  # a real refresh takes a moment
                self._reply(200, {"access_token": "NEW%d" % issued,
                                  "expires_in": 3600, "refresh_token": "R%d" % issued})

            def do_GET(self):
                self._reply(401, {"status": 401, "message": "invalid credential"})

        port, shutdown = serve(Stub)
        previous_base = config.API_BASE
        config.API_BASE = "http://127.0.0.1:%d" % port
        config.save_settings({"client_id": "id", "client_secret": "secret"})
        api.save_tokens({"access_token": "OLD", "refresh_token": "OLDR",
                         "expires_at": time.time() + 3600})
        try:
            barrier = threading.Barrier(6)

            def call():
                barrier.wait()
                try:
                    api.call("items/fresh")
                except api.ApiError:
                    pass

            threads = [threading.Thread(target=call) for _ in range(6)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(len(refreshes), 1,
                             "each extra refresh revokes the previous session")
        finally:
            shutdown()
            config.API_BASE = previous_base
            api.clear_tokens()


# --------------------------------------------------------------------------- #
class ServerTestCase(unittest.TestCase):
    """Boots the real server on a throwaway profile, once for the whole class."""

    APP = "X-KP-App"

    @classmethod
    def setUpClass(cls):
        import subprocess
        cls.profile = tempfile.mkdtemp(prefix="kp-server-")
        cls.library = os.path.join(cls.profile, "library")
        os.makedirs(cls.library)
        config.write_json(os.path.join(cls.profile, "settings.json"), {
            "client_id": "", "client_secret": "", "library_dir": cls.library,
            "preferred_quality": "1080p", "stream_type": "http",
            "max_parallel_downloads": 2, "download_subtitles": True,
            "sync_watching": False,
        })
        cls.port = free_port()
        environment = dict(os.environ, KP_CONFIG_DIR=cls.profile,
                           KP_MAX_ATTEMPTS="2")   # keep the retry test brisk
        cls.process = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server.py"),
             "--port", str(cls.port), "--no-browser"],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = "http://127.0.0.1:%d" % cls.port
        for _ in range(60):                      # wait for it to answer
            try:
                status, _body = http(cls.base + "/api/state",
                                     headers={cls.APP: "1"}, timeout=1)
                if status == 200:
                    break
            except Exception:                    # noqa: BLE001 - still starting
                pass
            time.sleep(0.25)
        else:
            raise AssertionError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=10)
        shutil.rmtree(cls.profile, ignore_errors=True)


class TestServerSecurity(ServerTestCase):
    """The app listens on loopback while you browse the web."""

    def test_a_page_you_visit_cannot_drive_the_app(self):
        # no custom header: what an <img>, <form> or <script> can reach
        status, _ = http(self.base + "/api/state")
        self.assertEqual(status, 403)

    def test_cross_origin_writes_are_refused(self):
        status, _ = http(
            self.base + "/api/settings", method="POST",
            headers={self.APP: "1", "Origin": "https://evil.example",
                     "Content-Type": "application/json"},
            body=json.dumps({"library_dir": "/tmp/pwned"}).encode())
        self.assertEqual(status, 403)
        settings = config.read_json(os.path.join(self.profile, "settings.json"), {})
        self.assertEqual(settings["library_dir"], self.library)

    def test_dns_rebinding_is_refused(self):
        status, _ = http(self.base + "/api/state",
                         headers={self.APP: "1", "Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_form_encoded_bodies_are_rejected(self):
        # form posts need no preflight, so they must not be a way in
        status, _ = http(
            self.base + "/api/settings", method="POST",
            headers={self.APP: "1",
                     "Content-Type": "application/x-www-form-urlencoded"},
            body=b"library_dir=/tmp/pwned")
        self.assertEqual(status, 400)

    def test_the_app_itself_still_works(self):
        status, body = http(self.base + "/api/state", headers={self.APP: "1"})
        self.assertEqual(status, 200)
        self.assertIn("version", json.loads(body))
        self.assertEqual(http(self.base + "/")[0], 200)          # the page
        self.assertEqual(http(self.base + "/app.js")[0], 200)    # its assets

    def test_media_paths_cannot_escape_the_library(self):
        for attack in ("/media/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
                       "/media/%2e%2e/config/tokens.json"):
            status, _ = http(self.base + attack)
            self.assertIn(status, (403, 404), "%s leaked" % attack)


class TestMediaServing(ServerTestCase):
    """Local playback needs working seeks."""

    def setUp(self):
        self.folder = os.path.join(self.library, "Show (2024)")
        os.makedirs(self.folder, exist_ok=True)
        self.payload = bytes(range(256)) * 40          # 10240 bytes
        with open(os.path.join(self.folder, "clip.mp4"), "wb") as handle:
            handle.write(self.payload)
        self.url = self.base + "/media/Show%20(2024)/clip.mp4"

    def test_whole_file(self):
        status, body = http(self.url)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload)

    def test_range_request_returns_exactly_the_asked_bytes(self):
        request = urllib.request.Request(self.url, headers={"Range": "bytes=100-199"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), self.payload[100:200])

    def test_range_past_the_end_is_refused(self):
        status, _ = http(self.url, headers={"Range": "bytes=999999-"})
        self.assertEqual(status, 416)


class TestDownloadEndToEnd(ServerTestCase):
    """Queue a real download and check what lands on disk."""

    def setUp(self):
        self.payload = os.urandom(300 * 1024)
        payload = self.payload

        class Origin(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.endswith(".srt"):
                    body = b"1\n00:00:01,000 --> 00:00:04,000\nhello\n\n"
                    ctype = "text/plain"
                else:
                    body = payload
                    ctype = "video/mp4"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.origin_port, self.shutdown_origin = serve(Origin)

    def tearDown(self):
        self.shutdown_origin()

    def _queue(self, **overrides):
        body = {
            "item_id": 1, "media_id": "m1", "title": "Clip", "year": 2024,
            "quality": "1080p", "stream_type": "http", "duration": 10,
            "url": "http://127.0.0.1:%d/v.mp4" % self.origin_port,
            "subtitles": [{"lang": "rus",
                           "url": "http://127.0.0.1:%d/s.srt" % self.origin_port}],
        }
        body.update(overrides)
        status, response = http(self.base + "/api/downloads", method="POST",
                                headers={self.APP: "1",
                                         "Content-Type": "application/json"},
                                body=json.dumps(body).encode())
        self.assertEqual(status, 200)
        return json.loads(response)["entry"]

    def _wait_for(self, entry_id, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _status, body = http(self.base + "/api/downloads",
                                 headers={self.APP: "1"})
            for entry in json.loads(body)["entries"]:
                if entry["id"] == entry_id and entry["status"] in ("done", "error"):
                    return entry
            time.sleep(0.4)
        raise AssertionError("download never finished")

    def test_file_arrives_intact_with_subtitles(self):
        entry = self._wait_for(self._queue()["id"])
        self.assertEqual(entry["status"], "done", entry.get("error"))
        path = os.path.join(self.library, entry["rel_path"])
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), self.payload, "downloaded file differs")
        self.assertEqual(len(entry["subtitles"]), 1)
        subtitle = os.path.join(os.path.dirname(path), entry["subtitles"][0]["file"])
        with open(subtitle, encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith("WEBVTT"), "srt was not converted")

    def test_no_part_file_is_left_behind(self):
        self._wait_for(self._queue(media_id="m2")["id"])
        leftovers = [name for _root, _dirs, files in os.walk(self.library)
                     for name in files if name.endswith(".part")]
        self.assertEqual(leftovers, [])

    def test_a_dead_link_reports_an_error_and_keeps_no_file(self):
        entry = self._wait_for(
            self._queue(media_id="m3", url="http://127.0.0.1:1/missing.mp4",
                        subtitles=[])["id"],
            timeout=90)
        self.assertEqual(entry["status"], "error")
        self.assertTrue(entry["error"])
        self.assertFalse(os.path.exists(os.path.join(self.library, entry["rel_path"])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
