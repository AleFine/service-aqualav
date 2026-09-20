"""Service catalog and tariffs (RF-009, RF-010).

EXTENSION POINT P6: a price change never overwrites an amount. It closes the
open ``servicio_precio`` row and inserts a new one, which is what lets an old
reservation keep the tariff it was created with (RF-010 CA-02) and what makes
the v1.0 revenue reports correct. RF-010 v1.0 adds the vehicle factors, which
``tarifa_service`` versions the very same way.

RF-009 v1.0 asks the catalogue to show "el precio aplicable a su vehículo".
That is why the listing and the detail can be asked for a vehicle type: the
regular price still travels in ``precio`` and the one this customer would pay
travels next to it, so the app can strike one through and highlight the other.
"""

from sqlalchemy.orm import Session

from app.core.errors import DatosInvalidos, RecursoNoEncontrado, detalle
from app.core.horario import ahora_utc
from app.models import MONEDA_PREDETERMINADA, Servicio, ServicioPrecio, Usuario
from app.repositories import servicio as servicio_repo
from app.schemas import ServicioActualizar, ServicioCrear, TarifaCalculoIn
from app.services import eventos, tarifa_service
from app.services.tarifa_service import Desglose, PrecioAplicable

#: What the catalogue answers: the rows plus, per service id, the price that
#: applies to the vehicle type asked for (empty when none was asked for).
Catalogo = tuple[list[Servicio], dict[int, PrecioAplicable]]


def listar_publico(db: Session, *, tipo_vehiculo: str | None = None) -> Catalogo:
    """Catalog seen by customers: active services only (RF-009 CA-01)."""
    servicios = servicio_repo.listar(db, solo_activos=True)
    return servicios, tarifa_service.precios_aplicables(db, servicios, tipo_vehiculo=tipo_vehiculo)


def listar_administracion(db: Session) -> list[Servicio]:
    """Catalog seen by the administrator: active and inactive (RF-010)."""
    return servicio_repo.listar(db, solo_activos=False)


def obtener_publico(db: Session, servicio_id: int) -> Servicio:
    """Detail of an active service. An inactive one is invisible (RF-010 CA-03)."""
    servicio = servicio_repo.obtener_por_id(db, servicio_id)
    if servicio is None or not servicio.activo:
        raise RecursoNoEncontrado("No encontramos ese servicio en el catálogo.")
    return servicio


def detalle_publico(
    db: Session, servicio_id: int, *, tipo_vehiculo: str | None = None
) -> tuple[Servicio, PrecioAplicable | None]:
    """Detail of an active service priced for one vehicle type (RF-009 CA-02)."""
    servicio = obtener_publico(db, servicio_id)
    aplicables = tarifa_service.precios_aplicables(db, [servicio], tipo_vehiculo=tipo_vehiculo)
    return servicio, aplicables.get(servicio.id)


def cotizar(db: Session, usuario: Usuario, datos: TarifaCalculoIn) -> Desglose:
    """Quote a service without booking it (RF-012).

    Lives here rather than in ``tarifa_service`` because quoting starts from
    the CATALOGUE - an inactive service cannot be quoted - and the tariff
    engine deliberately knows nothing about ``servicio_service`` so that this
    module can import it.
    """
    servicio = obtener_publico(db, datos.servicio_id)
    precio = precio_vigente(servicio)
    tipo = tarifa_service.tipo_de_vehiculo(
        db,
        usuario,
        vehiculo_id=datos.vehiculo_id,
        tipo_vehiculo=datos.tipo_vehiculo.value if datos.tipo_vehiculo else None,
    )

    desglose = tarifa_service.calcular(
        db,
        servicio_id=servicio.id,
        precio_base_centimos=precio.monto_centimos,
        moneda=precio.moneda,
        tipo_vehiculo=tipo,
        adicionales_ids=datos.adicionales,
        cupon=datos.cupon,
        fecha=datos.fecha,
    )
    # RF-012 flows 3a and 4a are REPORTED even on a quote: the counter must be
    # able to explain later why a customer was told a coupon did not work.
    tarifa_service.registrar_incidencias(
        db,
        desglose,
        entidad=eventos.ENTIDAD_SERVICIO,
        entidad_id=servicio.id,
        autor_id=usuario.id,
    )
    db.commit()
    return desglose


