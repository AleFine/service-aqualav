"""Notifications, reminders and live tracking (INC-5).

Schema for RF-029, RF-030 and the RF-022 delta.

What it does:

* creates ``plantilla_notificacion`` - RF-029 composes every message "según
  plantilla del evento y el idioma", so the text is a row and not a literal in
  a service. The same table decides which channels an event uses: a channel
  with no row for it is simply not used;
* creates ``notificacion`` - one row per ``(usuario, evento, canal)`` delivery,
  with ``intentos`` and the ``error`` of the last failure. That is what
  "registro del resultado del envío para trazabilidad" and CA-02 ask for, and
  it is also the table that finally sits behind ``NotificadorEnApp``, the MVP
  stub that only wrote to the log;
* creates ``dispositivo`` - the registered push token of RF-029's
  precondition. The token is unique shop-wide so re-registering one moves it
  to its new owner instead of duplicating it;
* creates ``recordatorio`` - RF-030's two-hour notice and the answer it got.
  One row per reservation, which is what makes the sweep idempotent;
* adds ``transicion_estado.evento_notificacion`` - EXTENSION POINT P3, the
  notification half: WHICH move raises which lifecycle event is a property of
  the move, so it is a column and not a mapping inside a service;
* adds ``reserva.hora_estimada_entrega`` - RF-022 step 4, recalculated on every
  state change;
* registers ``planificador:ejecutar`` and grants it to the administrator.

Two back-fills:

* ``hora_estimada_entrega`` takes ``fin``. That IS the estimate every existing
  reservation was given when it was booked; leaving it NULL would make every
  historical service look like it had no promised time;
* ``evento_notificacion`` is written for the six lifecycle moves RF-029 names.
  Without it a shop upgrading from INC-1A would keep the fourteen moves it
  already has and every one of them would stay silent.

The template TEXT is NOT inserted here on purpose. ``app/seed.py`` owns the
catalogue, refreshes it on every run and is what the container executes right
after ``alembic upgrade head``; writing the same forty rows in two places is
how the two copies start disagreeing. A deployment that runs with
``SEED_ENABLED=false`` and no templates still notifies - the dispatcher falls
back to a generic message - it just does it without the polished wording.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (código, descripción, roles que lo reciben)
PERMISOS_NUEVOS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "planificador:ejecutar",
        "Forzar el barrido de recordatorios y de la cola de espera.",
        ("administrador",),
    ),
)

#: (estado_origen, estado_destino, evento) - the six lifecycle events of
#: RF-029 that ARE a declared move. "Confirmación" also happens on creation,
#: which has no origin state and is dispatched by ``reserva_service.crear``.
EVENTOS_DE_TRANSICION: tuple[tuple[str, str, str], ...] = (
    ("pendiente_pago", "confirmada", "confirmacion"),
    ("pendiente_pago", "cancelada", "cancelacion"),
    ("confirmada", "cancelada", "cancelacion"),
    ("en_recepcion", "cancelada", "cancelacion"),
    ("asignado", "en_lavado", "inicio"),
    ("acabado", "finalizado", "finalizacion"),
    ("finalizado", "entregado", "entrega"),
    ("en_revision", "entregado", "entrega"),
)


def upgrade() -> None:
    conexion = op.get_bind()

    # 1. The text of every notice (RF-029).
    op.create_table(
        "plantilla_notificacion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("evento", sa.String(length=40), nullable=False),
        sa.Column("canal", sa.String(length=20), nullable=False),
        sa.Column("idioma", sa.String(length=5), nullable=False),
        sa.Column("asunto", sa.String(length=160), nullable=False),
        sa.Column("cuerpo", sa.String(length=1000), nullable=False),
        sa.UniqueConstraint("evento", "canal", "idioma", name="uq_plantilla_evento_canal_idioma"),
    )

    # 2. What was sent and how it went (RF-029 CA-02).
    op.create_table(
        "notificacion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "usuario_id",
            sa.Integer(),
            sa.ForeignKey("usuario.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("evento", sa.String(length=40), nullable=False),
        sa.Column("canal", sa.String(length=20), nullable=False),
        sa.Column("destino", sa.String(length=200), nullable=False),
        sa.Column("asunto", sa.String(length=160), nullable=False),
        sa.Column("cuerpo", sa.String(length=1000), nullable=False),
        sa.Column("estado_envio", sa.String(length=20), nullable=False, server_default="pendiente"),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column(
            "creada_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("enviado_en", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_notificacion_usuario_id", "notificacion", ["usuario_id"])
    op.create_index("ix_notificacion_reserva_id", "notificacion", ["reserva_id"])
    op.create_index("ix_notificacion_usuario_creada", "notificacion", ["usuario_id", "creada_en"])

    # 3. The registered push tokens (RF-029 precondition).
    op.create_table(
        "dispositivo",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "usuario_id",
            sa.Integer(),
            sa.ForeignKey("usuario.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_push", sa.String(length=200), nullable=False, unique=True),
        sa.Column("plataforma", sa.String(length=20), nullable=False),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "registrado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_dispositivo_usuario_id", "dispositivo", ["usuario_id"])
    op.create_index("ix_dispositivo_token_push", "dispositivo", ["token_push"], unique=True)

    # 4. The two-hour reminder and its answer (RF-030).
    op.create_table(
        "recordatorio",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("programado_para", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enviado_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("respuesta", sa.String(length=20), nullable=True),
        sa.Column("respondido_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estado", sa.String(length=20), nullable=False, server_default="pendiente"),
    )
    op.create_index("ix_recordatorio_reserva_id", "recordatorio", ["reserva_id"], unique=True)

    # 5. EXTENSION POINT P3, the notification half (RF-029).
    op.add_column(
        "transicion_estado",
        sa.Column("evento_notificacion", sa.String(length=40), nullable=True),
    )

    # 6. RF-022 step 4: the delivery time that moves as the service advances.
    op.add_column(
        "reserva",
        sa.Column("hora_estimada_entrega", sa.DateTime(timezone=True), nullable=True),
    )

    # 7. Back-fills (see the module docstring for why each one is safe).
    conexion.execute(
        sa.text(
            "UPDATE reserva SET hora_estimada_entrega = fin " "WHERE hora_estimada_entrega IS NULL"
        )
    )
    for origen, destino, evento in EVENTOS_DE_TRANSICION:
        conexion.execute(
            sa.text(
                "UPDATE transicion_estado SET evento_notificacion = :evento "
                "WHERE estado_origen = :origen AND estado_destino = :destino"
            ),
            {"evento": evento, "origen": origen, "destino": destino},
        )

    # 8. The permission that guards the internal sweep (principle P5).
    for codigo, descripcion, roles in PERMISOS_NUEVOS:
        conexion.execute(
            sa.text(
                "INSERT INTO permiso (codigo, descripcion) "
                "SELECT :codigo, :descripcion "
                "WHERE NOT EXISTS (SELECT 1 FROM permiso WHERE codigo = :codigo)"
            ),
            {"codigo": codigo, "descripcion": descripcion},
        )
        for rol in roles:
            conexion.execute(
                sa.text(
                    "INSERT INTO rol_permiso (rol_id, permiso_id) "
                    "SELECT r.id, p.id FROM rol r, permiso p "
                    "WHERE r.nombre = :rol AND p.codigo = :codigo "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM rol_permiso rp "
                    "  WHERE rp.rol_id = r.id AND rp.permiso_id = p.id"
                    ")"
                ),
                {"rol": rol, "codigo": codigo},
            )


def downgrade() -> None:
    """Back to INC-3: no notifications, no reminders, no live estimate."""
    conexion = op.get_bind()

    for codigo, _, _ in PERMISOS_NUEVOS:
        conexion.execute(
            sa.text(
                "DELETE FROM rol_permiso WHERE permiso_id = "
                "(SELECT id FROM permiso WHERE codigo = :codigo)"
            ),
            {"codigo": codigo},
        )
        conexion.execute(sa.text("DELETE FROM permiso WHERE codigo = :codigo"), {"codigo": codigo})

    op.drop_column("reserva", "hora_estimada_entrega")
    op.drop_column("transicion_estado", "evento_notificacion")

    op.drop_index("ix_recordatorio_reserva_id", table_name="recordatorio")
    op.drop_table("recordatorio")

    op.drop_index("ix_dispositivo_token_push", table_name="dispositivo")
    op.drop_index("ix_dispositivo_usuario_id", table_name="dispositivo")
    op.drop_table("dispositivo")

    op.drop_index("ix_notificacion_usuario_creada", table_name="notificacion")
    op.drop_index("ix_notificacion_reserva_id", table_name="notificacion")
    op.drop_index("ix_notificacion_usuario_id", table_name="notificacion")
    op.drop_table("notificacion")

    op.drop_table("plantilla_notificacion")
