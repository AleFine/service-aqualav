"""SQLAlchemy models, one module per aggregate.

Importing this package registers every table on ``app.database.Base.metadata``,
which is what Alembic's ``env.py`` and the seed rely on.
"""

from app.models.bahia import MAXIMO_BAHIAS, Bahia
from app.models.enums import (
    MONEDA_PREDETERMINADA,
    EstadoCuenta,
    EstadoPago,
    EstadoReserva,
    MedioPago,
    ModalidadPago,
    TipoVehiculo,
)
from app.models.evento import EventoDominio
from app.models.pago import Pago
from app.models.reserva import Reserva, ReservaEstadoHistorial, TransicionEstado
from app.models.rol import Permiso, Rol, RolPermiso
from app.models.servicio import Servicio, ServicioPrecio
from app.models.usuario import Usuario
from app.models.vehiculo import Vehiculo

__all__ = [
    "MAXIMO_BAHIAS",
    "MONEDA_PREDETERMINADA",
    "Bahia",
    "EstadoCuenta",
    "EstadoPago",
    "EstadoReserva",
    "EventoDominio",
    "MedioPago",
    "ModalidadPago",
    "Pago",
    "Permiso",
    "Reserva",
    "ReservaEstadoHistorial",
    "Rol",
    "RolPermiso",
    "Servicio",
    "ServicioPrecio",
    "TipoVehiculo",
    "TransicionEstado",
    "Usuario",
    "Vehiculo",
]
