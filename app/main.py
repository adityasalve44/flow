"""
app/main.py — FastAPI application entry point.

This is a placeholder until FLOW-023 (webhook rewrite).
The full webhook will be implemented with proper ingress, auth and agent wiring.
"""

from fastapi import FastAPI

from app.api.middleware import RequestCorrelationMiddleware
from app.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


def create_app() -> FastAPI:
    application = FastAPI(
        title="Flow",
        version="0.1.0",
        description="AI-powered WhatsApp recruitment intake assistant",
    )
    application.add_middleware(RequestCorrelationMiddleware)
    return application


app = create_app()


@app.get("/health")
def health():
    return {"status": "ok"}
