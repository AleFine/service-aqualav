"""Tarifas, catálogo por tipo de vehículo, paquetes y promociones.

RF-009 (delta), RF-010 (delta), RF-011, RF-012, RN-04 y RN-12.

El corazón es `RF-012`: `tarifa = (precio base x factor) + adicionales -
descuentos`, desglosada, trazable y **congelada** en la reserva. Los flujos
alternos (`3a` cupón rechazado, `4a` total limitado a cero, `RF-011 2a`
solapamiento y `RF-011 4a` promoción vencida) son requisito, no adorno, y cada
uno tiene aquí su prueba.
"""

from datetime import date, timedelta

from sqlalchemy import select

from app.models import EventoDominio, FactorTipoVehiculo, Promocion, Servicio
from app.services import tarifa_service
from tests.conftest import (
    RUTA,
    codigo_error,
    crear_reserva,
    crear_vehiculo,
    instante,
    proximo_lunes,
)

#: RF-012 CA-01 es literal: S/ 30,00 con factor SUV 1,3 => S/ 39,00.
SERVICIO_CA01 = {
    "nombre": "Lavado de prueba CA-01",
    "descripcion": "Servicio de S/ 30,00 para verificar el cálculo de la tarifa.",
    "categoria": "basico",
    "duracion_min": 30,
    "monto_centimos": 3000,
    "moneda": "PEN",
}

FACTOR_SUV = 1300
FACTOR_SEDAN = 1000


def _crear_servicio(api_admin, **cambios) -> dict:
    respuesta = api_admin.post(f"{RUTA}/admin/servicios", json=dict(SERVICIO_CA01, **cambios))
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _cotizar(api, **cuerpo):
    return api.post(f"{RUTA}/tarifas/calculo", json=cuerpo)


def _promocion(api_admin, **cuerpo):
    return api_admin.post(f"{RUTA}/admin/promociones", json=cuerpo)


def _eventos(db, accion: str) -> list[EventoDominio]:
    return list(db.scalars(select(EventoDominio).where(EventoDominio.accion == accion)).all())


def _hoy() -> date:
    from app.core.horario import ahora

    return ahora().date()


# --------------------------------------------------------------------------
# RF-012 - el cálculo
# --------------------------------------------------------------------------
def test_ca01_un_servicio_de_treinta_soles_con_factor_suv_cuesta_treinta_y_nueve(
    api_admin, api_cliente
):
    """RF-012 CA-01, al pie de la letra: 3000 x 1,3 = 3900 céntimos."""
    servicio = _crear_servicio(api_admin)

    respuesta = _cotizar(api_cliente, servicio_id=servicio["id"], tipo_vehiculo="suv")

    assert respuesta.status_code == 200, respuesta.text
    desglose = respuesta.json()
    assert desglose["precio_base"]["monto_centimos"] == 3000
    assert desglose["factor_milesimas"] == FACTOR_SUV
    assert desglose["base_ajustada"]["monto_centimos"] == 3900
    assert desglose["total"] == {"monto_centimos": 3900, "moneda": "PEN"}


def test_el_redondeo_del_factor_es_al_centimo_mas_cercano():
    """RF-012 / P6: el importe es entero; la mitad redondea hacia arriba."""
    # 1500 x 1,333 = 1999,5 -> 2000; 1500 x 1,332 = 1998 exacto.
    assert tarifa_service.aplicar_factor(1500, 1333) == 2000
    assert tarifa_service.aplicar_factor(1500, 1332) == 1998
    assert tarifa_service.aplicar_factor(3000, FACTOR_SUV) == 3900, "CA-01 sin pasar por HTTP"


def test_rn04_la_formula_suma_adicionales_y_resta_descuentos(api_admin, api_cliente, db):
    """RN-04 completa: (base x factor) + adicionales - descuento."""
    servicio = _crear_servicio(api_admin)
    adicional = api_admin.post(
        f"{RUTA}/admin/adicionales",
        json={"nombre": "Encerado exprés", "monto_centimos": 1000},
    ).json()
    _promocion(
        api_admin,
        nombre="Descuento de prueba",
        tipo_descuento="porcentaje",
        valor=10,
        servicio_id=servicio["id"],
        vigente_desde=_hoy().isoformat(),
    )

    respuesta = _cotizar(
        api_cliente,
        servicio_id=servicio["id"],
        tipo_vehiculo="suv",
        adicionales=[adicional["id"]],
    )

    desglose = respuesta.json()
    # 3900 + 1000 = 4900; 10 % = 490; total 4410.
    assert desglose["base_ajustada"]["monto_centimos"] == 3900
    assert desglose["adicionales_total"]["monto_centimos"] == 1000
    assert desglose["descuento"]["monto_centimos"] == 490
    assert desglose["total"]["monto_centimos"] == 4410
    assert [item["nombre"] for item in desglose["adicionales"]] == ["Encerado exprés"]


def test_rn12_todos_los_importes_viajan_en_soles(api_admin, api_cliente):
    """RN-12: PEN explícito en cada importe del desglose."""
    servicio = _crear_servicio(api_admin)

    desglose = _cotizar(api_cliente, servicio_id=servicio["id"], tipo_vehiculo="sedan").json()

    for campo in ("precio_base", "base_ajustada", "adicionales_total", "descuento", "total"):
        assert desglose[campo]["moneda"] == "PEN", campo


