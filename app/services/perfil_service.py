"""The customer's own profile (RF-006).

One rule shapes this whole module: **the e-mail is not edited, it is
requested**. CA-02 says that while a new address is not verified the previous
one stays in force, so ``PATCH /perfil`` with a ``correo`` applies everything
else immediately and leaves the address alone until the link in the mail is
opened (``auth_service.verificar_correo`` is what applies it).

Flow 4a - "a validation error points at the field and keeps the rest of what
was typed" - is why nothing is written until every field has been accepted: a
rejected request leaves the stored profile exactly as it was, so the screen
resubmits the same form with one field corrected and loses nothing.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.errors import CorreoYaRegistrado, detalle
from app.core.horario import ahora_utc
from app.models import Usuario
from app.repositories import autenticacion as autenticacion_repo
from app.repositories import usuario as usuario_repo
from app.schemas import PerfilActualizar
from app.services import auth_service, eventos
from app.services.proveedores.correo import PROVEEDOR_CORREO_PREDETERMINADO, ProveedorCorreo

#: Fields the customer edits straight away. The e-mail is NOT one of them.
CAMPOS_DIRECTOS = (
    "nombres",
    "apellidos",
    "telefono",
    "foto_perfil_key",
    "idioma",
    "notificar_push",
    "notificar_correo",
)


@dataclass(frozen=True)
class Perfil:
    """The profile, plus the address change that has not landed yet."""

    usuario: Usuario
    correo_pendiente: str | None = None
    verificacion_pendiente: bool = False


def _armar(db: Session, usuario: Usuario) -> Perfil:
    """Read the live verification back so the screen can explain CA-02."""
    fila = autenticacion_repo.obtener_verificacion_viva(db, usuario.id, ahora_utc())
    correo_pendiente = fila.correo if fila is not None and fila.correo != usuario.correo else None
    return Perfil(
        usuario=usuario,
        correo_pendiente=correo_pendiente,
        verificacion_pendiente=fila is not None,
    )


def obtener(db: Session, usuario: Usuario) -> Perfil:
    """The profile as the screen shows it (RF-006 CA-01)."""
    return _armar(db, usuario)


def actualizar(
    db: Session,
    usuario: Usuario,
    datos: PerfilActualizar,
    *,
    correo_proveedor: ProveedorCorreo = PROVEEDOR_CORREO_PREDETERMINADO,
) -> Perfil:
    """Save the editable profile and, if asked, start an address change.

    CA-01: a phone number that changed is visible the moment the profile is
    reloaded. CA-02: a ``correo`` opens a verification and changes nothing -
    the answer still carries the OLD address, plus ``correo_pendiente`` so the
    screen can say which one is waiting for its link.
    """
    cambios: dict[str, object] = {}

    for campo in CAMPOS_DIRECTOS:
        valor = getattr(datos, campo)
        if valor is None:
            continue
        valor = getattr(valor, "value", valor)
        if valor != getattr(usuario, campo):
            cambios[campo] = {"anterior": getattr(usuario, campo), "nuevo": valor}
            setattr(usuario, campo, valor)

    solicitado: str | None = None
    if datos.correo is not None:
        destino = str(datos.correo).strip().lower()
        if destino != usuario.correo:
            if usuario_repo.existe_correo(db, destino):
                raise CorreoYaRegistrado(
                    detalles=[detalle("correo", "Ese correo ya tiene una cuenta.")]
                )
            auth_service.enviar_verificacion(
                db,
                usuario,
                destino,
                correo_proveedor,
                asunto=auth_service.ASUNTO_CORREO_NUEVO,
            )
            solicitado = destino
            eventos.registrar_evento(
                db,
                eventos.ENTIDAD_USUARIO,
                usuario.id,
                eventos.USUARIO_CORREO_CAMBIO_SOLICITADO,
                autor_id=usuario.id,
                datos={"actual": usuario.correo, "solicitado": destino},
            )

    if cambios:
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_USUARIO,
            usuario.id,
            eventos.USUARIO_PERFIL_ACTUALIZADO,
            autor_id=usuario.id,
            datos=cambios,
        )

    if cambios or solicitado:
        db.commit()
        db.refresh(usuario)

    return _armar(db, usuario)
