"""Loyalty programme: points, benefits and redemption coupons (RF-032, RN-11).

RN-11 is two sentences and this module is both of them, each in exactly one
place:

* **"se acumula 1 punto por cada S/ 10.00 FACTURADOS"** is :func:`acreditar`,
  called when a payment is confirmed - from the counter charge and from the
  gateway charge, the same two doors that issue the receipt (RF-027). What
  counts as "facturado" is the TOTAL OF THE FROZEN BREAKDOWN
  (``reserva_tarifa_desglose.total_centimos``), not the catalogue price: INC-2
  went to the trouble of computing `(base x factor) + adicionales - descuentos`
  and freezing it, and billing somebody for that and rewarding them for
  something else would be two truths about the same service;
* **"100 puntos = un lavado básico sin costo"** is a ROW in ``beneficio``, not
  a constant here. :func:`canjear` reads ``puntos_requeridos`` from whatever
  the shop configured, so repricing the programme is an UPDATE.

The redemption does NOT invent a discount. It materialises a ``promocion``
carrying a 100 % coupon over the benefit's service and hands the customer its
code; from there the booking travels the ordinary RF-012 path, the breakdown
records the coupon like any other and the audit trail of RN-04 keeps having one
author. That is also why the entitlement (whose coupon it is, until when, was
it spent) lives in ``cupon_canje`` and not in the promotion: the promotion
answers "how much", the coupon answers "for whom, once".

All arithmetic is integer (P6). The accrual is one floor division - RN-11 says
"por cada S/ 10.00", so S/ 59.90 earns five points and not six.
"""

import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.core.codigos import ALFABETO_CODIGO
from app.core.errors import BeneficioNoDisponible, PuntosInsuficientes, detalle
from app.core.horario import a_lima, ahora, ahora_utc, desde_bd
from app.models import (
    CENTIMOS_POR_PUNTO,
    Beneficio,
    CuponCanje,
    EstadoCupon,
    Pago,
    PuntosMovimiento,
    Reserva,
    TipoDescuento,
    TipoMovimientoPuntos,
    Usuario,
)
from app.repositories import fidelizacion as fidelizacion_repo
from app.repositories import tarifa as tarifa_repo
from app.services import eventos

#: Permissions that guard this module (principle P5).
PERMISO_LEER = "fidelizacion:leer"
PERMISO_CANJEAR = "fidelizacion:canjear"

#: Prefix and length of the coupon code a redemption produces. Same alphabet as
#: the reservation code: it is read out loud and typed by hand.
PREFIJO_CUPON = "AQLP"
LONGITUD_CUPON = 8

#: How many attempts before giving up on a code collision. Astronomically
#: unlikely, exactly like the reservation code.
INTENTOS_CODIGO = 5

#: A redemption gives the service away whole: 100 % off, so "sin costo" holds
#: for every vehicle type instead of only for the one whose factor is 1.0.
DESCUENTO_TOTAL = 100

#: Reason written on the breakdown when a redemption coupon is refused.
MOTIVO_CUPON_AJENO = "Ese cupón de canje pertenece a otra cuenta."
MOTIVO_CUPON_USADO = "Ese cupón de canje ya se usó en otra reserva."
MOTIVO_CUPON_VENCIDO = "Ese cupón de canje venció."


@dataclass(frozen=True)
class Saldo:
    """What the loyalty screen shows: the balance and how it got there."""

    puntos: int
    movimientos: list[PuntosMovimiento]
    #: RN-11 made visible: what the customer still has to bill to reach the
    #: cheapest benefit on offer. ``None`` when they can already redeem one, or
    #: when there is nothing to redeem.
    puntos_para_el_siguiente: int | None
    #: The coupons they already redeemed, spent ones included.
    cupones: list[CuponCanje]


# --------------------------------------------------------------------------
# RN-11, first half: the accrual
# --------------------------------------------------------------------------
def puntos_por(centimos: int) -> int:
    """Points earned by billing ``centimos`` (RN-11).

    Floor division on purpose: the rule pays "por cada S/ 10.00", so a partial
    ten soles earns nothing and the arithmetic never leaves a float behind (P6).
    """
    if centimos <= 0:
        return 0
    return centimos // CENTIMOS_POR_PUNTO


def base_facturada(reserva: Reserva, pago: Pago) -> int:
    """What "facturado" means for RN-11, in cents.

    The frozen breakdown of INC-2 when there is one - it is the number the
    customer was actually charged, discounts and add-ons already inside it -
    and the reservation's own total otherwise, which is the same figure for
    every booking created before INC-2 existed. The PAYMENT amount is not used:
    a counter difference justified with ``motivo_diferencia`` (RF-026 flow 3a)
    changes what was collected, not what was billed.
    """
    if reserva.tarifa is not None:
        return reserva.tarifa.total_centimos
    return reserva.monto_centimos or pago.monto_centimos


