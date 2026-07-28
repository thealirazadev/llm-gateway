"""Model listing: the public names that have at least one active route."""

import time
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.auth import require_key
from app.db import session_scope
from app.errors import server_error
from app.logging import get_logger
from app.models import ModelRoute, VirtualKey

router = APIRouter()
logger = get_logger(__name__)


@router.get("/v1/models")
async def list_models(_: Annotated[VirtualKey, Depends(require_key)]) -> JSONResponse:
    try:
        with session_scope() as session:
            names = list(
                session.scalars(
                    select(ModelRoute.model)
                    .where(ModelRoute.active.is_(True))
                    .distinct()
                    .order_by(ModelRoute.model)
                )
            )
    except SQLAlchemyError as exc:
        logger.error("models.list_failed", extra={"reason": str(exc)})
        raise server_error() from exc

    created = int(time.time())
    return JSONResponse(
        content={
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": created, "owned_by": "llm-gateway"}
                for name in names
            ],
        }
    )
