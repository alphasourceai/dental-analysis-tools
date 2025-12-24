import logging

from upload_portal_server import get_tornado_routes

logger = logging.getLogger("upload_portal")
_ROUTES_REGISTERED = False


def register_upload_portal_routes() -> None:
    global _ROUTES_REGISTERED
    if _ROUTES_REGISTERED:
        return

    try:
        from streamlit.web.server import server
    except Exception:
        logger.info("Upload portal routes not registered: Streamlit server not available")
        return

    try:
        streamlit_server = server.Server.get_current()
    except Exception:
        streamlit_server = None

    if not streamlit_server:
        logger.info("Upload portal routes not registered: Streamlit server not initialized")
        return

    routes = get_tornado_routes()
    if not routes:
        logger.warning("Upload portal routes not registered: Tornado unavailable")
        return

    if hasattr(streamlit_server, "add_routes"):
        streamlit_server.add_routes(routes)
        _ROUTES_REGISTERED = True
        logger.info("Upload portal routes registered via Streamlit add_routes")
        return

    app = getattr(streamlit_server, "_app", None) or getattr(streamlit_server, "_web_app", None)
    if app and hasattr(app, "add_handlers"):
        app.add_handlers(r".*$", routes)
        _ROUTES_REGISTERED = True
        logger.info("Upload portal routes registered via Streamlit app handlers")
        return

    logger.warning("Upload portal routes could not be registered: unsupported Streamlit server")
