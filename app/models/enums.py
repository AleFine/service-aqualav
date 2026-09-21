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
    """How the money actually moved (RF-025, RF-026).

    The first three are what the counter takes (``presencial``); the last two
    are what the gateway takes (``en_linea``: "tarjeta o billetera digital").
    They share one column because they answer the same question - the SHAPE of
    the payment, not the door it came through - and which of them each door
    accepts is declared by :data:`MEDIOS_PRESENCIALES` and
    :data:`MEDIOS_EN_LINEA`, never by a branch in a service.
    """

    EFECTIVO = "efectivo"
    TARJETA_POS = "tarjeta_pos"
    TRANSFERENCIA = "transferencia"
    TARJETA = "tarjeta"
    BILLETERA = "billetera"


#: What ``POST /reservas/{id}/pagos`` (the counter) admits (RF-026).
MEDIOS_PRESENCIALES: frozenset[str] = frozenset(
    {MedioPago.EFECTIVO.value, MedioPago.TARJETA_POS.value, MedioPago.TRANSFERENCIA.value}
)

#: What ``POST /reservas/{id}/pagos/en-linea`` (the gateway) admits (RF-025).
MEDIOS_EN_LINEA: frozenset[str] = frozenset({MedioPago.TARJETA.value, MedioPago.BILLETERA.value})


class TipoVehiculo(str, Enum):
    SEDAN = "sedan"
    SUV = "suv"
    CAMIONETA = "camioneta"
    MOTOCICLETA = "motocicleta"


class EstadoPago(str, Enum):
    """Lifecycle of one payment (RF-026 postcondition, RF-028).

    The MVP only ever produced ``confirmado``; RF-026 v1.0 says the gateway
    answers "confirmado, rechazado o pendiente", and RF-028 adds the three
    outcomes of a reversal. ``pendiente`` is what the simulated gateway leaves
    behind when the operation was accepted but not settled yet, which is also
    what a reservation waiting for its online payment shows.
    """

    PENDIENTE = "pendiente"
    CONFIRMADO = "confirmado"
    RECHAZADO = "rechazado"
    ANULADO = "anulado"
    REEMBOLSADO_PARCIAL = "reembolsado_parcial"
    REEMBOLSADO_TOTAL = "reembolsado_total"


#: Payment states that still count as "the shop has the money" (RN-09).
#: ``reembolsado_parcial`` is in the set because the service WAS paid; what was
#: given back afterwards is a reversal, not an unpaid service.
ESTADOS_PAGO_COBRADO: frozenset[str] = frozenset(
    {EstadoPago.CONFIRMADO.value, EstadoPago.REEMBOLSADO_PARCIAL.value}
)


class ModalidadPago(str, Enum):
    """How the customer chose to pay (RF-025, RN-08).

    RN-08 reads "en línea al reservar o presencial al entregar", and that is
    the whole enumeration. The MVP shipped only ``presencial`` because there
    was no gateway; INC-4 opens the other half.
    """

    PRESENCIAL = "presencial"
    EN_LINEA = "en_linea"


class EstadoTransaccion(str, Enum):
    """What the payment gateway answered to one call (RF-026).

    ``tiempo_de_espera`` is not an answer, it is the ABSENCE of one: the
    request left, the reply never came back, and RF-026 flow 3b says what to do
    about it - ask again with the same idempotency key before retrying. The
    simulation reaches this outcome deterministically, from the test card.
    """

    APROBADA = "aprobada"
    RECHAZADA = "rechazada"
    PENDIENTE = "pendiente"
    TIEMPO_DE_ESPERA = "tiempo_de_espera"


class OperacionPasarela(str, Enum):
    """Which call of the gateway port a transaction row belongs to."""

    COBRO = "cobro"
    REEMBOLSO = "reembolso"


class EstadoComprobante(str, Enum):
    """Lifecycle of an issued receipt (RF-027).

    ``anulado`` is what a full reversal leaves behind (RF-028): the number is
    NOT reused - a correlative series with holes in it stops being one.
    """

    EMITIDO = "emitido"
    ANULADO = "anulado"


class TipoReembolso(str, Enum):
    """The two actions RF-028 names, plus the shape of the second one.

    ``anulacion`` gives the whole payment back and closes it; ``total`` and
    ``parcial`` are the reversal of an amount, which is what CA-01 measures
    against the remaining balance.
    """

    ANULACION = "anulacion"
    TOTAL = "total"
    PARCIAL = "parcial"


class EstadoReembolso(str, Enum):
    """How a refund request ended (RF-028 flow 3a).

    ``pendiente_manual`` is the whole point of that flow: a reversal the
    gateway refused is NOT lost, it is queued for a human. Nothing deletes a
    refund row.
    """

    PROCESADO = "procesado"
    PENDIENTE_MANUAL = "pendiente_manual"


