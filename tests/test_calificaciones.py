"""RF-031 y RN-10 - Calificación y reseña del servicio.

Cubre:

* `CA-01` un servicio entregado **hace 3 días se puede calificar**;
* `CA-02` un servicio entregado **hace 10 días ya no**, con el motivo;
* `RN-10` **una sola calificación por servicio**, y el flujo `3b`: la que ya
  existe **se muestra en modo lectura**, no se rechaza sin más;
* el **promedio por servicio y por operario**, que es el insumo de `RF-033`;
* el aviso «puedes calificar», que es una **plantilla y un evento**, no un
  despacho escrito a mano (entrega de INC-5).

La ventana se cuenta desde el evento `reserva.calificacion_habilitada` que
escribe el check-out, no desde `reserva.hora_entrega` ni desde el nombre del
estado: retroceder ese evento es exactamente «entregarlo hace N días», y es lo
que hace :func:`_entregado_hace`.
"""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.horario import ahora_utc
from app.models import (
    AsignacionServicio,
    Calificacion,
    CanalNotificacion,
    EventoNotificacion,
    Notificacion,
    PlantillaNotificacion,
    Servicio,
    Usuario,
)
from app.repositories import evento as evento_repo
from app.services import calificacion_service, eventos
from tests.conftest import (
    RUTA,
    codigo_error,
    crear_reserva,
    entregar,
    instante,
    llevar_hasta_finalizado,
    pagar_en_caja,
    proximo_lunes,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _reserva_entregada(api_cliente, api_recepcion, api_operario, db, servicio, vehiculo_id):
    """El recorrido completo de Anexo A hasta la entrega de RF-024."""
    reserva = crear_reserva(
        api_cliente, servicio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    assert pagar_en_caja(
        api_recepcion, reserva["id"], reserva["monto"]["monto_centimos"]
    ).status_code in (200, 201)
    respuesta = entregar(api_recepcion, reserva["id"])
    assert respuesta.status_code == 200, respuesta.text
    return reserva


def _entregado_hace(db, reserva_id: int, dias: int) -> None:
    """Retrocede la apertura de la ventana ``dias`` días.

    El evento ES el registro de cuándo se habilitó la calificación, así que
    moverlo hacia atrás es la forma honesta de simular una entrega antigua:
    no se toca ninguna columna ni se inventa un estado.
    """
    evento = evento_repo.obtener_ultimo(
        db, eventos.ENTIDAD_RESERVA, reserva_id, eventos.RESERVA_CALIFICACION_HABILITADA
    )
    assert evento is not None, "el check-out debe haber escrito el evento"
    momento = ahora_utc() - timedelta(days=dias)
    datos = dict(evento.datos)
    datos["habilitada_en"] = momento.isoformat()
    datos["vence_en"] = (momento + calificacion_service.VENTANA_CALIFICACION).isoformat()
    evento.datos = datos
    evento.ocurrido_en = momento
    db.commit()


def _calificar(api, reserva_id, puntuacion=5, comentario="Quedó impecable."):
    return api.post(
        f"{RUTA}/reservas/{reserva_id}/calificacion",
        json={"puntuacion": puntuacion, "comentario": comentario},
    )


# --------------------------------------------------------------------------
# CA-01 / CA-02 - la ventana de RN-10
# --------------------------------------------------------------------------
def test_un_servicio_entregado_hace_tres_dias_se_puede_calificar(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """`RF-031` `CA-01`: «entregado hace 3 días, la calificación se registra»."""
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    _entregado_hace(db, reserva["id"], 3)

    respuesta = _calificar(api_cliente, reserva["id"], 4, "Buen trabajo, algo lento.")

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["puntuacion"] == 4
    assert cuerpo["comentario"] == "Buen trabajo, algo lento."
    assert cuerpo["reserva_id"] == reserva["id"]


def test_un_servicio_entregado_hace_diez_dias_ya_no_se_puede_calificar(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """`RF-031` `CA-02` / `3a`: «el sistema lo rechaza» informando el plazo."""
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    _entregado_hace(db, reserva["id"], 10)

    respuesta = _calificar(api_cliente, reserva["id"])

    assert respuesta.status_code == 422, respuesta.text
    assert codigo_error(respuesta) == "PLAZO_DE_CALIFICACION_VENCIDO"
    campos = {d["campo"] for d in respuesta.json()["error"]["detalles"]}
    assert "vence_en" in campos, "el cliente merece saber cuándo venció"
    assert db.scalars(select(Calificacion)).all() == []


def test_la_ventana_dura_siete_dias_calendario(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """`RN-10`: siete días **calendario** desde la entrega."""
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )

    estado = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}/calificacion").json()

    assert estado["puede_calificar"] is True
    assert estado["motivo"] is None
    assert calificacion_service.VENTANA_CALIFICACION == timedelta(days=7)
    inicio = datetime.fromisoformat(estado["habilitada_en"])
    fin = datetime.fromisoformat(estado["vence_en"])
    assert fin - inicio == timedelta(days=7)


def test_un_servicio_sin_entregar_no_se_puede_calificar(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """`RN-10`: «solo servicios en estado Entregado»."""
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])

    respuesta = _calificar(api_cliente, reserva["id"])

    assert respuesta.status_code == 422, respuesta.text
    assert codigo_error(respuesta) == "CALIFICACION_NO_HABILITADA"

    estado = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}/calificacion").json()
    assert estado["puede_calificar"] is False
    assert estado["habilitada_en"] is None
    assert estado["motivo"]


# --------------------------------------------------------------------------
# 3b - una sola por servicio, y la existente se muestra en modo lectura
# --------------------------------------------------------------------------
def test_solo_se_admite_una_calificacion_por_servicio(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """`RN-10` + `3b`: la segunda devuelve la primera, no una segunda fila."""
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    primera = _calificar(api_cliente, reserva["id"], 5, "Impecable.")
    assert primera.status_code == 201, primera.text

    segunda = _calificar(api_cliente, reserva["id"], 1, "Me arrepentí.")

    # No se rechaza «sin más»: se devuelve la que ya existe, intacta.
    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["id"] == primera.json()["id"]
    assert segunda.json()["puntuacion"] == 5
    assert segunda.json()["comentario"] == "Impecable."
    assert len(db.scalars(select(Calificacion)).all()) == 1


def test_una_calificacion_existente_se_consulta_en_modo_lectura(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """`RF-031` `3b`: la pantalla la muestra, y sabe que no puede cambiarla."""
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    _calificar(api_cliente, reserva["id"], 3, "Correcto.")

    estado = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}/calificacion")

    assert estado.status_code == 200, estado.text
    cuerpo = estado.json()
    assert cuerpo["puede_calificar"] is False
    assert cuerpo["calificacion"]["puntuacion"] == 3
    assert cuerpo["motivo"] == calificacion_service.MOTIVO_YA_CALIFICADO


def test_la_calificacion_viaja_dentro_de_la_reserva(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """El historial de RF-017 enseña la calificación sin una segunda petición."""
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    _calificar(api_cliente, reserva["id"], 5)

    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()

    assert detalle["calificacion"]["puntuacion"] == 5


def test_una_puntuacion_fuera_de_rango_se_rechaza(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """`RF-031` entrada: «puntuación 1 a 5 estrellas»."""
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )

    assert _calificar(api_cliente, reserva["id"], 0).status_code == 422
    assert _calificar(api_cliente, reserva["id"], 6).status_code == 422


def test_solo_el_cliente_del_servicio_lo_califica(
    api_cliente, api_recepcion, api_operario, api_admin, db, servicio_medio, vehiculo_id
):
    """`RF-031` actor: el Cliente. Ni siquiera el administrador habla por él."""
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )

    respuesta = _calificar(api_admin, reserva["id"])

    assert respuesta.status_code == 403, respuesta.text
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"


# --------------------------------------------------------------------------
# El promedio por servicio y por operario (insumo de RF-033)
# --------------------------------------------------------------------------
def test_la_calificacion_actualiza_el_promedio_del_servicio_y_del_operario(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id, usuario_operario
):
    """`RF-031` salida: «promedio del operario y del servicio actualizados»."""
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    assert _calificar(api_cliente, reserva["id"], 4).status_code == 201

    db.expire_all()
    servicio = db.get(Servicio, servicio_medio.id)
    operario = db.get(Usuario, usuario_operario.id)

    assert servicio.calificaciones_count == 1
    assert servicio.calificacion_promedio == 4.0
    assert operario.calificaciones_count == 1
    assert operario.calificacion_promedio == 4.0

    catalogo = api_cliente.get(f"{RUTA}/servicios/{servicio_medio.id}").json()
    assert catalogo["calificacion_promedio"] == 4.0
    assert catalogo["calificaciones_count"] == 1


def test_el_promedio_es_exacto_con_varias_calificaciones(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """Suma y conteo, no un promedio redondeado guardado: 4 y 5 dan 4.5."""
    primera = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    assert _calificar(api_cliente, primera["id"], 4).status_code == 201

    segunda = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 14, 0)
    ).json()
    llevar_hasta_finalizado(api_recepcion, api_operario, db, segunda["id"])
    pagar_en_caja(api_recepcion, segunda["id"], segunda["monto"]["monto_centimos"], clave="caja-2")
    assert entregar(api_recepcion, segunda["id"]).status_code == 200
    assert _calificar(api_cliente, segunda["id"], 5).status_code == 201

    db.expire_all()
    servicio = db.get(Servicio, servicio_medio.id)

    assert servicio.calificaciones_count == 2
    assert servicio.calificaciones_suma == 9
    assert servicio.calificacion_promedio == 4.5


def test_un_servicio_sin_calificar_no_tiene_promedio(api_cliente, servicio_corto):
    """`None`, no cero: nadie lo calificó, no es que todos lo odiaran."""
    cuerpo = api_cliente.get(f"{RUTA}/servicios/{servicio_corto.id}").json()

    assert cuerpo["calificacion_promedio"] is None
    assert cuerpo["calificaciones_count"] == 0


# --------------------------------------------------------------------------
# El gancho de INC-1B: el evento es el único registro de la ventana
# --------------------------------------------------------------------------
def test_la_entrega_registra_cuando_se_abrio_la_ventana_y_quien_atendio(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id, usuario_operario
):
    """La asignación se borra con la entrega: el operario solo sobrevive aquí.

    ``bahia_service.liberar_recursos`` elimina ``asignacion_servicio`` cuando
    la reserva llega a un estado terminal, y la entrega es justo lo que la
    lleva ahí. Por eso el operario se fotografía dentro del evento antes del
    salto, y la calificación lo lee de vuelta desde ahí.
    """
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )

    asignaciones = db.scalars(
        select(AsignacionServicio).where(AsignacionServicio.reserva_id == reserva["id"])
    ).all()
    assert asignaciones == [], "la entrega libera la bahía y borra la asignación"

    evento = evento_repo.obtener_ultimo(
        db, eventos.ENTIDAD_RESERVA, reserva["id"], eventos.RESERVA_CALIFICACION_HABILITADA
    )
    assert evento is not None
    assert evento.datos["operario_id"] == usuario_operario.id
    assert evento.datos["dias"] == 7
    assert evento.datos["habilitada_en"]
    assert evento.datos["vence_en"]

    _calificar(api_cliente, reserva["id"], 5)
    fila = db.scalars(select(Calificacion)).first()
    assert fila.operario_id == usuario_operario.id


