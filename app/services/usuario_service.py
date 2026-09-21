"""Internal user administration (RF-035).

The administrator registers and edits the people who work at the shop, gives
them a role (by ID, never by name - principle P5) and a usual bay, and can
deactivate an account. Two rules have teeth:

* CA-01 - a deactivated account gets 403 at login, which
  :func:`app.services.auth_service.autenticar` enforces;
* flow 4a / CA-02 - an account holding services in progress cannot be
  deactivated until they are reassigned.

The password is never chosen by the administrator: the system generates a
temporary one and the simulated mail provider "delivers" it (plan section 4).
"""

from sqlalchemy.orm import Session

from app.core.errors import (
    CorreoYaRegistrado,
    RecursoNoEncontrado,
    UsuarioConServiciosEnCurso,
    detalle,
)
from app.core.horario import ahora_utc
from app.core.password import generar_password_temporal
from app.core.security import hash_password
from app.models import EstadoCuenta, Usuario
from app.repositories import asignacion as asignacion_repo
from app.repositories import bahia as bahia_repo
from app.repositories import transicion as transicion_repo
from app.repositories import usuario as usuario_repo
from app.schemas import (
    TAMANIO_PAGINA_DEFECTO,
    TAMANIO_PAGINA_MAXIMO,
    UsuarioInternoActualizar,
    UsuarioInternoCrear,
)
from app.services import eventos, rol_service
from app.services.proveedores.correo import (
    PROVEEDOR_CORREO_PREDETERMINADO,
    EnvioDeCorreoFallido,
    ProveedorCorreo,
)

#: Permission that guards this module (principle P5).
PERMISO_ADMINISTRAR = "usuario:administrar"

ASUNTO_BIENVENIDA = "Tu acceso a AquaLav"


def _cuerpo_bienvenida(usuario: Usuario, password: str) -> str:
    return (
        f"Hola {usuario.nombres}:\n\n"
        f"Se creó tu cuenta de AquaLav con el correo {usuario.correo}.\n"
        f"Tu contraseña temporal es: {password}\n\n"
        "Cámbiala la primera vez que inicies sesión."
    )


def listar(
    db: Session,
    *,
    rol_id: int | None = None,
    estado_cuenta: str | None = None,
    pagina: int = 1,
    tamanio: int = TAMANIO_PAGINA_DEFECTO,
) -> tuple[list[Usuario], int, int, int]:
    """One page of accounts for the administration screen (RF-035)."""
    pagina = max(1, int(pagina or 1))
    tamanio = max(1, min(int(tamanio or TAMANIO_PAGINA_DEFECTO), TAMANIO_PAGINA_MAXIMO))
    items, total = usuario_repo.listar_paginado(
        db, rol_id=rol_id, estado_cuenta=estado_cuenta, pagina=pagina, tamanio=tamanio
    )
    return items, total, pagina, tamanio


def obtener(db: Session, usuario_id: int) -> Usuario:
    usuario = usuario_repo.obtener_por_id(db, usuario_id)
    if usuario is None:
        raise RecursoNoEncontrado("No encontramos ese usuario.")
    return usuario


def _resolver_bahia(db: Session, bahia_id: int | None) -> int | None:
    if bahia_id is None:
        return None
    if bahia_repo.obtener_por_id(db, bahia_id) is None:
        raise RecursoNoEncontrado(
            "No encontramos esa bahía.",
            detalles=[detalle("bahia_habitual_id", "La bahía no existe.")],
        )
    return bahia_id


def servicios_en_curso(db: Session, usuario: Usuario) -> list[int]:
    """Reservation ids this worker is holding right now (flow 4a).

    "In progress" is derived from ``transicion_estado``: a reservation whose
    state still has an outgoing move is live. No state is named here (P3).
    """
    estados_activos = transicion_repo.listar_estados_no_terminales(db)
    return asignacion_repo.listar_reservas_de_operario(db, usuario.id, estados_activos)