def test_la_tarifa_y_su_desglose_quedan_congelados_en_la_reserva(
    api_admin, api_cliente, servicio_medio, vehiculo_id, db
):
    """RF-012 «congelada en la reserva» + RF-014 CA-03, ahora con desglose."""
    respuesta = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    )
    assert respuesta.status_code == 201, respuesta.text
    reserva = respuesta.json()
    assert reserva["tarifa"]["total"]["monto_centimos"] == reserva["monto"]["monto_centimos"]
    assert reserva["tarifa"]["tipo_vehiculo"] == "sedan"
    assert reserva["tarifa"]["factor_milesimas"] == FACTOR_SEDAN

    # El administrador sube el precio y el factor del sedán después de reservar.
    api_admin.patch(f"{RUTA}/admin/servicios/{servicio_medio.id}", json={"monto_centimos": 9900})
    api_admin.put(
        f"{RUTA}/admin/factores",
        json={"servicio_id": servicio_medio.id, "tipo_vehiculo": "sedan", "factor_milesimas": 2000},
    )

    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert detalle["monto"]["monto_centimos"] == reserva["monto"]["monto_centimos"]
    assert detalle["tarifa"]["precio_base"]["monto_centimos"] == 2500
    assert detalle["tarifa"]["factor_milesimas"] == FACTOR_SEDAN


def test_el_calculo_queda_en_la_bitacora_de_eventos(api_cliente, servicio_medio, vehiculo_id, db):
    """RF-012 «trazable y auditable»: el desglose se escribe como evento (P7)."""
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()

    calculos = _eventos(db, "tarifa.calculada")
    assert len(calculos) == 1
    assert calculos[0].entidad_id == reserva["id"]
    assert calculos[0].datos["total_centimos"] == reserva["monto"]["monto_centimos"]
    assert calculos[0].datos["factor_milesimas"] == FACTOR_SEDAN


def test_la_atencion_sin_reserva_tambien_congela_su_desglose(
    api_recepcion, servicio_corto, vehiculo_id, monkeypatch
):
    """RF-019 `1a` + RF-012: el cliente de paso también recibe su desglose.

    El reloj se fija a un lunes a las 10:00 para que la prueba no dependa de
    la hora real a la que se ejecute la suite, igual que en `test_operacion`.
    """
    from app.services import reserva_service

    monkeypatch.setattr(reserva_service, "ahora", lambda: instante(proximo_lunes(), 10, 0))

    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/atencion-inmediata",
        json={"servicio_id": servicio_corto.id, "vehiculo_id": vehiculo_id},
    )

    assert respuesta.status_code == 201, respuesta.text
    tarifa = respuesta.json()["tarifa"]
    assert tarifa is not None
    assert tarifa["tipo_vehiculo"] == "sedan"
    assert tarifa["total"]["monto_centimos"] == respuesta.json()["monto"]["monto_centimos"]


def test_el_suv_paga_el_factor_al_reservar(api_cliente, servicio_medio):
    """RN-04 en la reserva: el tipo del vehículo decide el importe congelado."""
    suv = crear_vehiculo(api_cliente, "SUV-001")

    reserva = crear_reserva(
        api_cliente, servicio_medio.id, suv, instante(proximo_lunes(), 10, 0)
    ).json()

    assert reserva["tarifa"]["factor_milesimas"] == FACTOR_SUV
    # 2500 x 1,3 = 3250.
    assert reserva["monto"]["monto_centimos"] == 3250


# --------------------------------------------------------------------------
# RF-012 flujo 3a y CA-02 - el cupón rechazado
# --------------------------------------------------------------------------
def test_ca02_un_cupon_vencido_no_cambia_el_total_e_informa_el_rechazo(api_admin, api_cliente, db):
    """RF-012 CA-02 y flujo 3a: se indica el motivo y se recalcula sin él."""
    servicio = _crear_servicio(api_admin)
    ayer = _hoy() - timedelta(days=1)
    _promocion(
        api_admin,
        nombre="Cupón caducado",
        tipo_descuento="porcentaje",
        valor=50,
        servicio_id=servicio["id"],
        codigo_cupon="CADUCADO",
        vigente_desde=(ayer - timedelta(days=30)).isoformat(),
        vigente_hasta=ayer.isoformat(),
    )

    sin_cupon = _cotizar(api_cliente, servicio_id=servicio["id"], tipo_vehiculo="suv").json()
    con_cupon = _cotizar(
        api_cliente, servicio_id=servicio["id"], tipo_vehiculo="suv", cupon="CADUCADO"
    )

    assert con_cupon.status_code == 200, "el cupón vencido no tumba la operación"
    desglose = con_cupon.json()
    assert desglose["total"] == sin_cupon["total"], "el total no varía (CA-02)"
    assert desglose["descuento"]["monto_centimos"] == 0
    assert desglose["cupon_rechazado"] == "CADUCADO"
    assert "venció" in desglose["motivo_rechazo_cupon"]
    assert _eventos(db, "tarifa.cupon_rechazado"), "el rechazo queda registrado"


