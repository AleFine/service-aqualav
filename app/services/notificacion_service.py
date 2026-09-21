"""Notifications of the service lifecycle (RF-029).

One entry point, :func:`despachar`, and four rules behind it:

1. **The text comes from a template**, never from string concatenation in a
   service. It is looked up in ``plantilla_notificacion`` by ``(evento, canal,
   idioma)`` and filled in with the reservation's own data. Changing the
   wording, or adding a language, is an UPDATE.
2. **The channel set is data too.** A channel is used when a template exists
   for that event on it AND the customer's preference allows it. That is why
   an internal move only touches the in-app feed while a finished service also
   mails and pushes: the difference is which rows the template table holds, not
   an ``if`` in this module.
3. **Every delivery is a row** with its attempts and, when it ends badly, the
   cause (CA-02). ``estado_envio`` says how it went and ``enviado_en`` is what
   CA-01 measures against.
4. **Up to three retries with an incremental wait** (flow 3a). The wait is
   injected so the suite exercises the backoff without sleeping.

Preferences (RF-029 flow 3b) are read from ``usuario``: ``notificar_push``,
``notificar_correo`` and ``idioma``, the columns INC-3 created. There is no
``preferencia_notificacion`` table on purpose - two places holding the same
switch is how they start disagreeing. Per-event granularity, if it is ever
needed, is a row that OVERRIDES these, not a copy of them.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import RecursoNoEncontrado
from app.core.horario import a_lima, ahora_utc, desde_bd
from app.models import (
    CanalNotificacion,
    Dispositivo,
    EstadoEnvio,
    Idioma,
    Notificacion,
    Reserva,
    Usuario,
)
from app.repositories import notificacion as notificacion_repo
from app.schemas import DispositivoIn
from app.services import eventos
from app.services.notificador import Notificador, notificador_en_app
from app.services.proveedores.correo import ProveedorCorreo, proveedor_correo
from app.services.proveedores.push import ProveedorPush, proveedor_push

logger = logging.getLogger("aqualav.notificaciones")

#: RF-029 flow 3a: "hasta TRES reintentos con espera incremental", so a
#: delivery is attempted once and then retried at most three more times.
REINTENTOS_MAXIMOS = 3
INTENTOS_MAXIMOS = REINTENTOS_MAXIMOS + 1

#: Incremental backoff: 1 s, then 2 s, then 4 s. Only a FAILING provider ever
#: waits, and the simulation only fails when it is told to.
ESPERA_INICIAL_SEGUNDOS = 1.0
FACTOR_ESPERA = 2.0

#: RF-029 CA-01: a state change has to reach the customer in under a minute.
#: The dispatch is synchronous, so the budget is a ceiling the tests assert
#: against ``enviado_en - ocurrido_en``, never a timer the code waits on.
LATENCIA_MAXIMA = timedelta(seconds=60)

#: Format used for every date a template interpolates.
FORMATO_FECHA = "%d/%m/%Y %H:%M"

#: Builders of a channel implementation, bound to the row they deliver to.
FabricaCorreo = Callable[..., ProveedorCorreo]
FabricaPush = Callable[..., ProveedorPush]
FabricaEnApp = Callable[..., Notificador]


def _dormir(segundos: float) -> None:  # pragma: no cover - replaced in the suite
    """The incremental wait of flow 3a. Injected so tests never really sleep."""
    time.sleep(segundos)


def esperas() -> list[float]:
    """The wait before each retry, in seconds: ``[1.0, 2.0, 4.0]``."""
    return [ESPERA_INICIAL_SEGUNDOS * FACTOR_ESPERA**indice for indice in range(REINTENTOS_MAXIMOS)]


class _Contexto(dict):
    """``format_map`` source that renders an unknown placeholder as empty text.

    A template is data and may name a field the caller did not provide. That
    must never be the reason a customer is not told their car is ready.
    """

    def __missing__(self, clave: str) -> str:
        return ""


@dataclass(frozen=True)
class Mensaje:
    """The text of one notice, already rendered."""

    asunto: str
    cuerpo: str


@dataclass(frozen=True)
class Envio:
    """One channel to deliver on, and where."""

    canal: str
    destino: str


def _idioma_de(usuario: Usuario) -> str:
    return usuario.idioma or Idioma.ES.value


def _fecha(momento: datetime | None) -> str:
    if momento is None:
        return ""
    return a_lima(desde_bd(momento)).strftime(FORMATO_FECHA)


def datos_de_reserva(reserva: Reserva) -> dict[str, Any]:
    """What every template about a reservation may interpolate.

    Deliberately flat and deliberately stringly typed: a template is text that
    somebody edits in a table, not code.
    """
    return {
        "codigo": reserva.codigo,
        "estado": reserva.estado,
        "servicio": reserva.servicio.nombre if reserva.servicio is not None else "",
        "placa": reserva.vehiculo.placa if reserva.vehiculo is not None else "",
        "bahia": reserva.bahia.nombre if reserva.bahia is not None else "",
        "inicio": _fecha(reserva.inicio),
        "fin": _fecha(reserva.fin),
        "entrega": _fecha(reserva.hora_estimada_entrega or reserva.fin),
        "cliente": reserva.usuario.nombres if reserva.usuario is not None else "",
    }


def _mensaje_generico(evento: str, datos: dict[str, Any]) -> Mensaje:
    """Fallback for an event nobody wrote a template for yet.

    It keeps a notification that has no row in ``plantilla_notificacion`` from
    disappearing silently, which matters because the template table is data and
    a deployment may legitimately be missing one.
    """
    codigo = datos.get("codigo") or ""
    referencia = f" {codigo}".rstrip()
    return Mensaje(
        asunto=f"Novedad de tu reserva{referencia}",
        cuerpo=f"Tu reserva{referencia} registró el evento «{evento}».",
    )


def redactar(db: Session, evento: str, canal: str, idioma: str, datos: dict[str, Any]) -> Mensaje:
    """Render the template of ``(evento, canal, idioma)`` with ``datos``."""
    plantilla = notificacion_repo.obtener_plantilla(db, evento, canal, idioma)
    if plantilla is None and idioma != Idioma.ES.value:
        # Spanish is the shop's language: falling back to it beats not writing.
        plantilla = notificacion_repo.obtener_plantilla(db, evento, canal, Idioma.ES.value)
    if plantilla is None:
        return _mensaje_generico(evento, datos)

    contexto = _Contexto(datos)
    return Mensaje(
        asunto=plantilla.asunto.format_map(contexto),
        cuerpo=plantilla.cuerpo.format_map(contexto),
    )


def canales_de(db: Session, usuario: Usuario, evento: str) -> list[Envio]:
    """Which channels this event reaches this person on, and where.

    The in-app feed is unconditional: it is the notice the customer finds in
    the application, it cannot fail, and it is what RF-022 polls. The other two
    need BOTH a template for the event and the customer's own switch, which is
    flow 3b read literally - turn push off and only the mail goes out.
    """
    idioma = _idioma_de(usuario)

    def _hay_plantilla(canal: str) -> bool:
        if notificacion_repo.obtener_plantilla(db, evento, canal, idioma) is not None:
            return True
        if idioma == Idioma.ES.value:
            return False
        return notificacion_repo.obtener_plantilla(db, evento, canal, Idioma.ES.value) is not None

    envios: list[Envio] = []

    if _hay_plantilla(CanalNotificacion.EN_APP.value):
        envios.append(Envio(CanalNotificacion.EN_APP.value, usuario.correo))

    if usuario.notificar_correo and _hay_plantilla(CanalNotificacion.CORREO.value):
        envios.append(Envio(CanalNotificacion.CORREO.value, usuario.correo))

    if usuario.notificar_push and _hay_plantilla(CanalNotificacion.PUSH.value):
        envios.extend(
            Envio(CanalNotificacion.PUSH.value, dispositivo.token_push)
            for dispositivo in notificacion_repo.listar_dispositivos(db, usuario.id)
        )

    return envios


def _entregar(
    db: Session,
    fila: Notificacion,
    enviar: Callable[[], None],
    *,
    espera: Callable[[float], None],
    momento: datetime,
) -> Notificacion:
    """Try a delivery, retrying with an incremental wait (RF-029 flow 3a).

    The provider is bound to ``fila``, so IT is what records each attempt. The
    closing stamp here is defensive: a caller that injects an unbound provider
    still gets an honest ``estado_envio``.
    """
    ultima: str | None = None
    pausas = esperas()

    for intento in range(1, INTENTOS_MAXIMOS + 1):
        try:
            enviar()
        except Exception as error:  # noqa: BLE001 - every provider failure is the same here
            ultima = str(error) or error.__class__.__name__
            logger.warning(
                "envio fallido canal=%s notificacion=%s intento=%s causa=%s",
                fila.canal,
                fila.id,
                intento,
                ultima,
            )
            if intento < INTENTOS_MAXIMOS:
                espera(pausas[intento - 1])
            continue

        if fila.estado_envio == EstadoEnvio.PENDIENTE.value:
            notificacion_repo.cerrar(
                db, fila, estado_envio=EstadoEnvio.ENVIADA.value, momento=momento
            )
        return fila

    # CA-02: the retries are exhausted and the CAUSE stays on the record.
    notificacion_repo.cerrar(
        db,
        fila,
        estado_envio=EstadoEnvio.FALLIDA.value,
        momento=momento,
        error=(ultima or "El proveedor no pudo entregar el mensaje.")[:300],
    )
    return fila


def despachar(
    db: Session,
    usuario: Usuario,
    evento: str,
    *,
    reserva: Reserva | None = None,
    datos: dict[str, Any] | None = None,
    momento: datetime | None = None,
    correo: FabricaCorreo = proveedor_correo,
    push: FabricaPush = proveedor_push,
    en_app: FabricaEnApp = notificador_en_app,
    espera: Callable[[float], None] = _dormir,
) -> list[Notificacion]:
    """Send one lifecycle event to one person on every channel they allow.

    Returns the rows it wrote, already flushed, so the caller can assert on
    them. It never raises: a notification must not roll back the business
    transaction that produced it, and a provider that keeps failing ends as a
    ``fallida`` row with its cause, which is precisely what RF-029 asks for.
    """
    evento = getattr(evento, "value", evento)
    momento = momento or ahora_utc()
    contexto: dict[str, Any] = dict(datos or {})
    if reserva is not None:
        contexto = {**datos_de_reserva(reserva), **contexto}

    filas: list[Notificacion] = []
    for envio in canales_de(db, usuario, evento):
        mensaje = redactar(db, evento, envio.canal, _idioma_de(usuario), contexto)
        fila = notificacion_repo.crear(
            db,
            usuario_id=usuario.id,
            reserva_id=reserva.id if reserva is not None else None,
            evento=evento,
            canal=envio.canal,
            destino=envio.destino,
            asunto=mensaje.asunto[:160],
            cuerpo=mensaje.cuerpo[:1000],
            estado_envio=EstadoEnvio.PENDIENTE.value,
            creada_en=momento,
        )

        if envio.canal == CanalNotificacion.CORREO.value:
            proveedor = correo(sesion=db, notificacion=fila)

            def _enviar(proveedor=proveedor, envio=envio, mensaje=mensaje) -> None:
                proveedor.enviar(envio.destino, mensaje.asunto, mensaje.cuerpo)

        elif envio.canal == CanalNotificacion.PUSH.value:
            proveedor = push(sesion=db, notificacion=fila)

            def _enviar(proveedor=proveedor, envio=envio, mensaje=mensaje) -> None:
                proveedor.enviar(envio.destino, mensaje.asunto, mensaje.cuerpo)

        else:
            proveedor = en_app(sesion=db, notificacion=fila)

            def _enviar(proveedor=proveedor, mensaje=mensaje, contexto=contexto) -> None:
                proveedor.notificar(usuario.id, mensaje.asunto, mensaje.cuerpo, contexto)

        filas.append(_entregar(db, fila, _enviar, espera=espera, momento=momento))

    return filas


# --------------------------------------------------------------------------
# Devices and the in-app feed (RF-029 precondition, RF-022)
# --------------------------------------------------------------------------
def registrar_dispositivo(db: Session, usuario: Usuario, datos: DispositivoIn) -> Dispositivo:
    """Authorize one device to receive pushes (RF-029 precondition).

    Idempotent by token: re-registering the same one revives it and moves it to
    whoever is asking, which is what happens when the app is reinstalled or the
    phone changes hands. The token NEVER reaches the event log: it is a
    delivery credential (RNF-014).
    """
    momento = ahora_utc()
    fila = notificacion_repo.guardar_dispositivo(
        db,
        usuario_id=usuario.id,
        token_push=datos.token_push.strip(),
        plataforma=datos.plataforma.value,
        registrado_en=momento,
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        eventos.USUARIO_DISPOSITIVO_REGISTRADO,
        autor_id=usuario.id,
        datos={"dispositivo_id": fila.id, "plataforma": fila.plataforma},
    )
    db.commit()
    db.refresh(fila)
    return fila


def listar_dispositivos(db: Session, usuario: Usuario) -> list[Dispositivo]:
    """The devices this person has authorized, active ones only."""
    return notificacion_repo.listar_dispositivos(db, usuario.id)


def dar_de_baja_dispositivo(db: Session, usuario: Usuario, dispositivo_id: int) -> None:
    """Stop pushing to one device (the customer signed out of it)."""
    fila = notificacion_repo.obtener_dispositivo(db, dispositivo_id)
    if fila is None or fila.usuario_id != usuario.id:
        # Same 404 as a missing row: a token id must not be probeable.
        raise RecursoNoEncontrado("No encontramos ese dispositivo en tu cuenta.")

    notificacion_repo.desactivar_dispositivo(db, fila)
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        eventos.USUARIO_DISPOSITIVO_DADO_DE_BAJA,
        autor_id=usuario.id,
        datos={"dispositivo_id": fila.id},
    )
    db.commit()


def bandeja(
    db: Session, usuario: Usuario, *, limite: int = notificacion_repo.LIMITE_BANDEJA
) -> list[Notificacion]:
    """Everything this person was notified of, newest first (RF-022).

    It is the whole record and not only the in-app channel on purpose: "se
    registra el resultado del envío para trazabilidad" (RF-029) is only useful
    if somebody can read it back.
    """
    return notificacion_repo.listar_por_usuario(db, usuario.id, limite=limite)
