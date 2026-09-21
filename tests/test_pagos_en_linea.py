"""RF-025, RF-026 y el delta de RF-014: pago en línea por la pasarela simulada.

Cubre:

* **RF-014 `2a`** — una reserva con pago en línea nace en «Pendiente de pago» y
  caduca a los 15 minutos;
* **RF-025 `CA-01`** — con pago presencial la reserva se confirma y su estado
  de pago queda «Pendiente»;
* **RF-025 `CA-02` / `3a`** — pasarela caída: se ofrece continuar presencial;
* **RF-025 `4a`** — se puede cambiar de modalidad mientras el pago no esté
  confirmado;
* **RF-026 `CA-02`** — pago aprobado: el estado de pago de la reserva es
  «Confirmado», con su identificador externo (paso 4);
* **RF-026 `3a`** — pago rechazado: se informa el motivo y se reintenta con
  otro medio;
* **RF-026 `3b`** — respuesta no recibida por tiempo de espera: **se consulta
  el estado con la misma clave de idempotencia antes de reintentar**;
* **RNF-013 M3** — nunca se guarda el número de tarjeta.

Nada aquí toca la red ni el reloj real: la pasarela simulada decide por el
número de tarjeta de prueba y el barrido del planificador recibe el instante.
"""

from datetime import timedelta

from sqlalchemy import func, select

from app.config import settings
from app.core.horario import desde_bd
from app.models import (
    EstadoPago,
    EventoDominio,
    EventoNotificacion,
    ModalidadPago,
    Notificacion,
    Pago,
    Reserva,
    TransaccionPasarela,
)
from app.schemas import PagoEnLineaCrear
from app.services import pago_service, planificador
from app.services.proveedores.pasarela import PasarelaSimulada, tokenizar
from tests.conftest import (
    RUTA,
    TARJETA_APROBADA,
    TARJETA_PENDIENTE,
    TARJETA_RECHAZADA,
    TARJETA_SIN_RESPUESTA,
    codigo_error,
    crear_reserva,
    instante,
    pagar_en_linea,
    proximo_lunes,
)

#: "Lavado Completo" con el vehículo sedán de demostración: S/ 25,00.
MONTO_ESPERADO = 2500


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _reserva_en_linea(api_cliente, servicio, vehiculo_id, hora: int = 10) -> dict:
    respuesta = crear_reserva(
        api_cliente,
        servicio.id,
        vehiculo_id,
        instante(proximo_lunes(), hora, 0),
        modalidad_pago=ModalidadPago.EN_LINEA.value,
    )
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _eventos(db, accion: str) -> list[EventoDominio]:
    return list(db.scalars(select(EventoDominio).where(EventoDominio.accion == accion)).all())


