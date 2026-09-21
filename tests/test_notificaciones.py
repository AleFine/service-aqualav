"""RF-029 - Notificaciones de cambio de estado.

Cubre los dos CA del requisito y sus dos flujos alternos:

* ``CA-01`` un cambio a «Finalizado» se notifica en menos de 60 segundos;
* ``CA-02`` agotados los reintentos, se registra el error con su causa;
* ``3a`` hasta tres reintentos con espera incremental;
* ``3b`` si el cliente desactivó el push se usa únicamente correo.

Además cierra el hueco del MVP: ``NotificadorEnApp`` era un stub sin tabla.
Ninguna prueba duerme: la espera incremental se inyecta y el «menos de 60
segundos» se mide contra la marca que quedó guardada, no esperando un minuto.
"""

from sqlalchemy import select

from app.core.horario import ahora_utc, desde_bd
from app.models import (
    CanalNotificacion,
    Dispositivo,
    EstadoEnvio,
    EventoNotificacion,
    Idioma,
    Notificacion,
    PlantillaNotificacion,
    Reserva,
)
from app.schemas import ReservaCrear
from app.services import notificacion_service, reserva_service
from app.services.proveedores.correo import CorreoSimulado
from app.services.proveedores.push import EnvioDePushFallido, PushSimulado
from tests.conftest import (
    RUTA,
    asignar,
    avanzar_estado,
    crear_reserva,
    instante,
    llevar_hasta_finalizado,
    proximo_lunes,
)

TOKEN_DEMO = "token-de-prueba-0001"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _reserva_confirmada(api_cliente, servicio, vehiculo_id, hora: int = 10) -> dict:
    respuesta = crear_reserva(
        api_cliente, servicio.id, vehiculo_id, instante(proximo_lunes(), hora)
    )
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _notificaciones(db, reserva_id: int, evento: str | None = None) -> list[Notificacion]:
    consulta = select(Notificacion).where(Notificacion.reserva_id == reserva_id)
    if evento is not None:
        consulta = consulta.where(Notificacion.evento == evento)
    return list(db.scalars(consulta.order_by(Notificacion.id)).all())


def _canales(filas: list[Notificacion]) -> set[str]:
    return {fila.canal for fila in filas}


def _correo_que_falla(**enlace):
    return CorreoSimulado(fallar=True, **enlace)


# --------------------------------------------------------------------------
# CA-01 - menos de 60 segundos
# --------------------------------------------------------------------------
def test_finalizar_el_servicio_notifica_en_menos_de_60_segundos(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-029 CA-01: «Finalizado» dispara la notificación en menos de un minuto.

    Se verifica de forma determinista: el despacho es síncrono, así que la
    prueba compara la marca ``enviado_en`` que quedó guardada contra el momento
    en que el servicio terminó (``hora_fin_real``), sin esperar nada.
    """
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
    )
    asignar(api_recepcion, reserva["id"])
    for estado in ("en_lavado", "secado", "acabado"):
        assert avanzar_estado(api_operario, reserva["id"], estado).status_code == 200

    antes = ahora_utc()
    respuesta = avanzar_estado(api_operario, reserva["id"], "finalizado")
    despues = ahora_utc()
    assert respuesta.status_code == 200, respuesta.text

    filas = _notificaciones(db, reserva["id"], EventoNotificacion.FINALIZACION.value)
    assert filas, "finalizar el servicio tiene que dejar su notificación"

    fila_reserva = db.get(Reserva, reserva["id"])
    for fila in filas:
        assert fila.estado_envio == EstadoEnvio.ENVIADA.value
        assert fila.enviado_en is not None
        # El envío ocurrió DENTRO de la petición que cambió el estado, y esa
        # petición cabe de sobra en el minuto que pide el CA.
        assert antes <= desde_bd(fila.enviado_en) <= despues
        assert despues - antes < notificacion_service.LATENCIA_MAXIMA
        assert desde_bd(fila_reserva.hora_fin_real) <= desde_bd(fila.enviado_en)


def test_los_seis_eventos_del_ciclo_de_vida_dejan_su_notificacion(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-029: confirmación, inicio, finalización y entrega son eventos propios.

    Cuál transición levanta cuál evento lo declara ``transicion_estado``; este
    recorrido comprueba que la tabla y el despachador están de acuerdo.
    """
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])

    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos",
        json={"medio": "efectivo", "monto_centimos": reserva["monto"]["monto_centimos"]},
        headers={"Idempotency-Key": "pago-notificaciones-1"},
    )
    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )
    assert respuesta.status_code == 200, respuesta.text

    eventos = {fila.evento for fila in _notificaciones(db, reserva["id"])}
    assert {
        EventoNotificacion.CONFIRMACION.value,
        EventoNotificacion.INICIO.value,
        EventoNotificacion.FINALIZACION.value,
        EventoNotificacion.ENTREGA.value,
    } <= eventos


