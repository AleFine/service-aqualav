"""Tariffs, packages and promotions (INC-2).

Schema for RF-009 (delta), RF-010 (delta), RF-011, RF-012, RN-04 and RN-12.

What it does:

* creates ``factor_tipo_vehiculo`` - RN-04 was the one business rule the MVP
  did not enforce at all, because a service had a single price for every
  vehicle. The factor is stored in THOUSANDTHS (``1300`` = 1.3) so the tariff
  is computed with integers only: ``3000 x 1.3`` in binary floating point is
  ``3899.9999999999995``, and RF-012 CA-01 demands exactly 3900;
* creates ``paquete`` / ``paquete_servicio`` and ``promocion`` (RF-011);
* creates ``servicio_adicional`` and ``reserva_adicional`` - the "adicionales"
  term of RN-04, frozen by name and amount on the reservation;
* creates ``reserva_tarifa_desglose``, the traceable and auditable breakdown
  RF-012 asks for: one row per reservation, written once, never updated;
* adds ``servicio.imagen_url`` (RF-009 v1.0);
* registers the permission ``promocion:administrar`` and grants it to the
  administrator, who is the only actor RF-011 names.

No data is back-filled on purpose. A reservation created before this migration
keeps its frozen ``monto_centimos`` and simply has no breakdown row: inventing
one would mean claiming a factor and a promotion that were never applied, and
``reserva.tarifa`` is nullable precisely so the API can say "no consta".

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# NOTE: the chain uses the short identifiers "0004"/"0005", not the file names.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: RF-011 names the administrator as the only actor of packages and promotions.
PERMISOS_NUEVOS: tuple[tuple[str, str], ...] = (
    ("promocion:administrar", "Crear, editar y desactivar paquetes y promociones."),
)
ROL_ADMINISTRADOR = "administrador"

MONEDA = "PEN"


def upgrade() -> None:
    conexion = op.get_bind()

    # 1. Vehicle factors (RF-010 delta, RN-04). Versioned like servicio_precio.
    op.create_table(
        "factor_tipo_vehiculo",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "servicio_id",
            sa.Integer(),
            sa.ForeignKey("servicio.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("tipo_vehiculo", sa.String(length=20), nullable=False),
        sa.Column("factor_milesimas", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column(
            "vigente_desde", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("vigente_hasta", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_factor_tipo_vehiculo_servicio_id", "factor_tipo_vehiculo", ["servicio_id"]
    )
    op.create_index(
        "ix_factor_servicio_tipo",
        "factor_tipo_vehiculo",
        ["servicio_id", "tipo_vehiculo", "vigente_desde"],
    )

    # 2. Packages (RF-011).
    op.create_table(
        "paquete",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nombre", sa.String(length=80), nullable=False, unique=True),
        sa.Column("descripcion", sa.String(length=400), nullable=False),
        sa.Column("precio_centimos", sa.Integer(), nullable=False),
        sa.Column("moneda", sa.String(length=3), nullable=False, server_default=MONEDA),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("vigente_desde", sa.Date(), nullable=False),
        sa.Column("vigente_hasta", sa.Date(), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "paquete_servicio",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "paquete_id",
            sa.Integer(),
            sa.ForeignKey("paquete.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("servicio_id", sa.Integer(), sa.ForeignKey("servicio.id"), nullable=False),
        sa.Column("cantidad", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("paquete_id", "servicio_id", name="uq_paquete_servicio"),
    )
    op.create_index("ix_paquete_servicio_paquete_id", "paquete_servicio", ["paquete_id"])
    op.create_index("ix_paquete_servicio_servicio_id", "paquete_servicio", ["servicio_id"])

    # 3. Promotions (RF-011). The non-overlap rule of flow 2a is enforced in
    #    the service layer: it only binds AUTOMATIC promotions (no coupon), and
    #    a partial unique index over a date range is not portable to SQLite,
    #    which is what the test suite runs on.
    op.create_table(
        "promocion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nombre", sa.String(length=80), nullable=False, unique=True),
        sa.Column("descripcion", sa.String(length=400), nullable=True),
        sa.Column("tipo_descuento", sa.String(length=20), nullable=False),
        sa.Column("valor", sa.Integer(), nullable=False),
        sa.Column(
            "servicio_id",
            sa.Integer(),
            sa.ForeignKey("servicio.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "paquete_id",
            sa.Integer(),
            sa.ForeignKey("paquete.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("codigo_cupon", sa.String(length=30), nullable=True),
        sa.Column("dias_semana", sa.String(length=20), nullable=True),
        sa.Column("vigente_desde", sa.Date(), nullable=False),
        sa.Column("vigente_hasta", sa.Date(), nullable=True),
        sa.Column("activa", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "creada_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_promocion_servicio_id", "promocion", ["servicio_id"])
    op.create_index("ix_promocion_paquete_id", "promocion", ["paquete_id"])
    op.create_index("ix_promocion_codigo_cupon", "promocion", ["codigo_cupon"], unique=True)
    op.create_index(
        "ix_promocion_servicio_vigencia", "promocion", ["servicio_id", "vigente_desde"]
    )

    # 4. Add-ons (the "adicionales" term of RN-04).
    op.create_table(
        "servicio_adicional",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nombre", sa.String(length=80), nullable=False, unique=True),
        sa.Column("descripcion", sa.String(length=400), nullable=True),
        sa.Column("monto_centimos", sa.Integer(), nullable=False),
        sa.Column("moneda", sa.String(length=3), nullable=False, server_default=MONEDA),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "reserva_adicional",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "servicio_adicional_id",
            sa.Integer(),
            sa.ForeignKey("servicio_adicional.id"),
            nullable=True,
        ),
        sa.Column("nombre", sa.String(length=80), nullable=False),
        sa.Column("monto_centimos", sa.Integer(), nullable=False),
        sa.Column("moneda", sa.String(length=3), nullable=False, server_default=MONEDA),
        sa.UniqueConstraint("reserva_id", "servicio_adicional_id", name="uq_reserva_adicional"),
    )
    op.create_index("ix_reserva_adicional_reserva_id", "reserva_adicional", ["reserva_id"])

    # 5. The breakdown RF-012 asks for: written once, next to the reservation.
    op.create_table(
        "reserva_tarifa_desglose",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("precio_base_centimos", sa.Integer(), nullable=False),
        sa.Column("tipo_vehiculo", sa.String(length=20), nullable=False),
        sa.Column("factor_milesimas", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("base_ajustada_centimos", sa.Integer(), nullable=False),
        sa.Column("adicionales_centimos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("descuento_centimos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("promocion_id", sa.Integer(), sa.ForeignKey("promocion.id"), nullable=True),
        sa.Column("promocion_nombre", sa.String(length=80), nullable=True),
        sa.Column("cupon_aplicado", sa.String(length=30), nullable=True),
        sa.Column("cupon_rechazado", sa.String(length=30), nullable=True),
        sa.Column("motivo_rechazo_cupon", sa.String(length=300), nullable=True),
        sa.Column("total_centimos", sa.Integer(), nullable=False),
        sa.Column("moneda", sa.String(length=3), nullable=False, server_default=MONEDA),
        sa.Column("incidencia", sa.String(length=300), nullable=True),
        sa.Column(
            "calculado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_reserva_tarifa_desglose_reserva_id",
        "reserva_tarifa_desglose",
        ["reserva_id"],
        unique=True,
    )

    # 6. RF-009 v1.0: the reference picture of a service.
    op.add_column("servicio", sa.Column("imagen_url", sa.String(length=300), nullable=True))

    # 7. The permission RF-011 needs, granted to the administrator.
    for codigo, descripcion in PERMISOS_NUEVOS:
        conexion.execute(
            sa.text(
                "INSERT INTO permiso (codigo, descripcion) "
                "SELECT :codigo, :descripcion "
                "WHERE NOT EXISTS (SELECT 1 FROM permiso WHERE codigo = :codigo)"
            ),
            {"codigo": codigo, "descripcion": descripcion},
        )
        conexion.execute(
            sa.text(
                "INSERT INTO rol_permiso (rol_id, permiso_id) "
                "SELECT r.id, p.id FROM rol r, permiso p "
                "WHERE r.nombre = :rol AND p.codigo = :codigo "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM rol_permiso rp WHERE rp.rol_id = r.id AND rp.permiso_id = p.id"
                ")"
            ),
            {"rol": ROL_ADMINISTRADOR, "codigo": codigo},
        )


def downgrade() -> None:
    """Back to INC-1B: one price per service, no factors and no promotions."""
    conexion = op.get_bind()

    for codigo, _ in PERMISOS_NUEVOS:
        conexion.execute(
            sa.text(
                "DELETE FROM rol_permiso WHERE permiso_id = "
                "(SELECT id FROM permiso WHERE codigo = :codigo)"
            ),
            {"codigo": codigo},
        )
        conexion.execute(
            sa.text("DELETE FROM permiso WHERE codigo = :codigo"), {"codigo": codigo}
        )

    op.drop_column("servicio", "imagen_url")

    op.drop_index("ix_reserva_tarifa_desglose_reserva_id", table_name="reserva_tarifa_desglose")
    op.drop_table("reserva_tarifa_desglose")
    op.drop_index("ix_reserva_adicional_reserva_id", table_name="reserva_adicional")
    op.drop_table("reserva_adicional")
    op.drop_table("servicio_adicional")

    op.drop_index("ix_promocion_servicio_vigencia", table_name="promocion")
    op.drop_index("ix_promocion_codigo_cupon", table_name="promocion")
    op.drop_index("ix_promocion_paquete_id", table_name="promocion")
    op.drop_index("ix_promocion_servicio_id", table_name="promocion")
    op.drop_table("promocion")

    op.drop_index("ix_paquete_servicio_servicio_id", table_name="paquete_servicio")
    op.drop_index("ix_paquete_servicio_paquete_id", table_name="paquete_servicio")
    op.drop_table("paquete_servicio")
    op.drop_table("paquete")

    op.drop_index("ix_factor_servicio_tipo", table_name="factor_tipo_vehiculo")
    op.drop_index("ix_factor_tipo_vehiculo_servicio_id", table_name="factor_tipo_vehiculo")
    op.drop_table("factor_tipo_vehiculo")
