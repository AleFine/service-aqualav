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
    Dispositivo,
    EstadoPago,
    FactorTipoVehiculo,
    Notificacion,
    Pago,
    Paquete,
    Permiso,
    Promocion,
    Recordatorio,
    Reserva,
    ReservaTarifaDesglose,
    Rol,
    Servicio,
    ServicioAdicional,
    Usuario,
)
from app.schemas import (
    AdicionalAplicadoOut,
    AdicionalOut,
    AsignacionOut,
    BahiaOut,
    BahiaResumen,
    CancelacionOut,
    ClienteResumen,
    ColaEsperaOut,
    DesgloseOut,
    DiaNoLaborableOut,
    Dinero,
    DispositivoOut,
    FactorOut,
    HistorialItem,
    NotificacionOut,
    PagoOut,
    PaqueteLineaOut,
    PaqueteOut,
    PermisoOut,
    PromocionOut,
    PromocionResumen,
    RecordatorioOut,
    ReservaOut,
    ResultadoAsignacionOut,
    RolOut,
    ServicioOut,
    ServicioResumen,
    SugerenciaOut,
    UsuarioOut,
    VehiculoResumen,
)
from app.services import (
    operacion_service,
    reserva_service,
    seguimiento_service,
    tarifa_service,
)
from app.services.tarifa_service import Desglose, PrecioAplicable


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


def armar_notificacion(fila: Notificacion) -> NotificacionOut:
    """RF-029: the notice AND how its delivery went, cause included."""
    return NotificacionOut.model_validate(fila)


def armar_dispositivo(fila: Dispositivo) -> DispositivoOut:
    return DispositivoOut.model_validate(fila)


def armar_recordatorio(fila: Recordatorio) -> RecordatorioOut:
    """RF-030: when the reminder went out and what it was answered."""
    return RecordatorioOut(
        reserva_id=fila.reserva_id,
        programado_para=a_lima(desde_bd(fila.programado_para)),
        enviado_en=a_lima(desde_bd(fila.enviado_en)) if fila.enviado_en else None,
        respuesta=fila.respuesta,
        respondido_en=(a_lima(desde_bd(fila.respondido_en)) if fila.respondido_en else None),
        estado=fila.estado,
    )


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


def armar_servicio(servicio: Servicio, aplicable: PrecioAplicable | None = None) -> ServicioOut:
    """``precio`` is an object, never a bare number (RF-009 'cómo escala').

    ``aplicable`` is the RF-009 v1.0 delta: the price for the vehicle type the
    caller asked about, plus the promotion the catalogue highlights beside the
    regular one. Omitting it answers exactly what the MVP answered.
    """
    precio = servicio.precio_vigente
    return ServicioOut(
        id=servicio.id,
        nombre=servicio.nombre,
        descripcion=servicio.descripcion,
        categoria=servicio.categoria,
        duracion_min=servicio.duracion_min,
        activo=servicio.activo,
        imagen_url=servicio.imagen_url,
        precio=(
            Dinero.de_centimos(precio.monto_centimos, precio.moneda)
            if precio is not None
            else Dinero.de_centimos(0)
        ),
        tipo_vehiculo=aplicable.tipo_vehiculo if aplicable is not None else None,
        factor_milesimas=aplicable.factor_milesimas if aplicable is not None else None,
        precio_aplicable=(
            Dinero.de_centimos(aplicable.monto_centimos, aplicable.moneda)
            if aplicable is not None
            else None
        ),
        promocion=(
            PromocionResumen.model_validate(aplicable.promocion)
            if aplicable is not None and aplicable.promocion is not None
            else None
        ),
        precio_promocional=(
            Dinero.de_centimos(aplicable.promocional_centimos, aplicable.moneda)
            if aplicable is not None and aplicable.promocional_centimos is not None
            else None
        ),
    )


def armar_servicios(
    servicios: list[Servicio], aplicables: dict[int, PrecioAplicable] | None = None
) -> list[ServicioOut]:
    aplicables = aplicables or {}
    return [armar_servicio(servicio, aplicables.get(servicio.id)) for servicio in servicios]


def armar_factor(factor: FactorTipoVehiculo) -> FactorOut:
    """RF-010 v1.0: the factor screen names the service it belongs to."""
    return FactorOut(
        id=factor.id,
        servicio_id=factor.servicio_id,
        servicio=factor.servicio.nombre if factor.servicio is not None else None,
        tipo_vehiculo=factor.tipo_vehiculo,
        factor_milesimas=factor.factor_milesimas,
        vigente_desde=a_lima(desde_bd(factor.vigente_desde)),
    )


def armar_adicional(adicional: ServicioAdicional) -> AdicionalOut:
    return AdicionalOut(
        id=adicional.id,
        nombre=adicional.nombre,
        descripcion=adicional.descripcion,
        monto=Dinero.de_centimos(adicional.monto_centimos, adicional.moneda),
        activo=adicional.activo,
    )


def armar_promocion(promocion: Promocion, *, vigente: bool = True) -> PromocionOut:
    """RF-011. ``vigente`` is derived from today, never stored (flow 4a)."""
    dias = tarifa_service.dias_de(promocion)
    return PromocionOut(
        id=promocion.id,
        nombre=promocion.nombre,
        descripcion=promocion.descripcion,
        tipo_descuento=promocion.tipo_descuento,
        valor=promocion.valor,
        servicio_id=promocion.servicio_id,
        paquete_id=promocion.paquete_id,
        codigo_cupon=promocion.codigo_cupon,
        dias_semana=sorted(dias) if dias else [],
        vigente_desde=promocion.vigente_desde,
        vigente_hasta=promocion.vigente_hasta,
        activa=promocion.activa,
        vigente=vigente,
    )


