"""
app/main.py — FastAPI application entry point.

Initializes the FastAPI application, mounts middlewares (request correlation, PII logging),
configures routers (webhook, health), and handles application lifecycle.
"""

from fastapi import FastAPI

from app.api.middleware import RequestCorrelationMiddleware
from app.api.recruiter import router as recruiter_router
from app.api.webhook import router as webhook_router
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
    application.include_router(recruiter_router)

    @application.get("/health")
    def health():
        """Healthcheck endpoint returning service status."""
        return {"status": "ok", "app": "flow", "version": "0.1.0"}

    return application


app = create_app()
