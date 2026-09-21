"""Object upload and serving payloads (RF-006, RF-009, RF-023, plan section 4).

The bytes travel **base64 inside a JSON body**, not as ``multipart/form-data``.
That is a deliberate choice and worth the sentence: every other request in this
API is a Pydantic model, and a multipart upload would be the one endpoint whose
body a router has to take apart by hand, field by field, with no schema and no
uniform validation error. Base64 costs a third more bytes on the wire and buys
the same contract, the same error envelope and, for RF-023 flow 4a, a request
the device can literally replay byte for byte when the connection comes back.
"""

from pydantic import BaseModel, Field

from app.models.enums import CarpetaArchivo


class ArchivoSubirIn(BaseModel):
    """Body of ``POST /archivos``.

    ``carpeta`` says what the object IS - a profile picture, a catalogue image,
    a piece of evidence - and it is the only part of the key the caller gets to
    choose. The rest is an opaque random name the service generates, so keys
    are neither guessable nor collidable.
    """

    contenido_base64: str = Field(min_length=1)
    mime: str = Field(min_length=3, max_length=100)
    carpeta: CarpetaArchivo = CarpetaArchivo.PERFILES


class ArchivoOut(BaseModel):
    """Where the object ended up.

    ``clave`` is what goes into ``usuario.foto_perfil_key`` or
    ``servicio.imagen_url``; ``url`` is where ``GET /archivos/...`` serves it
    from. Callers store the KEY and render the URL - the second one is derived
    from the first and changes if the store ever moves.
    """

    clave: str
    url: str
    mime: str
    tamano_bytes: int