# --------------------------------------------------------------------------
# RF-014 2a + RF-025 CA-01 - dónde nace cada reserva
# --------------------------------------------------------------------------
def test_la_reserva_en_linea_nace_pendiente_de_pago_y_caduca_a_los_15_minutos(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-014 `2a`: «se mantiene en Pendiente de pago durante 15 minutos»."""
    cuerpo = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)

    assert cuerpo["estado"] == "pendiente_pago"
    assert cuerpo["modalidad_pago"] == "en_linea"
    assert cuerpo["estado_pago"] == "pendiente"
    assert cuerpo["expira_en"] is not None

    fila = db.get(Reserva, cuerpo["id"])
    ventana = desde_bd(fila.expira_en) - desde_bd(fila.creada_en)
    assert ventana == timedelta(minutes=settings.pago_en_linea_ventana_minutos)
    assert ventana == timedelta(minutes=15), "el requisito dice quince literalmente"


def test_la_reserva_presencial_nace_confirmada_con_el_pago_pendiente(
    api_cliente, servicio_medio, vehiculo_id
):
    """RF-025 `CA-01`: «su estado de pago es Pendiente»."""
    respuesta = crear_reserva(
        api_cliente,
        servicio_medio.id,
        vehiculo_id,
        instante(proximo_lunes(), 11, 0),
        modalidad_pago=ModalidadPago.PRESENCIAL.value,
    )

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "confirmada"
    assert cuerpo["modalidad_pago"] == "presencial"
    assert cuerpo["estado_pago"] == "pendiente"
    assert cuerpo["expira_en"] is None, "una reserva confirmada no caduca"


def test_omitir_la_modalidad_sigue_creando_una_reserva_presencial(
    api_cliente, servicio_medio, vehiculo_id
):
    """RN-08: el cliente que no dice nada paga en el local, como en el MVP."""
    respuesta = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 12, 0)
    )

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["modalidad_pago"] == "presencial"
    assert respuesta.json()["estado"] == "confirmada"


# --------------------------------------------------------------------------
# RF-026 CA-02 - el cobro aprobado
# --------------------------------------------------------------------------
def test_el_cobro_aprobado_confirma_la_reserva_y_guarda_el_identificador_externo(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-026 `CA-02` y paso 4 del delta v1.0.

    La reserva pasa a «Confirmada» por la fila de ``transicion_estado`` cuyo
    ``endpoint`` es ``pago``: el destino no está escrito en el código (P3).
    """
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)

    respuesta = pagar_en_linea(api_cliente, reserva["id"], clave="aprobado-1")

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "confirmada"
    assert cuerpo["estado_pago"] == "confirmado"
    assert cuerpo["expira_en"] is None, "pagada ya no caduca"
    assert cuerpo["pago"]["monto"]["monto_centimos"] == MONTO_ESPERADO
    assert cuerpo["pago"]["medio"] == "tarjeta"
    assert cuerpo["pago"]["referencia_externa"], "RF-026 paso 4: identificador externo"
    assert cuerpo["pago"]["pasarela"] == settings.pasarela_nombre

    # La transición trae ``evento_notificacion='confirmacion'``, así que el
    # aviso sale solo: INC-5 lo dejó declarado y aquí no se despacha nada.
    confirmaciones = db.scalars(
        select(Notificacion).where(
            Notificacion.reserva_id == reserva["id"],
            Notificacion.evento == EventoNotificacion.CONFIRMACION.value,
        )
    ).all()
    assert confirmaciones, "confirmar por pasarela notifica por la tabla, no por código"

    historial = [fila["estado"] for fila in cuerpo["historial"]]
    assert historial == ["pendiente_pago", "confirmada"]


def test_la_reserva_en_linea_no_se_anuncia_como_confirmada_antes_de_pagar(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """Una reserva que aún no pagó no puede recibir el aviso de confirmación."""
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)

    filas = db.scalars(
        select(Notificacion).where(
            Notificacion.reserva_id == reserva["id"],
            Notificacion.evento == EventoNotificacion.CONFIRMACION.value,
        )
    ).all()
    assert filas == []


