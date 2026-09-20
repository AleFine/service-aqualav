"""FastAPI application factory and process entry point.

Alembic owns the schema: there is no ``Base.metadata.create_all`` here. The
container runs ``alembic upgrade head`` before starting uvicorn
(``docker-entrypoint.sh``), which is also what keeps the "clone and run" time
of RNF-015 under fifteen minutes.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import api_router
from app.config import settings
from app.core.errors import registrar_manejadores
from app.database import SessionLocal
from app.seed import ejecutar_seed

logger = logging.getLogger("aqualav")

DESCRIPCION = (
    "API del MVP de AquaLav: autenticación por permisos, catálogo de servicios, "
    "disponibilidad, reservas, operación del local y registro de pagos."
)


def _sembrar() -> None:
    """Run the idempotent seed if it is enabled.

    The Docker entrypoint already seeds before the first request; doing it here
    too covers ``uvicorn app.main:app`` started by hand. A failure is logged and
    swallowed: the API must still boot so the operator can read the error.
    """
    if not settings.seed_enabled:
        return
    db = SessionLocal()
    try:
        ejecutar_seed(db)
    except Exception as error:  # pragma: no cover - depends on the environment
        db.rollback()
        logger.warning("No se pudo ejecutar el seed inicial: %s", error)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _sembrar()
    yield


app = FastAPI(
    title="AquaLav API",
    version="0.1.0",
    description=DESCRIPCION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Uniform error body for every 4xx/5xx (contract section 0).
registrar_manejadores(app)

# RNF-005 M4: every resource of the contract lives under ``/api/v1``, and the
# liveness probe is one of them (``app/api/v1/health.py``). A bare ``/health``
# used to be published next to it, which left the schema with one path outside
# the prefix. ``/docs``, ``/redoc`` and ``/openapi.json`` do not count: RNF-005
# M1 requires them at exactly those paths.
app.include_router(api_router)
