"""Annex A v1.0: eleven states, fourteen transition rows and four roles.

This migration carries DATA, not schema: the MVP already put the state machine
in ``transicion_estado`` and the authorization in ``rol_permiso`` precisely so
growing to v1.0 would be a migration like this one (extension points P3 and P5).

What it does:

* adds the seven permissions v1.0 needs (assignment, review, agenda, users,
  roles and bays) and grants every permission to ``administrador``;
* SPLITS ``personal`` into ``recepcionista`` and ``operario``, moves the
  accounts that still hold ``personal`` to ``recepcionista`` (the counter is
  where they were working) and drops the old role;
* CONVERTS the stored data: ``en_atencion`` disappears from Annex A and becomes
  ``en_lavado``, both in ``reserva.estado`` and in the state history, so no row
  is left pointing at a state that no longer exists;
* replaces the four MVP transitions by the fourteen of Annex A v1.0. The other
  four transitions of the annex are not rows: two are the creation of the
  reservation (no origin state) and two are the terminal sinks, which are
  derived from the absence of outgoing rows.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# NOTE: the chain uses the short identifiers "0001"/"0002", not the file names.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------
# Reference data, frozen as literals on purpose: a migration must keep meaning
# the same thing after ``app/seed.py`` moves on.
# --------------------------------------------------------------------------
PERMISOS_NUEVOS: tuple[tuple[str, str], ...] = (
    ("reserva:asignar", "Asignar una bahía y un operario a un servicio."),
    (
        "reserva:revisar",
        "Registrar la observación del cliente y enviar el servicio a revisión.",
    ),
    ("agenda:leer", "Consultar la agenda diaria y semanal por bahía."),
    ("agenda:administrar", "Bloquear franjas, feriados y horarios de atención."),
    ("usuario:administrar", "Crear, editar, activar y desactivar usuarios internos."),
    ("rol:administrar", "Consultar roles y permisos y asignar el rol de un usuario."),
    ("bahia:administrar", "Crear, editar y desactivar bahías."),
)

ROLES_NUEVOS: tuple[tuple[str, str], ...] = (
    ("recepcionista", "Atiende el mostrador: recibe, asigna, cobra y entrega."),
    ("operario", "Ejecuta el servicio en la bahía y avanza su estado."),
)

ROL_PERMISOS_NUEVOS: dict[str, tuple[str, ...]] = {
    "recepcionista": (
        "servicio:leer",
        "agenda:leer",
        "reserva:leer_todas",
        "reserva:cancelar",
        "reserva:check_in",
        "reserva:asignar",
        "reserva:revisar",
        "reserva:check_out",
        "pago:registrar",
    ),
    "operario": (
        "servicio:leer",
        "reserva:leer_todas",
        "reserva:avanzar_estado",
    ),
}

#: (estado_origen, estado_destino, permiso_requerido, endpoint, marca_fin_servicio)
TRANSICIONES_V1: tuple[tuple[str, str, str, str | None, bool], ...] = (
    ("pendiente_pago", "confirmada", "pago:registrar", "pago", False),
    ("pendiente_pago", "cancelada", "reserva:cancelar", "cancelacion", False),
    ("confirmada", "en_recepcion", "reserva:check_in", "check_in", False),
    ("confirmada", "cancelada", "reserva:cancelar", "cancelacion", False),
    ("en_recepcion", "asignado", "reserva:asignar", "asignacion", False),
    ("en_recepcion", "cancelada", "reserva:cancelar", "cancelacion", False),
    ("asignado", "en_lavado", "reserva:avanzar_estado", None, False),
    ("en_lavado", "secado", "reserva:avanzar_estado", None, False),
    ("secado", "acabado", "reserva:avanzar_estado", None, False),
    ("acabado", "finalizado", "reserva:avanzar_estado", None, True),
    ("finalizado", "entregado", "reserva:check_out", "check_out", False),
    ("finalizado", "en_revision", "reserva:revisar", "revision", False),
    ("en_revision", "acabado", "reserva:avanzar_estado", None, False),
    ("en_revision", "entregado", "reserva:check_out", "check_out", False),
)

#: The four rows of the MVP, restored by ``downgrade``.
TRANSICIONES_MVP: tuple[tuple[str, str, str, str | None, bool], ...] = (
    ("confirmada", "en_atencion", "reserva:check_in", "check_in", False),
    ("confirmada", "cancelada", "reserva:cancelar", "cancelacion", False),
    ("en_atencion", "finalizado", "reserva:avanzar_estado", None, True),
    ("finalizado", "entregado", "reserva:check_out", "check_out", False),
)

#: v1.0 state -> MVP state, for the (lossy) downgrade.
EQUIVALENCIA_MVP: dict[str, str] = {
    "pendiente_pago": "confirmada",
    "en_recepcion": "en_atencion",
    "asignado": "en_atencion",
    "en_lavado": "en_atencion",
    "secado": "en_atencion",
    "acabado": "en_atencion",
    "en_revision": "finalizado",
}


def _insertar_permisos(conexion, permisos) -> None:
    for codigo, descripcion in permisos:
        conexion.execute(
            sa.text(
                "INSERT INTO permiso (codigo, descripcion) "
                "SELECT :codigo, :descripcion "
                "WHERE NOT EXISTS (SELECT 1 FROM permiso WHERE codigo = :codigo)"
            ),
            {"codigo": codigo, "descripcion": descripcion},
        )


def _conceder(conexion, nombre_rol: str, codigos) -> None:
    for codigo in codigos:
        conexion.execute(
            sa.text(
                "INSERT INTO rol_permiso (rol_id, permiso_id) "
                "SELECT r.id, p.id FROM rol r, permiso p "
                "WHERE r.nombre = :rol AND p.codigo = :codigo "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM rol_permiso rp WHERE rp.rol_id = r.id AND rp.permiso_id = p.id"
                ")"
            ),
            {"rol": nombre_rol, "codigo": codigo},
        )


def _insertar_transiciones(conexion, transiciones) -> None:
    for origen, destino, permiso, endpoint, marca_fin in transiciones:
        conexion.execute(
            sa.text(
                "INSERT INTO transicion_estado "
                "(estado_origen, estado_destino, permiso_requerido, endpoint, marca_fin_servicio) "
                "SELECT :origen, :destino, :permiso, :endpoint, :marca_fin "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM transicion_estado "
                "  WHERE estado_origen = :origen AND estado_destino = :destino"
                ")"
            ),
            {
                "origen": origen,
                "destino": destino,
                "permiso": permiso,
                "endpoint": endpoint,
                "marca_fin": marca_fin,
            },
        )


def upgrade() -> None:
    conexion = op.get_bind()

    # 1. Permissions v1.0 needs, and the administrator superset.
    _insertar_permisos(conexion, PERMISOS_NUEVOS)
    conexion.execute(
        sa.text(
            "INSERT INTO rol_permiso (rol_id, permiso_id) "
            "SELECT r.id, p.id FROM rol r CROSS JOIN permiso p "
            "WHERE r.nombre = 'administrador' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM rol_permiso rp WHERE rp.rol_id = r.id AND rp.permiso_id = p.id"
            ")"
        )
    )

    # 2. Split ``personal`` into the counter and the bay.
    for nombre, descripcion in ROLES_NUEVOS:
        conexion.execute(
            sa.text(
                "INSERT INTO rol (nombre, descripcion) "
                "SELECT :nombre, :descripcion "
                "WHERE NOT EXISTS (SELECT 1 FROM rol WHERE nombre = :nombre)"
            ),
            {"nombre": nombre, "descripcion": descripcion},
        )
    for nombre_rol, codigos in ROL_PERMISOS_NUEVOS.items():
        _conceder(conexion, nombre_rol, codigos)

    # 3. Everyone who was ``personal`` keeps working at the counter.
    conexion.execute(
        sa.text(
            "UPDATE usuario SET rol_id = (SELECT id FROM rol WHERE nombre = 'recepcionista') "
            "WHERE rol_id = (SELECT id FROM rol WHERE nombre = 'personal')"
        )
    )
    conexion.execute(
        sa.text(
            "DELETE FROM rol_permiso "
            "WHERE rol_id = (SELECT id FROM rol WHERE nombre = 'personal')"
        )
    )
    conexion.execute(sa.text("DELETE FROM rol WHERE nombre = 'personal'"))

    # 4. ``en_atencion`` disappears: the vehicle was being washed.
    conexion.execute(
        sa.text("UPDATE reserva SET estado = 'en_lavado' WHERE estado = 'en_atencion'")
    )
    conexion.execute(
        sa.text(
            "UPDATE reserva_estado_historial SET estado = 'en_lavado' "
            "WHERE estado = 'en_atencion'"
        )
    )

    # 5. The state machine itself.
    conexion.execute(
        sa.text(
            "DELETE FROM transicion_estado "
            "WHERE estado_origen = 'en_atencion' OR estado_destino = 'en_atencion'"
        )
    )
    _insertar_transiciones(conexion, TRANSICIONES_V1)


def downgrade() -> None:
    """Back to the five states and three roles of the MVP.

    LOSSY by definition: the seven states v1.0 introduced have no MVP
    equivalent, so every reservation is folded back onto the closest one
    (``EQUIVALENCIA_MVP``). The history keeps the same treatment, so no row is
    left on a state the MVP cannot name.
    """
    conexion = op.get_bind()

    # 1. Transitions back to the MVP set.
    conexion.execute(sa.text("DELETE FROM transicion_estado"))
    _insertar_transiciones(conexion, TRANSICIONES_MVP)

    # 2. States back to their closest MVP equivalent.
    for estado_v1, estado_mvp in EQUIVALENCIA_MVP.items():
        conexion.execute(
            sa.text("UPDATE reserva SET estado = :mvp WHERE estado = :v1"),
            {"mvp": estado_mvp, "v1": estado_v1},
        )
        conexion.execute(
            sa.text("UPDATE reserva_estado_historial SET estado = :mvp WHERE estado = :v1"),
            {"mvp": estado_mvp, "v1": estado_v1},
        )

    # 3. The two roles merge back into ``personal``.
    conexion.execute(
        sa.text(
            "INSERT INTO rol (nombre, descripcion) "
            "SELECT 'personal', 'Personal de atención y operación del local.' "
            "WHERE NOT EXISTS (SELECT 1 FROM rol WHERE nombre = 'personal')"
        )
    )
    _conceder(
        conexion,
        "personal",
        (
            "servicio:leer",
            "reserva:leer_todas",
            "reserva:cancelar",
            "reserva:check_in",
            "reserva:avanzar_estado",
            "reserva:check_out",
            "pago:registrar",
        ),
    )
    conexion.execute(
        sa.text(
            "UPDATE usuario SET rol_id = (SELECT id FROM rol WHERE nombre = 'personal') "
            "WHERE rol_id IN (SELECT id FROM rol WHERE nombre IN ('recepcionista', 'operario'))"
        )
    )
    conexion.execute(
        sa.text(
            "DELETE FROM rol_permiso WHERE rol_id IN "
            "(SELECT id FROM rol WHERE nombre IN ('recepcionista', 'operario'))"
        )
    )
    conexion.execute(
        sa.text("DELETE FROM rol WHERE nombre IN ('recepcionista', 'operario')")
    )

    # 4. The permissions v1.0 added.
    for codigo, _descripcion in PERMISOS_NUEVOS:
        conexion.execute(
            sa.text(
                "DELETE FROM rol_permiso WHERE permiso_id IN "
                "(SELECT id FROM permiso WHERE codigo = :codigo)"
            ),
            {"codigo": codigo},
        )
        conexion.execute(
            sa.text("DELETE FROM permiso WHERE codigo = :codigo"), {"codigo": codigo}
        )
