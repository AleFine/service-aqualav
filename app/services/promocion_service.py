"""Packages, promotions and add-ons (RF-011, RN-04, RN-12).

RF-011 asks for three things the MVP did not have: bundles sold at a
preferential price, discounts with a validity window, and the guarantee that
two of those discounts never fight over the same service (flow 2a).

The third one is the interesting one. Overlap is checked ONLY between
coupon-free promotions with the same target, because an automatic promotion is
one the system picks for the customer: if two of them covered the same service
on the same day, which price the catalogue shows would depend on the query
plan. A coupon is chosen by the person typing it, so two coupons never create
that ambiguity and the counter can run as many as it likes.

Nothing here expires anything. ``vigente_hasta`` is read at calculation time
(``tarifa_service``), so a promotion stops applying on its own the day after
it ends, which is exactly what flow 4a describes.
"""

from datetime import date

from sqlalchemy.orm import Session

from app.core.errors import (
    DatosInvalidos,
    PromocionSolapada,
    RecursoNoEncontrado,
    detalle,
)
from app.core.horario import ahora
from app.models import Paquete, Promocion, Servicio, ServicioAdicional
from app.repositories import tarifa as tarifa_repo
from app.schemas import (
    AdicionalActualizar,
    AdicionalIn,
    PaqueteActualizar,
    PaqueteIn,
    PromocionActualizar,
    PromocionIn,
)
from app.services import eventos, tarifa_service

#: Permission that guards this module (principle P5). RF-011 names the
#: administrator as its only actor, so it is NOT granted to the counter.
PERMISO_ADMINISTRAR = "promocion:administrar"

#: Permission every customer-facing read needs; the catalogue is one screen.
PERMISO_LEER = "servicio:leer"


# --------------------------------------------------------------------------
# Packages
# --------------------------------------------------------------------------
def listar_paquetes(db: Session, *, solo_activos: bool = True) -> list[Paquete]:
    return tarifa_repo.listar_paquetes(db, solo_activos=solo_activos)


def obtener_paquete(db: Session, paquete_id: int) -> Paquete:
    paquete = tarifa_repo.obtener_paquete(db, paquete_id)
    if paquete is None:
        raise RecursoNoEncontrado("No encontramos ese paquete.")
    return paquete


def _validar_servicios(db: Session, servicio_ids: list[int]) -> None:
    faltantes = [
        str(identificador)
        for identificador in servicio_ids
        if db.get(Servicio, identificador) is None
    ]
    if faltantes:
        raise RecursoNoEncontrado(
            "Alguno de los servicios del paquete no existe.",
            detalles=[
                detalle("servicios", f"El servicio {identificador} no existe.")
                for identificador in faltantes
            ],
        )


def crear_paquete(db: Session, datos: PaqueteIn, autor) -> Paquete:
    """Publish a bundle. Its price is preferential by definition, not by check."""
    if tarifa_repo.obtener_paquete_por_nombre(db, datos.nombre) is not None:
        raise DatosInvalidos(
            "Ya existe un paquete con ese nombre. Usa otro nombre para publicarlo.",
            detalles=[detalle("nombre", "El nombre del paquete ya está en uso.")],
        )
    _validar_servicios(db, [linea.servicio_id for linea in datos.servicios])

    paquete = tarifa_repo.crear_paquete(
        db,
        nombre=datos.nombre,
        descripcion=datos.descripcion,
        precio_centimos=datos.precio_centimos,
        moneda=tarifa_service.validar_moneda(datos.moneda),
        vigente_desde=datos.vigente_desde,
        vigente_hasta=datos.vigente_hasta,
    )
    for linea in datos.servicios:
        tarifa_repo.agregar_linea(
            db,
            paquete_id=paquete.id,
            servicio_id=linea.servicio_id,
            cantidad=linea.cantidad,
        )

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_PAQUETE,
        paquete.id,
        eventos.PAQUETE_CREADO,
        autor_id=autor.id,
        datos={
            "nombre": paquete.nombre,
            "precio_centimos": paquete.precio_centimos,
            "servicios": [linea.servicio_id for linea in datos.servicios],
        },
    )
    db.commit()
    db.refresh(paquete)
    return paquete


def actualizar_paquete(db: Session, paquete_id: int, datos: PaqueteActualizar, autor) -> Paquete:
    paquete = obtener_paquete(db, paquete_id)
    cambios: dict[str, object] = {}

    for campo in (
        "nombre",
        "descripcion",
        "precio_centimos",
        "vigente_desde",
        "vigente_hasta",
        "activo",
    ):
        valor = getattr(datos, campo)
        if valor is not None and valor != getattr(paquete, campo):
            setattr(paquete, campo, valor)
            cambios[campo] = valor.isoformat() if isinstance(valor, date) else valor

    if datos.servicios is not None:
        _validar_servicios(db, [linea.servicio_id for linea in datos.servicios])
        tarifa_repo.limpiar_lineas(db, paquete)
        for linea in datos.servicios:
            tarifa_repo.agregar_linea(
                db,
                paquete_id=paquete.id,
                servicio_id=linea.servicio_id,
                cantidad=linea.cantidad,
            )
        cambios["servicios"] = [linea.servicio_id for linea in datos.servicios]

    if cambios:
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_PAQUETE,
            paquete.id,
            eventos.PAQUETE_ACTUALIZADO,
            autor_id=autor.id,
            datos=cambios,
        )
    db.commit()
    db.refresh(paquete)
    return paquete


