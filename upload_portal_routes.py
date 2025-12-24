import os
import json
import pathlib
import tornado.web

BASE_DIR = pathlib.Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "upload_portal_static"

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

def register_upload_portal_routes(tornado_app, portal_api_prefix="/api/upload-portal"):
    handlers = []

    # 1) API (must be exact, must come before any catch-alls)
    handlers.append((rf"{portal_api_prefix}/health/?", UploadPortalHealthHandler))

    # 2) Static (must come before /uploads catch-all)
    handlers.append((
        r"/uploads/static/(.*)",
        tornado.web.StaticFileHandler,
        {"path": str(STATIC_DIR / "static")}
    ))

    # 3) Upload page routes (serve index.html)
    handlers.append((r"/uploads/?", UploadPortalIndexHandler))
    handlers.append((r"/uploads/.*", UploadPortalIndexHandler))

    tornado_app.add_handlers(r".*$", handlers)
