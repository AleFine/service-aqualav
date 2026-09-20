"""Payload assembly.

Every endpoint that answers with a reservation builds it here, so the shape the
mobile app receives is identical on creation, listing, detail, check-in, state
change, check-out and cancellation. Routers stay a parse/delegate/map sandwich.
"""

from sqlalchemy.orm import Session

from app.core.horario import a_lima, desde_bd
from app.models import (
    AsignacionServicio,
    Bahia,
    ColaEspera,
    DiaNoLaborable,
    EstadoPago,
    Pago,
    Permiso,
    Reserva,
    Rol,
    Servicio,
    Usuario,
)
from app.schemas import (
    AsignacionOut,
    BahiaOut,
    BahiaResumen,
    CancelacionOut,
    ClienteResumen,
    ColaEsperaOut,
    DiaNoLaborableOut,
    Dinero,
    HistorialItem,
    PagoOut,
    PermisoOut,
    ReservaOut,
    ResultadoAsignacionOut,
    RolOut,
    ServicioOut,
    ServicioResumen,
    SugerenciaOut,
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


def armar_bahia(bahia: Bahia) -> BahiaOut:
    return BahiaOut.model_validate(bahia)


def armar_dia_no_laborable(dia: DiaNoLaborable) -> DiaNoLaborableOut:
    return DiaNoLaborableOut(id=dia.id, fecha=dia.fecha, motivo=dia.motivo, autor=dia.autor)


def armar_asignacion(asignacion: AsignacionServicio) -> AsignacionOut:
    """RF-020: who works this service, where, and whether it was the suggestion."""
    return AsignacionOut(
        bahia=BahiaResumen.model_validate(asignacion.bahia),
        operario=asignacion.operario,
        operario_id=asignacion.operario_id,
        asignado_por=asignacion.asignado_por,
        asignado_en=a_lima(desde_bd(asignacion.asignado_en)),
        sugerida=asignacion.sugerida,
    )


def armar_cola(cola: ColaEspera) -> ColaEsperaOut:
    return ColaEsperaOut(posicion=cola.posicion, tiempo_estimado_min=cola.tiempo_estimado_min)


def armar_resultado_asignacion(
    db: Session, resultado, permisos: list[str]
) -> ResultadoAsignacionOut:
    """Answer of ``POST /reservas/{id}/asignacion`` (RF-020, including flow 2a)."""
    sugerencia = None
    if resultado.bahia_sugerida is not None or resultado.operario_sugerido is not None:
        sugerencia = SugerenciaOut(
            bahia=(
                BahiaResumen.model_validate(resultado.bahia_sugerida)
                if resultado.bahia_sugerida is not None
                else None
            ),
            operario_id=(
                resultado.operario_sugerido.id if resultado.operario_sugerido is not None else None
            ),
            operario=(
                resultado.operario_sugerido.nombre_completo
                if resultado.operario_sugerido is not None
                else None
            ),
        )

    return ResultadoAsignacionOut(
        reserva=armar_reserva(db, resultado.reserva, permisos),
        asignacion=(
            armar_asignacion(resultado.asignacion) if resultado.asignacion is not None else None
        ),
        cola=armar_cola(resultado.cola) if resultado.cola is not None else None,
        sugerencia=sugerencia,
    )


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
        # The QR is a scanning credential for the counter, so it follows the
        # same horizontal rule as ``observaciones_ingreso``.
        codigo_qr=(reserva.codigo_qr if reserva_service.puede_ver_todas(permisos) else None),
        atencion_sin_reserva=reserva.atencion_sin_reserva,
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
        observacion_revision=reserva.observacion_revision,
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