# --------------------------------------------------------------------------
# Promotions (RF-011)
# --------------------------------------------------------------------------
def listar_promociones(db: Session, *, solo_activas: bool = True) -> list[Promocion]:
    return tarifa_repo.listar_promociones(db, solo_activas=solo_activas)


def listar_vigentes(db: Session, fecha: date | None = None) -> list[Promocion]:
    """Promotions the customer can actually get today (RF-011 "Salidas").

    An expired one is simply not here: the catalogue shows the regular price
    again without anybody deactivating anything (flow 4a).
    """
    fecha = fecha or ahora().date()
    return [
        promocion
        for promocion in tarifa_repo.listar_promociones(db, solo_activas=True)
        if promocion.vigente_desde <= fecha
        and (promocion.vigente_hasta is None or promocion.vigente_hasta >= fecha)
    ]


def obtener_promocion(db: Session, promocion_id: int) -> Promocion:
    promocion = tarifa_repo.obtener_promocion(db, promocion_id)
    if promocion is None:
        raise RecursoNoEncontrado("No encontramos esa promoción.")
    return promocion


def _verificar_solapamiento(
    db: Session,
    *,
    servicio_id: int | None,
    paquete_id: int | None,
    codigo_cupon: str | None,
    vigente_desde: date,
    vigente_hasta: date | None,
    excluir_id: int | None = None,
) -> None:
    """RF-011 flow 2a: warn and ask for a different range.

    Only automatic promotions can collide; a coupon is opt-in, so it is left
    alone on purpose.
    """
    if codigo_cupon is not None:
        return

    solapadas = tarifa_repo.listar_promociones_solapadas(
        db,
        servicio_id=servicio_id,
        paquete_id=paquete_id,
        vigente_desde=vigente_desde,
        vigente_hasta=vigente_hasta,
        excluir_id=excluir_id,
    )
    if solapadas:
        raise PromocionSolapada(
            detalles=[detalle("vigente_desde", _rango_ocupado(promo)) for promo in solapadas]
        )


def _rango_ocupado(promocion: Promocion) -> str:
    """Which promotion already covers those dates, so the admin can move them."""
    hasta = (
        promocion.vigente_hasta.strftime("%d/%m/%Y")
        if promocion.vigente_hasta is not None
        else "indefinido"
    )
    return (
        f"«{promocion.nombre}» ya rige del "
        f"{promocion.vigente_desde.strftime('%d/%m/%Y')} al {hasta}."
    )


def _validar_destino(db: Session, servicio_id: int | None, paquete_id: int | None) -> None:
    if servicio_id is not None and paquete_id is not None:
        raise DatosInvalidos(
            "Una promoción aplica a un servicio o a un paquete, no a ambos. "
            "Elige uno de los dos.",
            detalles=[detalle("paquete_id", "Envía solo servicio_id o solo paquete_id.")],
        )
    if servicio_id is not None and db.get(Servicio, servicio_id) is None:
        raise RecursoNoEncontrado(
            "No encontramos ese servicio.",
            detalles=[detalle("servicio_id", "El servicio no existe.")],
        )
    if paquete_id is not None and db.get(Paquete, paquete_id) is None:
        raise RecursoNoEncontrado(
            "No encontramos ese paquete.",
            detalles=[detalle("paquete_id", "El paquete no existe.")],
        )


def crear_promocion(db: Session, datos: PromocionIn, autor) -> Promocion:
    """Publish a promotion, refusing to overlap another one (flow 2a)."""
    if tarifa_repo.obtener_promocion_por_nombre(db, datos.nombre) is not None:
        raise DatosInvalidos(
            "Ya existe una promoción con ese nombre. Usa otro nombre para publicarla.",
            detalles=[detalle("nombre", "El nombre de la promoción ya está en uso.")],
        )
    _validar_destino(db, datos.servicio_id, datos.paquete_id)

    cupon = datos.codigo_cupon
    if cupon is not None and tarifa_repo.obtener_promocion_por_cupon(db, cupon) is not None:
        raise DatosInvalidos(
            "Ese cupón ya está en uso por otra promoción. Elige otro código.",
            detalles=[detalle("codigo_cupon", "El cupón ya existe.")],
        )

    _verificar_solapamiento(
        db,
        servicio_id=datos.servicio_id,
        paquete_id=datos.paquete_id,
        codigo_cupon=cupon,
        vigente_desde=datos.vigente_desde,
        vigente_hasta=datos.vigente_hasta,
    )

    promocion = tarifa_repo.crear_promocion(
        db,
        nombre=datos.nombre,
        descripcion=datos.descripcion,
        tipo_descuento=datos.tipo_descuento.value,
        valor=datos.valor,
        servicio_id=datos.servicio_id,
        paquete_id=datos.paquete_id,
        codigo_cupon=cupon,
        dias_semana=datos.dias_semana_texto,
        vigente_desde=datos.vigente_desde,
        vigente_hasta=datos.vigente_hasta,
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_PROMOCION,
        promocion.id,
        eventos.PROMOCION_CREADA,
        autor_id=autor.id,
        datos={
            "nombre": promocion.nombre,
            "tipo_descuento": promocion.tipo_descuento,
            "valor": promocion.valor,
            "servicio_id": promocion.servicio_id,
            "paquete_id": promocion.paquete_id,
            "codigo_cupon": promocion.codigo_cupon,
            "vigente_desde": promocion.vigente_desde.isoformat(),
            "vigente_hasta": (
                promocion.vigente_hasta.isoformat() if promocion.vigente_hasta else None
            ),
        },
    )
    db.commit()
    db.refresh(promocion)
    return promocion


