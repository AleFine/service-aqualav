"""Domain enumerations.

The VALUES are the exact strings stored in the database and emitted by the API
(contract section 2). They are persisted in plain ``String`` columns, never as
native PostgreSQL enums, so extension points P3 (new states), the new vehicle
types and the v0.3 payment modality only need a data migration, not a type
migration.
"""

from enum import Enum


class EstadoReserva(str, Enum):
    """Annex A state machine. Allowed moves live in ``transicion_estado``."""

    CONFIRMADA = "confirmada"
    EN_ATENCION = "en_atencion"
    FINALIZADO = "finalizado"
    ENTREGADO = "entregado"
    CANCELADA = "cancelada"


# Which states are still active and which are terminal is NOT declared here.
# It is derived from ``transicion_estado`` (a state is terminal when no move
# leaves it): see ``app.repositories.transicion.listar_estados_no_terminales``.
# Listing them in code is what used to let a state added as data lose its bay
# and disappear from the staff search (principle P3, RN-03, RF-024 CA-02).


class MedioPago(str, Enum):
    EFECTIVO = "efectivo"
    TARJETA_POS = "tarjeta_pos"
    TRANSFERENCIA = "transferencia"


class TipoVehiculo(str, Enum):
    SEDAN = "sedan"
    SUV = "suv"
    CAMIONETA = "camioneta"
    MOTOCICLETA = "motocicleta"


class EstadoPago(str, Enum):
    PENDIENTE = "pendiente"
    CONFIRMADO = "confirmado"
    ANULADO = "anulado"


class ModalidadPago(str, Enum):
    """EXTENSION POINT: v0.3 adds ``en_linea``."""

    PRESENCIAL = "presencial"


class EstadoCuenta(str, Enum):
    """EXTENSION POINT: v0.2 adds ``pendiente_verificacion``."""

    ACTIVA = "activa"
    SUSPENDIDA = "suspendida"


#: Default currency for every amount in the MVP (RN-12: PEN, IGV included).
MONEDA_PREDETERMINADA = "PEN"
