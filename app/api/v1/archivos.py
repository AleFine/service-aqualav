"""Generic object upload and serving (RF-006, RF-009, RF-023, plan section 4).

The door INC-4 left open. ``ProveedorAlmacenamiento`` was born there for the
receipts of RF-027 and reached the API through a single, receipt-shaped route;
three columns that already held keys had none at all. This router is the
generic one they were waiting for: ``usuario.foto_perfil_key`` (RF-006),
``servicio.imagen_url`` (RF-009) and ``evidencia.objeto_key`` (RF-023) are all
served from here, and the upload returns the key their owners store.

Two notes on the authorization, because they are decisions and not oversights:

* **uploading** needs ``archivo:subir``, which every role holds. The permission
  exists so the gate is a code like every other one (P5) and so revoking it for
  a role is data, not a deploy;
* **reading** needs a session and nothing more, the way ``GET /estados`` does.
  A key is an opaque capability - a random 32 hex character name the caller
  never chooses - so holding the URL is what proves entitlement to the bytes,
  and the URL only ever travels inside a payload the reader was allowed to see.
  It is the weakest link of this increment and it is deliberate: the alternative
  is teaching this endpoint who owns which key, which would put the evidence
  rules, the profile rules and the catalogue rules in a second place.
"""

from fastapi import APIRouter, Depends, status
from fastapi.responses import Response as RespuestaBinaria

from app.deps import requiere_permiso, usuario_actual
from app.models import Usuario
from app.schemas import ArchivoOut, ArchivoSubirIn, ErrorBody
from app.services import archivo_service

router = APIRouter(prefix="/archivos", tags=["archivos"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.post(
    "",
    response_model=ArchivoOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Subir un archivo al almacenamiento",
)
def subir(
    datos: ArchivoSubirIn,
    _: Usuario = Depends(requiere_permiso("archivo:subir")),
) -> ArchivoOut:
    """Sube una imagen y devuelve su **clave** y su URL.

    La clave es lo que se guarda en `usuario.foto_perfil_key` (RF-006) o en
    `servicio.imagen_url` (RF-009); la URL es donde este mismo router la sirve.
    Un archivo por encima del límite o que no sea una imagen se rechaza con
    `422 ARCHIVO_RECHAZADO` indicando el motivo (RF-023 `3a`).
    """
    clave, contenido, mime = archivo_service.subir(
        datos.contenido_base64, datos.mime, datos.carpeta
    )
    return ArchivoOut(
        clave=clave,
        url=archivo_service.url(clave),
        mime=mime,
        tamano_bytes=len(contenido),
    )


@router.get(
    "/{clave:path}",
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "El archivo almacenado."},
        **RESPUESTAS,
    },
    summary="Descargar un archivo del almacenamiento",
)
def descargar(
    clave: str,
    _: Usuario = Depends(usuario_actual),
) -> RespuestaBinaria:
    """Sirve el objeto guardado bajo esa clave (RF-006, RF-009, RF-023 `CA-01`).

    El tipo de contenido se deduce de la extensión de la clave, que es la única
    metadata que el almacén conserva: la clave la construye
    `archivo_service`, nunca el cliente.
    """
    contenido = archivo_service.leer(clave)
    return RespuestaBinaria(content=contenido, media_type=archivo_service.mime_de_clave(clave))