def crear(
    db: Session,
    datos: UsuarioInternoCrear,
    autor: Usuario,
    *,
    permisos: list[str],
    correo: ProveedorCorreo = PROVEEDOR_CORREO_PREDETERMINADO,
) -> Usuario:
    """Register an internal account and mail its temporary password (RF-035).

    Flow 2a: an e-mail that already has an account is rejected with 409.

    Creating an account is ALSO handing out a role - ``rol_id`` is mandatory
    here - so ``rol:administrar`` is demanded exactly as it is on
    :func:`actualizar`. Minting a brand new administrator and promoting an
    existing operator are the same privilege change wearing two verbs, and a
    guard that only covered the second one would just move the door.
    """
    rol_service.exigir_permiso_de_rol(permisos)

    destino = str(datos.correo).strip().lower()
    if usuario_repo.existe_correo(db, destino):
        raise CorreoYaRegistrado(detalles=[detalle("correo", "Ese correo ya tiene una cuenta.")])

    rol = rol_service.obtener_rol(db, datos.rol_id)
    bahia_habitual_id = _resolver_bahia(db, datos.bahia_habitual_id)
    password = generar_password_temporal()

    usuario = usuario_repo.crear(
        db,
        nombres=datos.nombres,
        apellidos=datos.apellidos,
        correo=destino,
        telefono=datos.telefono,
        hash_password=hash_password(password),
        rol_id=rol.id,
        estado_cuenta=EstadoCuenta.ACTIVA.value,
        bahia_habitual_id=bahia_habitual_id,
    )

    # RNF-014: the audit trail keeps WHO was created and with which role. It
    # never keeps the password, not even hashed.
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        eventos.USUARIO_CREADO,
        autor_id=autor.id,
        datos={
            "correo": destino,
            "rol_id": rol.id,
            "rol": rol.nombre,
            "bahia_habitual_id": bahia_habitual_id,
        },
    )

    entregada = True
    try:
        correo.enviar(destino, ASUNTO_BIENVENIDA, _cuerpo_bienvenida(usuario, password))
    except EnvioDeCorreoFallido:
        # The account is created either way: losing the delivery must not lose
        # the worker. The administrator resets the password from the screen.
        entregada = False

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        (
            eventos.USUARIO_PASSWORD_TEMPORAL_ENVIADA
            if entregada
            else eventos.USUARIO_PASSWORD_TEMPORAL_NO_ENVIADA
        ),
        autor_id=autor.id,
        datos={"correo": destino},
    )

    db.commit()
    db.refresh(usuario)
    return usuario


def actualizar(
    db: Session,
    usuario_id: int,
    datos: UsuarioInternoActualizar,
    autor: Usuario,
    *,
    permisos: list[str],
) -> Usuario:
    """Edit an internal account, including its role and its activation (RF-035).

    ``usuario:administrar`` opens this door; it does NOT grant the power to
    move somebody between roles. When ``rol_id`` arrives,
    :func:`app.services.rol_service.aplicar_rol` demands ``rol:administrar``
    as well, inside this same transaction, so the editing screen cannot be
    used as a second way into ``PUT /admin/usuarios/{id}/rol`` (RF-004,
    RF-035). The permissions come from the router because authorization is
    decided on what the CALLER holds, never on the row being edited.
    """
    usuario = obtener(db, usuario_id)
    cambios: dict[str, object] = {}

    for campo in ("nombres", "apellidos", "telefono"):
        valor = getattr(datos, campo)
        if valor is not None and valor != getattr(usuario, campo):
            cambios[campo] = {"anterior": getattr(usuario, campo), "nuevo": valor}
            setattr(usuario, campo, valor)

    if datos.bahia_habitual_id is not None and datos.bahia_habitual_id != usuario.bahia_habitual_id:
        cambios["bahia_habitual_id"] = {
            "anterior": usuario.bahia_habitual_id,
            "nuevo": _resolver_bahia(db, datos.bahia_habitual_id),
        }
        usuario.bahia_habitual_id = datos.bahia_habitual_id

    if datos.rol_id is not None:
        # Reuses RF-004 whole: the ``rol:administrar`` check, the self-demotion
        # guard and the refresh token revocation hook all apply here too,
        # inside this same transaction.
        rol_service.aplicar_rol(
            db,
            usuario,
            rol_service.obtener_rol(db, datos.rol_id),
            autor,
            permisos=permisos,
        )

    if datos.activa is not None:
        activa_ahora = usuario.estado_cuenta == EstadoCuenta.ACTIVA.value
        if datos.activa != activa_ahora:
            if not datos.activa:
                pendientes = servicios_en_curso(db, usuario)
                if pendientes:
                    raise UsuarioConServiciosEnCurso(
                        detalles=[
                            detalle("reservas", f"Reserva #{reserva_id} sigue asignada.")
                            for reserva_id in pendientes
                        ]
                    )
            nuevo = EstadoCuenta.ACTIVA.value if datos.activa else EstadoCuenta.SUSPENDIDA.value
            cambios["estado_cuenta"] = {"anterior": usuario.estado_cuenta, "nuevo": nuevo}
            usuario.estado_cuenta = nuevo
            # INC-3 added the column; this is what writes it. A reactivated
            # account clears it, so "since when" is never a stale date.
            usuario.desactivado_en = None if datos.activa else ahora_utc()
            eventos.registrar_evento(
                db,
                eventos.ENTIDAD_USUARIO,
                usuario.id,
                eventos.USUARIO_ACTIVADO if datos.activa else eventos.USUARIO_DESACTIVADO,
                autor_id=autor.id,
                datos={"estado_cuenta": nuevo},
            )

    if cambios:
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_USUARIO,
            usuario.id,
            eventos.USUARIO_ACTUALIZADO,
            autor_id=autor.id,
            datos=cambios,
        )

    db.commit()
    db.refresh(usuario)
    return usuario
