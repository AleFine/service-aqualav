"""Agenda, assignment and internal user administration (INC-1B).

Schema AND data for RF-018, RF-019 (delta), RF-020, RF-024 (delta), RF-035 and
the completion of RN-07.

What it does:

* creates ``horario_atencion``, ``dia_no_laborable`` and ``bloqueo_franja`` -
  the agenda RN-07 used to be three constants in ``app/core/horario.py``, which
  is why ``es_laborable`` could only ever answer ``True``;
* SEEDS the RN-07 week into ``horario_atencion`` so the behaviour is byte for
  byte the one the MVP had, only read from data now;
* creates ``asignacion_servicio`` and ``cola_espera`` (RF-020);
* adds ``bahia.estado``, ``usuario.bahia_habitual_id``, ``reserva.codigo_qr``,
  ``reserva.atencion_sin_reserva`` and ``reserva.observacion_revision``;
* BACKFILLS a QR token for every reservation that already exists, so the
  counter can scan an old ticket too;
* grants ``agenda:administrar`` to ``recepcionista``: RF-018 names the
  receptionist as an actor of the agenda, and INC-1A could not decide it
  because no agenda endpoint existed yet.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-20

"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# NOTE: the chain uses the short identifiers "0003"/"0004", not the file names.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------
# Reference data, frozen as literals: a migration must keep meaning the same
# thing after ``app/seed.py`` and ``app/core/horario.py`` move on.
# --------------------------------------------------------------------------
#: (dia_semana, hora_apertura, hora_cierre) - RN-07, Monday is 0.
HORARIO_RN07: tuple[tuple[int, str, str], ...] = (
    (0, "08:00:00", "19:00:00"),
    (1, "08:00:00", "19:00:00"),
    (2, "08:00:00", "19:00:00"),
    (3, "08:00:00", "19:00:00"),
    (4, "08:00:00", "19:00:00"),
    (5, "08:00:00", "19:00:00"),
    (6, "09:00:00", "14:00:00"),
)
VIGENCIA_INICIAL = "2024-01-01"

#: RF-018 lists the receptionist as an actor of the agenda, next to the admin.
PERMISOS_RECEPCIONISTA: tuple[str, ...] = ("agenda:administrar",)

#: Same alphabet the application uses, minus the ambiguous glyphs.
ALFABETO_QR = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _codigo_qr() -> str:
    return "AQLQR-" + "".join(secrets.choice(ALFABETO_QR) for _ in range(12))


def upgrade() -> None:
    conexion = op.get_bind()

    # 1. The agenda (RF-018).
    op.create_table(
        "horario_atencion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dia_semana", sa.Integer(), nullable=False),
        sa.Column("hora_apertura", sa.Time(), nullable=True),
        sa.Column("hora_cierre", sa.Time(), nullable=True),
        sa.Column("vigente_desde", sa.Date(), nullable=False),
        sa.Column("autor_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=True),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_horario_dia_vigencia", "horario_atencion", ["dia_semana", "vigente_desde"]
    )

    op.create_table(
        "dia_no_laborable",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("motivo", sa.String(length=200), nullable=False),
        sa.Column("autor_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=True),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # ``unique=True, index=True`` on the model is ONE unique index, not an index
    # plus a separate constraint: declaring it any other way drifts.
    op.create_index("ix_dia_no_laborable_fecha", "dia_no_laborable", ["fecha"], unique=True)

    op.create_table(
        "bloqueo_franja",
        sa.Column("id", sa.Integer(), primary_key=True),
        # NULL blocks the whole shop, every bay at once.
        sa.Column("bahia_id", sa.Integer(), sa.ForeignKey("bahia.id"), nullable=True),
        sa.Column("inicio", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fin", sa.DateTime(timezone=True), nullable=False),
        sa.Column("motivo", sa.String(length=30), nullable=False),
        sa.Column("descripcion", sa.String(length=300), nullable=True),
        sa.Column("autor_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=True),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_bloqueo_franja_bahia_id", "bloqueo_franja", ["bahia_id"])
    op.create_index("ix_bloqueo_bahia_inicio", "bloqueo_franja", ["bahia_id", "inicio"])

    # 2. RN-07 becomes data. The values are the ones the MVP had hardcoded, so
    #    nothing about the behaviour changes on the day of the upgrade.
    for dia_semana, apertura, cierre in HORARIO_RN07:
        conexion.execute(
            sa.text(
                "INSERT INTO horario_atencion "
                "(dia_semana, hora_apertura, hora_cierre, vigente_desde) "
                "SELECT :dia, :apertura, :cierre, :vigencia "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM horario_atencion WHERE dia_semana = :dia"
                ")"
            ),
            {
                "dia": dia_semana,
                "apertura": apertura,
                "cierre": cierre,
                "vigencia": VIGENCIA_INICIAL,
            },
        )

    # 3. Assignment and waiting queue (RF-020).
    op.create_table(
        "asignacion_servicio",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bahia_id", sa.Integer(), sa.ForeignKey("bahia.id"), nullable=False),
        sa.Column("operario_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=False),
        sa.Column("asignado_por_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=True),
        sa.Column(
            "asignado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "sugerida", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_index(
        "ix_asignacion_servicio_reserva_id",
        "asignacion_servicio",
        ["reserva_id"],
        unique=True,
    )
    op.create_index("ix_asignacion_servicio_bahia_id", "asignacion_servicio", ["bahia_id"])
    op.create_index("ix_asignacion_servicio_operario_id", "asignacion_servicio", ["operario_id"])

    op.create_table(
        "cola_espera",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("posicion", sa.Integer(), nullable=False),
        sa.Column("tiempo_estimado_min", sa.Integer(), nullable=False),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_cola_espera_reserva_id", "cola_espera", ["reserva_id"], unique=True)

    # 4. New columns.
    op.add_column(
        "bahia",
        sa.Column("estado", sa.String(length=20), nullable=False, server_default="libre"),
    )
    # Batch mode: adding a column that carries a FOREIGN KEY is an ALTER of a
    # constraint, which SQLite cannot do in place. PostgreSQL - the real target
    # - runs it as a plain ALTER either way.
    with op.batch_alter_table("usuario") as lote:
        lote.add_column(
            sa.Column(
                "bahia_habitual_id",
                sa.Integer(),
                sa.ForeignKey("bahia.id", name="fk_usuario_bahia_habitual"),
                nullable=True,
            )
        )
    op.add_column("reserva", sa.Column("codigo_qr", sa.String(length=24), nullable=True))
    op.create_index("ix_reserva_codigo_qr", "reserva", ["codigo_qr"], unique=True)
    op.add_column(
        "reserva",
        sa.Column(
            "atencion_sin_reserva",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "reserva", sa.Column("observacion_revision", sa.String(length=500), nullable=True)
    )

    # 5. Backfill the QR of the reservations that already exist, one by one so
    #    each token is unique (there is no portable random generator in SQL).
    for (reserva_id,) in conexion.execute(
        sa.text("SELECT id FROM reserva WHERE codigo_qr IS NULL")
    ).all():
        conexion.execute(
            sa.text("UPDATE reserva SET codigo_qr = :qr WHERE id = :id"),
            {"qr": _codigo_qr(), "id": reserva_id},
        )

    # 6. The receptionist can manage the agenda (RF-018 actors).
    for codigo in PERMISOS_RECEPCIONISTA:
        conexion.execute(
            sa.text(
                "INSERT INTO rol_permiso (rol_id, permiso_id) "
                "SELECT r.id, p.id FROM rol r, permiso p "
                "WHERE r.nombre = 'recepcionista' AND p.codigo = :codigo "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM rol_permiso rp WHERE rp.rol_id = r.id AND rp.permiso_id = p.id"
                ")"
            ),
            {"codigo": codigo},
        )


def downgrade() -> None:
    """Back to INC-1A: the agenda goes away and RN-07 returns to the constants."""
    conexion = op.get_bind()

    for codigo in PERMISOS_RECEPCIONISTA:
        conexion.execute(
            sa.text(
                "DELETE FROM rol_permiso WHERE rol_id = "
                "(SELECT id FROM rol WHERE nombre = 'recepcionista') "
                "AND permiso_id = (SELECT id FROM permiso WHERE codigo = :codigo)"
            ),
            {"codigo": codigo},
        )

    op.drop_column("reserva", "observacion_revision")
    op.drop_column("reserva", "atencion_sin_reserva")
    op.drop_index("ix_reserva_codigo_qr", table_name="reserva")
    op.drop_column("reserva", "codigo_qr")
    with op.batch_alter_table("usuario") as lote:
        lote.drop_column("bahia_habitual_id")
    op.drop_column("bahia", "estado")

    op.drop_index("ix_cola_espera_reserva_id", table_name="cola_espera")
    op.drop_table("cola_espera")
    op.drop_index("ix_asignacion_servicio_operario_id", table_name="asignacion_servicio")
    op.drop_index("ix_asignacion_servicio_bahia_id", table_name="asignacion_servicio")
    op.drop_index("ix_asignacion_servicio_reserva_id", table_name="asignacion_servicio")
    op.drop_table("asignacion_servicio")

    op.drop_index("ix_bloqueo_bahia_inicio", table_name="bloqueo_franja")
    op.drop_index("ix_bloqueo_franja_bahia_id", table_name="bloqueo_franja")
    op.drop_table("bloqueo_franja")
    op.drop_index("ix_dia_no_laborable_fecha", table_name="dia_no_laborable")
    op.drop_table("dia_no_laborable")
    op.drop_index("ix_horario_dia_vigencia", table_name="horario_atencion")
    op.drop_table("horario_atencion")
