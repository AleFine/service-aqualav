"""Panel de indicadores y reportes exportables (RF-033, RF-034).

Las dos trampas de este par de requisitos están cubiertas aquí a propósito:

* **RF-033 `CA-01`** — los totales del tablero tienen que coincidir con los del
  reporte detallado. La prueba no compara «dos cifras parecidas»: compara cada
  tarjeta contra el total del reporte del que sale, y además contra los datos
  crudos del servicio que se atendió, para que no pueda pasar el caso de dos
  cálculos igualmente equivocados.
* **RF-034 `CA-01`** — el total del CSV tiene que coincidir con el de pantalla.
  La prueba descarga el archivo, lo parsea de verdad con el módulo ``csv`` y
  busca el total dentro de él.
"""

import csv
import io
from datetime import date, timedelta

import pytest

from app.config import settings
from app.models import EstadoExportacion
from app.services import planificador
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


def _periodo(dias: int = 30) -> tuple[str, str]:
    """Un rango que contiene con holgura a «hoy» en Lima."""
    hoy = date.today()
    return (hoy - timedelta(days=dias)).isoformat(), (hoy + timedelta(days=dias)).isoformat()


def _servicio_atendido_y_cobrado(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id, *, clave="caja-rep"
) -> dict:
    """Un servicio recorrido de punta a punta: atendido, cobrado y entregado.

    Es la materia prima de todos los indicadores: ``hora_fin_real`` estampada
    por la transición que marca el fin (P3) y un pago confirmado.
    """
    respuesta = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    )
    assert respuesta.status_code == 201, respuesta.text
    reserva = respuesta.json()

    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    assert pagar_en_caja(
        api_recepcion, reserva["id"], reserva["monto"]["monto_centimos"], clave=clave
    ).status_code in (200, 201)
    assert entregar(api_recepcion, reserva["id"]).status_code == 200
    return reserva


# --------------------------------------------------------------------------
# RF-033 - el tablero
# --------------------------------------------------------------------------
def test_el_tablero_de_un_periodo_sin_servicios_responde_ceros_sin_error(api_admin):
    """RF-033 `CA-02` y flujo `2a`: ceros con aviso, nunca un error."""
    desde, hasta = _periodo()

    respuesta = api_admin.get(f"{RUTA}/reportes/tablero?desde={desde}&hasta={hasta}")

    assert respuesta.status_code == 200, respuesta.text
    tablero = respuesta.json()
    assert tablero["servicios_atendidos"] == 0
    assert tablero["ingresos"]["monto_centimos"] == 0
    assert tablero["ticket_promedio"]["monto_centimos"] == 0
    assert tablero["ocupacion_porcentaje"] == 0.0
    assert tablero["tiempo_promedio_min"] == 0
    # El promedio de ``Calificable`` es None cuando nadie calificó; el TABLERO
    # muestra cero, que es lo que pide el requisito, y ``calificaciones`` dice
    # que el cero significa «nadie calificó» y no «todos odiaron el servicio».
    assert tablero["calificacion_media"] == 0.0
    assert tablero["calificaciones"] == 0
    assert tablero["sin_datos"] is True
    assert tablero["aviso"]


