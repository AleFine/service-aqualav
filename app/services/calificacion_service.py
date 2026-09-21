"""Service rating and its window (RF-031, RN-10).

The whole increment turns on one sentence of RN-10: "el cliente solo puede
calificar servicios en estado Entregado y dentro de los SIETE DIAS CALENDARIO
siguientes". Two facts are needed to enforce it and neither can be recovered
later, which is why INC-1B wrote the hook and this module reads it:

* **when the window opened.** Not ``reserva.hora_entrega``, which is a column
  somebody can correct, and not "the last history row", which says the state
  changed but not that the window was opened by it. It is the
  ``reserva.calificacion_habilitada`` event, appended once, never updated;
* **who worked the service.** ``asignacion_servicio`` is DELETED the moment
  the reservation becomes terminal (``bahia_service.liberar_recursos``), and
  the delivery is exactly what makes it terminal. The operator is therefore
  captured into the event while the row still exists, and the rating reads it
  back from there.

Neither :func:`habilitar` nor :func:`ventana` compares a state name: "the
service was delivered" IS "the event exists" (principle P3). A twelfth state
added as data, or a second delivery route, changes nothing here as long as the
operation that opens the window keeps calling :func:`habilitar`.

Flow 3b - "si ya existe, se muestra en modo lectura" - is answered the way
RF-026 answers a replayed idempotency key: the second POST returns **200 with
the rating on record** instead of 409. Refusing outright would make the app
show an error for something that is not one; the customer already spoke, and
the only thing left to do is show them what they said.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.errors import (
    CalificacionNoHabilitada,
    PermisoDenegado,
    PlazoDeCalificacionVencido,
    detalle,
)
from app.core.horario import a_lima, ahora_utc, desde_bd
from app.models import Calificacion, EventoNotificacion, Reserva, Usuario
from app.repositories import asignacion as asignacion_repo
from app.repositories import calidad as calidad_repo
from app.repositories import evento as evento_repo
from app.repositories import servicio as servicio_repo
from app.repositories import usuario as usuario_repo
from app.schemas import CalificacionIn
from app.services import eventos, notificacion_service

#: RN-10: "dentro de los 7 dias CALENDARIO siguientes". Calendar and not
#: working days, so weekends and holidays count, which is what a plain
#: seven-day delta means.
VENTANA_CALIFICACION = timedelta(days=7)

#: Format the window deadline is interpolated into a template with.
FORMATO_FECHA = "%d/%m/%Y %H:%M"

MOTIVO_NO_ENTREGADO = "Podrás calificar este servicio cuando te entreguemos el vehículo."
MOTIVO_VENCIDO = "El plazo de 7 días para calificar este servicio ya venció."
MOTIVO_YA_CALIFICADO = "Ya calificaste este servicio. Puedes consultarlo, no modificarlo."


@dataclass(frozen=True)
class Ventana:
    """The rating window of one reservation, as a screen needs to see it."""

    habilitada_en: datetime | None
    vence_en: datetime | None
    operario_id: int | None
    calificacion: Calificacion | None

    @property
    def abierta(self) -> bool:
        """Whether a rating may still be written right now."""
        return self.habilitada_en is not None and not self.vencida and self.calificacion is None

    @property
    def vencida(self) -> bool:
        return self.vence_en is not None and ahora_utc() > self.vence_en

    @property
    def motivo(self) -> str | None:
        """Why it is not open, in Spanish, ready to render."""
        if self.habilitada_en is None:
            return MOTIVO_NO_ENTREGADO
        if self.calificacion is not None:
            return MOTIVO_YA_CALIFICADO
        if self.vencida:
            return MOTIVO_VENCIDO
        return None


def _instante(valor: object) -> datetime | None:
    """Read back an ISO timestamp the event log stored as text.

    ``evento_dominio.datos`` is JSON, so every instant in it is a string
    (``eventos.registrar_evento`` says so). A value written before timezones
    were aware anywhere is read as UTC rather than dropped.
    """
    if not isinstance(valor, str) or not valor:
        return None
    try:
        momento = datetime.fromisoformat(valor)
    except ValueError:  # pragma: no cover - only a hand edited event row
        return None
    return momento if momento.tzinfo is not None else momento.replace(tzinfo=UTC)


def habilitar(
    db: Session,
    reserva: Reserva,
    autor: Usuario | None,
    *,
    momento: datetime | None = None,
    **proveedores,
) -> datetime:
    """Open the rating window of a delivered service (RF-024 step 4).

    Called from the operation that hands the vehicle back, at the point where
    INC-1B left the hook. It does the two things that cannot be done later:

    1. **appends the event** that RN-10 counts from, with the operator snapshot
       taken while ``asignacion_servicio`` still exists;
    2. **tells the customer** through ``notificacion_service.despachar`` with
       the ``calificacion`` event. A template and an event, which is what INC-5
       asked for - no message is built by hand here, and turning the notice
       off, translating it or moving it to another channel stays an UPDATE on
       ``plantilla_notificacion``.

    Returns the instant the window opened, which is also its only record.
    """
    habilitada_en = momento or ahora_utc()
    vence_en = habilitada_en + VENTANA_CALIFICACION

    asignacion = asignacion_repo.obtener_por_reserva(db, reserva.id)
    operario_id = asignacion.operario_id if asignacion is not None else None

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.RESERVA_CALIFICACION_HABILITADA,
        autor_id=autor.id if autor else None,
        datos={
            "habilitada_en": habilitada_en.isoformat(),
            "vence_en": vence_en.isoformat(),
            "dias": VENTANA_CALIFICACION.days,
            "operario_id": operario_id,
        },
    )

    notificacion_service.despachar(
        db,
        reserva.usuario,
        EventoNotificacion.CALIFICACION.value,
        reserva=reserva,
        datos={
            "dias": VENTANA_CALIFICACION.days,
            "vence_calificacion": a_lima(vence_en).strftime(FORMATO_FECHA),
        },
        momento=habilitada_en,
        **proveedores,
    )
    return habilitada_en


def ventana(db: Session, reserva: Reserva) -> Ventana:
    """The state of the rating window of one reservation (RN-10).

    ``habilitada_en is None`` means the service was never delivered, which is
    the P3-safe way of asking "is it in the Entregado state?" - the answer is
    the event the delivery wrote, not the name of the state it landed on.
    """
    evento = evento_repo.obtener_ultimo(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.RESERVA_CALIFICACION_HABILITADA,
    )
    existente = calidad_repo.obtener_calificacion(db, reserva.id)

    if evento is None:
        return Ventana(None, None, None, existente)

    datos = evento.datos or {}
    habilitada_en = _instante(datos.get("habilitada_en")) or desde_bd(evento.ocurrido_en)
    vence_en = _instante(datos.get("vence_en")) or (habilitada_en + VENTANA_CALIFICACION)
    operario_id = datos.get("operario_id")

    return Ventana(
        habilitada_en=habilitada_en,
        vence_en=vence_en,
        operario_id=int(operario_id) if operario_id else None,
        calificacion=existente,
    )


def obtener(db: Session, reserva: Reserva) -> Calificacion | None:
    """The rating on record, or ``None`` (RF-031 flow 3b, read only)."""
    return calidad_repo.obtener_calificacion(db, reserva.id)


def _refrescar_promedios(db: Session, fila: Calificacion) -> None:
    """Recompute the averages RF-031 says must stay updated.

    Recomputed from ``calificacion`` rather than incremented, so the pair
    (count, sum) can never drift away from the rows it summarises - and so a
    rating inserted by a migration or a fixture is picked up for free. It is
    two indexed aggregates over a very small table.
    """
    conteo, suma = calidad_repo.agregado_por_servicio(db, fila.servicio_id)
    servicio = servicio_repo.obtener_por_id(db, fila.servicio_id)
    if servicio is not None:
        servicio.calificaciones_count = conteo
        servicio.calificaciones_suma = suma

    if fila.operario_id is None:
        return
    conteo, suma = calidad_repo.agregado_por_operario(db, fila.operario_id)
    operario = usuario_repo.obtener_por_id(db, fila.operario_id)
    if operario is not None:
        operario.calificaciones_count = conteo
        operario.calificaciones_suma = suma


def calificar(
    db: Session,
    reserva: Reserva,
    usuario: Usuario,
    datos: CalificacionIn,
) -> tuple[Calificacion, bool]:
    """Register the customer's verdict (RF-031). Returns ``(fila, creada)``.

    ``creada`` is ``False`` when there already was one: flow 3b asks for the
    existing rating to be SHOWN, not for the request to be refused, so the
    router answers 200 with it instead of 201 and the app renders it read-only.

    Order of the checks, so the reason that comes back is the most useful one:

    1. an existing rating wins over everything (3b) - including over an expired
       window, because a customer who rated on day two must still be able to
       read what they wrote on day thirty;
    2. the service has to have been delivered (RN-10, through the event);
    3. the seven calendar days have to still be running (RN-10, flow 3a);
    4. only the customer of the service rates it (RF-031 actor).
    """
    estado = ventana(db, reserva)

    if estado.calificacion is not None:
        return estado.calificacion, False

    if estado.habilitada_en is None:
        raise CalificacionNoHabilitada(detalles=[detalle("reserva_id", MOTIVO_NO_ENTREGADO)])

    if estado.vencida:
        raise PlazoDeCalificacionVencido(
            detalles=[
                detalle("reserva_id", MOTIVO_VENCIDO),
                detalle("vence_en", a_lima(estado.vence_en).strftime(FORMATO_FECHA)),
            ]
        )

    if reserva.usuario_id != usuario.id:
        # RF-031's actor is the CUSTOMER. Comparing ids, never a role name (P5).
        raise PermisoDenegado(
            "Solo el cliente del servicio puede calificarlo.",
            detalles=[detalle("reserva_id", "La reserva pertenece a otro cliente.")],
        )

    fila = calidad_repo.crear_calificacion(
        db,
        reserva_id=reserva.id,
        usuario_id=usuario.id,
        operario_id=estado.operario_id,
        servicio_id=reserva.servicio_id,
        puntuacion=datos.puntuacion,
        comentario=datos.comentario,
        creada_en=ahora_utc(),
    )

    _refrescar_promedios(db, fila)

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.RESERVA_CALIFICADA,
        autor_id=usuario.id,
        datos={
            "calificacion_id": fila.id,
            "puntuacion": fila.puntuacion,
            "servicio_id": fila.servicio_id,
            "operario_id": fila.operario_id,
            "con_comentario": bool(fila.comentario),
        },
    )

    db.commit()
    db.refresh(fila)
    return fila, True
