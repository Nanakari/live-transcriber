from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..config import resource_root
from .routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="多语言影音研析")
    static_dir = resource_root() / "app" / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(router)
    return app


app = create_app()
