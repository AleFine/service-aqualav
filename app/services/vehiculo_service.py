"""Vehicle registration (RF-007)."""

from sqlalchemy.orm import Session

from app.core.errors import PlacaDuplicada, detalle
from app.models import Usuario, Vehiculo
from app.repositories import vehiculo as vehiculo_repo
from app.schemas import VehiculoIn
from app.services import eventos


def normalizar_placa(placa: str) -> str:
    """Uppercase and blank-free, the form the unique key is built on."""
    return "".join((placa or "").split()).upper()


def listar(db: Session, usuario: Usuario) -> list[Vehiculo]:
    """The caller's own vehicles.

    There is no "read every vehicle" permission in the MVP, so this is always
    scoped to the caller (contract section 3, horizontal authorization).
    """
    return vehiculo_repo.listar_por_usuario(db, usuario.id)


def crear(db: Session, usuario: Usuario, datos: VehiculoIn) -> Vehiculo:
    """Register a vehicle for the caller.

    The plate format was validated by the schema (422 ``PLACA_INVALIDA``); the
    rule left here is uniqueness inside the account (RF-007 CA-02 -> 409).
    """
    placa = normalizar_placa(datos.placa)

    if vehiculo_repo.obtener_por_usuario_y_placa(db, usuario.id, placa) is not None:
        raise PlacaDuplicada(
            detalles=[detalle("placa", f"El vehículo con placa {placa} ya está en tu cuenta.")]
        )

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
