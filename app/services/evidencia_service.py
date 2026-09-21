"""Photographic evidence of a service (RF-023).

"Hasta seis fotografias antes y despues, con marca de tiempo y autor, visibles
por el cliente." Four parts, and three of them are easy:

* **the cap** is counted PER MOMENT (six before, six after). A single shared
  budget would let the "antes" half exhaust it and leave the "despues" side -
  the one CA-01 shows the customer - with nothing in it, which is the opposite
  of what the evidence is for;
* **the timestamp and the author** are columns, not metadata of the file: the
  moment recorded is when the photograph was TAKEN, which stops being the same
  instant as "when the bytes landed" the moment flow 4a happens;
* **the customer sees them** because the listing hangs off the reservation and
  reuses its horizontal authorization - somebody else's evidence answers 404
  for exactly the same reason somebody else's reservation does.

The fourth part is flow 4a, and it is the reason this module exists at all
instead of being three lines inside a router. "Fallo de carga -> evidencia
pendiente en el dispositivo con reintento automatico", and CA-02 completes the
sentence: "cuando se recupera la conexion, la evidencia se sube SIN
INTERVENCION". Compressing and retrying is the mobile client's job; what the
backend owes it is a row that can exist before its bytes do and an upload that
is safe to replay. Hence:

* ``contenido_base64`` is optional. Without it the row is born ``pendiente``:
  the shop can already see that a photograph is missing and whose it is;
* ``referencia_cliente`` is the device's own id for the picture. The same body
  sent twice completes the SAME row - it is an idempotency key by another
  name - so an automatic retry can be genuinely automatic and still not end up
  with seven photographs of the same bumper;
* a store that raises does NOT raise back. The row is marked ``fallida`` with
  the cause and the attempt count, and the response says so. Failing the
  request would throw away the metadata the device just managed to deliver,
  and then there would be nothing to retry against.
"""

import logging

from sqlalchemy.orm import Session

from app.core.errors import LimiteDeEvidencias, RecursoNoEncontrado, ServicioNoEnCurso, detalle
from app.core.horario import ahora_utc
from app.models import CarpetaArchivo, EstadoCargaEvidencia, Evidencia, Reserva, Usuario
from app.repositories import calidad as calidad_repo
from app.schemas import EvidenciaIn
from app.services import archivo_service, eventos, operacion_service
from app.services.proveedores.almacenamiento import ProveedorAlmacenamiento

logger = logging.getLogger("aqualav.evidencias")

#: RF-023: "hasta SEIS fotografias", counted per moment of the service.
MAXIMO_POR_MOMENTO = 6

#: Media type assumed when the device did not declare one. Practically every
#: phone camera produces this, and declaring something else is one field away.
MIME_PREDETERMINADO = "image/jpeg"


def listar(db: Session, reserva: Reserva) -> list[Evidencia]:
    """Every photograph of one service, oldest first (CA-01)."""
    return calidad_repo.listar_evidencias(db, reserva.id)


def obtener(db: Session, reserva: Reserva, evidencia_id: int) -> Evidencia:
    """One photograph of THIS reservation, or 404."""
    fila = calidad_repo.obtener_evidencia(db, evidencia_id)
    if fila is None or fila.reserva_id != reserva.id:
        # Same 404 as a missing row: an id must not be probeable.
        raise RecursoNoEncontrado("No encontramos esa evidencia en el servicio.")
    return fila


def _exigir_servicio_en_curso(db: Session, reserva: Reserva) -> None:
    """RF-023 precondition, asked of the table and not of a state name (P3).

    A service is in course while ``transicion_estado`` still declares a move
    out of where it is. A delivered or cancelled reservation has none, so it
    takes no more evidence - and a state added as data behaves correctly here
    without a line changing.
    """
    if operacion_service.es_terminal(db, reserva.estado):
        raise ServicioNoEnCurso(
            detalles=[detalle("estado", f"La reserva está en «{reserva.estado}», que ya es final.")]
        )