def test_un_cupon_inexistente_se_rechaza_con_su_motivo(api_admin, api_cliente):
    """RF-012 flujo 3a: «no existe» es un motivo distinto de «venció»."""
    servicio = _crear_servicio(api_admin)

    desglose = _cotizar(
        api_cliente, servicio_id=servicio["id"], tipo_vehiculo="sedan", cupon="INVENTADO"
    ).json()

    assert desglose["cupon_rechazado"] == "INVENTADO"
    assert "no existe" in desglose["motivo_rechazo_cupon"]
    assert desglose["total"]["monto_centimos"] == 3000


def test_reservar_con_un_cupon_invalido_no_falla_y_deja_constancia(
    api_admin, api_cliente, servicio_medio, vehiculo_id, db
):
    """RF-012 flujo 3a en la reserva: se crea igual, con el motivo guardado."""
    respuesta = api_cliente.post(
        f"{RUTA}/reservas",
        json={
            "servicio_id": servicio_medio.id,
            "vehiculo_id": vehiculo_id,
            "inicio": instante(proximo_lunes(), 10, 0).isoformat(),
            "cupon": "NO-EXISTE",
        },
    )

    assert respuesta.status_code == 201, respuesta.text
    tarifa = respuesta.json()["tarifa"]
    assert tarifa["cupon_rechazado"] == "NO-EXISTE"
    assert tarifa["motivo_rechazo_cupon"]
    assert tarifa["total"]["monto_centimos"] == 2500, "se recalcula sin el descuento"
    assert _eventos(db, "tarifa.cupon_rechazado")


def test_un_cupon_vigente_si_descuenta(api_admin, api_cliente):
    """El cupón de bienvenida del seed aplica cuando el cliente lo escribe."""
    servicio = _crear_servicio(api_admin)

    desglose = _cotizar(
        api_cliente, servicio_id=servicio["id"], tipo_vehiculo="sedan", cupon="bienvenida10"
    ).json()

    assert desglose["cupon_aplicado"] == "BIENVENIDA10"
    assert desglose["descuento"]["monto_centimos"] == 300
    assert desglose["total"]["monto_centimos"] == 2700


# --------------------------------------------------------------------------
# RF-012 flujo 4a - el total negativo
# --------------------------------------------------------------------------
def test_4a_un_descuento_mayor_que_el_total_lo_limita_a_cero_y_lo_registra(
    api_admin, api_cliente, db
):
    """RF-012 flujo 4a: nunca se factura un negativo, y queda la incidencia."""
    servicio = _crear_servicio(api_admin)
    _promocion(
        api_admin,
        nombre="Cupón desproporcionado",
        tipo_descuento="monto",
        valor=99_000,
        servicio_id=servicio["id"],
        codigo_cupon="TODOGRATIS",
        vigente_desde=_hoy().isoformat(),
    )

    desglose = _cotizar(
        api_cliente, servicio_id=servicio["id"], tipo_vehiculo="sedan", cupon="TODOGRATIS"
    ).json()

    assert desglose["total"]["monto_centimos"] == 0
    assert desglose["descuento"]["monto_centimos"] == 3000, "el descuento se recorta al total"
    assert desglose["incidencia"], "la incidencia se informa"
    registrados = _eventos(db, "tarifa.total_limitado_a_cero")
    assert registrados, "la incidencia se registra (flujo 4a)"
    assert registrados[0].datos["total_centimos"] == 0


# --------------------------------------------------------------------------
# RF-011 - promociones
# --------------------------------------------------------------------------
def test_ca01_una_promocion_vigente_descuenta_al_reservar(
    api_admin, api_cliente, servicio_medio, vehiculo_id
):
    """RF-011 CA-01: la promoción se aplica sola al calcular la tarifa."""
    _promocion(
        api_admin,
        nombre="Lunes de lavado",
        tipo_descuento="porcentaje",
        valor=20,
        servicio_id=servicio_medio.id,
        vigente_desde=_hoy().isoformat(),
    )

    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()

    assert reserva["tarifa"]["promocion"] == "Lunes de lavado"
    assert reserva["tarifa"]["descuento"]["monto_centimos"] == 500
    assert reserva["monto"]["monto_centimos"] == 2000


def test_ca02_una_promocion_vencida_deja_el_precio_regular(api_cliente, api_admin, db, vehiculo_id):
    """RF-011 CA-02 y flujo 4a: caduca sola, sin que nadie la desactive."""
    detallado = db.scalars(select(Servicio).where(Servicio.nombre == "Detallado Interior")).first()
    vencida = db.scalars(select(Promocion).where(Promocion.nombre == "Aniversario AquaLav")).first()
    assert vencida is not None and vencida.activa, "el seed la deja activa pero caducada"
    assert vencida.servicio_id == detallado.id

    desglose = _cotizar(api_cliente, servicio_id=detallado.id, tipo_vehiculo="sedan").json()

    assert desglose["promocion"] is None
    assert desglose["descuento"]["monto_centimos"] == 0
    assert desglose["total"]["monto_centimos"] == 7000, "precio regular"


