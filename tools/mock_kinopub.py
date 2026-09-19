"""Stand-in for the kino.pub API - try the app without real credentials.

    python3 tools/mock_kinopub.py                 # terminal 1
    KP_API_BASE=http://127.0.0.1:8130 ./run.sh    # terminal 2

Then enter any non-empty client_id / client_secret in Settings and authorise:
the device code is ABCDEF and the mock approves it after a couple of polls.
Serves a short test video, so downloads and offline playback are real.
"""
import json, os, re, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SAMPLE = os.environ.get("MOCK_SAMPLE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sample.mp4")
PORT = 8130
BASE = "http://127.0.0.1:%d" % PORT

STATE = {"approved_after": 2, "polls": 0, "marktime": [], "watchlist": set()}
FOLDERS = [{"id": 1, "title": "Посмотреть в самолёте", "count": 2, "views": 0},
           {"id": 2, "title": "Любимое", "count": 1, "views": 0}]
ITEM_FOLDERS = {1: [1]}
NEXT_FOLDER = [3]

POSTER = BASE + "/poster.svg"


def files(quality_set=("1080p", "720p", "480p")):
    sizes = {"1080p": (1920, 1080), "720p": (1280, 720), "480p": (854, 480)}
    return [{"w": sizes[q][0], "h": sizes[q][1], "quality": q,
             "url": {"http": BASE + "/media/sample.mp4",
                     "hls": BASE + "/media/sample.mp4",
                     "hls4": BASE + "/media/sample.mp4"}} for q in quality_set]


SUBS = [{"lang": "rus", "shift": 0, "embed": False, "url": BASE + "/media/sub.srt"}]


def base_item(i, title, type_):
    return {"id": i, "title": title, "type": type_, "year": 2020 + (i % 5),
            "cast": "Актёр Первый, Актриса Вторая", "director": "Режиссёр Такой",
            "plot": "Описание для %s — тестовые данные мок-сервера." % title,
            "rating": 7.5, "imdb": 7.1, "kinopoisk": 7.8, "quality": 1080,
            "duration": {"average": 1200, "total": 1200},
            "genres": [{"id": 1, "title": "Драма"}], "countries": [{"id": 1, "title": "США"}],
            "posters": {"small": POSTER, "medium": POSTER, "big": POSTER}}


def catalog(n=24, type_="movie"):
    return [base_item(i, "%s %d" % ("Фильм" if type_ == "movie" else "Сериал", i), type_)
            for i in range(1, n + 1)]


def movie_detail(i):
    item = base_item(i, "Фильм %d" % i, "movie")
    item["videos"] = [{"id": 1000 + i, "title": item["title"], "duration": 1200,
                       "watched": 0, "watching": {"status": 0, "time": 180},
                       "subtitles": SUBS, "files": files()}]
    return item