def test_los_totales_del_tablero_coinciden_con_los_del_reporte_detallado(
    api_admin, api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-033 `CA-01`: una sola cuenta, no dos que hoy coinciden."""
    reserva = _servicio_atendido_y_cobrado(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    desde, hasta = _periodo()

    tablero = api_admin.get(f"{RUTA}/reportes/tablero?desde={desde}&hasta={hasta}").json()
    servicios = api_admin.get(f"{RUTA}/reportes/servicios?desde={desde}&hasta={hasta}").json()
    ingresos = api_admin.get(f"{RUTA}/reportes/ingresos?desde={desde}&hasta={hasta}").json()
    ocupacion = api_admin.get(f"{RUTA}/reportes/ocupacion?desde={desde}&hasta={hasta}").json()
    productividad = api_admin.get(
        f"{RUTA}/reportes/productividad?desde={desde}&hasta={hasta}"
    ).json()

    assert tablero["servicios_atendidos"] == servicios["totales"]["servicios"]
    assert tablero["ingresos"]["monto_centimos"] == ingresos["totales"]["neto_centimos"]
    assert (
        tablero["ticket_promedio"]["monto_centimos"]
        == ingresos["totales"]["ticket_promedio_centimos"]
    )
    assert tablero["ocupacion_porcentaje"] == ocupacion["totales"]["ocupacion_porcentaje"]
    assert tablero["tiempo_promedio_min"] == servicios["totales"]["minutos_promedio"]
    assert tablero["operarios_activos"] == productividad["totales"]["operarios"]

    # Y contra el dato crudo, para que dos cálculos igual de equivocados no
    # puedan pasar la prueba de arriba.
    assert tablero["servicios_atendidos"] == 1
    assert tablero["ingresos"]["monto_centimos"] == reserva["monto"]["monto_centimos"]
    assert servicios["filas"][0]["codigo"] == reserva["codigo"]
    assert tablero["sin_datos"] is False


def test_el_tablero_publica_la_hora_de_corte_y_la_tendencia(
    api_admin, api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-033: «gráficos de tendencia» y flujo `3a` («con la hora de corte»)."""
    _servicio_atendido_y_cobrado(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    desde, hasta = _periodo()

    tablero = api_admin.get(f"{RUTA}/reportes/tablero?desde={desde}&hasta={hasta}").json()

    assert tablero["generado_en"]
    assert tablero["tendencia"], "un periodo con un servicio tiene al menos un punto"
    assert sum(punto["servicios"] for punto in tablero["tendencia"]) == 1


def test_el_tablero_exige_el_permiso_de_reportes(api_recepcion):
    """P5: quién ve los indicadores es un permiso, nunca un nombre de rol."""
    desde, hasta = _periodo()

    respuesta = api_recepcion.get(f"{RUTA}/reportes/tablero?desde={desde}&hasta={hasta}")

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"


# --------------------------------------------------------------------------
# RF-034 - los cuatro reportes
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tipo", ["servicios", "ingresos", "productividad", "ocupacion"])
def test_los_cuatro_reportes_de_rf034_existen_y_se_describen_a_si_mismos(api_admin, tipo):
    """RF-034: servicios, ingresos, productividad por operario y ocupación."""
    desde, hasta = _periodo()

    respuesta = api_admin.get(f"{RUTA}/reportes/{tipo}?desde={desde}&hasta={hasta}")

    assert respuesta.status_code == 200, respuesta.text
    reporte = respuesta.json()
    assert reporte["tipo"] == tipo
    assert reporte["columnas"], "el reporte describe su propia tabla"
    assert set(reporte["totales"])


def test_la_productividad_atribuye_el_servicio_entregado_a_su_operario(
    api_admin, api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-034: «productividad por operario».

    La entrega borra ``asignacion_servicio`` (``bahia_service.liberar_recursos``),
    así que el único sitio donde sobrevive quién trabajó el servicio es el
    evento ``reserva.calificacion_habilitada`` que INC-6 escribió. El reporte
    lo lee de ahí: es el hueco del §7 —«``evento_dominio`` se escribe y nunca
    se lee»— cerrado con una consulta que hace falta de verdad.
    """
    _servicio_atendido_y_cobrado(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    desde, hasta = _periodo()

    reporte = api_admin.get(f"{RUTA}/reportes/productividad?desde={desde}&hasta={hasta}").json()

    assert reporte["totales"]["operarios"] == 1
    fila = reporte["filas"][0]
    assert fila["operario_id"] is not None
    assert fila["operario"] != "Sin operario"
    assert fila["servicios"] == 1


def test_la_ocupacion_mide_contra_el_horario_de_atencion(
    api_admin, api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-034: «ocupación por bahía», con el denominador que da RF-018."""
    _servicio_atendido_y_cobrado(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    desde, hasta = _periodo()

    reporte = api_admin.get(f"{RUTA}/reportes/ocupacion?desde={desde}&hasta={hasta}").json()

    assert reporte["totales"]["minutos_disponibles"] > 0, "los días laborables cuentan"
    assert reporte["totales"]["minutos_ocupados"] == servicio_medio.duracion_min
    ocupada = next(fila for fila in reporte["filas"] if fila["minutos_ocupados"])
    assert 0 < ocupada["ocupacion_porcentaje"] <= 100


def test_un_tipo_de_reporte_inexistente_responde_422(api_admin):
    desde, hasta = _periodo()

    respuesta = api_admin.get(f"{RUTA}/reportes/inventado?desde={desde}&hasta={hasta}")

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "DATOS_INVALIDOS"


# --------------------------------------------------------------------------
# RF-034 CA-02 / 2a - el rango máximo de 12 meses
# --------------------------------------------------------------------------
def test_un_rango_de_18_meses_se_rechaza_con_un_mensaje_explicativo(api_admin):
    """RF-034 `CA-02` y flujo `2a`: se rechaza y se explica por qué."""
    hasta = date.today()
    desde = hasta - timedelta(days=548)  # ~18 meses

    respuesta = api_admin.get(
        f"{RUTA}/reportes/servicios?desde={desde.isoformat()}&hasta={hasta.isoformat()}"
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "RANGO_DEMASIADO_AMPLIO"
    error = respuesta.json()["error"]
    assert "12 meses" in error["mensaje"]
    assert error["detalles"], "el mensaje dice cuánto se pidió y cuánto se permite"


def test_un_rango_de_doce_meses_justos_se_acepta(api_admin):
    """El límite es «más de 12 meses»: un año exacto todavía entra."""
    hasta = date.today()
    desde = hasta - timedelta(days=364)

    respuesta = api_admin.get(
        f"{RUTA}/reportes/servicios?desde={desde.isoformat()}&hasta={hasta.isoformat()}"
    )

    assert respuesta.status_code == 200


def test_un_rango_invertido_se_rechaza(api_admin):
    hoy = date.today()

    respuesta = api_admin.get(
        f"{RUTA}/reportes/servicios"
        f"?desde={hoy.isoformat()}&hasta={(hoy - timedelta(days=5)).isoformat()}"
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "DATOS_INVALIDOS"


# --------------------------------------------------------------------------
# RF-034 CA-01 - el total del CSV coincide con el de pantalla
# --------------------------------------------------------------------------
def _descargar(api_admin, exportacion_id: int) -> bytes:
    respuesta = api_admin.get(f"{RUTA}/reportes/exportaciones/{exportacion_id}/archivo")
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.content


def _totales_del_csv(contenido: bytes) -> dict[str, str]:
    """El bloque «Totales» del CSV, parseado de verdad."""
    filas = list(csv.reader(io.StringIO(contenido.decode("utf-8-sig")), delimiter=","))
    inicio = next(indice for indice, fila in enumerate(filas) if fila[:1] == ["Totales"])
    return {fila[0]: fila[1] for fila in filas[inicio + 1 :] if len(fila) >= 2}


def test_el_total_del_csv_de_ingresos_coincide_con_el_de_pantalla(
    api_admin, api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-034 `CA-01`, con el archivo descargado y parseado."""
    _servicio_atendido_y_cobrado(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    desde, hasta = _periodo()

    pantalla = api_admin.get(f"{RUTA}/reportes/ingresos?desde={desde}&hasta={hasta}").json()
    respuesta = api_admin.post(
        f"{RUTA}/reportes/exportaciones",
        json={"tipo": "ingresos", "formato": "csv", "desde": desde, "hasta": hasta},
    )

    assert respuesta.status_code == 201, respuesta.text
    exportacion = respuesta.json()
    assert exportacion["estado"] == EstadoExportacion.GENERADO.value
    assert exportacion["archivo_url"]

    totales = _totales_del_csv(_descargar(api_admin, exportacion["id"]))
    neto = pantalla["totales"]["neto_centimos"]
    assert totales["Ingresos netos"] == f"{neto // 100}.{neto % 100:02d}"
    assert totales["Pagos registrados"] == str(pantalla["totales"]["pagos"])


def test_el_csv_lleva_una_fila_por_servicio_atendido(
    api_admin, api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """El detalle del archivo es el mismo que el de pantalla, no un resumen."""
    reserva = _servicio_atendido_y_cobrado(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    desde, hasta = _periodo()

    exportacion = api_admin.post(
        f"{RUTA}/reportes/exportaciones",
        json={"tipo": "servicios", "formato": "csv", "desde": desde, "hasta": hasta},
    ).json()
    contenido = _descargar(api_admin, exportacion["id"]).decode("utf-8-sig")

    assert reserva["codigo"] in contenido
    assert exportacion["filas"] == 1


def test_la_exportacion_a_pdf_produce_un_pdf_de_verdad(api_admin):
    """RF-034: «exportables a PDF y CSV».

    Reutiliza el generador escrito a mano de INC-4
    (``ProveedorDocumentos.reporte``): este incremento no escribe otro.
    """
    desde, hasta = _periodo()

    exportacion = api_admin.post(
        f"{RUTA}/reportes/exportaciones",
        json={"tipo": "servicios", "formato": "pdf", "desde": desde, "hasta": hasta},
    ).json()
    respuesta = api_admin.get(f"{RUTA}/reportes/exportaciones/{exportacion['id']}/archivo")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("application/pdf")
    assert respuesta.content.startswith(b"%PDF-1.4")
    assert b"%%EOF" in respuesta.content


def test_una_exportacion_con_rango_de_18_meses_se_rechaza(api_admin):
    """El límite de RF-034 `2a` vale igual al exportar que al consultar."""
    hasta = date.today()
    desde = hasta - timedelta(days=548)

    respuesta = api_admin.post(
        f"{RUTA}/reportes/exportaciones",
        json={
            "tipo": "servicios",
            "formato": "csv",
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
        },
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "RANGO_DEMASIADO_AMPLIO"


# --------------------------------------------------------------------------
# RF-034 4a - exportación asíncrona con notificación
# --------------------------------------------------------------------------
def test_un_volumen_alto_encola_la_exportacion_y_responde_202(
    api_admin,
    api_cliente,
    api_recepcion,
    api_operario,
    db,
    servicio_medio,
    vehiculo_id,
    monkeypatch,
):
    """RF-034 `4a`: «volumen elevado → exportación asíncrona con notificación».

    El umbral es configuración, así que la prueba lo baja en vez de fabricar
    quinientos servicios: lo que se verifica es la REGLA, no el número.
    """
    _servicio_atendido_y_cobrado(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    monkeypatch.setattr(settings, "reporte_umbral_filas", 0)
    desde, hasta = _periodo()

    respuesta = api_admin.post(
        f"{RUTA}/reportes/exportaciones",
        json={"tipo": "servicios", "formato": "csv", "desde": desde, "hasta": hasta},
    )

    assert respuesta.status_code == 202, respuesta.text
    exportacion = respuesta.json()
    assert exportacion["estado"] == EstadoExportacion.PENDIENTE.value
    assert exportacion["archivo_url"] is None, "todavía no hay nada que descargar"

    # Y el archivo tampoco: pedirlo ahora es un 409, no un 404.
    descarga = api_admin.get(f"{RUTA}/reportes/exportaciones/{exportacion['id']}/archivo")
    assert descarga.status_code == 409
    assert codigo_error(descarga) == "EXPORTACION_NO_DISPONIBLE"


def test_el_planificador_genera_la_exportacion_pendiente_y_notifica(
    api_admin,
    api_cliente,
    api_recepcion,
    api_operario,
    db,
    servicio_medio,
    vehiculo_id,
    usuario_admin,
    monkeypatch,
):
    """RF-034 `4a`, la otra mitad: el barrido la produce y avisa.

    El planificador sigue siendo la función pura que los tests llaman
    directamente (plan §4), así que «asíncrono» nunca significa «espera a un
    reloj de verdad».
    """
    _servicio_atendido_y_cobrado(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    monkeypatch.setattr(settings, "reporte_umbral_filas", 0)
    desde, hasta = _periodo()
    exportacion = api_admin.post(
        f"{RUTA}/reportes/exportaciones",
        json={"tipo": "servicios", "formato": "csv", "desde": desde, "hasta": hasta},
    ).json()

    resultado = planificador.ejecutar_pendientes(db)

    assert exportacion["id"] in resultado.exportaciones
    estado = api_admin.get(f"{RUTA}/reportes/exportaciones/{exportacion['id']}").json()
    assert estado["estado"] == EstadoExportacion.GENERADO.value
    assert estado["archivo_url"]
    assert _descargar(api_admin, exportacion["id"])

    # La notificación es plantilla + evento, no un despacho a mano (INC-5).
    bandeja = api_admin.get(f"{RUTA}/notificaciones").json()["items"]
    avisos = [fila for fila in bandeja if fila["evento"] == "exportacion"]
    assert avisos, "el solicitante recibe el aviso de que su reporte está listo"
    assert "servicios" in avisos[0]["cuerpo"]


def test_el_barrido_del_planificador_informa_las_exportaciones_generadas(api_admin):
    """El endpoint interno publica lo que hizo el barrido (RF-030 + RF-034)."""
    respuesta = api_admin.post(f"{RUTA}/interno/planificador")

    assert respuesta.status_code == 200
    assert respuesta.json()["exportaciones_generadas"] == []
