"""KinoPub API client: OAuth 2.0 device flow, token refresh, request helper."""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

USER_AGENT = "KinoPubOffline/1.0 (macOS)"
TIMEOUT = 12          # a captive portal must not freeze the UI for half a minute


# --------------------------------------------------------------------------- #
# the API answers in Russian; the interface is English
# --------------------------------------------------------------------------- #
_ERROR_PATTERNS = [
    (r"Отсутствуют обязательные параметры:\s*(.+)", "Missing required parameter: %s"),
    (r"Неверные обязательные параметры:\s*(.+)", "Invalid parameter: %s"),
    (r"Не найден[оаы]?\b.*", "Not found"),
    (r"Доступ (?:запрещ|закрыт).*", "Access denied"),
    (r"Требуется авторизация.*", "Authorisation required"),
    (r"Неверный токен.*", "Invalid token"),
    (r"(?:Подписка|Абонемент).*(?:истек|законч).*", "Your subscription has expired"),
    (r"Слишком много запросов.*", "Too many requests - slow down"),
    (r"Внутренняя ошибка.*", "Server error on kino.pub"),
    (r"Сервис (?:временно )?недоступен.*", "kino.pub is temporarily unavailable"),
    (r"Файл не найден.*", "File not found on the server"),
    (r"Ссылка устарела.*", "This link has expired"),
]

_ERROR_WORDS = {
    "authorization_pending": "Waiting for you to confirm the code",
    "incorrect_client_credentials": "client_id or client_secret is wrong",
    "invalid_client": "client_id or client_secret is wrong",
    "invalid_grant": "The saved session is no longer valid",
    "invalid_request": "Malformed request",
    "access_denied": "Access denied",
    "expired_token": "The code expired - request a new one",
}


def translate_error(message):
    """Best-effort English for an API message, so the UI stays one language."""
    if not message:
        return message
    text = str(message).strip()
    known = _ERROR_WORDS.get(text.lower())
    if known:
        return known
    for pattern, replacement in _ERROR_PATTERNS:
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            return replacement % match.groups() if "%s" in replacement else replacement
    return text          # unknown wording: better the original than a guess


class ApiError(Exception):
    def __init__(self, message, status=0, payload=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload or {}


class NotAuthorized(ApiError):
    pass


class Offline(ApiError):
    pass


_lock = threading.RLock()
_refresh_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# token storage
# --------------------------------------------------------------------------- #
def load_tokens():
    return config.read_json(config.TOKENS_FILE, {})


def save_tokens(data):
    with _lock:
        config.write_json(config.TOKENS_FILE, data)


def clear_tokens():
    save_tokens({})


def is_authorized():
    return bool(load_tokens().get("access_token"))


def _store_token_response(payload):
    tokens = {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "expires_at": time.time() + int(payload.get("expires_in") or 3600) - 60,
        "obtained_at": time.time(),
    }
    save_tokens(tokens)
    return tokens


# --------------------------------------------------------------------------- #
# low level HTTP
# --------------------------------------------------------------------------- #
def _request(url, params=None, data=None, method=None, raw=False):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    body = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method=method or ("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except Exception:
            parsed = {"message": payload.decode("utf-8", "replace")[:400]}
        message = translate_error(
            parsed.get("error_description") or parsed.get("message")
            or parsed.get("error") or "HTTP %s" % exc.code)
        if exc.code in (401, 403):
            raise NotAuthorized(message, exc.code, parsed)
        raise ApiError(message, exc.code, parsed)
    except urllib.error.URLError as exc:
        raise Offline("No connection to kino.pub (%s)" % getattr(exc, "reason", exc))
    except OSError as exc:
        raise Offline("Network unavailable (%s)" % exc)
    if raw:
        return payload
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError:
        raise ApiError("Malformed JSON in API response")


# --------------------------------------------------------------------------- #
# device flow
# --------------------------------------------------------------------------- #
def _credentials():
    settings = config.load_settings()
    cid, secret = settings.get("client_id"), settings.get("client_secret")
    if not cid or not secret:
        raise ApiError("client_id / client_secret are not set — open Settings", 428)
    return cid, secret


def device_code():
    """Step 1: ask for a device code + user code."""
    cid, secret = _credentials()
    return _request(
        config.API_BASE + "/oauth2/device",
        data={"grant_type": "device_code", "client_id": cid, "client_secret": secret},
    )


def device_token(code):
    """Step 2/3: poll until the user confirms the code on kino.pub/device."""
    cid, secret = _credentials()
    try:
        payload = _request(
            config.API_BASE + "/oauth2/device",
            data={"grant_type": "device_token", "client_id": cid, "client_secret": secret, "code": code},
        )
    except ApiError as exc:
        error = (exc.payload or {}).get("error")
        if error in ("authorization_pending", "slow_down"):
            return {"pending": True, "error": error}
        raise
    _store_token_response(payload)
    return {"pending": False, "authorized": True}


def refresh():
    cid, secret = _credentials()
    tokens = load_tokens()
    token = tokens.get("refresh_token")
    if not token:
        raise NotAuthorized("No refresh_token — sign in again")
    try:
        payload = _request(
            config.API_BASE + "/oauth2/token",
            data={"grant_type": "refresh_token", "client_id": cid, "client_secret": secret,
                  "refresh_token": token},
        )
    except Offline:
        raise
    except ApiError as exc:
        # refresh_token lives 30 days; once it is gone the device must be re-linked
        clear_tokens()
        raise NotAuthorized("Session expired — link the device again (%s)" % exc.message)
    return _store_token_response(payload)


def _refresh_once(used_token):
    """Refresh the session, unless another thread already did it for us.

    kino.pub invalidates the old pair on every refresh, so two threads
    refreshing at once would revoke each other and drop a working session.
    A thread that finds a token different from the one its request used simply
    adopts it instead of refreshing again.
    """
    with _refresh_lock:
        current = load_tokens().get("access_token")
        if current and used_token and current != used_token:
            return current
        return refresh().get("access_token")


def access_token(auto_refresh=True):
    tokens = load_tokens()
    token = tokens.get("access_token")
    if not token:
        raise NotAuthorized("Application is not signed in")
    if auto_refresh and tokens.get("expires_at", 0) < time.time():
        with _refresh_lock:
            tokens = load_tokens()
            if tokens.get("expires_at", 0) < time.time():
                tokens = refresh()
        token = tokens.get("access_token")
    return token


# --------------------------------------------------------------------------- #
# public API calls
# --------------------------------------------------------------------------- #
def call(path, params=None, method="GET", data=None, retry=True):
    """Call a /v1 endpoint, transparently refreshing the token once on 401."""
    params = dict(params or {})
    token = access_token()
    params["access_token"] = token
    url = config.API_BASE + "/v1/" + path.lstrip("/")
    try:
        return _request(url, params=params, data=data, method=method)
    except NotAuthorized:
        if not retry:
            raise
        _refresh_once(token)
        return call(path, {k: v for k, v in params.items() if k != "access_token"},
                    method=method, data=data, retry=False)


def notify_device(title="KinoPub Offline (Mac)", hardware="Apple Mac", software="macOS"):
    """Best effort: tell the backend what this client supports."""
    try:
        return call("device/notify", params={"title": title, "hardware": hardware, "software": software},
                    method="POST")
    except ApiError:
        return None