def test_cancelar_una_reserva_notifica_la_cancelacion(api_cliente, db, servicio_medio, vehiculo_id):
    """RF-029: «cancelación» es uno de los seis eventos del ciclo de vida."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Cambio de planes"}
    )
    assert respuesta.status_code == 200, respuesta.text

    filas = _notificaciones(db, reserva["id"], EventoNotificacion.CANCELACION.value)
    assert filas
    assert all(fila.estado_envio == EstadoEnvio.ENVIADA.value for fila in filas)


# --------------------------------------------------------------------------
# El mensaje viene de una plantilla, no de concatenar cadenas
# --------------------------------------------------------------------------
def test_el_mensaje_se_compone_desde_la_plantilla_del_evento(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-029: «mensaje compuesto según plantilla del evento y el idioma»."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    filas = _notificaciones(db, reserva["id"], EventoNotificacion.CONFIRMACION.value)
    correo = next(fila for fila in filas if fila.canal == CanalNotificacion.CORREO.value)

    plantilla = db.scalars(
        select(PlantillaNotificacion).where(
            PlantillaNotificacion.evento == EventoNotificacion.CONFIRMACION.value,
            PlantillaNotificacion.canal == CanalNotificacion.CORREO.value,
            PlantillaNotificacion.idioma == Idioma.ES.value,
        )
    ).first()
    assert plantilla is not None, "el seed siembra la plantilla del evento"

    # El texto es exactamente el de la fila, interpolado con los datos de la
    # reserva: si alguien concatenase el mensaje en el servicio, esto fallaría.
    assert correo.asunto == plantilla.asunto.format(codigo=reserva["codigo"])
    assert reserva["codigo"] in correo.cuerpo
    assert servicio_medio.nombre in correo.cuerpo


def test_la_plantilla_se_elige_por_el_idioma_del_usuario(
    api_cliente, db, servicio_medio, vehiculo_id, usuario_cliente
):
    """RF-029: el idioma del usuario decide la plantilla, no una constante."""
    usuario_cliente.idioma = Idioma.EN.value
    db.commit()

    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    filas = _notificaciones(db, reserva["id"], EventoNotificacion.CONFIRMACION.value)
    correo = next(fila for fila in filas if fila.canal == CanalNotificacion.CORREO.value)

    assert correo.asunto.startswith("Booking")
    assert "confirmed" in correo.asunto


def test_una_plantilla_nueva_habilita_un_canal_sin_tocar_codigo(
    api_cliente, db, servicio_medio, vehiculo_id, usuario_cliente
):
    """El conjunto de canales de un evento es DATO, igual que las transiciones.

    ``estado_cambiado`` sólo publica en la bandeja porque nadie escribió su
    plantilla de push. Insertarla lo habilita sin desplegar código.
    """
    db.add(
        PlantillaNotificacion(
            evento=EventoNotificacion.ESTADO_CAMBIADO.value,
            canal=CanalNotificacion.PUSH.value,
            idioma=Idioma.ES.value,
            asunto="Tu reserva {codigo} avanzó",
            cuerpo="Ahora está en «{estado}».",
        )
    )
    db.add(
        Dispositivo(
            usuario_id=usuario_cliente.id,
            token_push=TOKEN_DEMO,
            plataforma="android",
            activo=True,
        )
    )
    db.commit()

    canales = _canales(
        notificacion_service.despachar(
            db,
            usuario_cliente,
            EventoNotificacion.ESTADO_CAMBIADO.value,
        )
    )
    db.commit()

    assert canales == {CanalNotificacion.EN_APP.value, CanalNotificacion.PUSH.value}


# --------------------------------------------------------------------------
# 3b - el cliente decide los canales
# --------------------------------------------------------------------------
def test_si_el_cliente_desactiva_el_push_se_usa_unicamente_correo(db, usuario_cliente):
    """RF-029 flujo 3b, leído al pie de la letra."""
    db.add(
        Dispositivo(
            usuario_id=usuario_cliente.id,
            token_push=TOKEN_DEMO,
            plataforma="android",
            activo=True,
        )
    )
    usuario_cliente.notificar_push = False
    db.commit()

    filas = notificacion_service.despachar(
        db, usuario_cliente, EventoNotificacion.CONFIRMACION.value
    )
    db.commit()

    assert _canales(filas) == {CanalNotificacion.EN_APP.value, CanalNotificacion.CORREO.value}


def test_con_push_activo_y_dispositivo_registrado_se_usan_los_tres_canales(db, usuario_cliente):
    """RF-029: «push + correo», más la bandeja que el cliente siempre tiene."""
    db.add(
        Dispositivo(
            usuario_id=usuario_cliente.id,
            token_push=TOKEN_DEMO,
            plataforma="android",
            activo=True,
        )
    )
    db.commit()

    filas = notificacion_service.despachar(
        db, usuario_cliente, EventoNotificacion.CONFIRMACION.value
    )
    db.commit()

    assert _canales(filas) == {
        CanalNotificacion.EN_APP.value,
        CanalNotificacion.CORREO.value,
        CanalNotificacion.PUSH.value,
    }
    assert all(fila.estado_envio == EstadoEnvio.ENVIADA.value for fila in filas)


def test_sin_dispositivo_registrado_no_se_intenta_el_push(db, usuario_cliente):
    """RF-029 precondición: hace falta un dispositivo registrado."""
    filas = notificacion_service.despachar(
        db, usuario_cliente, EventoNotificacion.CONFIRMACION.value
    )
    db.commit()

    assert CanalNotificacion.PUSH.value not in _canales(filas)


# --------------------------------------------------------------------------
# CA-02 y 3a - reintentos con espera incremental y registro de la causa
# --------------------------------------------------------------------------
def test_tras_agotar_los_reintentos_se_registra_el_error_con_su_causa(db, usuario_cliente):
    """RF-029 CA-02 y flujo 3a.

    Cuatro intentos (uno más tres reintentos) con esperas 1 s, 2 s y 4 s. La
    espera se inyecta, así que la prueba comprueba el retardo incremental sin
    dormir ni un milisegundo.
    """
    pausas: list[float] = []

    filas = notificacion_service.despachar(
        db,
        usuario_cliente,
        EventoNotificacion.CONFIRMACION.value,
        correo=_correo_que_falla,
        espera=pausas.append,
    )
    db.commit()

    correo = next(fila for fila in filas if fila.canal == CanalNotificacion.CORREO.value)
    assert correo.estado_envio == EstadoEnvio.FALLIDA.value
    assert correo.intentos == notificacion_service.INTENTOS_MAXIMOS
    assert correo.error and usuario_cliente.correo in correo.error
    assert correo.enviado_en is None
    assert pausas == notificacion_service.esperas() == [1.0, 2.0, 4.0]


def test_un_canal_caido_no_impide_los_demas(db, usuario_cliente):
    """Una notificación nunca puede tumbar la operación ni los otros canales."""
    filas = notificacion_service.despachar(
        db,
        usuario_cliente,
        EventoNotificacion.CONFIRMACION.value,
        correo=_correo_que_falla,
        espera=lambda _segundos: None,
    )
    db.commit()

    por_canal = {fila.canal: fila for fila in filas}
    assert por_canal[CanalNotificacion.CORREO.value].estado_envio == EstadoEnvio.FALLIDA.value
    assert por_canal[CanalNotificacion.EN_APP.value].estado_envio == EstadoEnvio.ENVIADA.value


def test_un_envio_que_falla_no_revierte_el_cambio_de_estado(
    api_cliente, db, servicio_medio, vehiculo_id, usuario_cliente
):
    """La reserva se crea aunque el correo se caiga (mismo espíritu que RF-001 5a)."""
    datos = ReservaCrear(
        servicio_id=servicio_medio.id,
        vehiculo_id=vehiculo_id,
        inicio=instante(proximo_lunes(), 15),
    )
    reserva = reserva_service.crear(
        db,
        usuario_cliente,
        datos,
        correo=_correo_que_falla,
        espera=lambda _segundos: None,
    )

    assert reserva.id is not None
    correo = next(
        fila
        for fila in _notificaciones(db, reserva.id)
        if fila.canal == CanalNotificacion.CORREO.value
    )
    assert correo.estado_envio == EstadoEnvio.FALLIDA.value


# --------------------------------------------------------------------------
# Los proveedores simulados persisten lo que entregaron
# --------------------------------------------------------------------------
def test_el_correo_simulado_persiste_el_mensaje_en_la_tabla_notificacion(db, usuario_cliente):
    """Sección 4 del plan: el proveedor registra el envío, no sólo lo loguea.

    La interfaz sigue siendo ``enviar(destino, asunto, cuerpo)`` y sus
    llamantes (RF-001, RF-003, RF-035) no cambiaron: la persistencia es una
    propiedad de la INSTANCIA, que el despachador construye ligada a la fila.
    """
    filas = notificacion_service.despachar(
        db, usuario_cliente, EventoNotificacion.CONFIRMACION.value
    )
    db.commit()

    correo = next(fila for fila in filas if fila.canal == CanalNotificacion.CORREO.value)
    assert correo.intentos == 1
    assert correo.estado_envio == EstadoEnvio.ENVIADA.value
    assert correo.enviado_en is not None
    assert correo.destino == usuario_cliente.correo


def test_el_correo_sin_sesion_se_comporta_como_en_inc_1b():
    """El proveedor sin ligar sigue guardando en memoria y sin tocar la red."""
    proveedor = CorreoSimulado()
    proveedor.enviar("alguien@aqualav.pe", "Asunto", "Cuerpo")

    assert proveedor.ultimo is not None
    assert proveedor.ultimo.destino == "alguien@aqualav.pe"


def test_el_push_simulado_rechaza_un_token_que_no_existe(db):
    """Sección 4 del plan: «token inexistente -> fallo», de forma determinista."""
    proveedor = PushSimulado(sesion=db)

    try:
        proveedor.enviar("token-que-nadie-registro", "Título", "Cuerpo")
    except EnvioDePushFallido as error:
        assert "no está registrado" in str(error)
    else:  # pragma: no cover - el fallo es el comportamiento esperado
        raise AssertionError("un token no registrado tiene que fallar")


def test_el_push_simulado_entrega_a_un_dispositivo_registrado(db, usuario_cliente):
    db.add(
        Dispositivo(
            usuario_id=usuario_cliente.id,
            token_push=TOKEN_DEMO,
            plataforma="ios",
            activo=True,
        )
    )
    db.commit()

    proveedor = PushSimulado(sesion=db)
    proveedor.enviar(TOKEN_DEMO, "Título", "Cuerpo")

    assert proveedor.ultimo is not None
    assert proveedor.ultimo.token_dispositivo == TOKEN_DEMO


def test_el_notificador_en_app_ya_tiene_una_tabla_detras(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """Cierra el hueco del MVP: ``NotificadorEnApp`` era un stub sin tabla."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    bandeja = [
        fila
        for fila in _notificaciones(db, reserva["id"])
        if fila.canal == CanalNotificacion.EN_APP.value
    ]
    assert bandeja, "la notificación en la app tiene que quedar guardada"
    assert bandeja[0].estado_envio == EstadoEnvio.ENVIADA.value


