import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from upload_portal import (
    PortalError,
    complete_upload,
    create_signed_upload_url,
    create_upload_request,
    verify_upload_token,
)

logger = logging.getLogger("upload_portal")

STATIC_ROOT = Path(__file__).parent / "upload_portal_static"

DEFAULT_ALLOWED_ORIGINS = [
    "https://upload.alphasourceai.com",
    "https://alphasourceai.com",
    "https://www.alphasourceai.com",
]
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("PORTAL_RATE_LIMIT_WINDOW_SECONDS", "600"))
RATE_LIMIT_MAX = int(os.getenv("PORTAL_RATE_LIMIT_MAX", "5"))
_rate_limit_store = {}


def _allowed_origin(origin: Optional[str]) -> Optional[str]:
    allowlist = [item.strip() for item in os.getenv("PORTAL_ALLOWED_ORIGINS", "").split(",") if item.strip()]
    if not allowlist:
        allowlist = DEFAULT_ALLOWED_ORIGINS
    if origin and origin in allowlist:
        return origin
    return None


def _origin_allowed(origin: Optional[str]) -> bool:
    if not origin:
        return True
    return _allowed_origin(origin) is not None


def _content_type_for(path: Path) -> str:
    if path.suffix == ".css":
        return "text/css; charset=utf-8"
    if path.suffix == ".js":
        return "application/javascript; charset=utf-8"
    if path.suffix == ".svg":
        return "image/svg+xml"
    if path.suffix == ".png":
        return "image/png"
    return "text/html; charset=utf-8"


def _get_client_ip(headers: dict, fallback: str = "") -> str:
    forwarded = headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return fallback