def obtener(db: Session, servicio_id: int) -> Servicio:
    """Detail for administration: an inactive service is still addressable.

    This is the direct door the administrator was missing. ``GET /servicios/{id}``
    only answers for active services (RF-010 CA-03), so the admin client used to
    have to list the whole catalogue and filter it client side just to reopen a
    service it had disabled a second ago; ``GET /admin/servicios/{id}`` closes
    that detour.
    """
    servicio = servicio_repo.obtener_por_id(db, servicio_id)
    if servicio is None:
        raise RecursoNoEncontrado("No encontramos ese servicio.")
    return servicio


def precio_vigente(servicio: Servicio) -> ServicioPrecio:
    """The open price row. A service without one cannot be sold."""
    precio = servicio.precio_vigente
    if precio is None:
        raise DatosInvalidos(
            "El servicio no tiene un precio vigente. Pide al administrador que lo configure.",
            detalles=[detalle("precio", "Falta la vigencia de precio del servicio.")],
        )
    return precio


def crear(db: Session, datos: ServicioCrear, autor: Usuario) -> Servicio:
    """Create a service together with its first open price row (RF-010 CA-01)."""
    servicio = servicio_repo.crear(
        db,
        nombre=datos.nombre,
        descripcion=datos.descripcion,
        categoria=datos.categoria,
        imagen_url=datos.imagen_url,
        duracion_min=datos.duracion_min,
    )
    servicio_repo.crear_precio(
        db,
        servicio_id=servicio.id,
        monto_centimos=datos.monto_centimos,
        moneda=datos.moneda or MONEDA_PREDETERMINADA,
        vigente_desde=ahora_utc(),
    )

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_SERVICIO,
        servicio.id,
        eventos.SERVICIO_CREADO,
        autor_id=autor.id,
        datos={"nombre": servicio.nombre, "monto_centimos": datos.monto_centimos},
    )
    db.commit()
    db.refresh(servicio)
    return servicio


def actualizar(
    db: Session, servicio_id: int, datos: ServicioActualizar, autor: Usuario
) -> Servicio:
    """Patch a service. A new amount opens a new price period, never an UPDATE."""
    servicio = obtener(db, servicio_id)
    cambios: dict[str, object] = {}

    for campo in ("nombre", "descripcion", "categoria", "imagen_url", "duracion_min", "activo"):
        valor = getattr(datos, campo)
        if valor is not None and valor != getattr(servicio, campo):
            setattr(servicio, campo, valor)
            cambios[campo] = valor

    precio_actual = servicio.precio_vigente
    moneda_pedida = datos.moneda or (
        precio_actual.moneda if precio_actual else MONEDA_PREDETERMINADA
    )
    cambia_monto = datos.monto_centimos is not None and (
        precio_actual is None
        or precio_actual.monto_centimos != datos.monto_centimos
        or precio_actual.moneda != moneda_pedida
    )

    if cambios:
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_SERVICIO,
            servicio.id,
            eventos.SERVICIO_ACTUALIZADO,
            autor_id=autor.id,
            datos=cambios,
        )

    if cambia_monto:
        momento = ahora_utc()
        anterior = servicio_repo.cerrar_precio_vigente(db, servicio.id, momento)
        servicio_repo.crear_precio(
            db,
            servicio_id=servicio.id,
            monto_centimos=int(datos.monto_centimos),
            moneda=moneda_pedida,
            vigente_desde=momento,
        )
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_SERVICIO,
            servicio.id,
            eventos.SERVICIO_PRECIO_CAMBIADO,
            autor_id=autor.id,
            datos={
                "monto_anterior": anterior.monto_centimos if anterior else None,
                "monto_nuevo": int(datos.monto_centimos),
                "moneda": moneda_pedida,
            },
        )

    db.commit()
    db.refresh(servicio)
    return servicio
