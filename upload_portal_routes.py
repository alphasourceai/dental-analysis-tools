import json
import logging
import pathlib
import threading
import time

import tornado.web

BASE_DIR = pathlib.Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "upload_portal_static"
_logger = logging.getLogger("upload_portal")
_register_lock = threading.Lock()
_mount_lock = threading.Lock()
_routes_registered = False
_mount_started = False
_probe_logged = False
_probe_logged_selected = None

def _json(handler, payload, status=200):
    handler.set_header("Content-Type", "application/json")
    handler.set_status(status)
    handler.write(json.dumps(payload))

class UploadPortalHealthHandler(tornado.web.RequestHandler):
    def get(self):
        _json(self, {"ok": True, "service": "upload-portal"})

class UploadPortalIndexHandler(tornado.web.RequestHandler):
    def get(self):
        index_path = STATIC_DIR / "index.html"
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.write(index_path.read_text(encoding="utf-8"))

def _routes_already_registered(tornado_app) -> bool:
    settings = getattr(tornado_app, "settings", None)
    if isinstance(settings, dict) and settings.get("upload_portal_routes_added"):
        return True
    return bool(getattr(tornado_app, "_upload_portal_routes_added", False))


def _mark_routes_registered(tornado_app) -> None:
    settings = getattr(tornado_app, "settings", None)
    if isinstance(settings, dict):
        settings["upload_portal_routes_added"] = True
    else:
        setattr(tornado_app, "_upload_portal_routes_added", True)


def register_upload_portal_routes(tornado_app, portal_api_prefix="/api/upload-portal"):
    global _routes_registered
    if tornado_app is None:
        return False
    if _routes_registered:
        return False
    with _register_lock:
        if _routes_registered or _routes_already_registered(tornado_app):
            _routes_registered = True
            return False
        handlers = []

        # 1) API (must be exact, must come before any catch-alls)
        handlers.append((rf"{portal_api_prefix}/health/?", UploadPortalHealthHandler))

        # 2) Static (must come before /uploads catch-all)
        handlers.append((
            r"/uploads/static/(.*)",
            tornado.web.StaticFileHandler,
            {"path": str(STATIC_DIR / "static")}
        ))
        handlers.append((
            r"/uploads/(styles\.css)",
            tornado.web.StaticFileHandler,
            {"path": str(STATIC_DIR)}
        ))
        handlers.append((
            r"/uploads/(app\.js)",
            tornado.web.StaticFileHandler,
            {"path": str(STATIC_DIR)}
        ))

        # 3) Upload page routes (serve index.html)
        handlers.append((r"/uploads/?", UploadPortalIndexHandler))
        handlers.append((r"/uploads/.*", UploadPortalIndexHandler))

        tornado_app.add_handlers(r".*$", handlers)
        _mark_routes_registered(tornado_app)
        _routes_registered = True
        _logger.info("Upload portal routes registered.")
        return True


def _looks_like_tornado_app(value) -> bool:
    return isinstance(value, tornado.web.Application) or hasattr(value, "add_handlers")


def _log_tornado_probe(found, selected_label) -> None:
    global _probe_logged
    global _probe_logged_selected
    if _probe_logged and selected_label == _probe_logged_selected:
        return
    try:
        found_summary = {
            name: (type(value).__name__ if value is not None else None)
            for name, value in found.items()
        }
        _logger.info(
            "Upload portal tornado app probe: found=%s selected=%s",
            found_summary,
            selected_label,
        )
    except Exception:
        _logger.info("Upload portal tornado app probe: logging failed")
    _probe_logged = True
    _probe_logged_selected = selected_label


def _tornado_app_from_server(server):
    if server is None:
        return None
    found = {}
    selected = None
    selected_label = None
    for attr in ("_tornado", "_tornado_app", "tornado_app"):
        if hasattr(server, attr):
            value = getattr(server, attr, None)
            found[attr] = value
            if selected is None and _looks_like_tornado_app(value):
                selected = value
                selected_label = attr

    http_server = getattr(server, "_http_server", None)
    if http_server is not None:
        found["_http_server"] = http_server
        if selected is None and _looks_like_tornado_app(http_server):
            selected = http_server
            selected_label = "_http_server"
        for attr in ("_app", "_tornado_app", "_application", "_server_request_callback"):
            if hasattr(http_server, attr):
                value = getattr(http_server, attr, None)
                label = f"_http_server.{attr}"
                found[label] = value
                if selected is None and _looks_like_tornado_app(value):
                    selected = value
                    selected_label = label

    _log_tornado_probe(found, selected_label)
    return selected


def _schedule_register(server, tornado_app) -> None:
    ioloop = getattr(server, "_ioloop", None)
    if ioloop is None:
        http_server = getattr(server, "_http_server", None)
        ioloop = getattr(http_server, "_ioloop", None) or getattr(http_server, "io_loop", None)
    if ioloop is not None and hasattr(ioloop, "add_callback"):
        ioloop.add_callback(register_upload_portal_routes, tornado_app)
        return
    register_upload_portal_routes(tornado_app)


def _mount_worker(poll_interval: float, timeout: float) -> None:
    try:
        from streamlit.web.server.server import Server
    except Exception as exc:
        _logger.warning("Upload portal routes not mounted; Streamlit server unavailable: %s", exc)
        return
    deadline = time.time() + timeout
    while time.time() < deadline and not _routes_registered:
        server = Server.get_current()
        tornado_app = _tornado_app_from_server(server)
        if tornado_app is not None:
            _schedule_register(server, tornado_app)
            return
        time.sleep(poll_interval)
    if not _routes_registered:
        _logger.warning("Upload portal routes not mounted; timed out waiting for Streamlit server.")


def ensure_upload_portal_routes_mounted(poll_interval: float = 0.25, timeout: float = 30.0) -> None:
    if _routes_registered:
        return
    _logger.info("Upload portal route mount starting.")
    try:
        from streamlit.web.server.server import Server
    except Exception as exc:
        _logger.warning("Upload portal routes not mounted; Streamlit server unavailable: %s", exc)
        return
    server = Server.get_current()
    tornado_app = _tornado_app_from_server(server)
    if tornado_app is not None:
        _schedule_register(server, tornado_app)
        return
    global _mount_started
    with _mount_lock:
        if _mount_started or _routes_registered:
            return
        _mount_started = True
    thread = threading.Thread(
        target=_mount_worker,
        args=(poll_interval, timeout),
        name="upload-portal-mounter",
        daemon=True,
    )
    thread.start()
