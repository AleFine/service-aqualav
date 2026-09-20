"""SQLAlchemy models, one module per aggregate.

Importing this package registers every table on ``app.database.Base.metadata``,
which is what Alembic's ``env.py`` and the seed rely on.
"""

from app.models.agenda import BloqueoFranja, DiaNoLaborable, HorarioAtencion
from app.models.asignacion import AsignacionServicio, ColaEspera
from app.models.autenticacion import (
    IntentoLogin,
    TokenRecuperacion,
    TokenRefresco,
    VerificacionCorreo,
)
from app.models.bahia import MAXIMO_BAHIAS, Bahia
from app.models.enums import (
    ESTADOS_CUENTA_CON_ACCESO,
    FACTOR_BASE_MILESIMAS,
    MONEDA_PREDETERMINADA,
    EstadoBahia,
    EstadoCuenta,
    EstadoPago,
    EstadoReserva,
    Idioma,
    MedioPago,
    ModalidadPago,
    MotivoBloqueo,
    TipoDescuento,
    TipoDocumento,
    TipoVehiculo,
)
from app.models.evento import EventoDominio
from app.models.pago import Pago
from app.models.reserva import Reserva, ReservaEstadoHistorial, TransicionEstado
from app.models.rol import Permiso, Rol, RolPermiso
from app.models.servicio import Servicio, ServicioPrecio
from app.models.tarifa import (
    FactorTipoVehiculo,
    Paquete,
    PaqueteServicio,
    Promocion,
    ReservaAdicional,
    ReservaTarifaDesglose,
    ServicioAdicional,
)
from app.models.usuario import Usuario
from app.models.vehiculo import Vehiculo

__all__ = [
    "ESTADOS_CUENTA_CON_ACCESO",
    "FACTOR_BASE_MILESIMAS",
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
    "FactorTipoVehiculo",
    "HorarioAtencion",
    "Idioma",
    "IntentoLogin",
    "MedioPago",
    "ModalidadPago",
    "MotivoBloqueo",
    "Pago",
    "Paquete",
    "PaqueteServicio",
    "Permiso",
    "Promocion",
    "Reserva",
    "ReservaAdicional",
    "ReservaEstadoHistorial",
    "ReservaTarifaDesglose",
    "Rol",
    "RolPermiso",
    "Servicio",
    "ServicioAdicional",
    "ServicioPrecio",
    "TipoDescuento",
    "TipoDocumento",
    "TipoVehiculo",
    "TokenRecuperacion",
    "TokenRefresco",
    "TransicionEstado",
    "Usuario",
    "Vehiculo",
    "VerificacionCorreo",
]