# --------------------------------------------------------------------------
# RF-026 3a - rechazo con motivo y reintento con otro medio
# --------------------------------------------------------------------------
def test_el_pago_rechazado_informa_el_motivo_y_permite_reintentar(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-026 `3a`: «informa el motivo y permite reintentar con otro medio»."""
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)

    rechazo = pagar_en_linea(
        api_cliente, reserva["id"], tarjeta=TARJETA_RECHAZADA, clave="rechazo-1"
    )

    assert rechazo.status_code == 422, rechazo.text
    assert codigo_error(rechazo) == "PAGO_RECHAZADO"
    motivos = [detalle["mensaje"] for detalle in rechazo.json()["error"]["detalles"]]
    assert any("fondos" in mensaje for mensaje in motivos), "el motivo llega al cliente"

    # El intento quedó registrado: el rechazo no es un agujero en el historial.
    fallido = db.scalars(select(Pago).where(Pago.reserva_id == reserva["id"])).all()
    assert [pago.estado for pago in fallido] == [EstadoPago.RECHAZADO.value]
    assert fallido[0].saldo_centimos == 0, "nada que devolver de un cobro rechazado"
    assert fallido[0].motivo_rechazo

    # La reserva sigue esperando, así que el reintento es posible.
    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert detalle["estado"] == "pendiente_pago"
    assert detalle["estado_pago"] == "rechazado"

    reintento = pagar_en_linea(
        api_cliente, reserva["id"], tarjeta=TARJETA_APROBADA, clave="rechazo-2"
    )
    assert reintento.status_code == 200, reintento.text
    assert reintento.json()["estado"] == "confirmada"
    assert reintento.json()["estado_pago"] == "confirmado"


def test_el_cobro_sin_liquidar_deja_la_reserva_esperando(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-026 postcondición: «confirmado, rechazado o pendiente», los tres."""
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)

    respuesta = pagar_en_linea(
        api_cliente, reserva["id"], tarjeta=TARJETA_PENDIENTE, clave="pendiente-1"
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "pendiente_pago"
    assert respuesta.json()["estado_pago"] == "pendiente"
    pago = db.scalars(select(Pago).where(Pago.reserva_id == reserva["id"])).one()
    assert pago.estado == EstadoPago.PENDIENTE.value


# --------------------------------------------------------------------------
# RF-026 3b - tiempo de espera agotado
# --------------------------------------------------------------------------
def test_tiempo_de_espera_agotado_consulta_con_la_misma_clave_antes_de_reintentar(
    api_cliente, db, servicio_medio, vehiculo_id, usuario_cliente
):
    """RF-026 `3b`, el flujo que hace demostrable este incremento.

    La tarjeta ``4999`` liquida el cobro en la pasarela y **pierde la
    respuesta**. El servicio no puede reintentar el cobro —sería cobrar dos
    veces—: consulta el estado con la MISMA clave de idempotencia. La prueba
    lo comprueba sobre la propia pasarela: un cobro, una consulta, una fila.
    """
    reserva_json = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)
    reserva = db.get(Reserva, reserva_json["id"])
    pasarela = PasarelaSimulada(sesion=db)

    pago, creado = pago_service.cobrar_en_linea(
        db,
        reserva,
        PagoEnLineaCrear(numero_tarjeta=TARJETA_SIN_RESPUESTA),
        usuario_cliente,
        usuario_cliente.rol.codigos_permisos,
        "sin-respuesta-1",
        pasarela=pasarela,
    )

    assert creado is True
    assert pasarela.cobros == ["sin-respuesta-1"], "se cobra UNA vez"
    assert pasarela.consultas == ["sin-respuesta-1"], "y se consulta con la MISMA clave"

    # Un solo cobro en la pasarela y un solo pago en la base.
    assert db.scalar(select(func.count(TransaccionPasarela.id))) == 1
    assert db.scalar(select(func.count(Pago.id)).where(Pago.reserva_id == reserva.id)) == 1

    # El cobro se había liquidado: la consulta lo descubre y la reserva avanza.
    assert pago.estado == EstadoPago.CONFIRMADO.value
    assert pago.referencia_externa
    db.refresh(reserva)
    assert reserva.estado == "confirmada"

    # Y queda constancia de que se consultó antes de dar nada por hecho (P7).
    consultas = _eventos(db, "pago.en_linea_consultado")
    assert len(consultas) == 1
    assert consultas[0].datos["idempotency_key"] == "sin-respuesta-1"