def actualizar_promocion(
    db: Session, promocion_id: int, datos: PromocionActualizar, autor
) -> Promocion:
    """Adjust a promotion. Moving its range re-runs the overlap check (2a)."""
    promocion = obtener_promocion(db, promocion_id)

    desde = datos.vigente_desde or promocion.vigente_desde
    hasta = datos.vigente_hasta if datos.vigente_hasta is not None else promocion.vigente_hasta
    activa = promocion.activa if datos.activa is None else datos.activa

    if activa and (
        datos.vigente_desde is not None or datos.vigente_hasta is not None or datos.activa
    ):
        _verificar_solapamiento(
            db,
            servicio_id=promocion.servicio_id,
            paquete_id=promocion.paquete_id,
            codigo_cupon=promocion.codigo_cupon,
            vigente_desde=desde,
            vigente_hasta=hasta,
            excluir_id=promocion.id,
        )

    cambios: dict[str, object] = {}
    for campo in ("nombre", "descripcion", "valor", "vigente_desde", "vigente_hasta", "activa"):
        valor = getattr(datos, campo)
        if valor is not None and valor != getattr(promocion, campo):
            setattr(promocion, campo, valor)
            cambios[campo] = valor.isoformat() if isinstance(valor, date) else valor

    if datos.dias_semana is not None:
        promocion.dias_semana = datos.dias_semana_texto
        cambios["dias_semana"] = promocion.dias_semana

    if cambios:
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_PROMOCION,
            promocion.id,
            eventos.PROMOCION_ACTUALIZADA,
            autor_id=autor.id,
            datos=cambios,
        )
    db.commit()
    db.refresh(promocion)
    return promocion


# --------------------------------------------------------------------------
# Add-ons (the "adicionales" term of RN-04)
# --------------------------------------------------------------------------
def listar_adicionales(db: Session, *, solo_activos: bool = True) -> list[ServicioAdicional]:
    return tarifa_repo.listar_adicionales(db, solo_activos=solo_activos)


def obtener_adicional(db: Session, adicional_id: int) -> ServicioAdicional:
    adicional = tarifa_repo.obtener_adicional(db, adicional_id)
    if adicional is None:
        raise RecursoNoEncontrado("No encontramos ese adicional.")
    return adicional


def crear_adicional(db: Session, datos: AdicionalIn, autor) -> ServicioAdicional:
    if tarifa_repo.obtener_adicional_por_nombre(db, datos.nombre) is not None:
        raise DatosInvalidos(
            "Ya existe un adicional con ese nombre. Usa otro nombre.",
            detalles=[detalle("nombre", "El nombre del adicional ya está en uso.")],
        )
    adicional = tarifa_repo.crear_adicional(
        db,
        nombre=datos.nombre,
        descripcion=datos.descripcion,
        monto_centimos=datos.monto_centimos,
        moneda=tarifa_service.validar_moneda(datos.moneda),
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_ADICIONAL,
        adicional.id,
        eventos.ADICIONAL_CREADO,
        autor_id=autor.id,
        datos={"nombre": adicional.nombre, "monto_centimos": adicional.monto_centimos},
    )
    db.commit()
    db.refresh(adicional)
    return adicional


def actualizar_adicional(
    db: Session, adicional_id: int, datos: AdicionalActualizar, autor
) -> ServicioAdicional:
    adicional = obtener_adicional(db, adicional_id)
    cambios: dict[str, object] = {}
    for campo in ("nombre", "descripcion", "monto_centimos", "activo"):
        valor = getattr(datos, campo)
        if valor is not None and valor != getattr(adicional, campo):
            setattr(adicional, campo, valor)
            cambios[campo] = valor

    if cambios:
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_ADICIONAL,
            adicional.id,
            eventos.ADICIONAL_ACTUALIZADO,
            autor_id=autor.id,
            datos=cambios,
        )
    db.commit()
    db.refresh(adicional)
    return adicional