def _validar_contenido(datos: EvidenciaIn) -> tuple[bytes, str] | None:
    """Decode and check the picture BEFORE anything is written.

    Flow 3a and flow 4a are different failures and must not be confused: a file
    that is too big or is not an image will fail identically forever, so it is
    refused with 422 and its reason and NO row is opened for it. Only a store
    that could not take perfectly valid bytes is worth retrying, and that is
    what leaves an evidence row behind.
    """
    if not datos.contenido_base64:
        return None
    tipo = archivo_service.normalizar_mime(datos.mime or MIME_PREDETERMINADO)
    contenido = archivo_service.validar_tamano(archivo_service.decodificar(datos.contenido_base64))
    return contenido, tipo


def registrar(
    db: Session,
    reserva: Reserva,
    datos: EvidenciaIn,
    autor: Usuario,
    *,
    almacenamiento: ProveedorAlmacenamiento | None = None,
) -> tuple[Evidencia, bool]:
    """Record one photograph of the service. Returns ``(fila, creada)``.

    ``creada`` is ``False`` when the device was retrying a picture it had
    already registered (CA-02): the same ``referencia_cliente`` completes the
    row that is waiting instead of opening another one, so the router answers
    200 rather than 201 and nothing is duplicated.
    """
    _exigir_servicio_en_curso(db, reserva)

    # Refused up front, before a row exists: flow 3a, not flow 4a.
    archivo = _validar_contenido(datos)

    momento = ahora_utc()
    valor_momento = datos.momento.value
    existente = (
        calidad_repo.obtener_por_referencia(db, reserva.id, datos.referencia_cliente)
        if datos.referencia_cliente
        else None
    )

    if existente is None:
        registradas = calidad_repo.contar_por_momento(db, reserva.id, valor_momento)
        if registradas >= MAXIMO_POR_MOMENTO:
            raise LimiteDeEvidencias(
                detalles=[
                    detalle("momento", f"Ya hay {registradas} fotografías de «{valor_momento}»."),
                    detalle("maximo", str(MAXIMO_POR_MOMENTO)),
                ]
            )
        fila = calidad_repo.crear_evidencia(
            db,
            reserva_id=reserva.id,
            momento=valor_momento,
            autor_id=autor.id,
            estado_carga=EstadoCargaEvidencia.PENDIENTE.value,
            observacion=datos.observacion,
            referencia_cliente=datos.referencia_cliente,
            registrada_en=momento,
        )
        creada = True
    else:
        fila = existente
        creada = False
        if datos.observacion is not None:
            fila.observacion = datos.observacion
        if fila.estado_carga == EstadoCargaEvidencia.SUBIDA.value:
            # Already delivered: a retry that arrives late changes nothing.
            db.commit()
            db.refresh(fila)
            return fila, False

    fallo: str | None = None
    if archivo is not None:
        contenido, tipo = archivo
        try:
            clave = archivo_service.guardar(
                contenido,
                tipo,
                CarpetaArchivo.EVIDENCIAS,
                prefijo=str(reserva.id),
                almacenamiento=almacenamiento,
            )
        except Exception as error:  # noqa: BLE001 - flow 4a: never lose the row
            fallo = str(error) or error.__class__.__name__
            logger.warning(
                "evidencia %s de la reserva %s no se pudo subir: %s", fila.id, reserva.id, fallo
            )
            calidad_repo.anotar_carga(
                db,
                fila,
                estado_carga=EstadoCargaEvidencia.FALLIDA.value,
                error=fallo[:300],
            )
        else:
            calidad_repo.anotar_carga(
                db,
                fila,
                estado_carga=EstadoCargaEvidencia.SUBIDA.value,
                objeto_key=clave,
                mime=tipo,
                tamano_bytes=len(contenido),
                subida_en=momento,
                error=None,
            )

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.EVIDENCIA_REGISTRADA if fallo is None else eventos.EVIDENCIA_NO_SUBIDA,
        autor_id=autor.id,
        datos={
            "evidencia_id": fila.id,
            "momento": fila.momento,
            "estado_carga": fila.estado_carga,
            "intentos": fila.intentos,
            "objeto_key": fila.objeto_key,
            "causa": fallo[:300] if fallo else None,
        },
    )

    db.commit()
    db.refresh(fila)
    return fila, creada