def test_la_transaccion_queda_ligada_al_pago_que_produjo(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-026 paso 4: la transacción y el pago son la misma operación."""
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)
    assert pagar_en_linea(api_cliente, reserva["id"], clave="ligada-1").status_code == 200

    transaccion = db.scalars(select(TransaccionPasarela)).one()
    pago = db.scalars(select(Pago).where(Pago.reserva_id == reserva["id"])).one()
    assert transaccion.pago_id == pago.id
    assert transaccion.idempotency_key == pago.idempotency_key == "ligada-1"
    assert transaccion.referencia_externa == pago.referencia_externa


# --------------------------------------------------------------------------
# RNF-017 M1 - idempotencia
# --------------------------------------------------------------------------
def test_repetir_la_clave_no_cobra_dos_veces(api_cliente, db, servicio_medio, vehiculo_id):
    """`RF-026 CA-01` aplicado al cobro en línea."""
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)

    primero = pagar_en_linea(api_cliente, reserva["id"], clave="repetida")
    segundo = pagar_en_linea(api_cliente, reserva["id"], clave="repetida")

    assert primero.status_code == 200, primero.text
    assert segundo.status_code == 200, segundo.text
    assert db.scalar(select(func.count(Pago.id)).where(Pago.reserva_id == reserva["id"])) == 1
    assert db.scalar(select(func.count(TransaccionPasarela.id))) == 1


def test_el_cobro_en_linea_exige_clave_de_idempotencia(api_cliente, servicio_medio, vehiculo_id):
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos/en-linea",
        json={"medio": "tarjeta", "numero_tarjeta": TARJETA_APROBADA},
    )

    assert respuesta.status_code == 400
    assert codigo_error(respuesta) == "IDEMPOTENCY_KEY_REQUERIDA"


# --------------------------------------------------------------------------
# RF-025 3a y CA-02 - la pasarela caída
# --------------------------------------------------------------------------
def test_la_pasarela_caida_ofrece_continuar_con_pago_presencial(
    api_cliente, db, servicio_medio, vehiculo_id, monkeypatch
):
    """RF-025 `CA-02` y `3a`: la alternativa presencial se ofrece, no se impone."""
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)
    monkeypatch.setattr(settings, "pasarela_disponible", False)

    respuesta = pagar_en_linea(api_cliente, reserva["id"], clave="caida-1")

    assert respuesta.status_code == 503, respuesta.text
    assert codigo_error(respuesta) == "PASARELA_NO_DISPONIBLE"
    assert "presencial" in respuesta.json()["error"]["mensaje"]
    assert any(
        "presencial" in detalle["mensaje"] for detalle in respuesta.json()["error"]["detalles"]
    )

    # La reserva sigue intacta: conserva su bloque y su plazo.
    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert detalle["estado"] == "pendiente_pago"
    assert detalle["expira_en"] is not None
    assert db.scalar(select(func.count(Pago.id)).where(Pago.reserva_id == reserva["id"])) == 0
    assert _eventos(db, "pago.pasarela_no_disponible")

    # Y la alternativa funciona de verdad.
    alternativa = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/modalidad-pago",
        json={"modalidad": "presencial"},
    )
    assert alternativa.status_code == 200, alternativa.text
    assert alternativa.json()["estado"] == "confirmada"


# --------------------------------------------------------------------------
# RF-025 4a - cambiar de modalidad
# --------------------------------------------------------------------------
def test_cambiar_a_presencial_confirma_la_reserva_y_quita_el_plazo(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-025 `4a` + `CA-01`."""
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/modalidad-pago",
        json={"modalidad": "presencial"},
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["modalidad_pago"] == "presencial"
    assert cuerpo["estado"] == "confirmada"
    assert cuerpo["estado_pago"] == "pendiente"
    assert cuerpo["expira_en"] is None
    assert db.get(Reserva, reserva["id"]).expira_en is None


def test_cambiar_a_en_linea_una_reserva_confirmada_no_la_devuelve_a_pendiente(
    api_cliente, servicio_medio, vehiculo_id
):
    """RF-025 `4a` al revés: el bloque ya está retenido, no hay nada que caducar.

    El Anexo A no declara una vuelta a «Pendiente de pago» y no se inventa una:
    haría que un cliente ya confirmado pudiera perder su bloque por un reloj.
    """
    respuesta = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 13, 0)
    )
    reserva = respuesta.json()

    cambio = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/modalidad-pago",
        json={"modalidad": "en_linea"},
    )

    assert cambio.status_code == 200, cambio.text
    assert cambio.json()["modalidad_pago"] == "en_linea"
    assert cambio.json()["estado"] == "confirmada"
    assert cambio.json()["expira_en"] is None

    # Y puede pagarse en línea desde ahí.
    pago = pagar_en_linea(api_cliente, reserva["id"], clave="desde-confirmada")
    assert pago.status_code == 200, pago.text
    assert pago.json()["estado_pago"] == "confirmado"
    assert pago.json()["estado"] == "confirmada"


def test_no_se_cambia_la_modalidad_con_el_pago_confirmado(api_cliente, servicio_medio, vehiculo_id):
    """RF-025 `4a` leído al pie de la letra: «mientras el pago no esté confirmado»."""
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)
    assert pagar_en_linea(api_cliente, reserva["id"], clave="fijada").status_code == 200

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/modalidad-pago",
        json={"modalidad": "presencial"},
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "MODALIDAD_NO_MODIFICABLE"


