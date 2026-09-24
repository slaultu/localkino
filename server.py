#!/usr/bin/env python3
"""KinoPub Offline — local web app for browsing, streaming and downloading.

Run:  python3 server.py [--port 8777] [--no-browser]
"""
import argparse
import atexit
import json
import mimetypes
import os
import posixpath
import re
import signal
import socket
import subprocess
import sys
import time
import threading
import urllib.parse
import webbrowser
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kinopub import api, config, downloader, routes, store  # noqa: E402

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def build_id():
    """The build this process is running (captured when it started)."""
    return config.BUILD_ID
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/vtt", ".vtt")


class QuietServer(ThreadingHTTPServer):
    """A browser closing a connection mid-request is normal, not a crash."""

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "KinoPubOffline/1.0"
    protocol_version = "HTTP/1.1"

    # ----------------------------------------------------------------- utils
    def log_message(self, fmt, *args):
        if os.environ.get("KP_DEBUG"):
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, status, body=b"", content_type="application/octet-stream", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, data, status=200):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status, message):
        self._json({"error": message, "status": status}, status)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            # Deliberately no form-encoded fallback: form posts are "simple
            # requests" that any page can send cross-origin without a preflight.
            raise routes.HttpError(400, "Body must be JSON")

    # ---------------------------------------------------------------- routing
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

    def _guard(self, path):
        """Reject anything a web page you happen to be visiting could send us.

        The server is loopback-only, but a browser is happy to talk to it on a
        site's behalf, so three things are checked:
          * Host must be loopback  - stops DNS rebinding (evil.com -> 127.0.0.1)
          * Origin, when sent, must be us - stops cross-site form/fetch posts
          * /api/ needs a custom header - unforgeable from <img>/<form>/<script>
        """
        host_header = (self.headers.get("Host") or "")
        hostname = host_header.rsplit(":", 1)[0].strip("[]") if host_header else ""
        if hostname not in self.LOCAL_HOSTS:
            self._error(403, "Invalid Host header")
            return False

        origin = self.headers.get("Origin")
        if origin:
            origin_host = urllib.parse.urlsplit(origin).hostname or ""
            if origin_host not in self.LOCAL_HOSTS:
                self._error(403, "Cross-origin request refused")
                return False

        if path.startswith("/api/") and not self.headers.get("X-KP-App"):
            self._error(403, "Missing X-KP-App header")
            return False
        return True

    def _dispatch(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if not self._guard(path):
            return
        try:
            if path.startswith("/api/"):
                body = self._body() if method in ("POST", "DELETE") else {}
                self._api(method, path[len("/api"):], params, body)
            elif path.startswith("/media/"):
                self._serve_within(config.get("library_dir"), path[len("/media/"):])
            elif path.startswith("/poster/"):
                self._serve_within(config.POSTER_CACHE, os.path.basename(path))
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except routes.HttpError as exc:
            self._error(exc.status, exc.message)
        except api.Offline as exc:
            self._error(503, str(exc))
        except api.NotAuthorized as exc:
            self._error(401, str(exc))
        except api.ApiError as exc:
            self._error(exc.status or 500, str(exc))
        except Exception as exc:                      # noqa: BLE001
            if os.environ.get("KP_DEBUG"):
                import traceback
                traceback.print_exc()
            self._error(500, "%s: %s" % (type(exc).__name__, exc))

    def _api(self, method, path, params, body):
        # /api/kp/<anything> -> proxied kino.pub call
        if path.startswith("/kp/"):
            return self._json(routes.kp_proxy(path[len("/kp/"):], params, method, body))

        match = re.match(r"^/downloads/([0-9a-f]+)/limit$", path)
        if match and method == "POST":
            return self._json(routes.downloads_limit(match.group(1), body))

        match = re.match(r"^/downloads/([0-9a-f]+)/(\w+)$", path)
        if match and method == "POST":
            return self._json(routes.downloads_action(match.group(1), match.group(2)))

        match = re.match(r"^/library/([0-9a-f]+)/progress$", path)
        if match and method == "POST":
            return self._json(routes.library_progress(match.group(1), body))

        table = {
            ("GET", "/state"): routes.state,
            ("GET", "/settings"): routes.get_settings,
            ("POST", "/settings"): routes.save_settings,
            ("POST", "/auth/start"): routes.auth_start,
            ("POST", "/auth/poll"): routes.auth_poll,
            ("POST", "/auth/logout"): routes.auth_logout,
            ("GET", "/downloads"): routes.downloads_list,
            ("POST", "/downloads"): routes.downloads_add,
            ("GET", "/library"): routes.library_list,
            ("POST", "/reveal"): routes.reveal,
            ("POST", "/quit"): lambda _p, _b: (shutdown_soon(), {"stopping": True})[1],
        }
        handler = table.get((method, path))
        if not handler:
            return self._error(404, "No such route: %s %s" % (method, path))
        return self._json(handler(params, body))

    # ---------------------------------------------------------------- static
    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = posixpath.normpath(path).lstrip("/")
        target = os.path.join(WEB_DIR, rel)
        if not os.path.abspath(target).startswith(WEB_DIR) or not os.path.isfile(target):
            target = os.path.join(WEB_DIR, "index.html")     # SPA fallback
        self._serve_file(target, cache=True)

    def _serve_within(self, root, relative):
        """Serve `relative` only if it really resolves inside `root`."""
        root = os.path.realpath(root)
        target = os.path.realpath(os.path.join(root, relative.lstrip("/")))
        if target != root and not target.startswith(root + os.sep):
            return self._error(403, "Access denied")
        self._serve_file(target)

    def _serve_file(self, path, cache=False):
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return self._error(404, "File not found")
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        mtime = os.path.getmtime(path)
        etag = '"%x-%x"' % (int(mtime), size)
        if cache and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if match:
                if match.group(1):
                    start = int(match.group(1))
                    if match.group(2):
                        end = min(int(match.group(2)), size - 1)
                elif match.group(2):                      # suffix range
                    start = max(size - int(match.group(2)), 0)
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache" if cache else "no-store")
        if cache:
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", formatdate(mtime, usegmt=True))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


HTTPD = None          # set in main(), so a request can ask the server to stop


def shutdown_soon():
    """Let the current response finish, then close everything down tidily."""
    def stop():
        time.sleep(0.4)
        store.flush()
        release_instance()
        if HTTPD is not None:
            HTTPD.shutdown()
        # when launched from the Desktop app, close that too so the Dock agrees
        subprocess.Popen(
            ["osascript", "-e", 'tell application "KinoPub Offline" to quit'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=stop, daemon=True).start()


def running_instance():
    """Port of an instance already serving this profile, or None.

    Two copies sharing one config directory would each keep their own copy of
    library.json and overwrite the other's, and their download schedulers would
    fight over the same .part files - so the second copy defers to the first.

    Unless the first is running older code: after a rebuild you want the new
    version, so the previous one is stopped and this one takes over.
    """
    data = config.read_json(config.INSTANCE_FILE, {})
    pid, port = data.get("pid"), data.get("port")
    if not pid or not port or pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)                     # still alive?
    except OSError:
        return None                         # stale marker, pid is gone
    try:                                    # and is it really our app?
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/state" % port, headers={"X-KP-App": "1"})
        with urllib.request.urlopen(request, timeout=1.5) as response:
            state = json.loads(response.read().decode("utf-8"))
    except Exception:                       # noqa: BLE001 - not ours, or not answering
        return None
    if "version" not in state:
        return None
    if state.get("build") != build_id():
        print("  replacing the running copy (older build)", flush=True)
        _stop_instance(pid, port)
        return None
    return port


def _stop_instance(pid, port):
    """Stop the older copy directly with a signal.

    Deliberately not /api/quit: that also asks the desktop app to quit, and the
    app's quit handler kills whatever pid the instance file names - which by
    then would be this new server.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(40):                     # up to ~8s for it to wind down
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)        # last resort
    except OSError:
        pass
    time.sleep(0.5)


def claim_instance(port):
    config.write_json(config.INSTANCE_FILE, {"pid": os.getpid(), "port": port})
    atexit.register(release_instance)


def release_instance():
    data = config.read_json(config.INSTANCE_FILE, {})
    if data.get("pid") == os.getpid():
        try:
            os.remove(config.INSTANCE_FILE)
        except OSError:
            pass


def free_port(preferred):
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


def main():
    parser = argparse.ArgumentParser(description="KinoPub Offline")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    config.ensure_dirs()

    def leave(_signum, _frame):
        raise SystemExit(0)                 # lets the finally block run

    signal.signal(signal.SIGTERM, leave)

    already = running_instance()
    if already:
        url = "http://127.0.0.1:%d/" % already
        print("\n  KinoPub Offline is already running at %s\n" % url, flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return

    store.all_entries()
    downloader.start()

    port = free_port(args.port or int(config.get("port") or 8777))
    httpd = QuietServer(("127.0.0.1", port), Handler)
    global HTTPD
    HTTPD = httpd
    claim_instance(port)
    url = "http://127.0.0.1:%d/" % port
    print("\n  %s\n  %s\n  Library: %s\n  Ctrl+C to quit\n" % (config.APP_NAME, url, config.get("library_dir")), flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("\nStopping...", flush=True)
        store.flush()
        release_instance()
        httpd.shutdown()
    finally:
        release_instance()
        print("Stopped.", flush=True)


if __name__ == "__main__":
    main()