# --------------------------------------------------------------------------
# La bandeja y los dispositivos por HTTP
# --------------------------------------------------------------------------
def test_la_bandeja_devuelve_las_notificaciones_propias(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-029: el resultado del envío se puede leer, que para eso se registra."""
    _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_cliente.get(f"{RUTA}/notificaciones")
    assert respuesta.status_code == 200, respuesta.text

    items = respuesta.json()["items"]
    assert items
    assert items[0]["evento"] == EventoNotificacion.CONFIRMACION.value
    assert items[0]["estado_envio"] == EstadoEnvio.ENVIADA.value
    assert "intentos" in items[0]


def test_la_bandeja_no_muestra_las_notificaciones_de_otro(
    api_cliente, api_operario, db, servicio_medio, vehiculo_id
):
    """Autorización horizontal: la bandeja es estrictamente propia."""
    _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_operario.get(f"{RUTA}/notificaciones")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["items"] == []


def test_la_bandeja_exige_token(cliente_http):
    assert cliente_http.get(f"{RUTA}/notificaciones").status_code == 401


def test_registrar_un_dispositivo_es_idempotente_por_token(api_cliente, db):
    """RF-029 precondición: reinstalar la app no duplica el dispositivo."""
    cuerpo = {"token_push": TOKEN_DEMO, "plataforma": "android"}

    primera = api_cliente.post(f"{RUTA}/notificaciones/dispositivos", json=cuerpo)
    segunda = api_cliente.post(f"{RUTA}/notificaciones/dispositivos", json=cuerpo)

    assert primera.status_code == 201, primera.text
    assert segunda.status_code == 201, segunda.text
    assert primera.json()["id"] == segunda.json()["id"]
    assert db.scalars(select(Dispositivo)).all().__len__() == 1


def test_dar_de_baja_un_dispositivo_lo_deja_fuera_del_push(
    api_cliente, db, servicio_medio, vehiculo_id
):
    creado = api_cliente.post(
        f"{RUTA}/notificaciones/dispositivos",
        json={"token_push": TOKEN_DEMO, "plataforma": "web"},
    ).json()

    baja = api_cliente.delete(f"{RUTA}/notificaciones/dispositivos/{creado['id']}")
    assert baja.status_code == 204, baja.text

    listado = api_cliente.get(f"{RUTA}/notificaciones/dispositivos").json()
    assert listado["items"] == []

    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    filas = _notificaciones(db, reserva["id"], EventoNotificacion.CONFIRMACION.value)
    assert CanalNotificacion.PUSH.value not in _canales(filas)


def test_no_se_puede_dar_de_baja_el_dispositivo_de_otro(api_cliente, api_operario):
    creado = api_cliente.post(
        f"{RUTA}/notificaciones/dispositivos",
        json={"token_push": TOKEN_DEMO, "plataforma": "web"},
    ).json()

    respuesta = api_operario.delete(f"{RUTA}/notificaciones/dispositivos/{creado['id']}")

    assert respuesta.status_code == 404