class TipoDescuento(str, Enum):
    """How a promotion reduces the tariff (RF-011).

    ``porcentaje`` reads ``Promocion.valor`` as percentage points (10 = 10 %);
    ``monto`` reads it as integer cents, the same unit every amount uses (P6).
    """

    PORCENTAJE = "porcentaje"
    MONTO = "monto"


class TipoDocumento(str, Enum):
    """Identity document RF-001 v1.0 captures together with its number."""

    DNI = "dni"
    CARNE_EXTRANJERIA = "carne_extranjeria"
    PASAPORTE = "pasaporte"


class Idioma(str, Enum):
    """Language the customer wants to be written to in (RF-006).

    RF-029 composes every notification "from a template according to the event
    and the LANGUAGE", so the preference has to exist before INC-5 can read it.
    """

    ES = "es"
    EN = "en"


class EstadoCuenta(str, Enum):
    """Lifecycle of an account.

    ``pendiente_verificacion`` is where RF-001 leaves a self-registered account
    until the address is confirmed; ``suspendida`` is what RF-035 writes when
    the administrator deactivates an internal account, and the login answers
    403 from then on (CA-01).
    """

    ACTIVA = "activa"
    PENDIENTE_VERIFICACION = "pendiente_verificacion"
    SUSPENDIDA = "suspendida"


#: Account states that may still obtain and use a token.
#:
#: The two non-active states are NOT interchangeable and this is the single
#: place that says so. ``suspendida`` is a decision somebody took about the
#: person (RF-035 CA-01 -> 403); ``pendiente_verificacion`` is an errand the
#: person has not run yet, and RF-001 flow 5a explicitly creates the account
#: even when the verification mail could not be delivered. Locking that account
#: out would mean the one flow the requirement describes as recoverable is the
#: one nobody can recover from, with no real SMTP server to fix it. RF-002 flow
#: 2c is therefore served by OFFERING the resend (``Sesion.verificacion_pendiente``
#: plus ``POST /auth/verificacion/reenviar``), not by refusing the login.
ESTADOS_CUENTA_CON_ACCESO: frozenset[str] = frozenset(
    {EstadoCuenta.ACTIVA.value, EstadoCuenta.PENDIENTE_VERIFICACION.value}
)


class CanalNotificacion(str, Enum):
    """How a notice reaches the customer (RF-029).

    ``en_app`` is the feed the mobile app reads while polling (RF-022); it
    never fails and it is always written, which is what finally gives
    ``NotificadorEnApp`` a table behind it. The other two are the simulated
    providers of the plan's section 4.
    """

    EN_APP = "en_app"
    CORREO = "correo"
    PUSH = "push"


class EstadoEnvio(str, Enum):
    """Outcome of one delivery (RF-029 "registro del resultado del envío")."""

    PENDIENTE = "pendiente"
    ENVIADA = "enviada"
    FALLIDA = "fallida"


class EventoNotificacion(str, Enum):
    """The lifecycle moments that are worth telling the customer about.

    RF-029 names six of them literally - "confirmación, recordatorio, inicio,
    finalización, entrega y cancelación" - and RF-022 flow 4a adds the seventh,
    the new delivery time when the service runs more than fifteen minutes late.

    This is VOCABULARY, like :class:`EstadoReserva`: which state change raises
    which event is NOT decided here, it is read from
    ``transicion_estado.evento_notificacion`` (principle P3). ``asignacion`` and
    ``estado_cambiado`` are internal notices with no external channel; the
    template table decides that too.
    """

    CONFIRMACION = "confirmacion"
    RECORDATORIO = "recordatorio"
    INICIO = "inicio"
    FINALIZACION = "finalizacion"
    ENTREGA = "entrega"
    CANCELACION = "cancelacion"
    RETRASO = "retraso"
    ASIGNACION = "asignacion"
    ESTADO_CAMBIADO = "estado_cambiado"
    #: RF-027: the electronic receipt left with its correlative number. It is
    #: an event with a template like every other notice - INC-5 asked for
    #: exactly that instead of a hand-written dispatch - so flow 3a (the mail
    #: failed) is already covered: the row records the failure and the receipt
    #: stays downloadable from the application.
    COMPROBANTE = "comprobante"
    #: RF-028 output: "cliente notificado". A reversal - processed or left
    #: for a human - is something the customer is entitled to hear about.
    REEMBOLSO = "reembolso"


class PlataformaDispositivo(str, Enum):
    """Where a registered push token lives (RF-029 precondition)."""

    ANDROID = "android"
    IOS = "ios"
    WEB = "web"


class RespuestaRecordatorio(str, Enum):
    """The three actions RF-030 offers on the two-hour reminder."""

    CONFIRMO = "confirmo"
    REPROGRAMO = "reprogramo"
    CANCELO = "cancelo"


class EstadoRecordatorio(str, Enum):
    """Lifecycle of one reminder. ``enviado`` with no answer is flow 3a."""

    PENDIENTE = "pendiente"
    ENVIADO = "enviado"
    RESPONDIDO = "respondido"


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