def acreditar(
    db: Session,
    reserva: Reserva,
    pago: Pago,
    *,
    momento: datetime | None = None,
) -> PuntosMovimiento | None:
    """Credit the points a confirmed payment earned (RF-032 CA-01, RN-11).

    Called from both charging doors, right where the receipt is issued, because
    "al confirmarse el pago" is one moment however the money arrived. Returns
    the ledger line, or ``None`` when there is nothing to write.

    Idempotent by ``pago_id``: RF-026 CA-02 replays a charge with the same
    idempotency key as a matter of routine, and a replay must not pay twice.
    The uniqueness is also a constraint in the database, so the guard here is
    the polite half and the schema is the enforcing one.

    The caller owns the transaction: this only ``flush``es, like the receipt
    next to it, so a payment and its points commit together or not at all.
    """
    if fidelizacion_repo.obtener_movimiento_de_pago(db, pago.id) is not None:
        return None

    base = base_facturada(reserva, pago)
    puntos = puntos_por(base)
    if puntos <= 0:
        # A free or nearly free service earns nothing. Writing a zero line
        # would only make the statement harder to read.
        return None

    momento = momento or ahora_utc()
    saldo_previo = fidelizacion_repo.saldo(db, reserva.usuario_id)
    fila = fidelizacion_repo.crear_movimiento(
        db,
        usuario_id=reserva.usuario_id,
        tipo=TipoMovimientoPuntos.ACUMULACION.value,
        puntos=puntos,
        saldo_resultante=saldo_previo + puntos,
        reserva_id=reserva.id,
        pago_id=pago.id,
        base_centimos=base,
        ocurrido_en=momento,
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        reserva.usuario_id,
        eventos.PUNTOS_ACUMULADOS,
        autor_id=reserva.usuario_id,
        datos={
            "reserva_id": reserva.id,
            "pago_id": pago.id,
            "base_centimos": base,
            "puntos": puntos,
            "saldo_resultante": fila.saldo_resultante,
        },
    )
    return fila


# --------------------------------------------------------------------------
# Reading the programme
# --------------------------------------------------------------------------
def beneficios(db: Session, fecha: date | None = None) -> list[Beneficio]:
    """Benefits on offer today (RF-032 flow 4b: the rest are withdrawn)."""
    return fidelizacion_repo.listar_beneficios(db, fecha or ahora().date())


def saldo(db: Session, usuario: Usuario) -> Saldo:
    """The customer's balance, statement and coupons in one read (RF-032)."""
    puntos = fidelizacion_repo.saldo(db, usuario.id)
    disponibles = beneficios(db)
    faltan = None
    if disponibles:
        minimo = min(item.puntos_requeridos for item in disponibles)
        if puntos < minimo:
            faltan = minimo - puntos

    return Saldo(
        puntos=puntos,
        movimientos=fidelizacion_repo.listar_movimientos(db, usuario.id),
        puntos_para_el_siguiente=faltan,
        cupones=fidelizacion_repo.listar_cupones(db, usuario.id),
    )


# --------------------------------------------------------------------------
# RN-11, second half: the redemption
# --------------------------------------------------------------------------
def _generar_codigo(db: Session) -> str:
    """A unique ``AQLP-XXXXXXXX`` coupon code."""
    for _ in range(INTENTOS_CODIGO):
        sufijo = "".join(secrets.choice(ALFABETO_CODIGO) for _ in range(LONGITUD_CUPON))
        codigo = f"{PREFIJO_CUPON}-{sufijo}"
        if not fidelizacion_repo.existe_codigo_cupon(db, codigo):
            return codigo
    # pragma: no cover - practically unreachable
    return f"{PREFIJO_CUPON}-{secrets.token_hex(4).upper()}"


def vigencia_cupon() -> timedelta:
    """How long a redeemed coupon stays usable.

    RF-032 does not put a number on it and a coupon that never expires is a
    liability the shop cannot close, so it is a setting with a working default
    (the plan forbids ``.env``, so every option ships usable).
    """
    return timedelta(days=settings.cupon_canje_vigencia_dias)