def _rate_limit_ok(key: str) -> bool:
    if not key:
        return True
    now = time.time()
    entries = _rate_limit_store.get(key, [])
    entries = [t for t in entries if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(entries) >= RATE_LIMIT_MAX:
        _rate_limit_store[key] = entries
        return False
    entries.append(now)
    _rate_limit_store[key] = entries
    return True


def _parse_json_body(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        raise PortalError("invalid_json", "Invalid JSON payload", status=400)


def _get_bearer_token(headers: dict) -> str:
    header = headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return ""


def _static_file_for_path(path: str) -> Optional[Path]:
    if path in ("/uploads", "/uploads/"):
        file_path = STATIC_ROOT / "index.html"
    else:
        relative = path[len("/uploads/"):] if path.startswith("/uploads/") else ""
        file_path = (STATIC_ROOT / relative).resolve()
    if not str(file_path).startswith(str(STATIC_ROOT.resolve())):
        return None
    if not file_path.exists() or file_path.is_dir():
        return None
    return file_path


def _handle_api_get(path: str) -> Tuple[int, dict]:
    if path == "/api/upload-portal/health":
        return 200, {"ok": True, "status": "healthy"}
    return 404, {"error": "Not found", "code": "not_found", "detail": None}


def _handle_api_post(path: str, body: dict, headers: dict, client_ip: str) -> Tuple[int, dict]:
    if path == "/api/upload-portal/request":
        if not _rate_limit_ok(client_ip):
            return 429, {"error": "Too many requests", "code": "rate_limited", "detail": None}
        result = create_upload_request(body.get("email", ""), request_ip=client_ip)
        return 200, {"ok": True, "data": result}
    if path == "/api/upload-portal/verify":
        result = verify_upload_token(body.get("token", ""))
        return 200, {"ok": True, "data": result}
    if path == "/api/upload-portal/signed-upload-url":
        session_token = _get_bearer_token(headers)
        result = create_signed_upload_url(
            session_token,
            body.get("filename", ""),
            body.get("content_type"),
            body.get("byte_size"),
        )
        return 200, {"ok": True, "data": result}
    if path == "/api/upload-portal/complete":
        session_token = _get_bearer_token(headers)
        result = complete_upload(session_token, body.get("upload_id", ""))
        return 200, {"ok": True, "data": result}
    return 404, {"error": "Not found", "code": "not_found", "detail": None}


class UploadPortalHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        self.send_response(status)
        self._add_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def _send_text(self, status: int, text: str) -> None:
        self.send_response(status)
        self._add_cors_headers()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def _add_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        allowed = _allowed_origin(origin)
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def _parse_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return _parse_json_body(raw)

    def _serve_static(self, path: str) -> None:
        file_path = _static_file_for_path(path)
        if not file_path:
            self._send_text(404, "Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", _content_type_for(file_path))
        self.end_headers()
        self.wfile.write(file_path.read_bytes())

    def do_OPTIONS(self) -> None:
        if not _origin_allowed(self.headers.get("Origin")):
            self._send_json(403, {"error": "Origin not allowed", "code": "forbidden", "detail": None})
            return
        self.send_response(204)
        self._add_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/uploads"):
            self._serve_static(parsed.path)
            return
        if parsed.path.startswith("/api/"):
            status, payload = _handle_api_get(parsed.path)
            self._send_json(status, payload)
            return
        self._send_text(404, "Not found")

    def do_POST(self) -> None:
        if not _origin_allowed(self.headers.get("Origin")):
            self._send_json(403, {"error": "Origin not allowed", "code": "forbidden", "detail": None})
            return
        parsed = urlparse(self.path)
        try:
            body = self._parse_json_body()
            status, payload = _handle_api_post(
                parsed.path,
                body,
                dict(self.headers),
                _get_client_ip(dict(self.headers), self.client_address[0] if self.client_address else ""),
            )
            self._send_json(status, payload)
        except PortalError as exc:
            payload = {"error": exc.message, "code": exc.code, "detail": exc.detail}
            self._send_json(exc.status, payload)
        except Exception:
            logger.exception("Upload portal server error")
            self._send_json(500, {"error": "Server error", "code": "server_error", "detail": None})


def get_tornado_routes():
    try:
        import tornado.web
    except Exception:
        return []

    class UploadPortalStaticHandler(tornado.web.RequestHandler):
        def get(self, path: Optional[str] = None) -> None:
            file_path = _static_file_for_path(self.request.path)
            if not file_path:
                self.set_status(404)
                self.finish("Not found")
                return
            self.set_header("Content-Type", _content_type_for(file_path))
            self.finish(file_path.read_bytes())

    class UploadPortalApiHandler(tornado.web.RequestHandler):
        def set_default_headers(self) -> None:
            origin = self.request.headers.get("Origin")
            allowed = _allowed_origin(origin)
            if allowed:
                self.set_header("Access-Control-Allow-Origin", allowed)
                self.set_header("Vary", "Origin")
            self.set_header("Access-Control-Allow-Headers", "authorization, content-type")
            self.set_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.set_header("Content-Type", "application/json")

        def options(self) -> None:
            if not _origin_allowed(self.request.headers.get("Origin")):
                self.set_status(403)
                self.finish(json.dumps({"error": "Origin not allowed", "code": "forbidden", "detail": None}))
                return
            self.set_status(204)
            self.finish()

        def get(self) -> None:
            if not _origin_allowed(self.request.headers.get("Origin")):
                self.set_status(403)
                self.finish(json.dumps({"error": "Origin not allowed", "code": "forbidden", "detail": None}))
                return
            try:
                status, payload = _handle_api_get(self.request.path)
                self.set_status(status)
                self.finish(json.dumps(payload))
            except Exception:
                logger.exception("Upload portal server error")
                self.set_status(500)
                self.finish(json.dumps({"error": "Server error", "code": "server_error", "detail": None}))

        def post(self) -> None:
            if not _origin_allowed(self.request.headers.get("Origin")):
                self.set_status(403)
                self.finish(json.dumps({"error": "Origin not allowed", "code": "forbidden", "detail": None}))
                return
            try:
                body = _parse_json_body(self.request.body)
                status, payload = _handle_api_post(
                    self.request.path,
                    body,
                    dict(self.request.headers),
                    _get_client_ip(dict(self.request.headers), self.request.remote_ip or ""),
                )
                self.set_status(status)
                self.finish(json.dumps(payload))
            except PortalError as exc:
                payload = {"error": exc.message, "code": exc.code, "detail": exc.detail}
                self.set_status(exc.status)
                self.finish(json.dumps(payload))
            except Exception:
                logger.exception("Upload portal server error")
                self.set_status(500)
                self.finish(json.dumps({"error": "Server error", "code": "server_error", "detail": None}))

    return [
        (r"/uploads", UploadPortalStaticHandler),
        (r"/uploads/(.*)", UploadPortalStaticHandler),
        (r"/api/upload-portal/health", UploadPortalApiHandler),
        (r"/api/upload-portal/request", UploadPortalApiHandler),
        (r"/api/upload-portal/verify", UploadPortalApiHandler),
        (r"/api/upload-portal/signed-upload-url", UploadPortalApiHandler),
        (r"/api/upload-portal/complete", UploadPortalApiHandler),
    ]


def run() -> None:
    host = os.getenv("UPLOAD_PORTAL_HOST", "0.0.0.0")
    port = int(os.getenv("UPLOAD_PORTAL_PORT", "8090"))
    server = ThreadingHTTPServer((host, port), UploadPortalHandler)
    logger.info("Upload portal server running on %s:%s", host, port)
    server.serve_forever()


if __name__ == "__main__":
    run()
