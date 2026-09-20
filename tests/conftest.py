"""Shared test fixtures.

The suite runs against an in-memory SQLite database: phase 1 declared
``evento_dominio.datos`` as ``JSON().with_variant(JSONB, "postgresql")``
precisely so this works. ``SELECT ... FOR UPDATE`` is a no-op there, which is
why the concurrency test asserts the OUTCOME (one 201, one 409) and never the
locking mechanism.
"""

import os
from datetime import date, datetime, time, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "clave-solo-para-pruebas")
os.environ.setdefault("CORS_ORIGINS", "*")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core import security  # noqa: E402

# bcrypt with cost 12 is a deliberate production cost (RNF-012) and a very
# expensive one to pay on every seeded user of every test. The suite lowers it
# to the library minimum; nothing else about hashing changes.
security.contexto_password = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=4)

from app.config import settings  # noqa: E402
from app.core.horario import ZONA_LIMA, ahora  # noqa: E402
from app.database import Base  # noqa: E402
from app.deps import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Servicio, Usuario  # noqa: E402  (registers the metadata)
from app.seed import ejecutar_seed  # noqa: E402

RUTA = "/api/v1"


# --------------------------------------------------------------------------
# Date helpers: every test books on a date whose opening window is known.
# --------------------------------------------------------------------------
def proximo_dia(dia_semana: int, dias_minimos: int = 1) -> date:
    """Next date with that ``weekday()``, at least ``dias_minimos`` days ahead."""
    fecha = ahora().date() + timedelta(days=dias_minimos)
    while fecha.weekday() != dia_semana:
        fecha += timedelta(days=1)
    return fecha


def proximo_lunes(dias_minimos: int = 1) -> date:
    """A Monday: opening window 08:00-19:00 (RN-07)."""
    return proximo_dia(0, dias_minimos)


def proximo_domingo(dias_minimos: int = 1) -> date:
    """A Sunday: opening window 09:00-14:00 (RN-07)."""
    return proximo_dia(6, dias_minimos)


def instante(fecha: date, hora: int = 10, minuto: int = 0) -> datetime:
    """An aware America/Lima datetime, the way the mobile app sends it."""
    return datetime.combine(fecha, time(hora, minuto), tzinfo=ZONA_LIMA)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
@pytest.fixture()
def db():
    """A seeded, isolated in-memory database per test."""
    motor = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=motor)
    Sesion = sessionmaker(bind=motor, autoflush=False, autocommit=False)
    sesion = Sesion()
    ejecutar_seed(sesion)
    try:
        yield sesion
    finally:
        sesion.close()
        Base.metadata.drop_all(bind=motor)
        motor.dispose()


@pytest.fixture()
def cliente_http(db):
    """Unauthenticated client wired to the test database."""

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    # No ``with``: entering the context manager would run the lifespan, which
    # tries to seed the real PostgreSQL database.
    http = TestClient(app)
    try:
        yield http
    finally:
        app.dependency_overrides.clear()


def _autenticar(http: TestClient, correo: str, password: str) -> TestClient:
    respuesta = http.post(f"{RUTA}/auth/login", json={"correo": correo, "password": password})
    assert respuesta.status_code == 200, respuesta.text
    token = respuesta.json()["access_token"]
    autenticado = TestClient(app)
    autenticado.headers.update({"Authorization": f"Bearer {token}"})
    return autenticado


@pytest.fixture()
def api_cliente(cliente_http):
    """Authenticated as the demo customer (permissions of the ``cliente`` role)."""
    return _autenticar(cliente_http, settings.seed_cliente_correo, settings.seed_cliente_password)


@pytest.fixture()
def api_recepcion(cliente_http):
    """Authenticated as the demo receptionist.

    RF-004 v1.0 splits the MVP role ``personal`` in two: the counter
    (check-in, assignment, charge, delivery) and the bay (state advance).
    """
    return _autenticar(
        cliente_http, settings.seed_recepcion_correo, settings.seed_recepcion_password
    )


@pytest.fixture()
def api_operario(cliente_http):
    """Authenticated as the demo bay operator (RF-021)."""
    return _autenticar(cliente_http, settings.seed_operario_correo, settings.seed_operario_password)


@pytest.fixture()
def api_admin(cliente_http):
    """Authenticated as the demo administrator."""
    return _autenticar(cliente_http, settings.seed_admin_correo, settings.seed_admin_password)


# --------------------------------------------------------------------------
# Seeded reference data
# --------------------------------------------------------------------------
def _servicio(db, nombre: str) -> Servicio:
    servicio = db.scalars(select(Servicio).where(Servicio.nombre == nombre)).first()
    assert servicio is not None, f"El seed debería contener «{nombre}»"
    return servicio


