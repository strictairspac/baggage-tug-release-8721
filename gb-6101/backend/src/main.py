import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from scripts.seed_demo import seed, setup_schema
from src.config.settings import SEED_DEMO
from src.domain.allocation import AllocationRuleError, InsufficientVehicles
from src.domain.scenario import CONFIG
from src.middlewares.errors import catch_errors
from src.middlewares.request_id import request_id
from src.routes import allocations, cases, runs, session, system


@asynccontextmanager
async def lifespan(_app):
    setup_schema()
    if SEED_DEMO:
        try:
            seed()
        except Exception:
            logging.getLogger(__name__).exception("Demo seed failed")
    yield


app = FastAPI(title=CONFIG["title"], lifespan=lifespan)


@app.exception_handler(AllocationRuleError)
async def allocation_rule_error_handler(_request: Request, exc: AllocationRuleError):
    conflict = isinstance(exc, InsufficientVehicles)
    return JSONResponse(
        status_code=409 if conflict else 422,
        content={
            "detail": str(exc),
            "code": "insufficient_available_vehicles" if conflict else "invalid_allocation_request",
            "required": getattr(exc, "required", None),
            "eligible": getattr(exc, "eligible", None),
            "free": getattr(exc, "free", None),
        },
    )


app.middleware("http")(catch_errors)
app.middleware("http")(request_id)
for group in (system, session, cases, runs, allocations):
    app.include_router(group.router)
