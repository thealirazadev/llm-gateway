"""Public liveness route: up and the database answers."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db import session_scope
from app.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.get("/health")
async def health() -> JSONResponse:
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.error("health.degraded", extra={"reason": str(exc)})
        return JSONResponse(status_code=503, content={"status": "degraded"})
    return JSONResponse(status_code=200, content={"status": "ok"})
