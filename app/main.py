"""
app/main.py — FastAPI application entry point.

Initializes the FastAPI application, mounts middlewares (request correlation, PII logging),
configures routers (webhook, health), and handles application lifecycle.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.middleware import RequestCorrelationMiddleware
from app.api.recruiter import router as recruiter_router
from app.api.simulator import router as simulator_router
from app.api.webhook import router as webhook_router
from app.channel.whatsapp import whatsapp_router
from app.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application."""
    application = FastAPI(
        title="Flow",
        version="0.1.0",
        description="AI-powered WhatsApp recruitment intake assistant",
    )

    # Middlewares
    application.add_middleware(RequestCorrelationMiddleware)

    # Routes
    application.include_router(webhook_router)
    application.include_router(whatsapp_router)
    application.include_router(recruiter_router)
    application.include_router(simulator_router)

    # Static assets and interactive frontend
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        application.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @application.get("/", include_in_schema=False)
        @application.get("/simulator", include_in_schema=False)
        def index():
            """Serve the WhatsApp simulator & live agent inspector cockpit."""
            return FileResponse(static_dir / "index.html")

    @application.get("/health")
    def health():
        """Healthcheck endpoint returning service status."""
        return {"status": "ok", "app": "flow", "version": "0.1.0"}

    return application


app = create_app()
