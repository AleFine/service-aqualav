"""SQLAlchemy models, one module per aggregate.

Importing this package registers every table on ``app.database.Base.metadata``,
which is what Alembic's ``env.py`` and the seed rely on.
"""

from app.models.agenda import BloqueoFranja, DiaNoLaborable, HorarioAtencion
from app.models.asignacion import AsignacionServicio, ColaEspera
from app.models.bahia import MAXIMO_BAHIAS, Bahia
from app.models.enums import (
    MONEDA_PREDETERMINADA,
    EstadoBahia,
    EstadoCuenta,
    EstadoPago,
    EstadoReserva,
    MedioPago,
    ModalidadPago,
    MotivoBloqueo,
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
    "AsignacionServicio",
    "Bahia",
    "BloqueoFranja",
    "ColaEspera",
    "DiaNoLaborable",
    "EstadoBahia",
    "EstadoCuenta",
    "EstadoPago",
    "EstadoReserva",
    "EventoDominio",
    "HorarioAtencion",
    "MedioPago",
    "ModalidadPago",
    "MotivoBloqueo",
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