def armar_paquete(
    paquete: Paquete,
    *,
    promocional_centimos: int | None = None,
    promocion: Promocion | None = None,
) -> PaqueteOut:
    """RF-011: the bundle, its lines and what those lines cost separately."""
    lineas = []
    regular = 0
    for linea in paquete.lineas:
        precio = linea.servicio.precio_vigente
        monto = precio.monto_centimos if precio is not None else 0
        regular += monto * linea.cantidad
        lineas.append(
            PaqueteLineaOut(
                servicio_id=linea.servicio_id,
                nombre=linea.servicio.nombre,
                cantidad=linea.cantidad,
                precio=Dinero.de_centimos(monto, paquete.moneda),
            )
        )

    return PaqueteOut(
        id=paquete.id,
        nombre=paquete.nombre,
        descripcion=paquete.descripcion,
        precio=Dinero.de_centimos(paquete.precio_centimos, paquete.moneda),
        precio_regular=Dinero.de_centimos(regular, paquete.moneda),
        precio_promocional=(
            Dinero.de_centimos(promocional_centimos, paquete.moneda)
            if promocional_centimos is not None
            else None
        ),
        promocion=(PromocionResumen.model_validate(promocion) if promocion is not None else None),
        activo=paquete.activo,
        vigente_desde=paquete.vigente_desde,
        vigente_hasta=paquete.vigente_hasta,
        servicios=lineas,
    )


def armar_desglose(desglose: Desglose) -> DesgloseOut:
    """RF-012: the quote, term by term, exactly as it was computed."""
    return DesgloseOut(
        precio_base=Dinero.de_centimos(desglose.precio_base_centimos, desglose.moneda),
        tipo_vehiculo=desglose.tipo_vehiculo,
        factor_milesimas=desglose.factor_milesimas,
        base_ajustada=Dinero.de_centimos(desglose.base_ajustada_centimos, desglose.moneda),
        adicionales=[
            AdicionalAplicadoOut(
                servicio_adicional_id=item.servicio_adicional_id,
                nombre=item.nombre,
                monto=Dinero.de_centimos(item.monto_centimos, desglose.moneda),
            )
            for item in desglose.adicionales
        ],
        adicionales_total=Dinero.de_centimos(desglose.adicionales_centimos, desglose.moneda),
        descuento=Dinero.de_centimos(desglose.descuento_centimos, desglose.moneda),
        total=Dinero.de_centimos(desglose.total_centimos, desglose.moneda),
        promocion_id=desglose.promocion_id,
        promocion=desglose.promocion_nombre,
        cupon_aplicado=desglose.cupon_aplicado,
        cupon_rechazado=desglose.cupon_rechazado,
        motivo_rechazo_cupon=desglose.motivo_rechazo_cupon,
        incidencia=desglose.incidencia,
    )


def armar_desglose_guardado(fila: ReservaTarifaDesglose, reserva: Reserva) -> DesgloseOut:
    """The FROZEN breakdown of a reservation, read back from its own row."""
    return DesgloseOut(
        precio_base=Dinero.de_centimos(fila.precio_base_centimos, fila.moneda),
        tipo_vehiculo=fila.tipo_vehiculo,
        factor_milesimas=fila.factor_milesimas,
        base_ajustada=Dinero.de_centimos(fila.base_ajustada_centimos, fila.moneda),
        adicionales=[
            AdicionalAplicadoOut(
                servicio_adicional_id=item.servicio_adicional_id,
                nombre=item.nombre,
                monto=Dinero.de_centimos(item.monto_centimos, item.moneda),
            )
            for item in reserva.adicionales
        ],
        adicionales_total=Dinero.de_centimos(fila.adicionales_centimos, fila.moneda),
        descuento=Dinero.de_centimos(fila.descuento_centimos, fila.moneda),
        total=Dinero.de_centimos(fila.total_centimos, fila.moneda),
        promocion_id=fila.promocion_id,
        promocion=fila.promocion_nombre,
        cupon_aplicado=fila.cupon_aplicado,
        cupon_rechazado=fila.cupon_rechazado,
        motivo_rechazo_cupon=fila.motivo_rechazo_cupon,
        incidencia=fila.incidencia,
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
    # RF-022: read, never recalculated. The estimate is written where something
    # changed (``seguimiento_service.actualizar_estimado``), so a GET never
    # moves it and never notifies anybody.
    estimada = desde_bd(reserva.hora_estimada_entrega) or desde_bd(reserva.fin)

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
        porcentaje_avance=seguimiento_service.porcentaje_avance(db, reserva),
        hora_estimada_entrega=a_lima(estimada),
        minutos_retraso=seguimiento_service.minutos_de_retraso(reserva, estimada),
        # Same criterion the horizontal authorization filter uses (RF-017 CA-03).
        observaciones_ingreso=(
            reserva.observaciones_ingreso if reserva_service.puede_ver_todas(permisos) else None
        ),
        conformidad_cliente=reserva.conformidad_cliente,
        observacion_revision=reserva.observacion_revision,
        cancelacion=cancelacion,
        pago=armar_pago(pago) if pago is not None else None,
        tarifa=(
            armar_desglose_guardado(reserva.tarifa, reserva) if reserva.tarifa is not None else None
        ),
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
