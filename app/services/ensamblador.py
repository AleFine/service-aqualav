"""Payload assembly.

Every endpoint that answers with a reservation builds it here, so the shape the
mobile app receives is identical on creation, listing, detail, check-in, state
change, check-out and cancellation. Routers stay a parse/delegate/map sandwich.
"""

from sqlalchemy.orm import Session

from app.core.horario import a_lima, desde_bd
from app.models import EstadoPago, Pago, Permiso, Reserva, Rol, Servicio, Usuario
from app.schemas import (
    BahiaResumen,
    CancelacionOut,
    ClienteResumen,
    Dinero,
    HistorialItem,
    PagoOut,
    PermisoOut,
    ReservaOut,
    RolOut,
    ServicioOut,
    ServicioResumen,
    UsuarioOut,
    VehiculoResumen,
)
from app.services import operacion_service, reserva_service


def _autor(usuario: Usuario | None) -> str | None:
    return usuario.nombre_completo if usuario is not None else None


def armar_usuario(usuario: Usuario) -> UsuarioOut:
    return UsuarioOut.model_validate(usuario)


def armar_rol(rol: Rol) -> RolOut:
    """``permisos`` is the sorted list of codes the role grants (RF-004)."""
    return RolOut(
        id=rol.id,
        nombre=rol.nombre,
        descripcion=rol.descripcion,
        permisos=rol.codigos_permisos,
    )


def armar_permiso(permiso: Permiso) -> PermisoOut:
    return PermisoOut.model_validate(permiso)


def armar_servicio(servicio: Servicio) -> ServicioOut:
    """``precio`` is an object, never a bare number (RF-009 'cómo escala')."""
    precio = servicio.precio_vigente
    return ServicioOut(
        id=servicio.id,
        nombre=servicio.nombre,
        descripcion=servicio.descripcion,
        categoria=servicio.categoria,
        duracion_min=servicio.duracion_min,
        activo=servicio.activo,
        precio=(
            Dinero.de_centimos(precio.monto_centimos, precio.moneda)
            if precio is not None
            else Dinero.de_centimos(0)
        ),
    )


def armar_pago(pago: Pago) -> PagoOut:
    return PagoOut(
        id=pago.id,
        monto=Dinero.de_centimos(pago.monto_centimos, pago.moneda),
        medio=pago.medio,
        estado=pago.estado,
        registrado_en=a_lima(desde_bd(pago.registrado_en)),
        autor=_autor(pago.autor),
    )


def _pago_visible(reserva: Reserva) -> Pago | None:
    """The confirmed payment, or the last one registered if none is confirmed."""
    if not reserva.pagos:
        return None
    confirmados = [pago for pago in reserva.pagos if pago.estado == EstadoPago.CONFIRMADO.value]
    return (confirmados or list(reserva.pagos))[-1]


def armar_reserva(
    db: Session,
    reserva: Reserva,
    permisos: list[str],
    *,
    penalidad: Dinero | None = None,
) -> ReservaOut:
    """Build ``ReservaOut``, including the transitions this caller may trigger."""
    cancelacion = None
    if reserva.cancelada_en is not None or reserva.motivo_cancelacion is not None:
        cancelacion = CancelacionOut(
            motivo=reserva.motivo_cancelacion,
            cancelada_en=a_lima(desde_bd(reserva.cancelada_en)) if reserva.cancelada_en else None,
            autor=_autor(reserva.cancelada_por),
        )

    pago = _pago_visible(reserva)

    return ReservaOut(
        id=reserva.id,
        codigo=reserva.codigo,
        estado=reserva.estado,
        inicio=a_lima(desde_bd(reserva.inicio)),
        fin=a_lima(desde_bd(reserva.fin)),
        creada_en=a_lima(desde_bd(reserva.creada_en)),
        monto=Dinero.de_centimos(reserva.monto_centimos, reserva.moneda),
        modalidad_pago=reserva.modalidad_pago,
        servicio=ServicioResumen.model_validate(reserva.servicio),
        vehiculo=VehiculoResumen.model_validate(reserva.vehiculo),
        bahia=BahiaResumen.model_validate(reserva.bahia),
        cliente=ClienteResumen.model_validate(reserva.usuario),
        transiciones_permitidas=operacion_service.transiciones_permitidas(db, reserva, permisos),
        hora_ingreso=a_lima(desde_bd(reserva.hora_ingreso)) if reserva.hora_ingreso else None,
        hora_fin_real=a_lima(desde_bd(reserva.hora_fin_real)) if reserva.hora_fin_real else None,
        hora_entrega=a_lima(desde_bd(reserva.hora_entrega)) if reserva.hora_entrega else None,
        fin_estimado=a_lima(reserva_service.fin_estimado(reserva)),
        # Same criterion the horizontal authorization filter uses (RF-017 CA-03).
        observaciones_ingreso=(
            reserva.observaciones_ingreso if reserva_service.puede_ver_todas(permisos) else None
        ),
        conformidad_cliente=reserva.conformidad_cliente,
        cancelacion=cancelacion,
        pago=armar_pago(pago) if pago is not None else None,
        historial=[
            HistorialItem(
                estado=fila.estado,
                ocurrido_en=a_lima(desde_bd(fila.ocurrido_en)),
                autor=_autor(fila.autor),
            )
            for fila in reserva.historial
        ],
        penalidad=penalidad,
    )


def armar_reservas(db: Session, reservas: list[Reserva], permisos: list[str]) -> list[ReservaOut]:
    return [armar_reserva(db, reserva, permisos) for reserva in reservas]