def test_calificar_deja_su_evento_de_dominio(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """P7, y sin copiar el comentario: el log audita, no archiva reseñas."""
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    _calificar(api_cliente, reserva["id"], 2, "Quedó mojado por dentro.")

    evento = evento_repo.obtener_ultimo(
        db, eventos.ENTIDAD_RESERVA, reserva["id"], eventos.RESERVA_CALIFICADA
    )

    assert evento is not None
    assert evento.datos["puntuacion"] == 2
    assert evento.datos["con_comentario"] is True
    assert "Quedó mojado por dentro." not in str(evento.datos)


# --------------------------------------------------------------------------
# El aviso «puedes calificar» es una plantilla, no un despacho a mano
# --------------------------------------------------------------------------
def test_la_entrega_avisa_al_cliente_que_puede_calificar(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """Entrega de INC-5: plantilla + evento nuevos, ninguna línea de despacho.

    El texto sale de ``plantilla_notificacion``, como el de cualquier otro
    aviso: cambiarlo, traducirlo o apagar un canal sigue siendo un UPDATE.
    """
    reserva = _reserva_entregada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )

    filas = db.scalars(
        select(Notificacion).where(
            Notificacion.reserva_id == reserva["id"],
            Notificacion.evento == EventoNotificacion.CALIFICACION.value,
        )
    ).all()

    assert filas, "la entrega invita a calificar (RF-024 paso 5)"
    canales = {fila.canal for fila in filas}
    assert CanalNotificacion.EN_APP.value in canales
    assert CanalNotificacion.CORREO.value in canales

    plantilla = db.scalars(
        select(PlantillaNotificacion).where(
            PlantillaNotificacion.evento == EventoNotificacion.CALIFICACION.value,
            PlantillaNotificacion.canal == CanalNotificacion.CORREO.value,
        )
    ).first()
    assert plantilla is not None, "el seed siembra la plantilla del evento nuevo"
    correo = next(fila for fila in filas if fila.canal == CanalNotificacion.CORREO.value)
    assert reserva["codigo"] in correo.asunto
    assert "7 días" in correo.cuerpo
