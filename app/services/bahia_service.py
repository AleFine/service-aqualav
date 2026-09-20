"""Bays: administration and physical occupancy (RF-020 CA-01, RE-07).

This module sits BELOW the operation services on purpose: it knows how to
occupy and release a bay and nothing about the state machine, which is what
lets ``operacion_service`` release the resources of a reservation that reached
a terminal state without either module importing the other.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.errors import (
    BahiaConReservas,
    LimiteDeBahias,
    NombreDeBahiaDuplicado,
    RecursoNoEncontrado,
    detalle,
)
from app.core.horario import ahora_utc
from app.models import MAXIMO_BAHIAS, Bahia, EstadoBahia, Reserva, Usuario
from app.repositories import asignacion as asignacion_repo
from app.repositories import bahia as bahia_repo
from app.repositories import reserva as reserva_repo
from app.repositories import transicion as transicion_repo
from app.schemas import BahiaActualizar, BahiaCrear
from app.services import eventos

#: The permission that guards the bay administration (principle P5).
PERMISO_ADMINISTRAR = "bahia:administrar"


# --------------------------------------------------------------------------
# Occupancy (RF-020)
# --------------------------------------------------------------------------
def ocupar(db: Session, bahia: Bahia) -> None:
    """Mark a bay as physically holding a vehicle (RF-020 CA-01)."""
    bahia.estado = EstadoBahia.OCUPADA.value
    db.flush()


def liberar(db: Session, bahia: Bahia) -> None:
    bahia.estado = EstadoBahia.LIBRE.value
    db.flush()


def liberar_recursos(db: Session, reserva: Reserva) -> None:
    """Give back everything a reservation was holding: its bay and its slot.

    Called when a reservation reaches a TERMINAL state - the delivery of
    RF-024 and the cancellation of RF-016 alike. Which states are terminal is
    never listed here: the caller derives it from ``transicion_estado`` (P3),
    so a state added as data releases the bay for free.
    """
    asignacion = asignacion_repo.obtener_por_reserva(db, reserva.id)
    if asignacion is not None:
        bahia = bahia_repo.obtener_por_id(db, asignacion.bahia_id)
        if bahia is not None:
            liberar(db, bahia)
        asignacion_repo.eliminar(db, asignacion)

    en_cola = asignacion_repo.obtener_cola_por_reserva(db, reserva.id)
    if en_cola is not None:
        asignacion_repo.desencolar(db, en_cola)


def libres_para(
    db: Session,
    inicio: datetime,
    fin: datetime,
    estados_activos: set[str],
    *,
    bloqueadas: set[int] | None = None,
    excluir_reserva_id: int | None = None,
) -> list[Bahia]:
    """Active bays able to take a service running in ``[inicio, fin)``.

    A bay is unavailable when it is physically holding another service, when a
    blocking covers the slot (RF-018), or when another active reservation has
    that block booked (RN-03).
    """
    ocupadas = set(asignacion_repo.bahias_asignadas(db, estados_activos))
    ocupadas |= set(bloqueadas or ())

    # The reservation being assigned holds its own booked bay: it must not
    # count itself as the reason that bay is unavailable.
    reservadas = {
        otra.bahia_id
        for otra in reserva_repo.listar_activas_de_bahia(db, None, inicio, fin, estados_activos)
        if otra.id != excluir_reserva_id
    }

    return [
        bahia
        for bahia in bahia_repo.listar_activas(db)
        if bahia.id not in ocupadas and bahia.id not in reservadas
    ]


# --------------------------------------------------------------------------
# Administration (gap closed by INC-1B)
# --------------------------------------------------------------------------
def listar(db: Session) -> list[Bahia]:
    """Every bay, active or not (the administration screen sees them all)."""
    return bahia_repo.listar_todas(db)


def obtener(db: Session, bahia_id: int) -> Bahia:
    bahia = bahia_repo.obtener_por_id(db, bahia_id)
    if bahia is None:
        raise RecursoNoEncontrado("No encontramos esa bahía.")
    return bahia


def crear(db: Session, datos: BahiaCrear, autor: Usuario) -> Bahia:
    """Register a bay. RE-07 caps how many of them can be active at once."""
    nombre = datos.nombre.strip()
    if bahia_repo.obtener_por_nombre(db, nombre) is not None:
        raise NombreDeBahiaDuplicado(detalles=[detalle("nombre", "Ese nombre ya está en uso.")])

    if datos.activa and bahia_repo.contar_activas(db) >= MAXIMO_BAHIAS:
        raise LimiteDeBahias(
            detalles=[detalle("activa", f"El local tiene {MAXIMO_BAHIAS} bahías físicas.")]
        )

    bahia = bahia_repo.crear(db, nombre=nombre, activa=datos.activa, estado=EstadoBahia.LIBRE.value)
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_BAHIA,
        bahia.id,
        eventos.BAHIA_CREADA,
        autor_id=autor.id,
        datos={"nombre": bahia.nombre, "activa": bahia.activa},
    )
    db.commit()
    db.refresh(bahia)
    return bahia


def actualizar(db: Session, bahia_id: int, datos: BahiaActualizar, autor: Usuario) -> Bahia:
    """Rename, activate, deactivate or manually free a bay.

    Deactivating one that still has active reservations is refused: those
    bookings would silently lose the place they were promised (RN-03).
    """
    bahia = obtener(db, bahia_id)
    cambios: dict[str, object] = {}

    if datos.nombre is not None and datos.nombre.strip() != bahia.nombre:
        nombre = datos.nombre.strip()
        existente = bahia_repo.obtener_por_nombre(db, nombre)
        if existente is not None and existente.id != bahia.id:
            raise NombreDeBahiaDuplicado(detalles=[detalle("nombre", "Ese nombre ya está en uso.")])
        cambios["nombre"] = {"anterior": bahia.nombre, "nuevo": nombre}
        bahia.nombre = nombre

    if datos.activa is not None and datos.activa != bahia.activa:
        if not datos.activa:
            estados_activos = transicion_repo.listar_estados_no_terminales(db)
            if reserva_repo.existe_activa_de_bahia(db, bahia.id, ahora_utc(), estados_activos):
                raise BahiaConReservas(
                    detalles=[detalle("activa", "Reubica o cierra sus reservas activas primero.")]
                )
        elif bahia_repo.contar_activas(db) >= MAXIMO_BAHIAS:
            raise LimiteDeBahias(
                detalles=[detalle("activa", f"El local tiene {MAXIMO_BAHIAS} bahías físicas.")]
            )
        cambios["activa"] = {"anterior": bahia.activa, "nuevo": datos.activa}
        bahia.activa = datos.activa

    if datos.estado is not None and datos.estado.value != bahia.estado:
        cambios["estado"] = {"anterior": bahia.estado, "nuevo": datos.estado.value}
        bahia.estado = datos.estado.value

    if cambios:
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_BAHIA,
            bahia.id,
            eventos.BAHIA_ACTUALIZADA,
            autor_id=autor.id,
            datos=cambios,
        )
        db.commit()
        db.refresh(bahia)
    return bahia