def test_4a_una_promocion_deja_de_aplicarse_cuando_se_le_acorta_la_vigencia(
    api_admin, api_cliente, servicio_medio
):
    """RF-011 flujo 4a: al vencer, el catálogo vuelve al precio regular."""
    promocion = _promocion(
        api_admin,
        nombre="Promo efímera",
        tipo_descuento="porcentaje",
        valor=20,
        servicio_id=servicio_medio.id,
        vigente_desde=(_hoy() - timedelta(days=10)).isoformat(),
    ).json()

    con_promocion = _cotizar(
        api_cliente, servicio_id=servicio_medio.id, tipo_vehiculo="sedan"
    ).json()
    assert con_promocion["total"]["monto_centimos"] == 2000

    api_admin.patch(
        f"{RUTA}/admin/promociones/{promocion['id']}",
        json={"vigente_hasta": (_hoy() - timedelta(days=1)).isoformat()},
    )

    sin_promocion = _cotizar(
        api_cliente, servicio_id=servicio_medio.id, tipo_vehiculo="sedan"
    ).json()
    assert sin_promocion["promocion"] is None
    assert sin_promocion["total"]["monto_centimos"] == 2500


def test_2a_una_promocion_que_solapa_fechas_se_rechaza_pidiendo_ajustar_el_rango(
    api_admin, servicio_medio
):
    """RF-011 flujo 2a: se advierte y se pide ajustar el rango."""
    base = _hoy()
    primera = _promocion(
        api_admin,
        nombre="Promo de enero",
        tipo_descuento="porcentaje",
        valor=10,
        servicio_id=servicio_medio.id,
        vigente_desde=base.isoformat(),
        vigente_hasta=(base + timedelta(days=30)).isoformat(),
    )
    assert primera.status_code == 201, primera.text

    segunda = _promocion(
        api_admin,
        nombre="Promo de febrero",
        tipo_descuento="porcentaje",
        valor=15,
        servicio_id=servicio_medio.id,
        vigente_desde=(base + timedelta(days=20)).isoformat(),
        vigente_hasta=(base + timedelta(days=50)).isoformat(),
    )

    assert segunda.status_code == 409, segunda.text
    assert codigo_error(segunda) == "PROMOCION_SOLAPADA"
    cuerpo = segunda.json()["error"]
    assert "ajusta el rango" in cuerpo["mensaje"].lower()
    assert any("Promo de enero" in det["mensaje"] for det in cuerpo["detalles"])


def test_2a_no_hay_solapamiento_si_los_rangos_no_se_tocan(api_admin, servicio_medio):
    """El rango ajustado entra: la advertencia era por las fechas, no por el servicio."""
    base = _hoy()
    _promocion(
        api_admin,
        nombre="Promo de enero",
        tipo_descuento="porcentaje",
        valor=10,
        servicio_id=servicio_medio.id,
        vigente_desde=base.isoformat(),
        vigente_hasta=(base + timedelta(days=30)).isoformat(),
    )

    segunda = _promocion(
        api_admin,
        nombre="Promo de marzo",
        tipo_descuento="porcentaje",
        valor=15,
        servicio_id=servicio_medio.id,
        vigente_desde=(base + timedelta(days=31)).isoformat(),
        vigente_hasta=(base + timedelta(days=60)).isoformat(),
    )

    assert segunda.status_code == 201, segunda.text


def test_dos_cupones_sobre_el_mismo_servicio_conviven(api_admin, servicio_medio):
    """Un cupón lo elige el cliente escribiéndolo: no hay ambigüedad que evitar."""
    base = _hoy()
    for nombre, codigo in (("Cupón A", "CUPONA"), ("Cupón B", "CUPONB")):
        respuesta = _promocion(
            api_admin,
            nombre=nombre,
            tipo_descuento="porcentaje",
            valor=10,
            servicio_id=servicio_medio.id,
            codigo_cupon=codigo,
            vigente_desde=base.isoformat(),
            vigente_hasta=(base + timedelta(days=30)).isoformat(),
        )
        assert respuesta.status_code == 201, respuesta.text


def test_una_promocion_por_dias_de_la_semana_solo_aplica_esos_dias(
    api_admin, api_cliente, servicio_medio
):
    """RF-011: «vigencia por fechas, días de la semana o cupón»."""
    lunes = proximo_lunes()
    _promocion(
        api_admin,
        nombre="Solo los martes",
        tipo_descuento="porcentaje",
        valor=50,
        servicio_id=servicio_medio.id,
        dias_semana=[1],
        vigente_desde=(_hoy() - timedelta(days=1)).isoformat(),
    )

    en_lunes = _cotizar(
        api_cliente,
        servicio_id=servicio_medio.id,
        tipo_vehiculo="sedan",
        fecha=lunes.isoformat(),
    ).json()
    en_martes = _cotizar(
        api_cliente,
        servicio_id=servicio_medio.id,
        tipo_vehiculo="sedan",
        fecha=(lunes + timedelta(days=1)).isoformat(),
    ).json()

    assert en_lunes["descuento"]["monto_centimos"] == 0
    assert en_martes["descuento"]["monto_centimos"] == 1250


def test_el_catalogo_publico_solo_muestra_promociones_vigentes(api_cliente):
    """RF-011: la promoción vencida del seed no se publica."""
    items = api_cliente.get(f"{RUTA}/promociones").json()["items"]

    nombres = {item["nombre"] for item in items}
    assert "Verano Premium" in nombres
    assert "Aniversario AquaLav" not in nombres


