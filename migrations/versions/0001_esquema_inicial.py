"""Initial AquaLav schema.

Creates every table of the MVP data model, including the extension points
(servicio_precio, transicion_estado, reserva_estado_historial, evento_dominio).

Revision ID: 0001
Revises:
Create Date: 2026-09-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere (keeps the schema portable).
TIPO_DATOS = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "rol",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("nombre", sa.String(length=40), nullable=False),
        sa.Column("descripcion", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nombre"),
    )

    op.create_table(
        "permiso",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("codigo", sa.String(length=60), nullable=False),
        sa.Column("descripcion", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo"),
    )

    op.create_table(
        "rol_permiso",
        sa.Column("rol_id", sa.Integer(), nullable=False),
        sa.Column("permiso_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["permiso_id"], ["permiso.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rol_id"], ["rol.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("rol_id", "permiso_id"),
    )

    op.create_table(
        "usuario",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("nombres", sa.String(length=80), nullable=False),
        sa.Column("apellidos", sa.String(length=80), nullable=False),
        sa.Column("correo", sa.String(length=160), nullable=False),
        sa.Column("telefono", sa.String(length=20), nullable=False),
        sa.Column("hash_password", sa.String(length=255), nullable=False),
        sa.Column("rol_id", sa.Integer(), nullable=False),
        sa.Column(
            "estado_cuenta", sa.String(length=30), server_default="activa", nullable=False
        ),
        sa.Column("intentos_fallidos", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bloqueado_hasta", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["rol_id"], ["rol.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usuario_correo", "usuario", ["correo"], unique=True)

    op.create_table(
        "vehiculo",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("placa", sa.String(length=10), nullable=False),
        sa.Column("tipo", sa.String(length=20), nullable=False),
        sa.Column("marca", sa.String(length=60), nullable=False),
        sa.Column("modelo", sa.String(length=60), nullable=False),
        sa.Column("color", sa.String(length=40), nullable=False),
        sa.Column("anio", sa.Integer(), nullable=False),
        sa.Column("activo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuario.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("usuario_id", "placa", name="uq_vehiculo_usuario_placa"),
    )
    op.create_index("ix_vehiculo_usuario_id", "vehiculo", ["usuario_id"], unique=False)

    op.create_table(
        "servicio",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("nombre", sa.String(length=80), nullable=False),
        sa.Column("descripcion", sa.String(length=400), nullable=False),
        sa.Column("categoria", sa.String(length=40), server_default="general", nullable=False),
        sa.Column("duracion_min", sa.Integer(), nullable=False),
        sa.Column("activo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "servicio_precio",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("servicio_id", sa.Integer(), nullable=False),
        sa.Column("monto_centimos", sa.Integer(), nullable=False),
        sa.Column("moneda", sa.String(length=3), server_default="PEN", nullable=False),
        sa.Column(
            "vigente_desde",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("vigente_hasta", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["servicio_id"], ["servicio.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_servicio_precio_servicio_id", "servicio_precio", ["servicio_id"], unique=False
    )

    op.create_table(
        "bahia",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("nombre", sa.String(length=40), nullable=False),
        sa.Column("activa", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nombre"),
    )

    op.create_table(
        "reserva",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("codigo", sa.String(length=12), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("vehiculo_id", sa.Integer(), nullable=False),
        sa.Column("servicio_id", sa.Integer(), nullable=False),
        sa.Column("bahia_id", sa.Integer(), nullable=False),
        sa.Column("inicio", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fin", sa.DateTime(timezone=True), nullable=False),
        sa.Column("estado", sa.String(length=30), nullable=False),
        sa.Column("monto_centimos", sa.Integer(), nullable=False),
        sa.Column("moneda", sa.String(length=3), server_default="PEN", nullable=False),
        sa.Column(
            "modalidad_pago", sa.String(length=20), server_default="presencial", nullable=False
        ),
        sa.Column("motivo_cancelacion", sa.String(length=300), nullable=True),
        sa.Column("cancelada_por_id", sa.Integer(), nullable=True),
        sa.Column("cancelada_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observaciones_ingreso", sa.String(length=500), nullable=True),
        sa.Column("conformidad_cliente", sa.Boolean(), nullable=True),
        sa.Column("hora_ingreso", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hora_fin_real", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hora_entrega", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "creada_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["bahia_id"], ["bahia.id"]),
        sa.ForeignKeyConstraint(["cancelada_por_id"], ["usuario.id"]),
        sa.ForeignKeyConstraint(["servicio_id"], ["servicio.id"]),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuario.id"]),
        sa.ForeignKeyConstraint(["vehiculo_id"], ["vehiculo.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reserva_codigo", "reserva", ["codigo"], unique=True)
    op.create_index("ix_reserva_usuario_id", "reserva", ["usuario_id"], unique=False)
    op.create_index("ix_reserva_bahia_id", "reserva", ["bahia_id"], unique=False)
    # RNF-002 mandates this composite index for the RN-03 overlap check.
    op.create_index("ix_reserva_bahia_inicio", "reserva", ["bahia_id", "inicio"], unique=False)
    op.create_index("ix_reserva_usuario_inicio", "reserva", ["usuario_id", "inicio"], unique=False)

    op.create_table(
        "transicion_estado",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("estado_origen", sa.String(length=30), nullable=False),
        sa.Column("estado_destino", sa.String(length=30), nullable=False),
        sa.Column("permiso_requerido", sa.String(length=60), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "estado_origen", "estado_destino", name="uq_transicion_origen_destino"
        ),
    )

    op.create_table(
        "reserva_estado_historial",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reserva_id", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(length=30), nullable=False),
        sa.Column("autor_id", sa.Integer(), nullable=True),
        sa.Column(
            "ocurrido_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["autor_id"], ["usuario.id"]),
        sa.ForeignKeyConstraint(["reserva_id"], ["reserva.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reserva_estado_historial_reserva_id",
        "reserva_estado_historial",
        ["reserva_id"],
        unique=False,
    )

    op.create_table(
        "pago",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reserva_id", sa.Integer(), nullable=False),
        sa.Column("monto_centimos", sa.Integer(), nullable=False),
        sa.Column("moneda", sa.String(length=3), server_default="PEN", nullable=False),
        sa.Column("medio", sa.String(length=20), nullable=False),
        sa.Column("estado", sa.String(length=20), server_default="confirmado", nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("referencia_externa", sa.String(length=120), nullable=True),
        sa.Column("motivo_diferencia", sa.String(length=300), nullable=True),
        sa.Column("autor_id", sa.Integer(), nullable=False),
        sa.Column(
            "registrado_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["autor_id"], ["usuario.id"]),
        sa.ForeignKeyConstraint(["reserva_id"], ["reserva.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_pago_reserva_id", "pago", ["reserva_id"], unique=False)

    op.create_table(
        "evento_dominio",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entidad", sa.String(length=40), nullable=False),
        sa.Column("entidad_id", sa.Integer(), nullable=False),
        sa.Column("accion", sa.String(length=60), nullable=False),
        sa.Column("autor_id", sa.Integer(), nullable=True),
        sa.Column("datos", TIPO_DATOS, server_default=sa.text("'{}'"), nullable=False),
        sa.Column(
            "ocurrido_en",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["autor_id"], ["usuario.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("evento_dominio")
    op.drop_index("ix_pago_reserva_id", table_name="pago")
    op.drop_table("pago")
    op.drop_index(
        "ix_reserva_estado_historial_reserva_id", table_name="reserva_estado_historial"
    )
    op.drop_table("reserva_estado_historial")
    op.drop_table("transicion_estado")
    op.drop_index("ix_reserva_usuario_inicio", table_name="reserva")
    op.drop_index("ix_reserva_bahia_inicio", table_name="reserva")
    op.drop_index("ix_reserva_bahia_id", table_name="reserva")
    op.drop_index("ix_reserva_usuario_id", table_name="reserva")
    op.drop_index("ix_reserva_codigo", table_name="reserva")
    op.drop_table("reserva")
    op.drop_table("bahia")
    op.drop_index("ix_servicio_precio_servicio_id", table_name="servicio_precio")
    op.drop_table("servicio_precio")
    op.drop_table("servicio")
    op.drop_index("ix_vehiculo_usuario_id", table_name="vehiculo")
    op.drop_table("vehiculo")
    op.drop_index("ix_usuario_correo", table_name="usuario")
    op.drop_table("usuario")
    op.drop_table("rol_permiso")
    op.drop_table("permiso")
    op.drop_table("rol")
