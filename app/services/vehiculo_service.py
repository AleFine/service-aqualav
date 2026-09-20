"""Vehicle registration, edition and logical deletion (RF-007, RF-008, RN-01).

RF-008 is the reason ``Vehiculo.activo`` finally moves: the MVP wrote it once
as ``True`` and never again. A deletion here is LOGICAL - the vehicle leaves
the active list and can no longer be booked, and every reservation it ever had
stays readable (CA-02), which is exactly what a hard delete would destroy.
"""

from sqlalchemy.orm import Session

from app.core.errors import (
    PlacaDuplicada,
    RecursoNoEncontrado,
    VehiculoConReservaVigente,
    detalle,
)
from app.core.horario import a_lima, ahora_utc, desde_bd
from app.models import Usuario, Vehiculo
from app.repositories import transicion as transicion_repo
from app.repositories import vehiculo as vehiculo_repo
from app.schemas import VehiculoActualizar, VehiculoIn
from app.services import eventos

#: RN-01 v1.0: the counter confirms the plate. Granting it is an act of the
#: shop, so it is guarded by its own permission and never by a role name (P5).
PERMISO_VERIFICAR = "vehiculo:verificar"

#: Fields ``PATCH /vehiculos/{id}`` may rewrite. The plate is handled apart
#: because it carries the uniqueness rule of RF-007 CA-02.
CAMPOS_EDITABLES = ("tipo", "marca", "modelo", "color", "anio")


def normalizar_placa(placa: str) -> str:
    """Uppercase and blank-free, the form the unique key is built on."""
    return "".join((placa or "").split()).upper()


def listar(db: Session, usuario: Usuario, *, incluir_inactivos: bool = False) -> list[Vehiculo]:
    """The caller's own vehicles.

    There is no "read every vehicle" permission for the customer, so this is
    always scoped to the caller (contract section 3, horizontal authorization).
    Deactivated vehicles are left out unless the history screen asks for them
    (RF-008 CA-01 and CA-02 are the two sides of this flag).
    """
    return vehiculo_repo.listar_por_usuario(db, usuario.id, incluir_inactivos=incluir_inactivos)


def obtener_propio(db: Session, usuario: Usuario, vehiculo_id: int) -> Vehiculo:
    """One vehicle of the caller, 404 when it is not theirs.

    A 404 rather than a 403: confirming that somebody else's id exists is a
    leak, and the customer has no use for the difference.
    """
    vehiculo = vehiculo_repo.obtener_por_id(db, vehiculo_id)
    if vehiculo is None or vehiculo.usuario_id != usuario.id:
        raise RecursoNoEncontrado(
            "No encontramos ese vehículo en tu cuenta.",
            detalles=[detalle("vehiculo_id", "El vehículo no pertenece a tu cuenta.")],
        )
    return vehiculo


def _exigir_placa_libre(
    db: Session, usuario_id: int, placa: str, *, excepto_id: int | None = None
) -> None:
    if vehiculo_repo.obtener_por_usuario_y_placa(db, usuario_id, placa, excepto_id=excepto_id):
        raise PlacaDuplicada(
            detalles=[detalle("placa", f"El vehículo con placa {placa} ya está en tu cuenta.")]
        )


def crear(db: Session, usuario: Usuario, datos: VehiculoIn) -> Vehiculo:
    """Register a vehicle for the caller.

    The plate format was validated by the schema (422 ``PLACA_INVALIDA``); the
    rule left here is uniqueness inside the account (RF-007 CA-02 -> 409).
    """
    placa = normalizar_placa(datos.placa)
    _exigir_placa_libre(db, usuario.id, placa)

    vehiculo = vehiculo_repo.crear(
        db,
        usuario_id=usuario.id,
        placa=placa,
        tipo=datos.tipo.value,
        marca=datos.marca,
        modelo=datos.modelo,
        color=datos.color,
        anio=datos.anio,
    )

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_VEHICULO,
        vehiculo.id,
        eventos.VEHICULO_REGISTRADO,
        autor_id=usuario.id,
        datos={"placa": placa, "tipo": vehiculo.tipo},
    )
    db.commit()
    db.refresh(vehiculo)
    return vehiculo