# --------------------------------------------------------------------------
# RF-011 - paquetes
# --------------------------------------------------------------------------
def test_el_paquete_del_seed_se_publica_con_su_precio_preferencial(api_cliente):
    """RF-011: «agrupa varios servicios con precio preferencial»."""
    items = api_cliente.get(f"{RUTA}/paquetes").json()["items"]

    paquete = next(item for item in items if item["nombre"] == "Pack Brillo Total")
    assert paquete["precio"]["monto_centimos"] == 6000
    assert paquete["precio_regular"]["monto_centimos"] == 7000, "2500 + 4500 por separado"
    assert {linea["nombre"] for linea in paquete["servicios"]} == {
        "Lavado Completo",
        "Lavado + Encerado",
    }


def test_una_promocion_sobre_un_paquete_destaca_su_precio(api_admin, api_cliente, db):
    """RF-011: la promoción también puede colgar de un paquete."""
    from app.models import Paquete

    paquete = db.scalars(select(Paquete).where(Paquete.nombre == "Pack Brillo Total")).first()
    _promocion(
        api_admin,
        nombre="Pack con descuento",
        tipo_descuento="porcentaje",
        valor=10,
        paquete_id=paquete.id,
        vigente_desde=_hoy().isoformat(),
    )

    items = api_cliente.get(f"{RUTA}/paquetes").json()["items"]

    publicado = next(item for item in items if item["id"] == paquete.id)
    assert publicado["promocion"]["nombre"] == "Pack con descuento"
    assert publicado["precio_promocional"]["monto_centimos"] == 5400


