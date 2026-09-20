"""The tariff engine (RF-012, RN-04, RN-12) and the vehicle factors (RF-010).

One formula, computed in one place::

    total = (precio_base x factor_tipo_vehiculo) + adicionales - descuentos

and the result is not a number but a :class:`Desglose`: every term separately,
plus the coupon that was rejected and why (flow 3a) and the incident raised
when the discount swallowed the whole total (flow 4a). ``reserva_service``
freezes that breakdown into ``reserva_tarifa_desglose`` at creation time, so
the charge stays explainable after prices, factors and promotions move on.

EVERY amount is an integer number of cents and the factor is an integer number
of thousandths (P6, RN-12). Nothing here ever builds a ``float``: RF-012 CA-01
is ``3000 x 1.3 = 3900`` exactly, and in binary floating point that product is
``3899.9999999999995``, which truncates to S/ 38.99.

This module deliberately does NOT import ``servicio_service``: it receives the
base price already resolved, which is what lets ``servicio_service`` import
THIS one to price the catalogue per vehicle type (RF-009 delta) without a
circular import.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.core.errors import (
    AdicionalNoDisponible,
    DatosInvalidos,
    RecursoNoEncontrado,
    detalle,
)
from app.core.horario import ahora, ahora_utc
from app.models import (
    FACTOR_BASE_MILESIMAS,
    MONEDA_PREDETERMINADA,
    FactorTipoVehiculo,
    Promocion,
    Reserva,
    Servicio,
    TipoDescuento,
    Usuario,
)
from app.repositories import tarifa as tarifa_repo
from app.repositories import vehiculo as vehiculo_repo
from app.schemas import FactorIn
from app.services import eventos

#: Permission that guards the factor administration (RF-010 delta, P5).
PERMISO_ADMINISTRAR = "servicio:administrar"

#: Separator of ``promocion.dias_semana`` ("0,1,2" = Monday to Wednesday).
SEPARADOR_DIAS = ","

#: Spanish weekday names, indexed by ``datetime.weekday()``. Used only to tell
#: the customer WHY a coupon restricted to certain days did not apply.
DIAS_EN_ESPANIOL: tuple[str, ...] = (
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
)


# --------------------------------------------------------------------------
# Integer arithmetic (P6)
# --------------------------------------------------------------------------
def redondear(numerador: int, denominador: int) -> int:
    """Divide two integers rounding half away from zero, with no float.

    ``(n * 2 + d) // (2 * d)`` is the classic half-up trick; it is spelled out
    here because it is the single place where a cent can appear or vanish, and
    RF-012 asks for the rounding to be explicit.
    """
    if denominador <= 0:
        raise ValueError("El denominador debe ser positivo.")
    if numerador >= 0:
        return (numerador * 2 + denominador) // (2 * denominador)
    return -((-numerador * 2 + denominador) // (2 * denominador))


def aplicar_factor(monto_centimos: int, factor_milesimas: int) -> int:
    """``monto x factor``, in cents. RF-012 CA-01: 3000 x 1300/1000 = 3900."""
    return redondear(monto_centimos * factor_milesimas, FACTOR_BASE_MILESIMAS)


# --------------------------------------------------------------------------
# The breakdown
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AdicionalAplicado:
    """One add-on as it entered the total, with the amount of that moment."""

    servicio_adicional_id: int | None
    nombre: str
    monto_centimos: int


@dataclass(frozen=True)
class Desglose:
    """Every term of RN-04, traceable and auditable (RF-012 "Salidas")."""

    precio_base_centimos: int
    #: ``None`` only on a quote asked without naming a vehicle or a type; a
    #: reservation always has one (RN-01), which is why the column is NOT NULL.
    tipo_vehiculo: str | None
    factor_milesimas: int
    base_ajustada_centimos: int
    adicionales_centimos: int
    descuento_centimos: int
    total_centimos: int
    moneda: str = MONEDA_PREDETERMINADA
    adicionales: tuple[AdicionalAplicado, ...] = ()
    promocion_id: int | None = None
    promocion_nombre: str | None = None
    cupon_aplicado: str | None = None
    #: RF-012 flow 3a: the coupon the customer typed and the reason it failed.
    cupon_rechazado: str | None = None
    motivo_rechazo_cupon: str | None = None
    #: RF-012 flow 4a: set when the total had to be clamped to zero.
    incidencia: str | None = None

    def a_datos(self) -> dict:
        """JSON-serializable view, for the domain event log (P7)."""
        return {
            "precio_base_centimos": self.precio_base_centimos,
            "tipo_vehiculo": self.tipo_vehiculo,
            "factor_milesimas": self.factor_milesimas,
            "base_ajustada_centimos": self.base_ajustada_centimos,
            "adicionales_centimos": self.adicionales_centimos,
            "descuento_centimos": self.descuento_centimos,
            "total_centimos": self.total_centimos,
            "moneda": self.moneda,
            "promocion_id": self.promocion_id,
            "cupon_aplicado": self.cupon_aplicado,
            "cupon_rechazado": self.cupon_rechazado,
        }


@dataclass(frozen=True)
class PrecioAplicable:
    """What the catalogue shows for one service and one vehicle type (RF-009)."""

    factor_milesimas: int
    monto_centimos: int
    moneda: str
    tipo_vehiculo: str | None = None
    promocion: Promocion | None = None
    promocional_centimos: int | None = None


# --------------------------------------------------------------------------
# Factors (RF-010 delta, RN-04)
# --------------------------------------------------------------------------
def _clave(factor: FactorTipoVehiculo) -> tuple[int | None, str]:
    return factor.servicio_id, factor.tipo_vehiculo


def factores_vigentes(db: Session, servicio_id: int | None = None) -> dict[tuple, int]:
    """Open factors as ``{(servicio_id, tipo): milesimas}``, one query."""
    return {
        _clave(factor): factor.factor_milesimas
        for factor in tarifa_repo.listar_factores_vigentes(db, servicio_id)
    }


def resolver_factor(factores: dict[tuple, int], servicio_id: int, tipo_vehiculo: str | None) -> int:
    """The factor that wins: the service's own row, the global one, or 1.0.

    Falling back to 1.0 rather than refusing is deliberate: a service created
    this morning has no factors yet and still has to be bookable at its base
    price (RF-010 CA-01 must keep working after RF-012 lands).
    """
    if tipo_vehiculo is None:
        return FACTOR_BASE_MILESIMAS
    propio = factores.get((servicio_id, tipo_vehiculo))
    if propio is not None:
        return propio
    return factores.get((None, tipo_vehiculo), FACTOR_BASE_MILESIMAS)


def factor_de(db: Session, servicio_id: int, tipo_vehiculo: str | None) -> int:
    """Convenience wrapper of :func:`resolver_factor` for a single service."""
    return resolver_factor(factores_vigentes(db, servicio_id), servicio_id, tipo_vehiculo)


def listar_factores(db: Session) -> list[FactorTipoVehiculo]:
    """Factors in force, global ones first (RF-010 delta, administration)."""
    return tarifa_repo.listar_factores(db, solo_vigentes=True)


def definir_factor(db: Session, datos: FactorIn, autor: Usuario) -> FactorTipoVehiculo:
    """Set the factor of a ``(servicio, tipo)`` pair, versioning the previous one.

    Same shape as a price change (EXTENSION POINT P6): the open row is closed
    and a new one is inserted, and the change is written to the event log with
    the old and the new value, which is the audit trail RNF-014 asks of every
    tariff change.
    """
    if datos.servicio_id is not None and db.get(Servicio, datos.servicio_id) is None:
        raise RecursoNoEncontrado(
            "No encontramos ese servicio.",
            detalles=[detalle("servicio_id", "El servicio no existe.")],
        )

    momento = ahora_utc()
    anterior = tarifa_repo.cerrar_factor_vigente(
        db, datos.servicio_id, datos.tipo_vehiculo.value, momento
    )
    factor = tarifa_repo.crear_factor(
        db,
        servicio_id=datos.servicio_id,
        tipo_vehiculo=datos.tipo_vehiculo.value,
        factor_milesimas=datos.factor_milesimas,
        vigente_desde=momento,
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_SERVICIO,
        datos.servicio_id or 0,
        eventos.SERVICIO_FACTOR_CAMBIADO,
        autor_id=autor.id,
        datos={
            "servicio_id": datos.servicio_id,
            "tipo_vehiculo": factor.tipo_vehiculo,
            "factor_anterior": anterior.factor_milesimas if anterior else None,
            "factor_nuevo": factor.factor_milesimas,
        },
    )
    db.commit()
    db.refresh(factor)
    return factor


# --------------------------------------------------------------------------
# Promotions (RF-011, applied from RF-012)
# --------------------------------------------------------------------------
def dias_de(promocion: Promocion) -> set[int] | None:
    """``dias_semana`` as a set of ``weekday()`` values; ``None`` = every day."""
    crudo = (promocion.dias_semana or "").strip()
    if not crudo:
        return None
    return {int(parte) for parte in crudo.split(SEPARADOR_DIAS) if parte.strip().isdigit()}


def motivo_no_aplica(promocion: Promocion, servicio_id: int | None, fecha: date) -> str | None:
    """Why the promotion does not apply that day, or ``None`` when it does.

    The reason is user facing copy: RF-012 flow 3a demands that a rejected
    coupon says WHY, and "venció el 31/12/2024" is what lets the customer stop
    typing it.
    """
    if not promocion.activa:
        return "La promoción está desactivada."
    if promocion.servicio_id is not None and promocion.servicio_id != servicio_id:
        return "La promoción no aplica al servicio elegido."
    if promocion.paquete_id is not None:
        return "La promoción solo aplica a un paquete, no a un servicio suelto."
    if fecha < promocion.vigente_desde:
        return f"La promoción empieza a regir el {promocion.vigente_desde.strftime('%d/%m/%Y')}."
    if promocion.vigente_hasta is not None and fecha > promocion.vigente_hasta:
        return f"La promoción venció el {promocion.vigente_hasta.strftime('%d/%m/%Y')}."
    dias = dias_de(promocion)
    if dias is not None and fecha.weekday() not in dias:
        nombres = ", ".join(DIAS_EN_ESPANIOL[dia] for dia in sorted(dias))
        return f"La promoción solo aplica los {nombres}."
    return None


def descuento_de(promocion: Promocion, base_centimos: int) -> int:
    """The discount in cents, per ``tipo_descuento``. Never negative."""
    if promocion.tipo_descuento == TipoDescuento.PORCENTAJE.value:
        return max(0, redondear(base_centimos * promocion.valor, 100))
    return max(0, promocion.valor)


def promocion_automatica(db: Session, servicio_id: int, fecha: date) -> Promocion | None:
    """The coupon-free promotion that applies to a service on a date.

    A promotion that expired yesterday simply stops matching here, which is
    all flow 4a ("deja de aplicarse automáticamente") needs: nothing sweeps,
    nothing deactivates, the regular price comes back on its own.

    When both a global promotion and a service-specific one match, the one that
    discounts MORE wins, and ties break by id so the answer is deterministic.
    """
    candidatas = [
        promocion
        for promocion in tarifa_repo.listar_promociones_automaticas(db, fecha)
        if motivo_no_aplica(promocion, servicio_id, fecha) is None
    ]
    return _mejor(candidatas)


def _mejor(candidatas: list[Promocion]) -> Promocion | None:
    if not candidatas:
        return None
    #: Ranked on a reference amount so a percentage and a fixed amount are
    #: comparable; the real discount is recomputed on the real base afterwards.
    referencia = 10_000
    return max(candidatas, key=lambda promo: (descuento_de(promo, referencia), -promo.id))


def _promocion_del_catalogo(
    promociones: list[Promocion], servicio_id: int, fecha: date
) -> Promocion | None:
    """Same choice as :func:`promocion_automatica` over a pre-loaded list."""
    candidatas = [
        promocion
        for promocion in promociones
        if motivo_no_aplica(promocion, servicio_id, fecha) is None
    ]
    return _mejor(candidatas)


# --------------------------------------------------------------------------
# Add-ons
# --------------------------------------------------------------------------
def resolver_adicionales(db: Session, ids: list[int] | None) -> list[AdicionalAplicado]:
    """Validate the chosen add-ons and freeze their name and amount."""
    pedidos = list(dict.fromkeys(ids or []))
    if not pedidos:
        return []

    encontrados = {fila.id: fila for fila in tarifa_repo.listar_adicionales_por_ids(db, pedidos)}
    faltantes = [
        str(identificador) for identificador in pedidos if identificador not in encontrados
    ]
    if faltantes:
        raise RecursoNoEncontrado(
            "No encontramos alguno de los adicionales elegidos.",
            detalles=[
                detalle("adicionales", f"El adicional {ident} no existe.") for ident in faltantes
            ],
        )

    inactivos = [encontrados[ident] for ident in pedidos if not encontrados[ident].activo]
    if inactivos:
        raise AdicionalNoDisponible(
            detalles=[
                detalle("adicionales", f"«{fila.nombre}» ya no está disponible.")
                for fila in inactivos
            ]
        )

    return [
        AdicionalAplicado(
            servicio_adicional_id=encontrados[ident].id,
            nombre=encontrados[ident].nombre,
            monto_centimos=encontrados[ident].monto_centimos,
        )
        for ident in pedidos
    ]


# --------------------------------------------------------------------------
# THE calculation (RF-012)
# --------------------------------------------------------------------------
def calcular(
    db: Session,
    *,
    servicio_id: int,
    precio_base_centimos: int,
    moneda: str,
    tipo_vehiculo: str | None,
    adicionales_ids: list[int] | None = None,
    cupon: str | None = None,
    fecha: date | None = None,
) -> Desglose:
    """``(base x factor) + adicionales - descuentos``, term by term (RN-04).

    ``fecha`` is the day the SERVICE happens, not the day it is booked: a
    promotion valid on Tuesdays has to look at the Tuesday the customer is
    coming in. It defaults to today for the tariff preview.

    Flow 3a: an invalid or expired coupon does NOT abort the calculation. It is
    recorded with its reason and the total is recomputed without it, falling
    back to whatever automatic promotion was already in force.

    Flow 4a: a discount larger than the total clamps it to zero and raises an
    incident instead of billing a negative amount.
    """
    fecha = fecha or ahora().date()
    factor_milesimas = factor_de(db, servicio_id, tipo_vehiculo)
    base_ajustada = aplicar_factor(precio_base_centimos, factor_milesimas)

    adicionales = resolver_adicionales(db, adicionales_ids)
    adicionales_centimos = sum(item.monto_centimos for item in adicionales)
    antes_de_descuento = base_ajustada + adicionales_centimos

    promocion: Promocion | None = None
    cupon_aplicado: str | None = None
    cupon_rechazado: str | None = None
    motivo_rechazo: str | None = None

    codigo = (cupon or "").strip().upper()
    if codigo:
        candidata = tarifa_repo.obtener_promocion_por_cupon(db, codigo)
        motivo = (
            "El cupón no existe. Revisa el código e inténtalo de nuevo."
            if candidata is None
            else motivo_no_aplica(candidata, servicio_id, fecha)
        )
        if candidata is not None and motivo is None:
            promocion = candidata
            cupon_aplicado = codigo
        else:
            cupon_rechazado = codigo
            motivo_rechazo = motivo

    if promocion is None:
        promocion = promocion_automatica(db, servicio_id, fecha)

    descuento = descuento_de(promocion, antes_de_descuento) if promocion is not None else 0
    total = antes_de_descuento - descuento
    incidencia: str | None = None
    if total < 0:
        incidencia = (
            f"El descuento aplicado ({descuento} céntimos) superó el total "
            f"({antes_de_descuento} céntimos): la tarifa se limitó a cero."
        )
        descuento = antes_de_descuento
        total = 0

    return Desglose(
        precio_base_centimos=precio_base_centimos,
        tipo_vehiculo=tipo_vehiculo,
        factor_milesimas=factor_milesimas,
        base_ajustada_centimos=base_ajustada,
        adicionales=tuple(adicionales),
        adicionales_centimos=adicionales_centimos,
        descuento_centimos=descuento,
        total_centimos=total,
        moneda=moneda,
        promocion_id=promocion.id if promocion is not None else None,
        promocion_nombre=promocion.nombre if promocion is not None else None,
        cupon_aplicado=cupon_aplicado,
        cupon_rechazado=cupon_rechazado,
        motivo_rechazo_cupon=motivo_rechazo,
        incidencia=incidencia,
    )


def congelar(db: Session, reserva: Reserva, desglose: Desglose) -> None:
    """Write the breakdown and the add-ons next to the reservation (RF-012).

    The caller owns the transaction: this only ``flush``es, exactly like a
    repository call, so a reservation and its breakdown commit together.
    """
    tarifa_repo.crear_desglose(
        db,
        reserva_id=reserva.id,
        precio_base_centimos=desglose.precio_base_centimos,
        tipo_vehiculo=desglose.tipo_vehiculo,
        factor_milesimas=desglose.factor_milesimas,
        base_ajustada_centimos=desglose.base_ajustada_centimos,
        adicionales_centimos=desglose.adicionales_centimos,
        descuento_centimos=desglose.descuento_centimos,
        promocion_id=desglose.promocion_id,
        promocion_nombre=desglose.promocion_nombre,
        cupon_aplicado=desglose.cupon_aplicado,
        cupon_rechazado=desglose.cupon_rechazado,
        motivo_rechazo_cupon=desglose.motivo_rechazo_cupon,
        total_centimos=desglose.total_centimos,
        moneda=desglose.moneda,
        incidencia=desglose.incidencia,
        calculado_en=ahora_utc(),
    )
    for item in desglose.adicionales:
        tarifa_repo.crear_reserva_adicional(
            db,
            reserva_id=reserva.id,
            servicio_adicional_id=item.servicio_adicional_id,
            nombre=item.nombre,
            monto_centimos=item.monto_centimos,
            moneda=desglose.moneda,
        )


def registrar_incidencias(
    db: Session,
    desglose: Desglose,
    *,
    entidad: str,
    entidad_id: int,
    autor_id: int | None = None,
) -> None:
    """Write the two events RF-012 asks for by name (P7).

    Flow 3a says the rejection is "indicada" and flow 4a says the clamped total
    is "registrada"; both live in ``evento_dominio`` so the administrator can
    find them later without reading a log file.
    """
    if desglose.cupon_rechazado is not None:
        eventos.registrar_evento(
            db,
            entidad,
            entidad_id,
            eventos.TARIFA_CUPON_RECHAZADO,
            autor_id=autor_id,
            datos={
                "cupon": desglose.cupon_rechazado,
                "motivo": desglose.motivo_rechazo_cupon,
                "total_centimos": desglose.total_centimos,
            },
        )
    if desglose.incidencia is not None:
        eventos.registrar_evento(
            db,
            entidad,
            entidad_id,
            eventos.TARIFA_TOTAL_LIMITADO,
            autor_id=autor_id,
            datos={
                "incidencia": desglose.incidencia,
                "descuento_centimos": desglose.descuento_centimos,
                "total_centimos": desglose.total_centimos,
            },
        )


# --------------------------------------------------------------------------
# Catalogue pricing (RF-009 delta)
# --------------------------------------------------------------------------
def tipo_de_vehiculo(
    db: Session,
    usuario: Usuario,
    *,
    vehiculo_id: int | None = None,
    tipo_vehiculo: str | None = None,
) -> str | None:
    """Which vehicle type prices this request (RF-009 CA-02 v1.0).

    ``vehiculo_id`` wins and must belong to the caller - the same 404 a foreign
    vehicle gets when booking, so ids cannot be probed. Counter staff quoting
    for a walk-in send ``tipo_vehiculo`` directly instead.
    """
    if vehiculo_id is None:
        return tipo_vehiculo
    vehiculo = vehiculo_repo.obtener_por_id(db, vehiculo_id)
    if vehiculo is None or vehiculo.usuario_id != usuario.id:
        raise RecursoNoEncontrado(
            "No encontramos ese vehículo en tu cuenta.",
            detalles=[detalle("vehiculo_id", "El vehículo no pertenece a tu cuenta.")],
        )
    return vehiculo.tipo


def precios_aplicables(
    db: Session,
    servicios: list[Servicio],
    *,
    tipo_vehiculo: str | None,
    fecha: date | None = None,
) -> dict[int, PrecioAplicable]:
    """Price of every service for one vehicle type, plus its promotion.

    Two queries for the whole catalogue - the open factors and the automatic
    promotions of the day - and the rest is arithmetic, so opening the list
    does not cost one round trip per row (RNF-002).
    """
    fecha = fecha or ahora().date()
    factores = factores_vigentes(db)
    promociones = tarifa_repo.listar_promociones_automaticas(db, fecha)

    aplicables: dict[int, PrecioAplicable] = {}
    for servicio in servicios:
        precio = servicio.precio_vigente
        if precio is None:
            continue
        factor = resolver_factor(factores, servicio.id, tipo_vehiculo)
        monto = aplicar_factor(precio.monto_centimos, factor)
        promocion = _promocion_del_catalogo(promociones, servicio.id, fecha)
        aplicables[servicio.id] = PrecioAplicable(
            tipo_vehiculo=tipo_vehiculo,
            factor_milesimas=factor,
            monto_centimos=monto,
            moneda=precio.moneda,
            promocion=promocion,
            promocional_centimos=(
                monto - descuento_de(promocion, monto) if promocion is not None else None
            ),
        )
    return aplicables


def precio_de_paquete(
    db: Session, paquete, fecha: date | None = None
) -> tuple[int | None, Promocion | None]:
    """Promotional price of a package, or ``(None, None)`` when it has none."""
    fecha = fecha or ahora().date()
    candidatas = [
        promocion
        for promocion in tarifa_repo.listar_promociones_automaticas(db, fecha)
        if promocion.paquete_id == paquete.id and promocion.activa
    ]
    if not candidatas:
        return None, None
    promocion = max(
        candidatas, key=lambda promo: (descuento_de(promo, paquete.precio_centimos), -promo.id)
    )
    return paquete.precio_centimos - descuento_de(promocion, paquete.precio_centimos), promocion


def validar_moneda(moneda: str) -> str:
    """RN-12: every amount of the system is expressed in the same currency."""
    limpia = (moneda or MONEDA_PREDETERMINADA).upper()
    if limpia != MONEDA_PREDETERMINADA:
        raise DatosInvalidos(
            f"Los importes se expresan en {MONEDA_PREDETERMINADA} (RN-12). "
            "Envía el monto en esa moneda.",
            detalles=[detalle("moneda", f"Moneda no admitida: {limpia}.")],
        )
    return limpia
