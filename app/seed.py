"""Idempotent seed for the AquaLav MVP.

Run it as many times as you like: every row is looked up by its natural key
before being inserted. Entry point::

    python -m app.seed

This is the ONLY place where role names appear (contract section 3): every
authorization decision is taken on a permission code, never on a role name.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.horario import TRAMOS_RN07, ahora
from app.core.security import hash_password
from app.database import SessionLocal
from app.models import (
    Bahia,
    EstadoBahia,
    EstadoCuenta,
    EstadoReserva,
    FactorTipoVehiculo,
    HorarioAtencion,
    Paquete,
    PaqueteServicio,
    Permiso,
    Promocion,
    Rol,
    Servicio,
    ServicioAdicional,
    ServicioPrecio,
    TipoDescuento,
    TipoDocumento,
    TipoVehiculo,
    TransicionEstado,
    Usuario,
    Vehiculo,
)

# --------------------------------------------------------------------------
# Catalogs (contract sections 2 and 3)
# --------------------------------------------------------------------------

#: permission code -> Spanish description shown in admin screens.
PERMISOS: dict[str, str] = {
    "vehiculo:leer": "Consultar los vehículos visibles para el usuario.",
    "vehiculo:crear": "Registrar un vehículo.",
    "vehiculo:editar": "Editar los datos de un vehículo propio.",
    "vehiculo:eliminar": "Dar de baja un vehículo propio.",
    "vehiculo:verificar": "Verificar que la placa del vehículo corresponde (RN-01).",
    "servicio:leer": "Consultar el catálogo de servicios activos.",
    "servicio:administrar": "Crear, editar y desactivar servicios y sus precios.",
    "disponibilidad:leer": "Consultar los bloques horarios disponibles.",
    "reserva:crear": "Crear una reserva.",
    "reserva:leer_propias": "Consultar únicamente las reservas propias.",
    "reserva:leer_todas": "Consultar las reservas de todos los clientes.",
    "reserva:cancelar": "Cancelar una reserva.",
    "reserva:check_in": "Registrar el ingreso del vehículo.",
    "reserva:asignar": "Asignar una bahía y un operario a un servicio.",
    "reserva:avanzar_estado": "Avanzar el estado de una reserva.",
    "reserva:revisar": "Registrar la observación del cliente y enviar el servicio a revisión.",
    "reserva:check_out": "Registrar la entrega del vehículo.",
    "pago:registrar": "Registrar el pago de una reserva.",
    "agenda:leer": "Consultar la agenda diaria y semanal por bahía.",
    "agenda:administrar": "Bloquear franjas, feriados y horarios de atención.",
    "usuario:administrar": "Crear, editar, activar y desactivar usuarios internos.",
    "rol:administrar": "Consultar roles y permisos y asignar el rol de un usuario.",
    "bahia:administrar": "Crear, editar y desactivar bahías.",
    "promocion:administrar": "Crear, editar y desactivar paquetes y promociones.",
}

#: role name -> Spanish description (RF-004 v1.0: four roles).
#: ``personal`` of the MVP is SPLIT into ``recepcionista`` and ``operario``;
#: migration ``0003`` reassigns the accounts that still hold it.
ROLES: dict[str, str] = {
    "cliente": "Cliente que reserva servicios de lavado.",
    "recepcionista": "Atiende el mostrador: recibe, asigna, cobra y entrega.",
    "operario": "Ejecuta el servicio en la bahía y avanza su estado.",
    "administrador": "Administrador del catálogo, la operación y los accesos.",
}

#: Role every self-registered account gets (RF-001 flow step 5).
#: Exported so ``auth_service`` never has to spell a role name itself: this
#: module is the only place where role names may appear (contract section 3).
ROL_REGISTRO_PUBLICO = "cliente"

#: role name -> permission codes granted.
ROL_PERMISOS: dict[str, tuple[str, ...]] = {
    "cliente": (
        "vehiculo:leer",
        "vehiculo:crear",
        "vehiculo:editar",
        "vehiculo:eliminar",
        "servicio:leer",
        "disponibilidad:leer",
        "reserva:crear",
        "reserva:leer_propias",
        "reserva:cancelar",
    ),
    # The counter: receives the vehicle, assigns it, charges it and hands it
    # back. It does NOT advance the service inside the bay.
    "recepcionista": (
        "servicio:leer",
        # RN-01 v1.0: verifying a vehicle is the counter looking at the plate.
        "vehiculo:verificar",
        "agenda:leer",
        # RF-018 names BOTH the administrator and the receptionist as the
        # actors of the agenda: the counter is who reschedules a slot when a
        # bay breaks down, so it has to be able to block one.
        "agenda:administrar",
        "reserva:leer_todas",
        "reserva:cancelar",
        "reserva:check_in",
        "reserva:asignar",
        "reserva:revisar",
        "reserva:check_out",
        "pago:registrar",
    ),
    # The bay: advances the service through its operative states (RF-021).
    # ``reserva:leer_todas`` is shared with the counter because an operator
    # works on reservations that belong to a customer, not to themselves:
    # without it every operative endpoint would answer 404.
    "operario": (
        "servicio:leer",
        "reserva:leer_todas",
        "reserva:avanzar_estado",
    ),
    # The administrator is a superset of every permission.
    "administrador": tuple(PERMISOS),
}

#: (estado_origen, estado_destino, permiso_requerido, endpoint, marca_fin_servicio)
#: EXTENSION POINT P3. ``endpoint`` names the operation that OWNS the move: the
#: generic ``POST /reservas/{id}/estado`` refuses a move that belongs to another
#: one, so no caller can reach a state while skipping the invariants and side
#: effects that operation carries (RN-09, RF-024 CA-01, RF-016 CA-03). ``None``
#: means the move has no extra rule and the generic endpoint may perform it.
#:
#: These are the eighteen transitions of Annex A v1.0 minus the four that are
#: NOT rows by construction:
#:
#: * ``[*] -> pendiente_pago`` and ``[*] -> confirmada`` are CREATION. There is
#:   no origin state to declare; ``POST /reservas`` picks the initial state from
#:   the payment modality (RF-014 + RF-025).
#: * ``entregado -> [*]`` and ``cancelada -> [*]`` are the two TERMINAL sinks.
#:   Terminality is derived from the absence of outgoing rows
#:   (``listar_estados_no_terminales``): inserting a sink row would make both
#:   states look active again and they would never release their bay (RN-03).
#:
#: The remaining fourteen are rows, and every state of Annex A appears in at
#: least one of them, which is what makes the catalogue of ``GET /estados``
#: complete without a list of states anywhere in the code.
TRANSICIONES: tuple[tuple[str, str, str, str | None, bool], ...] = (
    # 3. The gateway approves the online payment (RF-026). INC-4 implements the
    #    owning operation; the row already refuses every other door.
    (
        EstadoReserva.PENDIENTE_PAGO.value,
        EstadoReserva.CONFIRMADA.value,
        "pago:registrar",
        "pago",
        False,
    ),
    # 4. The fifteen minute window expires, or the customer cancels (RF-016).
    (
        EstadoReserva.PENDIENTE_PAGO.value,
        EstadoReserva.CANCELADA.value,
        "reserva:cancelar",
        "cancelacion",
        False,
    ),
    # 5. Check-in (RF-019). v1.0 lands on "en recepción", not on "en atención".
    (
        EstadoReserva.CONFIRMADA.value,
        EstadoReserva.EN_RECEPCION.value,
        "reserva:check_in",
        "check_in",
        False,
    ),
    # 6. Cancellation before the vehicle arrives (RF-016).
    (
        EstadoReserva.CONFIRMADA.value,
        EstadoReserva.CANCELADA.value,
        "reserva:cancelar",
        "cancelacion",
        False,
    ),
    # 7. Bay and operator assignment (RF-020). INC-1B implements the operation.
    (
        EstadoReserva.EN_RECEPCION.value,
        EstadoReserva.ASIGNADO.value,
        "reserva:asignar",
        "asignacion",
        False,
    ),
    # 8. v1.0 allows cancelling after the check-in; the MVP did not (RF-016).
    (
        EstadoReserva.EN_RECEPCION.value,
        EstadoReserva.CANCELADA.value,
        "reserva:cancelar",
        "cancelacion",
        False,
    ),
    # 9-12. The operator walks the service through the bay (RF-021). No owning
    #       operation: these are exactly what the generic endpoint is for.
    (
        EstadoReserva.ASIGNADO.value,
        EstadoReserva.EN_LAVADO.value,
        "reserva:avanzar_estado",
        None,
        False,
    ),
    (
        EstadoReserva.EN_LAVADO.value,
        EstadoReserva.SECADO.value,
        "reserva:avanzar_estado",
        None,
        False,
    ),
    (
        EstadoReserva.SECADO.value,
        EstadoReserva.ACABADO.value,
        "reserva:avanzar_estado",
        None,
        False,
    ),
    # 12. Finishing the service is what stamps ``hora_fin_real`` and unlocks
    #     the charge (RF-022 CA-02, RN-09): the flag moved here from
    #     ``en_atencion -> finalizado`` without a single line of code changing.
    (
        EstadoReserva.ACABADO.value,
        EstadoReserva.FINALIZADO.value,
        "reserva:avanzar_estado",
        None,
        True,
    ),
    # 13. Check-out (RF-024).
    (
        EstadoReserva.FINALIZADO.value,
        EstadoReserva.ENTREGADO.value,
        "reserva:check_out",
        "check_out",
        False,
    ),
    # 14. The customer objects to the result (RF-024 flow 3a). Its own owner so
    #     the check-out never has two destinations to choose from; INC-1B wires
    #     ``POST /reservas/{id}/revision`` to it.
    (
        EstadoReserva.FINALIZADO.value,
        EstadoReserva.EN_REVISION.value,
        "reserva:revisar",
        "revision",
        False,
    ),
    # 15. The operator reworks the vehicle (RF-021).
    (
        EstadoReserva.EN_REVISION.value,
        EstadoReserva.ACABADO.value,
        "reserva:avanzar_estado",
        None,
        False,
    ),
    # 16. The customer accepts after the review and takes the vehicle (RF-024).
    (
        EstadoReserva.EN_REVISION.value,
        EstadoReserva.ENTREGADO.value,
        "reserva:check_out",
        "check_out",
        False,
    ),
)

#: Four bays, the physical limit of the shop (RE-07).
BAHIAS: tuple[str, ...] = ("Bahía 1", "Bahía 2", "Bahía 3", "Bahía 4")

#: RN-07 as DATA (RF-018): the opening week moves out of ``app/core/horario``
#: and into ``horario_atencion``. The constants stay in the core module as the
#: FALLBACK calendar, so an empty table never leaves the shop closed.
VIGENCIA_HORARIO_INICIAL = date(2024, 1, 1)

#: (nombre, descripcion, categoria, duracion_min, monto_centimos)
SERVICIOS: tuple[tuple[str, str, str, int, int], ...] = (
    (
        "Lavado Express",
        "Lavado exterior rápido con secado a mano. Ideal si tienes poco tiempo.",
        "basico",
        30,
        1500,
    ),
    (
        "Lavado Completo",
        "Lavado exterior e interior, aspirado de alfombras y limpieza de tableros.",
        "basico",
        45,
        2500,
    ),
    (
        "Lavado + Encerado",
        "Lavado completo más encerado protector que realza el brillo de la pintura.",
        "premium",
        60,
        4500,
    ),
    (
        "Lavado de Motor",
        "Limpieza y desengrasado del compartimiento del motor con productos especializados.",
        "especializado",
        45,
        3500,
    ),
    (
        "Detallado Interior",
        "Shampoo de tapiz y alfombras, limpieza profunda de interiores y aromatización.",
        "premium",
        90,
        7000,
    ),
)

#: (nombres, apellidos, telefono, documento, rol, prefijo en settings)
#: RF-001 v1.0 captures the identity document, so the demo accounts carry one
#: too: without it the uniqueness rule would never be exercised in a demo.
USUARIOS_DEMO: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("Carla", "Quispe", "987000001", "40000001", "administrador", "seed_admin"),
    ("Luis", "Ramos", "987000002", "40000002", "recepcionista", "seed_recepcion"),
    ("Marco", "Huamán", "987000004", "40000004", "operario", "seed_operario"),
    ("Ana", "Torres", "987000003", "40000003", "cliente", "seed_cliente"),
)

#: (nombre del servicio o None para el factor global, tipo de vehículo, milésimas)
#: RN-04 as demo data: an SUV pays 1.3x everywhere, a motorbike 0.8x, and the
#: express wash charges a motorbike even less because it barely takes the bay.
#: RF-012 CA-01 is exactly the second row: 3000 x 1.3 = 3900.
FACTORES: tuple[tuple[str | None, str, int], ...] = (
    (None, TipoVehiculo.SEDAN.value, 1000),
    (None, TipoVehiculo.SUV.value, 1300),
    (None, TipoVehiculo.CAMIONETA.value, 1400),
    (None, TipoVehiculo.MOTOCICLETA.value, 800),
    ("Lavado Express", TipoVehiculo.MOTOCICLETA.value, 700),
)

#: (nombre, descripción, monto_centimos) - the "adicionales" term of RN-04.
ADICIONALES: tuple[tuple[str, str, int], ...] = (
    ("Aromatización", "Aroma de larga duración a elección del cliente.", 500),
    ("Abrillantado de llantas", "Sellador y brillo para llantas y aros.", 800),
    ("Shampoo de tapiz", "Lavado profundo de asientos y alfombras.", 1200),
)

#: (nombre, descripción, precio_centimos, ((servicio, cantidad), ...)) - RF-011.
PAQUETES: tuple[tuple[str, str, int, tuple[tuple[str, int], ...]], ...] = (
    (
        "Pack Brillo Total",
        "Lavado completo más lavado con encerado, a precio preferencial.",
        6000,
        (("Lavado Completo", 1), ("Lavado + Encerado", 1)),
    ),
)

#: RF-011 demo promotions, dated RELATIVE to the day the seed runs so the
#: catalogue always shows one in force and one expired, whenever it is run.
#: (nombre, descripción, servicio, tipo_descuento, valor, cupón, desde, hasta)
DIAS_PROMO_PASADA = 365
DIAS_PROMO_VENCIDA = 30
DIAS_PROMO_FUTURA = 60
DIAS_CUPON = 180


def promociones_demo(hoy: date) -> tuple[dict, ...]:
    """The three promotions the demo catalogue needs, anchored on ``hoy``."""
    return (
        {
            "nombre": "Verano Premium",
            "descripcion": "15 % de descuento en el lavado con encerado.",
            "servicio": "Lavado + Encerado",
            "tipo_descuento": TipoDescuento.PORCENTAJE.value,
            "valor": 15,
            "codigo_cupon": None,
            "vigente_desde": hoy - timedelta(days=DIAS_PROMO_VENCIDA),
            "vigente_hasta": hoy + timedelta(days=DIAS_PROMO_FUTURA),
        },
        {
            "nombre": "Aniversario AquaLav",
            "descripcion": "20 % de descuento en el detallado interior. Promoción cerrada.",
            "servicio": "Detallado Interior",
            "tipo_descuento": TipoDescuento.PORCENTAJE.value,
            "valor": 20,
            "codigo_cupon": None,
            "vigente_desde": hoy - timedelta(days=DIAS_PROMO_PASADA),
            "vigente_hasta": hoy - timedelta(days=DIAS_PROMO_VENCIDA),
        },
        {
            "nombre": "Bienvenida AquaLav",
            "descripcion": "10 % de descuento con el cupón de bienvenida.",
            "servicio": None,
            "tipo_descuento": TipoDescuento.PORCENTAJE.value,
            "valor": 10,
            "codigo_cupon": "BIENVENIDA10",
            "vigente_desde": hoy - timedelta(days=DIAS_PROMO_VENCIDA),
            "vigente_hasta": hoy + timedelta(days=DIAS_CUPON),
        },
    )


#: Demo vehicle attached to the demo customer. It arrives VERIFIED (RN-01):
#: the shop has seen this car, which is what lets a demo turn
#: ``EXIGIR_VEHICULO_VERIFICADO`` on and still book with it.
VEHICULO_DEMO = {
    "placa": "ABC-123",
    "tipo": "sedan",
    "marca": "Toyota",
    "modelo": "Yaris",
    "color": "Rojo",
    "anio": 2020,
}


# --------------------------------------------------------------------------
# Seed steps
# --------------------------------------------------------------------------
def _sembrar_permisos(db: Session) -> dict[str, Permiso]:
    existentes = {permiso.codigo: permiso for permiso in db.scalars(select(Permiso)).all()}
    for codigo, descripcion in PERMISOS.items():
        permiso = existentes.get(codigo)
        if permiso is None:
            permiso = Permiso(codigo=codigo, descripcion=descripcion)
            db.add(permiso)
            existentes[codigo] = permiso
        else:
            permiso.descripcion = descripcion
    db.flush()
    return existentes


def _sembrar_roles(db: Session, permisos: dict[str, Permiso]) -> dict[str, Rol]:
    existentes = {rol.nombre: rol for rol in db.scalars(select(Rol)).all()}
    for nombre, descripcion in ROLES.items():
        rol = existentes.get(nombre)
        if rol is None:
            rol = Rol(nombre=nombre, descripcion=descripcion)
            db.add(rol)
            existentes[nombre] = rol
        else:
            rol.descripcion = descripcion
    db.flush()

    # rol_permiso mapping, additive and idempotent.
    for nombre, codigos in ROL_PERMISOS.items():
        rol = existentes[nombre]
        actuales = {permiso.codigo for permiso in rol.permisos}
        for codigo in codigos:
            if codigo not in actuales:
                rol.permisos.append(permisos[codigo])
    db.flush()
    return existentes


def _sembrar_transiciones(db: Session) -> None:
    existentes = {
        (transicion.estado_origen, transicion.estado_destino)
        for transicion in db.scalars(select(TransicionEstado)).all()
    }
    for origen, destino, permiso, endpoint, marca_fin in TRANSICIONES:
        if (origen, destino) not in existentes:
            db.add(
                TransicionEstado(
                    estado_origen=origen,
                    estado_destino=destino,
                    permiso_requerido=permiso,
                    endpoint=endpoint,
                    marca_fin_servicio=marca_fin,
                )
            )
    db.flush()


def _sembrar_bahias(db: Session) -> None:
    existentes = {bahia.nombre for bahia in db.scalars(select(Bahia)).all()}
    for nombre in BAHIAS:
        if nombre not in existentes:
            db.add(Bahia(nombre=nombre, activa=True, estado=EstadoBahia.LIBRE.value))
    db.flush()


def _sembrar_horarios(db: Session) -> None:
    """Write RN-07 into ``horario_atencion``, one row per weekday."""
    existentes = {fila.dia_semana for fila in db.scalars(select(HorarioAtencion)).all()}
    for dia_semana, (apertura, cierre) in sorted(TRAMOS_RN07.items()):
        if dia_semana not in existentes:
            db.add(
                HorarioAtencion(
                    dia_semana=dia_semana,
                    hora_apertura=apertura,
                    hora_cierre=cierre,
                    vigente_desde=VIGENCIA_HORARIO_INICIAL,
                )
            )
    db.flush()


def _sembrar_servicios(db: Session) -> None:
    ahora = datetime.now(UTC)
    existentes = {servicio.nombre: servicio for servicio in db.scalars(select(Servicio)).all()}

    for nombre, descripcion, categoria, duracion, monto in SERVICIOS:
        servicio = existentes.get(nombre)
        if servicio is None:
            servicio = Servicio(
                nombre=nombre,
                descripcion=descripcion,
                categoria=categoria,
                duracion_min=duracion,
                activo=True,
            )
            db.add(servicio)
            db.flush()

        # Every service must own exactly one open price row.
        abiertos = [precio for precio in servicio.precios if precio.vigente_hasta is None]
        if not abiertos:
            db.add(
                ServicioPrecio(
                    servicio_id=servicio.id,
                    monto_centimos=monto,
                    moneda="PEN",
                    vigente_desde=ahora,
                )
            )
    db.flush()


def _servicios_por_nombre(db: Session) -> dict[str, Servicio]:
    return {servicio.nombre: servicio for servicio in db.scalars(select(Servicio)).all()}


def _sembrar_factores(db: Session) -> None:
    """RN-04 demo factors. A pair that already has an open row is left alone."""
    servicios = _servicios_por_nombre(db)
    abiertos = {
        (factor.servicio_id, factor.tipo_vehiculo)
        for factor in db.scalars(
            select(FactorTipoVehiculo).where(FactorTipoVehiculo.vigente_hasta.is_(None))
        ).all()
    }
    momento = datetime.now(UTC)

    for nombre_servicio, tipo, milesimas in FACTORES:
        servicio = servicios.get(nombre_servicio) if nombre_servicio else None
        if nombre_servicio and servicio is None:
            continue
        servicio_id = servicio.id if servicio else None
        if (servicio_id, tipo) in abiertos:
            continue
        db.add(
            FactorTipoVehiculo(
                servicio_id=servicio_id,
                tipo_vehiculo=tipo,
                factor_milesimas=milesimas,
                vigente_desde=momento,
            )
        )
    db.flush()


def _sembrar_adicionales(db: Session) -> None:
    existentes = {fila.nombre for fila in db.scalars(select(ServicioAdicional)).all()}
    for nombre, descripcion, monto in ADICIONALES:
        if nombre not in existentes:
            db.add(
                ServicioAdicional(
                    nombre=nombre,
                    descripcion=descripcion,
                    monto_centimos=monto,
                    moneda="PEN",
                    activo=True,
                )
            )
    db.flush()


def _sembrar_paquetes(db: Session) -> None:
    servicios = _servicios_por_nombre(db)
    existentes = {paquete.nombre for paquete in db.scalars(select(Paquete)).all()}

    for nombre, descripcion, precio, lineas in PAQUETES:
        if nombre in existentes:
            continue
        incluidos = [servicios[s] for s, _ in lineas if s in servicios]
        if len(incluidos) != len(lineas):
            continue
        paquete = Paquete(
            nombre=nombre,
            descripcion=descripcion,
            precio_centimos=precio,
            moneda="PEN",
            activo=True,
            vigente_desde=VIGENCIA_HORARIO_INICIAL,
        )
        db.add(paquete)
        db.flush()
        for nombre_servicio, cantidad in lineas:
            db.add(
                PaqueteServicio(
                    paquete_id=paquete.id,
                    servicio_id=servicios[nombre_servicio].id,
                    cantidad=cantidad,
                )
            )
    db.flush()


def _sembrar_promociones(db: Session) -> None:
    """One promotion in force, one already expired and one coupon (RF-011).

    The expired one is the point: nothing sweeps it, nothing deactivates it,
    and the catalogue shows the regular price again because the calculation
    compares its ``vigente_hasta`` against the service date (flow 4a).
    """
    servicios = _servicios_por_nombre(db)
    existentes = {promocion.nombre for promocion in db.scalars(select(Promocion)).all()}

    for datos in promociones_demo(ahora().date()):
        if datos["nombre"] in existentes:
            continue
        nombre_servicio = datos["servicio"]
        servicio = servicios.get(nombre_servicio) if nombre_servicio else None
        if nombre_servicio and servicio is None:
            continue
        db.add(
            Promocion(
                nombre=datos["nombre"],
                descripcion=datos["descripcion"],
                tipo_descuento=datos["tipo_descuento"],
                valor=datos["valor"],
                servicio_id=servicio.id if servicio else None,
                codigo_cupon=datos["codigo_cupon"],
                vigente_desde=datos["vigente_desde"],
                vigente_hasta=datos["vigente_hasta"],
                activa=True,
            )
        )
    db.flush()


def _credenciales_demo(clave: str) -> tuple[str, str]:
    """Read the ``<clave>_correo`` / ``<clave>_password`` pair from settings."""
    return (
        getattr(settings, f"{clave}_correo"),
        getattr(settings, f"{clave}_password"),
    )


def _sembrar_usuarios(db: Session, roles: dict[str, Rol]) -> dict[str, Usuario]:
    creados: dict[str, Usuario] = {}

    for nombres, apellidos, telefono, documento, nombre_rol, clave in USUARIOS_DEMO:
        correo, password = _credenciales_demo(clave)
        usuario = db.scalars(select(Usuario).where(Usuario.correo == correo)).first()
        if usuario is None:
            usuario = Usuario(
                nombres=nombres,
                apellidos=apellidos,
                correo=correo,
                telefono=telefono,
                tipo_documento=TipoDocumento.DNI.value,
                numero_documento=documento,
                hash_password=hash_password(password),
                rol_id=roles[nombre_rol].id,
                # The demo accounts are created BY the shop, not self
                # registered: there is nobody to click a verification link.
                estado_cuenta=EstadoCuenta.ACTIVA.value,
                consentimiento_privacidad_en=datetime.now(UTC),
                intentos_fallidos=0,
            )
            db.add(usuario)
            db.flush()
        creados[nombre_rol] = usuario

    return creados


def _sembrar_vehiculo_demo(db: Session, cliente: Usuario) -> None:
    existente = db.scalars(
        select(Vehiculo).where(
            Vehiculo.usuario_id == cliente.id,
            Vehiculo.placa == VEHICULO_DEMO["placa"],
        )
    ).first()
    if existente is None:
        db.add(
            Vehiculo(
                usuario_id=cliente.id,
                activo=True,
                verificado=True,
                verificado_en=datetime.now(UTC),
                **VEHICULO_DEMO,
            )
        )
    db.flush()


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def ejecutar_seed(db: Session) -> None:
    """Populate the reference data. Safe to run repeatedly."""
    permisos = _sembrar_permisos(db)
    roles = _sembrar_roles(db, permisos)
    _sembrar_transiciones(db)
    _sembrar_bahias(db)
    _sembrar_horarios(db)
    _sembrar_servicios(db)
    _sembrar_factores(db)
    _sembrar_adicionales(db)
    _sembrar_paquetes(db)
    _sembrar_promociones(db)
    usuarios = _sembrar_usuarios(db, roles)
    _sembrar_vehiculo_demo(db, usuarios["cliente"])
    db.commit()


def main() -> None:
    """``python -m app.seed`` entry point."""
    if not settings.seed_enabled:
        print("Seed deshabilitado (SEED_ENABLED=false). No se insertó nada.")
        return

    db = SessionLocal()
    try:
        ejecutar_seed(db)
        print("Seed ejecutado correctamente.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
