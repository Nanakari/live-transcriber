from __future__ import annotations

import mimetypes
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import resource_root
from .routes import router


def create_app() -> FastAPI:
    # Windows registry associations can label .js as text/plain. Browsers reject
    # that response when nosniff is enabled, so define the web asset MIME types.
    mimetypes.init()
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    app = FastAPI(title="多语言影音研析")

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        # The pinned TrustedHostMiddleware splits at the first colon, which
        # rejects bracketed IPv6 hosts. Validate the complete local authority.
        host = request.headers.get("host", "")
        if not re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\]|testserver)(?::[0-9]{1,5})?", host, re.IGNORECASE):
            return JSONResponse({"detail": "Invalid host header"}, status_code=400)
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{host}"
        if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != expected):
            return JSONResponse({"detail": "仅接受本机页面的请求。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        return response
    static_dir = resource_root() / "app" / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(router)
    return app


app = create_app()