# --------------------------------------------------------------------------
# RF-014 2a - la caducidad la barre el planificador
# --------------------------------------------------------------------------
def test_el_planificador_caduca_la_reserva_que_no_se_pago_en_15_minutos(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-014 `2a`: pasado el plazo la reserva se cancela y libera su bloque.

    Es la misma función que ejecuta ``POST /interno/planificador``; el instante
    se le pasa, así que la prueba no espera quince minutos reales.
    """
    reserva_json = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)
    fila = db.get(Reserva, reserva_json["id"])
    vencido = desde_bd(fila.expira_en) + timedelta(seconds=1)

    dentro_del_plazo = desde_bd(fila.expira_en) - timedelta(minutes=1)
    antes = planificador.ejecutar_pendientes(db, momento=dentro_del_plazo)
    assert antes.expiradas == [], "dentro del plazo no se toca nada"

    resultado = planificador.ejecutar_pendientes(db, momento=vencido)

    assert resultado.expiradas == [fila.id]
    db.refresh(fila)
    assert fila.estado == "cancelada"
    assert "15 minutos" in fila.motivo_cancelacion
    assert fila.penalidad_centimos == 0, "RN-05: faltan horas para el servicio"
    assert _eventos(db, "reserva.pago_expirado")


def test_la_reserva_pagada_no_caduca(api_cliente, db, servicio_medio, vehiculo_id):
    """Pagar cierra el plazo: el barrido no puede cancelarla después."""
    reserva_json = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)
    assert pagar_en_linea(api_cliente, reserva_json["id"], clave="a-tiempo").status_code == 200

    resultado = planificador.ejecutar_pendientes(
        db, momento=desde_bd(db.get(Reserva, reserva_json["id"]).creada_en) + timedelta(hours=1)
    )

    assert resultado.expiradas == []
    assert db.get(Reserva, reserva_json["id"]).estado == "confirmada"


# --------------------------------------------------------------------------
# RNF-013 M3 - nunca el PAN
# --------------------------------------------------------------------------
def test_nunca_se_guarda_el_numero_de_tarjeta(api_cliente, db, servicio_medio, vehiculo_id):
    """RNF-013 M3: lo que se guarda es un token, jamás el PAN.

    Se revisa donde podría haberse colado: la fila del pago, el libro de la
    pasarela (petición y respuesta) y el registro de eventos de dominio.
    """
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)
    assert pagar_en_linea(api_cliente, reserva["id"], clave="sin-pan").status_code == 200

    pago = db.scalars(select(Pago).where(Pago.reserva_id == reserva["id"])).one()
    assert pago.token_tarjeta == tokenizar(TARJETA_APROBADA)
    assert pago.token_tarjeta.startswith("tok_4111_")
    assert TARJETA_APROBADA not in pago.token_tarjeta

    transaccion = db.scalars(select(TransaccionPasarela)).one()
    for documento in (transaccion.solicitud, transaccion.respuesta):
        assert TARJETA_APROBADA not in str(documento)

    for evento in db.scalars(select(EventoDominio)).all():
        assert TARJETA_APROBADA not in str(evento.datos)


def test_el_medio_de_caja_no_se_acepta_en_el_cobro_en_linea(
    api_cliente, servicio_medio, vehiculo_id
):
    """RF-025: «en línea = tarjeta o billetera digital»; el efectivo es de caja."""
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos/en-linea",
        json={"medio": "efectivo", "numero_tarjeta": TARJETA_APROBADA},
        headers={"Idempotency-Key": "medio-malo"},
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "VALIDACION"


def test_un_operario_no_puede_pagar_en_linea(
    api_cliente, api_operario, servicio_medio, vehiculo_id
):
    """P5: ``pago:en_linea`` es del cliente y del mostrador, no de la bahía."""
    reserva = _reserva_en_linea(api_cliente, servicio_medio, vehiculo_id)

    respuesta = pagar_en_linea(api_operario, reserva["id"], clave="del-operario")

    assert respuesta.status_code == 403


def test_el_cobro_de_caja_rechaza_un_medio_de_pasarela(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """La puerta de caja y la de la pasarela no se mezclan (RF-025, RF-026)."""
    from tests.conftest import llevar_hasta_finalizado

    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 14, 0)
    ).json()
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])

    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos",
        json={"medio": "tarjeta", "monto_centimos": MONTO_ESPERADO},
        headers={"Idempotency-Key": "caja-con-tarjeta-en-linea"},
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "VALIDACION"


def test_el_reloj_de_la_pasarela_simulada_es_el_numero_de_tarjeta(db):
    """Sección 4 del plan: determinista por número de tarjeta de prueba.

    Sin base de datos de por medio: la misma tarjeta siempre da el mismo
    resultado, y ``consultar`` con la misma clave siempre devuelve lo mismo.
    """
    pasarela = PasarelaSimulada(sesion=db, disponible=True)

    aprobada = pasarela.cobrar(
        idempotency_key="det-1",
        reserva_id=1,
        monto_centimos=100,
        moneda="PEN",
        token_tarjeta=tokenizar(TARJETA_APROBADA),
    )
    assert aprobada.aprobada is True
    assert pasarela.consultar("det-1") == aprobada
    assert pasarela.consultar("det-1") == aprobada, "la misma clave, el mismo resultado"
    assert pasarela.consultar("clave-que-nunca-existio") is None

    rechazada = pasarela.cobrar(
        idempotency_key="det-2",
        reserva_id=1,
        monto_centimos=100,
        moneda="PEN",
        token_tarjeta=tokenizar(TARJETA_RECHAZADA),
    )
    assert rechazada.rechazada is True
    assert rechazada.referencia_externa is None
    assert rechazada.motivo