def actualizar(
    db: Session, usuario: Usuario, vehiculo_id: int, datos: VehiculoActualizar
) -> Vehiculo:
    """Edit a vehicle of the caller (RF-008).

    Nothing is written until every field has been accepted, so a rejected
    edition leaves the stored vehicle untouched and the form can be resubmitted
    with one field fixed.
    """
    vehiculo = obtener_propio(db, usuario, vehiculo_id)
    cambios: dict[str, object] = {}

    if datos.placa is not None:
        placa = normalizar_placa(datos.placa)
        if placa != vehiculo.placa:
            _exigir_placa_libre(db, usuario.id, placa, excepto_id=vehiculo.id)
            cambios["placa"] = {"anterior": vehiculo.placa, "nuevo": placa}
            vehiculo.placa = placa

    for campo in CAMPOS_EDITABLES:
        valor = getattr(datos, campo)
        if valor is None:
            continue
        valor = getattr(valor, "value", valor)
        if valor != getattr(vehiculo, campo):
            cambios[campo] = {"anterior": getattr(vehiculo, campo), "nuevo": valor}
            setattr(vehiculo, campo, valor)

    if cambios:
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_VEHICULO,
            vehiculo.id,
            eventos.VEHICULO_ACTUALIZADO,
            autor_id=usuario.id,
            datos=cambios,
        )
        db.commit()
        db.refresh(vehiculo)
    return vehiculo


def reservas_vigentes(db: Session, vehiculo: Vehiculo):
    """Live reservations of a vehicle (RF-008 flow 3a).

    "Live" is derived from ``transicion_estado``: a reservation whose state
    still has an outgoing move has not finished. No state is named here (P3).
    """
    estados_activos = transicion_repo.listar_estados_no_terminales(db)
    return vehiculo_repo.listar_reservas_activas(db, vehiculo.id, estados_activos)


def dar_de_baja(db: Session, usuario: Usuario, vehiculo_id: int) -> Vehiculo:
    """Logical deletion (RF-008 CA-01, flow 3a and 4a).

    A vehicle with a live reservation is NOT deleted: the answer is a 409 that
    names each booking by its code and date, which is the part the requirement
    insists on. Everything else only flips ``activo``: the vehicle stops being
    listed and stops being bookable, and its history stays readable (CA-02).
    """
    vehiculo = obtener_propio(db, usuario, vehiculo_id)
    if not vehiculo.activo:
        return vehiculo

    vigentes = reservas_vigentes(db, vehiculo)
    if vigentes:
        raise VehiculoConReservaVigente(
            detalles=[
                detalle(
                    "reservas",
                    f"La reserva {reserva.codigo} del "
                    f"{a_lima(desde_bd(reserva.inicio)).strftime('%d/%m/%Y %H:%M')} "
                    "sigue vigente.",
                )
                for reserva in vigentes
            ]
        )

    vehiculo.activo = False
    vehiculo.desactivado_en = ahora_utc()
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_VEHICULO,
        vehiculo.id,
        eventos.VEHICULO_DADO_DE_BAJA,
        autor_id=usuario.id,
        datos={"placa": vehiculo.placa},
    )
    db.commit()
    db.refresh(vehiculo)
    return vehiculo


def verificar(db: Session, autor: Usuario, vehiculo_id: int) -> Vehiculo:
    """Mark a vehicle as verified by the shop (RN-01 v1.0).

    This is the counter saying "the plate on the card is the plate on the car".
    It is the only way ``vehiculo.verificado`` becomes ``True``, and it is what
    ``settings.exigir_vehiculo_verificado`` gates the booking on when a shop
    decides to demand it.
    """
    vehiculo = vehiculo_repo.obtener_por_id(db, vehiculo_id)
    if vehiculo is None:
        raise RecursoNoEncontrado("No encontramos ese vehículo.")
    if vehiculo.verificado:
        return vehiculo

    vehiculo.verificado = True
    vehiculo.verificado_en = ahora_utc()
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_VEHICULO,
        vehiculo.id,
        eventos.VEHICULO_VERIFICADO,
        autor_id=autor.id,
        datos={"placa": vehiculo.placa},
    )
    db.commit()
    db.refresh(vehiculo)
    return vehiculo
