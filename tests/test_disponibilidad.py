"""Availability of time blocks (RF-013)."""

from datetime import datetime, timedelta

from app.repositories import bahia as bahia_repo
from app.services import disponibilidad_service
from tests.conftest import (
    RUTA,
    crear_reserva,
    crear_vehiculo,
    instante,
    proximo_domingo,
    proximo_lunes,
)


def _bloques(api, fecha, servicio_id):
    respuesta = api.get(
        f"{RUTA}/disponibilidad", params={"fecha": fecha.isoformat(), "servicio_id": servicio_id}
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def _horas(cuerpo) -> set[tuple[int, int]]:
    return {
        (datetime.fromisoformat(b["inicio"]).hour, datetime.fromisoformat(b["inicio"]).minute)
        for b in cuerpo["bloques"]
    }


def test_los_bloques_respetan_el_horario_de_atencion(api_cliente, servicio_corto):
    """RN-07: Monday to Saturday, 08:00 to 19:00, in 15 minute steps."""
    cuerpo = _bloques(api_cliente, proximo_lunes(), servicio_corto.id)

    assert cuerpo["laborable"] is True
    assert cuerpo["duracion_min"] == 30
    horas = _horas(cuerpo)
    assert (8, 0) in horas, "abre a las 08:00"
    assert (18, 30) in horas, "el último bloque de 30 min termina a las 19:00"
    assert (18, 45) not in horas, "no cabe un bloque que termine después del cierre"
    assert all(b["bahias_libres"] == 4 for b in cuerpo["bloques"])


def test_no_se_ofrecen_bloques_con_menos_de_una_hora_de_anticipacion(api_cliente, servicio_corto):
    """RF-013 CA-02 / RN-02, comprobado sobre el endpoint para la fecha de hoy."""
    from app.core.horario import a_lima, ahora

    hoy = ahora().date()
    cuerpo = _bloques(api_cliente, hoy, servicio_corto.id)

    limite = a_lima(ahora()) + timedelta(minutes=60)
    for bloque in cuerpo["bloques"]:
        assert datetime.fromisoformat(bloque["inicio"]) >= limite


def test_una_consulta_a_las_nueve_y_media_no_ofrece_antes_de_las_diez_y_media(db, servicio_corto):
    """RF-013 CA-02, con el reloj fijado: consultando a las 09:30 el primer
    bloque posible es el de las 10:30."""
    fecha = proximo_lunes()
    ids = [bahia.id for bahia in bahia_repo.listar_activas(db)]

    bloques = disponibilidad_service.calcular_bloques(
        db, servicio_corto, fecha, ids, instante(fecha, 9, 30)
    )

    assert bloques, "debería quedar buena parte del día"
    assert bloques[0].inicio.hour == 10 and bloques[0].inicio.minute == 30


def test_un_domingo_a_las_cuatro_de_la_tarde_no_quedan_bloques(db, servicio_corto):
    """RF-013 CA-03: el domingo cierra a las 14:00."""
    fecha = proximo_domingo()
    ids = [bahia.id for bahia in bahia_repo.listar_activas(db)]

    bloques = disponibilidad_service.calcular_bloques(
        db, servicio_corto, fecha, ids, instante(fecha, 16, 0)
    )

    assert bloques == []


def test_el_domingo_nunca_ofrece_bloques_por_la_tarde(api_cliente, servicio_corto):
    """RN-07: el domingo la ventana es 09:00-14:00."""
    cuerpo = _bloques(api_cliente, proximo_domingo(), servicio_corto.id)

    for bloque in cuerpo["bloques"]:
        inicio = datetime.fromisoformat(bloque["inicio"])
        fin = datetime.fromisoformat(bloque["fin"])
        assert inicio.hour >= 9
        assert (fin.hour, fin.minute) <= (14, 0)


def test_un_bloque_con_todas_las_bahias_ocupadas_no_se_ofrece(
    api_cliente, servicio_corto, vehiculo_id
):
    """RF-013 CA-01: cuatro bahías ocupadas de 10:00 a 10:30 borran ese bloque
    y también los que se solapan con él."""
    fecha = proximo_lunes()
    vehiculos = [vehiculo_id] + [
        crear_vehiculo(api_cliente, placa) for placa in ("XYZ-789", "JKL-456", "MNO-321")
    ]

    for identificador in vehiculos:
        respuesta = crear_reserva(
            api_cliente, servicio_corto.id, identificador, instante(fecha, 10, 0)
        )
        assert respuesta.status_code == 201, respuesta.text

    horas = _horas(_bloques(api_cliente, fecha, servicio_corto.id))

    assert (10, 0) not in horas, "el bloque tomado desaparece"
    assert (9, 45) not in horas, "un bloque que se solapa tampoco se ofrece"
    assert (10, 15) not in horas
    assert (10, 30) in horas, "el bloque siguiente sigue libre"


def test_sin_cupos_se_sugiere_la_siguiente_fecha(api_cliente, servicio_corto):
    """RF-013 flow 3a: una fecha ya vencida no tiene bloques y el sistema
    propone la siguiente con cupo."""
    from app.core.horario import ahora

    ayer = ahora().date() - timedelta(days=1)

    cuerpo = _bloques(api_cliente, ayer, servicio_corto.id)

    assert cuerpo["bloques"] == []
    assert cuerpo["siguiente_fecha_disponible"] is not None


def test_disponibilidad_requiere_token(cliente_http, servicio_corto):
    respuesta = cliente_http.get(
        f"{RUTA}/disponibilidad",
        params={"fecha": proximo_lunes().isoformat(), "servicio_id": servicio_corto.id},
    )

    assert respuesta.status_code == 401


def test_estado_insertado_como_dato_ocupa_bahia(
    api_cliente, api_personal, db, servicio_corto, vehiculo_id
):
    """RN-03 / EXTENSION POINT P3.

    Un estado declarado como fila es un estado en el que el coche sigue dentro:
    su bahía no puede volver a ofrecerse. Antes de C3 la lista de estados
    activos estaba en código y un estado nuevo liberaba la bahía.
    """
    from app.models import TransicionEstado

    fecha = proximo_lunes()
    inicio = instante(fecha, 10, 0)

    def libres() -> int:
        cuerpo = _bloques(api_cliente, fecha, servicio_corto.id)
        return next(
            b["bahias_libres"]
            for b in cuerpo["bloques"]
            if datetime.fromisoformat(b["inicio"]) == inicio
        )

    reserva = crear_reserva(api_cliente, servicio_corto.id, vehiculo_id, inicio).json()
    api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
    )
    ocupadas_en_atencion = libres()
    assert ocupadas_en_atencion == 3, "una de las cuatro bahías está tomada"

    db.add(
        TransicionEstado(
            estado_origen="en_atencion",
            estado_destino="en_lavado",
            permiso_requerido="reserva:avanzar_estado",
            endpoint=None,
            marca_fin_servicio=False,
        )
    )
    db.add(
        TransicionEstado(
            estado_origen="en_lavado",
            estado_destino="finalizado",
            permiso_requerido="reserva:avanzar_estado",
            endpoint=None,
            marca_fin_servicio=True,
        )
    )
    db.commit()

    avance = api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/estado", json={"estado": "en_lavado"}
    )
    assert avance.status_code == 200, avance.text

    assert libres() == ocupadas_en_atencion, "el coche sigue dentro: la bahía no se libera"
