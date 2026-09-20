"""Domain enumerations.

The VALUES are the exact strings stored in the database and emitted by the API
(contract section 2). They are persisted in plain ``String`` columns, never as
native PostgreSQL enums, so extension points P3 (new states), the new vehicle
types and the v0.3 payment modality only need a data migration, not a type
migration.
"""

from enum import Enum


class EstadoReserva(str, Enum):
    """Annex A v1.0 state machine. Allowed moves live in ``transicion_estado``.

    This is VOCABULARY, not the machine: it spells the strings the database
    stores so the seed and the migrations do not repeat literals. Nothing in
    the service layer may branch on a member of this enum (principle P3); which
    move is possible, who owns it and where each operation leads are all read
    from ``transicion_estado``.

    ``en_atencion`` of the MVP is GONE: v1.0 splits it into ``en_lavado``,
    ``secado`` and ``acabado`` (migration ``0003`` converts the stored data).
    """

    PENDIENTE_PAGO = "pendiente_pago"
    CONFIRMADA = "confirmada"
    EN_RECEPCION = "en_recepcion"
    ASIGNADO = "asignado"
    EN_LAVADO = "en_lavado"
    SECADO = "secado"
    ACABADO = "acabado"
    FINALIZADO = "finalizado"
    EN_REVISION = "en_revision"
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


class TipoDescuento(str, Enum):
    """How a promotion reduces the tariff (RF-011).

    ``porcentaje`` reads ``Promocion.valor`` as percentage points (10 = 10 %);
    ``monto`` reads it as integer cents, the same unit every amount uses (P6).
    """

    PORCENTAJE = "porcentaje"
    MONTO = "monto"


class EstadoCuenta(str, Enum):
    """EXTENSION POINT: v0.2 adds ``pendiente_verificacion``.

    ``suspendida`` is what RF-035 writes when the administrator deactivates an
    internal account: the login answers 403 from then on (CA-01).
    """

    ACTIVA = "activa"
    SUSPENDIDA = "suspendida"


class EstadoBahia(str, Enum):
    """Whether a bay is physically holding a vehicle right now (RF-020 CA-01).

    It is NOT the same thing as "the bay has a reservation in this block": the
    booking calendar is derived from ``reserva``, this flag is what the counter
    sees on the assignment screen the moment a vehicle drives in.
    """

    LIBRE = "libre"
    OCUPADA = "ocupada"


class MotivoBloqueo(str, Enum):
    """Why a slot of the agenda is not available (RF-018)."""

    MANTENIMIENTO = "mantenimiento"
    FERIADO = "feriado"
    AUSENCIA = "ausencia"


#: Default currency for every amount in the MVP (RN-12: PEN, IGV included).
MONEDA_PREDETERMINADA = "PEN"

#: Neutral vehicle factor, in thousandths: 1000 = 1.0 (RN-04).
#: Factors are integers on purpose - ``3000 x 1.3`` stops being ``3900`` the
#: moment a float takes part, and RF-012 CA-01 is exactly that multiplication.
FACTOR_BASE_MILESIMAS = 1000