def serial_detail(i):
    item = base_item(i, "Сериал %d" % i, "serial")
    item["seasons"] = []
    for season in (1, 2):
        episodes = []
        for ep in (1, 2, 3):
            watched = 1 if (season == 1 and ep <= 2) else 0
            episodes.append({"id": i * 1000 + season * 10 + ep, "number": ep,
                             "title": "Серия %d" % ep, "duration": 2400,
                             "watched": watched,
                             "watching": {"status": 1 if watched else -1, "time": 0},
                             "subtitles": SUBS, "files": files(("1080p", "720p"))})
        item["seasons"].append({"title": "Сезон %d" % season, "number": season, "episodes": episodes})
    return item


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        path = urllib.parse.urlsplit(self.path).path
        if path == "/oauth2/device":
            if form.get("grant_type") == "device_code":
                return self._json({"code": "DEVCODE123", "user_code": "ABCDEF",
                                   "verification_uri": BASE + "/device",
                                   "expires_in": 600, "interval": 1})
            STATE["polls"] += 1
            if STATE["polls"] < STATE["approved_after"]:
                return self._json({"error": "authorization_pending",
                                   "error_description": "waiting"}, 400)
            return self._json({"access_token": "ACCESS1", "token_type": "bearer",
                               "expires_in": 3600, "refresh_token": "REFRESH1", "scope": ""})
        if path == "/oauth2/token":
            return self._json({"access_token": "ACCESS2", "token_type": "bearer",
                               "expires_in": 3600, "refresh_token": "REFRESH2"})
        # real API accepts these as POST form fields: fold them into the query
        if form:
            joiner = "&" if "?" in self.path else "?"
            self.path = self.path + joiner + urllib.parse.urlencode(form)
        return self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path, q = parsed.path, {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if path == "/media/sample.mp4":
            return self._file(SAMPLE, "video/mp4")
        if path == "/media/sub.srt":
            body = "1\n00:00:01,000 --> 00:00:05,000\nМок-субтитры\n\n".encode()
            self.send_response(200); self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            return self.wfile.write(body)
        if path == "/poster.svg":
            body = ("<svg xmlns='http://www.w3.org/2000/svg' width='300' height='450'>"
                    "<rect width='300' height='450' fill='#2b3648'/>"
                    "<text x='150' y='230' font-size='90' text-anchor='middle'>🎞</text></svg>").encode()
            self.send_response(200); self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            return self.wfile.write(body)

        if path.startswith("/v1/") and not q.get("access_token"):
            return self._json({"status": 401, "message": "You are requesting with an invalid credential.",
                               "name": "Unauthorized", "code": 0}, 401)

        if path in ("/v1/items/fresh", "/v1/items/hot", "/v1/items/popular"):
            return self._json({"status": 200, "items": catalog(12)})
        if path == "/v1/items":
            type_ = q.get("type", "movie")
            return self._json({"status": 200, "items": catalog(24, type_),
                               "pagination": {"total": 96, "current": int(q.get("page", 1)), "perpage": 24}})
        if path == "/v1/items/search":
            return self._json({"status": 200, "items": catalog(6)})
        if path == "/v1/genres":
            return self._json({"status": 200, "items": [{"id": 1, "title": "Драма"}, {"id": 2, "title": "Комедия"}]})
        if path == "/v1/types":
            return self._json({"status": 200, "items": [{"id": "movie", "title": "Фильмы"}]})
        if path == "/v1/collections":
            return self._json({"status": 200, "items": [
                {"id": 1, "title": "Лучшее за год", "posters": {"medium": POSTER}, "views": 10}]})
        if path == "/v1/collections/view":
            return self._json({"status": 200, "collection": {"id": 1, "title": "Лучшее за год"},
                               "items": catalog(8)})
        if path == "/v1/bookmarks":
            return self._json({"status": 200, "items": FOLDERS})
        if re.match(r"^/v1/bookmarks/\d+$", path):
            fid = int(path.rsplit("/", 1)[1])
            return self._json({"status": 200, "folder": next(f for f in FOLDERS if f["id"] == fid),
                               "items": catalog(4)})
        if path == "/v1/bookmarks/get-item-folders":
            item = int(q.get("item", 0))
            ids = ITEM_FOLDERS.get(item, [])
            return self._json({"status": 200, "folders": [f for f in FOLDERS if f["id"] in ids]})
        if path == "/v1/bookmarks/toggle-item":
            item, folder = int(q.get("item", 0)), int(q.get("folder", 0))
            current = ITEM_FOLDERS.setdefault(item, [])
            if folder in current:
                current.remove(folder)
            else:
                current.append(folder)
            return self._json({"status": 200})
        if path == "/v1/bookmarks/create":
            new = {"id": NEXT_FOLDER[0], "title": q.get("title", "Новая"), "count": 0, "views": 0}
            NEXT_FOLDER[0] += 1
            FOLDERS.append(new)
            return self._json({"status": 200, "item": new})
        if path == "/v1/watching/marktime":
            STATE["marktime"].append(q)
            return self._json({"status": 200})
        if path == "/v1/watching/togglewatchlist":
            item = int(q.get("id", 0))
            STATE["watchlist"] ^= {item}
            return self._json({"status": 200, "watching": int(item in STATE["watchlist"])})
        if path == "/v1/watching/serials":
            return self._json({"status": 200, "items": [
                {"id": 2, "title": "Сериал 2", "posters": {"medium": POSTER}, "new": 3, "year": 2021}]})
        if path == "/v1/watching/movies":
            return self._json({"status": 200, "items": [
                {"id": 1, "title": "Фильм 1", "posters": {"medium": POSTER}, "year": 2021}]})
        if path == "/v1/device/notify":
            return self._json({"status": 200})

        m = re.match(r"^/v1/items/(\d+)$", path)
        if m:
            i = int(m.group(1))
            return self._json({"status": 200, "item": serial_detail(i) if i % 2 == 0 else movie_detail(i)})
        return self._json({"status": 404, "message": "not found"}, 404)


if __name__ == "__main__":
    print("mock kino.pub on", BASE, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
