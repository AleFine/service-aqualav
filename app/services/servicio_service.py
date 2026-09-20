"""Service catalog and tariffs (RF-009, RF-010).

EXTENSION POINT P6: a price change never overwrites an amount. It closes the
open ``servicio_precio`` row and inserts a new one, which is what lets an old
reservation keep the tariff it was created with (RF-010 CA-02) and what makes
the v1.0 revenue reports correct.
"""

from sqlalchemy.orm import Session

from app.core.errors import DatosInvalidos, RecursoNoEncontrado, detalle
from app.core.horario import ahora_utc
from app.models import MONEDA_PREDETERMINADA, Servicio, ServicioPrecio, Usuario
from app.repositories import servicio as servicio_repo
from app.schemas import ServicioActualizar, ServicioCrear
from app.services import eventos


def listar_publico(db: Session) -> list[Servicio]:
    """Catalog seen by customers: active services only (RF-009 CA-01)."""
    return servicio_repo.listar(db, solo_activos=True)


def listar_administracion(db: Session) -> list[Servicio]:
    """Catalog seen by the administrator: active and inactive (RF-010)."""
    return servicio_repo.listar(db, solo_activos=False)


def obtener_publico(db: Session, servicio_id: int) -> Servicio:
    """Detail of an active service. An inactive one is invisible (RF-010 CA-03)."""
    servicio = servicio_repo.obtener_por_id(db, servicio_id)
    if servicio is None or not servicio.activo:
        raise RecursoNoEncontrado("No encontramos ese servicio en el catálogo.")
    return servicio


def obtener(db: Session, servicio_id: int) -> Servicio:
    """Detail for administration: an inactive service is still addressable."""
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

    for campo in ("nombre", "descripcion", "categoria", "duracion_min", "activo"):
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