@pytest.fixture()
def servicio_corto(db) -> Servicio:
    """ "Lavado Express": 30 minutes, S/ 15.00. Easy availability arithmetic."""
    return _servicio(db, "Lavado Express")


@pytest.fixture()
def servicio_medio(db) -> Servicio:
    """ "Lavado Completo": 45 minutes, S/ 25.00."""
    return _servicio(db, "Lavado Completo")


def _usuario(db, correo: str) -> Usuario:
    usuario = db.scalars(select(Usuario).where(Usuario.correo == correo)).first()
    assert usuario is not None, f"El seed debería crear «{correo}»"
    return usuario


@pytest.fixture()
def usuario_cliente(db) -> Usuario:
    return _usuario(db, settings.seed_cliente_correo)


@pytest.fixture()
def usuario_operario(db) -> Usuario:
    return _usuario(db, settings.seed_operario_correo)


@pytest.fixture()
def usuario_admin(db) -> Usuario:
    return _usuario(db, settings.seed_admin_correo)


@pytest.fixture()
def vehiculo_id(api_cliente) -> int:
    """The demo vehicle seeded for the demo customer (ABC-123)."""
    respuesta = api_cliente.get(f"{RUTA}/vehiculos")
    assert respuesta.status_code == 200, respuesta.text
    items = respuesta.json()["items"]
    assert items, "El seed debería dejar un vehículo de demostración"
    return items[0]["id"]


# --------------------------------------------------------------------------
# Action helpers
# --------------------------------------------------------------------------
def crear_vehiculo(api: TestClient, placa: str) -> int:
    respuesta = api.post(
        f"{RUTA}/vehiculos",
        json={
            "placa": placa,
            "tipo": "suv",
            "marca": "Kia",
            "modelo": "Sportage",
            "color": "Negro",
            "anio": 2021,
        },
    )
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()["id"]


def crear_reserva(api: TestClient, servicio_id: int, vehiculo_id: int, inicio: datetime):
    """POST /reservas with an aware start time. Returns the raw response."""
    return api.post(
        f"{RUTA}/reservas",
        json={
            "servicio_id": servicio_id,
            "vehiculo_id": vehiculo_id,
            "inicio": inicio.isoformat(),
        },
    )


def codigo_error(respuesta) -> str:
    """The ``error.codigo`` of a uniform error body."""
    return respuesta.json()["error"]["codigo"]


#: The bay chain of Annex A v1.0 (RF-021): what the operator walks through
#: after the receptionist assigns the service.
ESTADOS_DE_BAHIA = ("en_lavado", "secado", "acabado", "finalizado")


def asignar(api_recepcion, reserva_id: int, **cuerpo):
    """``POST /reservas/{id}/asignacion``: the real operation of RF-020.

    INC-1A had to fake this jump with a direct write because the endpoint did
    not exist yet; INC-1B implements it, so the bridge is gone and every test
    walks the chain the way the shop does.
    """
    return api_recepcion.post(f"{RUTA}/reservas/{reserva_id}/asignacion", json=cuerpo)


def avanzar_estado(api, reserva_id: int, estado: str):
    """``POST /reservas/{id}/estado``: the generic move of RF-021."""
    return api.post(f"{RUTA}/reservas/{reserva_id}/estado", json={"estado": estado})


def llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva_id: int) -> None:
    """Walk a confirmed reservation down the whole v1.0 operative chain.

    ``confirmada -> en_recepcion -> asignado -> en_lavado -> secado -> acabado
    -> finalizado``, every move through its own endpoint: check-in (RF-019),
    assignment (RF-020) and the four bay moves (RF-021).
    """
    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva_id}/check-in", json={"confirmar_retraso": False}
    )
    assert respuesta.status_code == 200, respuesta.text

    respuesta = asignar(api_recepcion, reserva_id)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["asignacion"] is not None, respuesta.text

    for estado in ESTADOS_DE_BAHIA:
        respuesta = avanzar_estado(api_operario, reserva_id, estado)
        assert respuesta.status_code == 200, respuesta.text


def dejar_una_sola_bahia(db) -> None:
    """Deactivate every bay but the first one.

    That turns "the block is taken" into a one-line scenario: the second
    booking of the same slot has nowhere to go (RF-014 CA-02).
    """
    from app.models import Bahia

    for bahia in db.scalars(select(Bahia).order_by(Bahia.id)).all()[1:]:
        bahia.activa = False
    db.commit()