def test_una_promocion_no_puede_apuntar_a_un_servicio_y_a_un_paquete(api_admin, db, servicio_medio):
    """Una promoción tiene un destino: o el servicio o el paquete."""
    from app.models import Paquete

    paquete = db.scalars(select(Paquete).where(Paquete.nombre == "Pack Brillo Total")).first()

    respuesta = _promocion(
        api_admin,
        nombre="Promo ambigua",
        tipo_descuento="porcentaje",
        valor=10,
        servicio_id=servicio_medio.id,
        paquete_id=paquete.id,
        vigente_desde=_hoy().isoformat(),
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "DATOS_INVALIDOS"


def test_el_administrador_publica_un_paquete(
    api_admin, api_cliente, servicio_corto, servicio_medio
):
    """RF-011: alta de paquete desde la administración."""
    respuesta = api_admin.post(
        f"{RUTA}/admin/paquetes",
        json={
            "nombre": "Pack Rápido",
            "descripcion": "Dos lavados cortos a precio de uno y medio.",
            "precio_centimos": 3000,
            "vigente_desde": _hoy().isoformat(),
            "servicios": [
                {"servicio_id": servicio_corto.id, "cantidad": 1},
                {"servicio_id": servicio_medio.id, "cantidad": 1},
            ],
        },
    )

    assert respuesta.status_code == 201, respuesta.text
    publicados = api_cliente.get(f"{RUTA}/paquetes").json()["items"]
    assert any(item["nombre"] == "Pack Rápido" for item in publicados)


# --------------------------------------------------------------------------
# RF-009 delta - el catálogo por tipo de vehículo
# --------------------------------------------------------------------------
def test_ca02_el_catalogo_muestra_el_precio_del_tipo_de_vehiculo(api_cliente, servicio_medio):
    """RF-009 CA-02 v1.0: «el precio mostrado corresponde a ese tipo»."""
    respuesta = api_cliente.get(f"{RUTA}/servicios/{servicio_medio.id}?tipo_vehiculo=suv")

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["precio"]["monto_centimos"] == 2500, "el precio regular sigue ahí"
    assert cuerpo["tipo_vehiculo"] == "suv"
    assert cuerpo["factor_milesimas"] == FACTOR_SUV
    assert cuerpo["precio_aplicable"]["monto_centimos"] == 3250


def test_el_catalogo_acepta_el_vehiculo_del_cliente(api_cliente, servicio_medio):
    """RF-009 paso 4: el cliente abre el servicio con SU vehículo seleccionado."""
    suv = crear_vehiculo(api_cliente, "SUV-777")

    items = api_cliente.get(f"{RUTA}/servicios?vehiculo_id={suv}").json()["items"]

    servicio = next(item for item in items if item["id"] == servicio_medio.id)
    assert servicio["tipo_vehiculo"] == "suv"
    assert servicio["precio_aplicable"]["monto_centimos"] == 3250


def test_el_catalogo_no_deja_mirar_el_vehiculo_de_otro(api_cliente, api_recepcion, servicio_medio):
    """Autorización horizontal: un vehículo ajeno responde 404, no 403."""
    ajeno = crear_vehiculo(api_cliente, "AJE-123")

    respuesta = api_recepcion.get(f"{RUTA}/servicios?vehiculo_id={ajeno}")

    assert respuesta.status_code == 404
    assert codigo_error(respuesta) == "RECURSO_NO_ENCONTRADO"


def test_el_catalogo_sin_vehiculo_cobra_el_factor_neutro(api_cliente, servicio_medio):
    """Sin tipo de vehículo el factor es 1,0: el catálogo nunca se queda mudo.

    Compatibilidad con el MVP: ``precio`` sigue siendo el precio regular y el
    aplicable coincide con él, así que un cliente sin vehículo elegido ve
    exactamente lo que veía antes.
    """
    items = api_cliente.get(f"{RUTA}/servicios").json()["items"]

    servicio = next(item for item in items if item["id"] == servicio_medio.id)
    assert servicio["precio"]["monto_centimos"] == 2500
    assert servicio["tipo_vehiculo"] is None
    assert servicio["factor_milesimas"] == FACTOR_SEDAN
    assert servicio["precio_aplicable"]["monto_centimos"] == 2500


def test_el_catalogo_destaca_el_precio_promocional_junto_al_regular(api_admin, api_cliente, db):
    """RF-011 «precio promocional destacado junto al regular»."""
    encerado = db.scalars(select(Servicio).where(Servicio.nombre == "Lavado + Encerado")).first()

    items = api_cliente.get(f"{RUTA}/servicios?tipo_vehiculo=sedan").json()["items"]

    servicio = next(item for item in items if item["id"] == encerado.id)
    assert servicio["promocion"]["nombre"] == "Verano Premium"
    assert servicio["precio_aplicable"]["monto_centimos"] == 4500
    # 15 % de 4500 = 675.
    assert servicio["precio_promocional"]["monto_centimos"] == 3825


def test_el_servicio_lleva_imagen_referencial(api_admin, api_cliente):
    """RF-009 v1.0: imagen referencial en el catálogo."""
    servicio = _crear_servicio(api_admin, imagen_url="catalogo/lavado-prueba.png")

    detalle = api_cliente.get(f"{RUTA}/servicios/{servicio['id']}").json()

    assert detalle["imagen_url"] == "catalogo/lavado-prueba.png"


# --------------------------------------------------------------------------
# RF-010 delta - administración de factores
# --------------------------------------------------------------------------
def test_definir_un_factor_abre_una_vigencia_nueva_y_queda_auditado(api_admin, db, servicio_medio):
    """RF-010 v1.0 + RNF-014: el cambio de tarifa no se sobrescribe, se versiona."""
    primera = api_admin.put(
        f"{RUTA}/admin/factores",
        json={"servicio_id": servicio_medio.id, "tipo_vehiculo": "suv", "factor_milesimas": 1500},
    )
    assert primera.status_code == 200, primera.text

    segunda = api_admin.put(
        f"{RUTA}/admin/factores",
        json={"servicio_id": servicio_medio.id, "tipo_vehiculo": "suv", "factor_milesimas": 1600},
    )
    assert segunda.status_code == 200, segunda.text

    filas = db.scalars(
        select(FactorTipoVehiculo)
        .where(
            FactorTipoVehiculo.servicio_id == servicio_medio.id,
            FactorTipoVehiculo.tipo_vehiculo == "suv",
        )
        .order_by(FactorTipoVehiculo.id)
    ).all()
    assert len(filas) == 2, "la vigencia anterior se conserva"
    assert filas[0].vigente_hasta is not None
    assert filas[1].vigente_hasta is None

    auditoria = _eventos(db, "servicio.factor_cambiado")
    assert len(auditoria) == 2
    assert auditoria[-1].datos == {
        "servicio_id": servicio_medio.id,
        "tipo_vehiculo": "suv",
        "factor_anterior": 1500,
        "factor_nuevo": 1600,
    }


def test_el_factor_del_servicio_gana_al_global(api_admin, api_cliente, servicio_medio):
    """RN-04: el servicio puede corregir el factor global de su tipo."""
    api_admin.put(
        f"{RUTA}/admin/factores",
        json={"servicio_id": servicio_medio.id, "tipo_vehiculo": "suv", "factor_milesimas": 1100},
    )

    desglose = _cotizar(api_cliente, servicio_id=servicio_medio.id, tipo_vehiculo="suv").json()

    assert desglose["factor_milesimas"] == 1100
    assert desglose["total"]["monto_centimos"] == 2750


def test_un_servicio_sin_factores_se_cobra_a_precio_base(api_admin, api_cliente, db):
    """Un tipo sin fila configurada vale 1,0: el catálogo nunca se queda mudo."""
    db.query(FactorTipoVehiculo).delete()
    db.commit()
    servicio = _crear_servicio(api_admin)

    desglose = _cotizar(api_cliente, servicio_id=servicio["id"], tipo_vehiculo="suv").json()

    assert desglose["factor_milesimas"] == 1000
    assert desglose["total"]["monto_centimos"] == 3000


def test_rechaza_un_factor_fuera_de_rango(api_admin, servicio_medio):
    """RF-010: un factor de cero dejaría el servicio gratis por descuido."""
    respuesta = api_admin.put(
        f"{RUTA}/admin/factores",
        json={"servicio_id": servicio_medio.id, "tipo_vehiculo": "suv", "factor_milesimas": 0},
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "VALIDACION"


# --------------------------------------------------------------------------
# Adicionales
# --------------------------------------------------------------------------
def test_un_adicional_desactivado_no_se_puede_pedir(api_admin, api_cliente, servicio_medio):
    """RN-04: el término «adicionales» solo admite lo que está a la venta."""
    adicional = api_admin.get(f"{RUTA}/admin/adicionales").json()["items"][0]
    api_admin.patch(f"{RUTA}/admin/adicionales/{adicional['id']}", json={"activo": False})

    respuesta = _cotizar(
        api_cliente,
        servicio_id=servicio_medio.id,
        tipo_vehiculo="sedan",
        adicionales=[adicional["id"]],
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "ADICIONAL_NO_DISPONIBLE"


def test_un_adicional_inexistente_responde_404(api_cliente, servicio_medio):
    respuesta = _cotizar(
        api_cliente, servicio_id=servicio_medio.id, tipo_vehiculo="sedan", adicionales=[9999]
    )

    assert respuesta.status_code == 404
    assert codigo_error(respuesta) == "RECURSO_NO_ENCONTRADO"


def test_los_adicionales_quedan_congelados_en_la_reserva(
    api_admin, api_cliente, servicio_medio, vehiculo_id
):
    """RF-012: renombrar o reprecificar un adicional no reescribe lo cobrado."""
    adicional = api_admin.get(f"{RUTA}/adicionales").json()["items"][0]
    reserva = api_cliente.post(
        f"{RUTA}/reservas",
        json={
            "servicio_id": servicio_medio.id,
            "vehiculo_id": vehiculo_id,
            "inicio": instante(proximo_lunes(), 10, 0).isoformat(),
            "adicionales": [adicional["id"]],
        },
    ).json()
    esperado = 2500 + adicional["monto"]["monto_centimos"]
    assert reserva["monto"]["monto_centimos"] == esperado

    api_admin.patch(
        f"{RUTA}/admin/adicionales/{adicional['id']}",
        json={"nombre": "Otro nombre", "monto_centimos": 9900},
    )

    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert detalle["monto"]["monto_centimos"] == esperado
    assert detalle["tarifa"]["adicionales"][0]["nombre"] == adicional["nombre"]


# --------------------------------------------------------------------------
# El hueco del MVP: el administrador y el servicio inactivo
# --------------------------------------------------------------------------
def test_el_administrador_lee_un_servicio_inactivo_sin_rodeos(
    api_admin, api_cliente, servicio_corto
):
    """El cliente sigue sin verlo (RF-010 CA-03); el administrador sí, directo."""
    api_admin.patch(f"{RUTA}/admin/servicios/{servicio_corto.id}", json={"activo": False})

    assert api_cliente.get(f"{RUTA}/servicios/{servicio_corto.id}").status_code == 404

    respuesta = api_admin.get(f"{RUTA}/admin/servicios/{servicio_corto.id}")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["activo"] is False


def test_el_detalle_administrativo_de_un_servicio_inexistente_es_404(api_admin):
    assert api_admin.get(f"{RUTA}/admin/servicios/9999").status_code == 404


# --------------------------------------------------------------------------
# Autorización (P5)
# --------------------------------------------------------------------------
def test_un_cliente_no_administra_promociones(api_cliente):
    """RF-011: el actor es el administrador."""
    respuesta = api_cliente.get(f"{RUTA}/admin/promociones")

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"


def test_la_recepcion_tampoco_administra_promociones(api_recepcion):
    assert api_recepcion.post(f"{RUTA}/admin/paquetes", json={}).status_code == 403


def test_el_catalogo_de_tarifas_exige_token(cliente_http):
    assert cliente_http.post(f"{RUTA}/tarifas/calculo", json={"servicio_id": 1}).status_code == 401
    assert cliente_http.get(f"{RUTA}/paquetes").status_code == 401
    assert cliente_http.get(f"{RUTA}/promociones").status_code == 401


# --------------------------------------------------------------------------
# GET /admin/factores - el catálogo de factores, sin pruebas hasta ahora
# --------------------------------------------------------------------------
def test_el_listado_de_factores_muestra_los_vigentes_con_su_alcance(api_admin):
    """RF-010 delta: la pantalla desde la que se administra RN-04.

    Se comprueba lo que la pantalla necesita para poder editar sin equivocarse:
    que el factor global (``servicio_id`` nulo) y el factor de un servicio
    concreto se distinguen, y que el SUV trae las **milésimas** de RF-012
    CA-01 (1300 = 1,3), no un float.
    """
    respuesta = api_admin.get(f"{RUTA}/admin/factores")

    assert respuesta.status_code == 200, respuesta.text
    items = respuesta.json()["items"]
    globales = {fila["tipo_vehiculo"]: fila for fila in items if fila["servicio_id"] is None}

    assert globales["suv"]["factor_milesimas"] == 1300
    assert globales["sedan"]["factor_milesimas"] == 1000
    por_servicio = [fila for fila in items if fila["servicio_id"] is not None]
    assert por_servicio, "el seed deja un factor propio de «Lavado Express»"
    assert all(fila["servicio"] for fila in por_servicio), "la pantalla necesita el nombre"


def test_el_listado_de_factores_solo_devuelve_el_vigente_de_cada_pareja(api_admin):
    """Los factores se versionan como ``servicio_precio``: el listado no acumula.

    Se redefine el factor del SUV y el listado tiene que seguir teniendo UNA
    fila para ``(global, suv)`` — la nueva. Si devolviera el histórico, la
    pantalla de administración mostraría dos factores contradictorios.
    """
    antes = api_admin.get(f"{RUTA}/admin/factores").json()["items"]
    cuantos = len([f for f in antes if f["servicio_id"] is None and f["tipo_vehiculo"] == "suv"])
    assert cuantos == 1

    cambio = api_admin.put(
        f"{RUTA}/admin/factores", json={"tipo_vehiculo": "suv", "factor_milesimas": 1500}
    )
    assert cambio.status_code == 200, cambio.text

    despues = api_admin.get(f"{RUTA}/admin/factores").json()["items"]
    vigentes = [f for f in despues if f["servicio_id"] is None and f["tipo_vehiculo"] == "suv"]

    assert len(vigentes) == 1
    assert vigentes[0]["factor_milesimas"] == 1500


def test_el_listado_de_factores_exige_administrar_servicios(api_recepcion, cliente_http):
    """RF-010: los factores son catálogo, y el catálogo lo administra quien puede."""
    assert api_recepcion.get(f"{RUTA}/admin/factores").status_code == 403
    assert cliente_http.get(f"{RUTA}/admin/factores").status_code == 401


# --------------------------------------------------------------------------
# GET /admin/paquetes y PATCH /admin/paquetes/{id}
# --------------------------------------------------------------------------
def _paquete_sembrado(api_admin) -> dict:
    respuesta = api_admin.get(f"{RUTA}/admin/paquetes")
    assert respuesta.status_code == 200, respuesta.text
    items = respuesta.json()["items"]
    assert items, "el seed deja «Pack Brillo Total»"
    return items[0]


def test_el_listado_de_paquetes_trae_sus_servicios_y_el_ahorro(api_admin):
    """RF-011: un paquete se administra sabiendo qué incluye y cuánto ahorra.

    ``precio_regular`` es la suma de los precios sueltos de sus líneas; sin él
    la pantalla no puede decir «ahorras S/ X», que es la razón de ser de un
    paquete.
    """
    paquete = _paquete_sembrado(api_admin)

    assert paquete["nombre"] == "Pack Brillo Total"
    assert paquete["activo"] is True
    assert len(paquete["servicios"]) == 2
    assert paquete["precio"]["moneda"] == "PEN"
    assert (
        paquete["precio_regular"]["monto_centimos"] > paquete["precio"]["monto_centimos"]
    ), "un paquete que no ahorra nada no es un paquete"


def test_desactivar_un_paquete_lo_saca_del_catalogo_publico_pero_no_del_de_administracion(
    api_admin, api_cliente
):
    """RF-011: ``GET /admin/paquetes`` lista activos e inactivos, el público no.

    Es la diferencia entre retirar un paquete y borrarlo: el administrador
    tiene que poder volver a encenderlo, y el cliente no tiene que verlo
    mientras esté apagado.
    """
    paquete = _paquete_sembrado(api_admin)

    apagado = api_admin.patch(f"{RUTA}/admin/paquetes/{paquete['id']}", json={"activo": False})

    assert apagado.status_code == 200, apagado.text
    assert apagado.json()["activo"] is False
    assert [p["id"] for p in api_admin.get(f"{RUTA}/admin/paquetes").json()["items"]] == [
        paquete["id"]
    ], "el administrador lo sigue viendo"
    publicos = api_cliente.get(f"{RUTA}/paquetes").json()["items"]
    assert paquete["id"] not in [p["id"] for p in publicos]

    encendido = api_admin.patch(f"{RUTA}/admin/paquetes/{paquete['id']}", json={"activo": True})
    assert encendido.json()["activo"] is True


def test_editar_el_precio_de_un_paquete_lo_deja_auditado(api_admin, db):
    """RNF-014: cambiar un precio es una operación sensible (RF-036 `CA-01`)."""
    paquete = _paquete_sembrado(api_admin)
    anterior = paquete["precio"]["monto_centimos"]

    respuesta = api_admin.patch(
        f"{RUTA}/admin/paquetes/{paquete['id']}",
        json={"precio_centimos": anterior + 500, "nombre": "Pack Brillo Total Plus"},
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["precio"]["monto_centimos"] == anterior + 500
    assert respuesta.json()["nombre"] == "Pack Brillo Total Plus"

    eventos_paquete = db.scalars(
        select(EventoDominio).where(EventoDominio.entidad == "paquete")
    ).all()
    assert eventos_paquete, "el cambio dejó rastro en la bitácora"


def test_editar_un_paquete_inexistente_responde_404(api_admin):
    respuesta = api_admin.patch(f"{RUTA}/admin/paquetes/999999", json={"activo": False})

    assert respuesta.status_code == 404
    assert codigo_error(respuesta) == "RECURSO_NO_ENCONTRADO"


def test_los_paquetes_de_administracion_exigen_su_permiso(api_recepcion, cliente_http):
    """RF-011: ``promocion:administrar``, nunca un nombre de rol."""
    assert api_recepcion.get(f"{RUTA}/admin/paquetes").status_code == 403
    assert (
        api_recepcion.patch(f"{RUTA}/admin/paquetes/1", json={"activo": False}).status_code == 403
    )
    assert cliente_http.get(f"{RUTA}/admin/paquetes").status_code == 401