def canjear(db: Session, usuario: Usuario, beneficio_id: int) -> CuponCanje:
    """Exchange points for a benefit (RF-032 "Salidas", CA-02, flow 4a).

    Order of the checks, so the reason that comes back is the useful one:

    1. the benefit must still be on offer - exhausted or expired answers 422
       ``BENEFICIO_NO_DISPONIBLE`` (flow 4b, for whoever had the stale listing
       open);
    2. the balance must cover it - otherwise 422 ``PUNTOS_INSUFICIENTES``
       **carrying how many points are missing**, which is what CA-02 asks for
       by name.

    Only then is the coupon materialised, the stock decremented and the ledger
    line written. One commit: a coupon whose points were not deducted would be
    a free wash, and points deducted without a coupon would be a theft.
    """
    beneficio = fidelizacion_repo.obtener_beneficio(db, beneficio_id)
    disponibles = {item.id for item in beneficios(db)}
    if beneficio is None or beneficio.id not in disponibles:
        raise BeneficioNoDisponible(
            detalles=[detalle("beneficio_id", "El beneficio no está disponible para canje.")]
        )

    puntos = fidelizacion_repo.saldo(db, usuario.id)
    if puntos < beneficio.puntos_requeridos:
        faltan = beneficio.puntos_requeridos - puntos
        raise PuntosInsuficientes(
            detalles=[
                detalle("puntos_faltantes", str(faltan)),
                detalle(
                    "beneficio_id",
                    f"«{beneficio.nombre}» cuesta {beneficio.puntos_requeridos} puntos y "
                    f"tienes {puntos}: te faltan {faltan}.",
                ),
            ]
        )

    momento = ahora_utc()
    vence_en = momento + vigencia_cupon()
    codigo = _generar_codigo(db)

    # The discount itself is an ordinary promotion with a coupon, so RF-012
    # stays the only place a tariff is computed (RN-04). It is scoped to the
    # benefit's service, which is what makes "un lavado básico" basic.
    promocion = tarifa_repo.crear_promocion(
        db,
        nombre=f"Canje {codigo}",
        descripcion=f"Canje de {beneficio.puntos_requeridos} puntos: {beneficio.nombre}.",
        tipo_descuento=TipoDescuento.PORCENTAJE.value,
        valor=DESCUENTO_TOTAL,
        servicio_id=beneficio.servicio_id,
        paquete_id=None,
        codigo_cupon=codigo,
        dias_semana=None,
        vigente_desde=a_lima(momento).date(),
        vigente_hasta=a_lima(vence_en).date(),
    )

    cupon = fidelizacion_repo.crear_cupon(
        db,
        usuario_id=usuario.id,
        beneficio_id=beneficio.id,
        codigo=codigo,
        promocion_id=promocion.id,
        estado=EstadoCupon.EMITIDO.value,
        vence_en=vence_en,
        emitido_en=momento,
    )

    if beneficio.stock is not None:
        # Flow 4b: the last unit takes the benefit out of the listing by
        # itself, because the listing asks for ``stock > 0``.
        beneficio.stock = max(0, beneficio.stock - 1)

    fidelizacion_repo.crear_movimiento(
        db,
        usuario_id=usuario.id,
        tipo=TipoMovimientoPuntos.CANJE.value,
        puntos=-beneficio.puntos_requeridos,
        saldo_resultante=puntos - beneficio.puntos_requeridos,
        beneficio_id=beneficio.id,
        ocurrido_en=momento,
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        eventos.PUNTOS_CANJEADOS,
        autor_id=usuario.id,
        datos={
            "beneficio_id": beneficio.id,
            "beneficio": beneficio.nombre,
            "puntos": beneficio.puntos_requeridos,
            "saldo_resultante": puntos - beneficio.puntos_requeridos,
            "cupon": codigo,
            "promocion_id": promocion.id,
        },
    )

    db.commit()
    db.refresh(cupon)
    return cupon


# --------------------------------------------------------------------------
# The tariff engine's side of the coupon (RF-012 flow 3a)
# --------------------------------------------------------------------------
def motivo_cupon_no_canjeable(
    db: Session, promocion_id: int, usuario_id: int | None, momento: datetime | None = None
) -> str | None:
    """Why this customer may not use that redemption coupon, or ``None``.

    Called by ``tarifa_service`` for every coupon it resolves. A promotion that
    is NOT backed by a redemption returns ``None`` straight away, so an
    ordinary marketing coupon behaves exactly as it did before INC-7.

    Three ways a redeemed coupon is refused, and each of them is a sentence the
    customer can act on, because RF-012 flow 3a demands a REASON: it belongs to
    somebody else, it was already spent, or it expired.
    """
    cupon = fidelizacion_repo.obtener_cupon_por_promocion(db, promocion_id)
    if cupon is None:
        return None

    if usuario_id is None or cupon.usuario_id != usuario_id:
        return MOTIVO_CUPON_AJENO
    if cupon.estado != EstadoCupon.EMITIDO.value:
        return MOTIVO_CUPON_USADO
    if (momento or ahora_utc()) > desde_bd(cupon.vence_en):
        return MOTIVO_CUPON_VENCIDO
    return None


def consumir(db: Session, reserva: Reserva, codigo: str | None) -> CuponCanje | None:
    """Spend the redemption coupon a booking actually applied (RF-032).

    Called from the reservation creation once the breakdown is frozen, with
    ``desglose.cupon_aplicado``: only a coupon that really made it into the
    total is spent, so a rejected one (flow 3a) is still there for the next
    attempt. The promotion is deactivated at the same time, which is what stops
    the code working a second time even if somebody copies it.

    The caller owns the transaction, like every other writer in this module
    except :func:`canjear`.
    """
    if not codigo:
        return None
    cupon = fidelizacion_repo.obtener_cupon(db, codigo)
    if cupon is None or cupon.estado != EstadoCupon.EMITIDO.value:
        return None

    momento = ahora_utc()
    fidelizacion_repo.marcar_cupon_usado(
        db, cupon, estado=EstadoCupon.USADO.value, reserva_id=reserva.id, usado_en=momento
    )
    if cupon.promocion is not None:
        cupon.promocion.activa = False

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.PUNTOS_CUPON_USADO,
        autor_id=reserva.usuario_id,
        datos={
            "cupon": cupon.codigo,
            "beneficio_id": cupon.beneficio_id,
            "promocion_id": cupon.promocion_id,
        },
    )
    return cupon
