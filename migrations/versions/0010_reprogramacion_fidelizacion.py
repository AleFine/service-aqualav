"""Rescheduling and the loyalty programme (INC-7).

Schema for RF-015, RF-032, RN-06 and RN-11.

What it does:

* adds ``reserva.reprogramaciones_count``. That single integer IS RN-06: a
  booking may be moved at most twice. It sits on the reservation because
  rescheduling EDITS that row - ``inicio``, ``fin`` and ``bahia_id`` move,
  nothing is created and nothing is cancelled - which is what keeps the code
  the customer knows, the payment, the receipt, the evidence and the frozen
  breakdown attached to the service they belong to. ``reprogramada_de_id``,
  which the gap analysis sketched, is deliberately NOT added: it only means
  something if a reschedule produces a NEW reservation, and a column that
  would always be NULL is the dead column INC-2 argued against when it left
  ``reserva.paquete_id`` out. Where the booking used to be lives in
  ``evento_dominio`` (``reserva.reprogramada``), append-only and already the
  surface RF-036 will read;
* creates ``beneficio`` - what points buy (RF-032). ``stock`` NULL is
  unlimited and ``vigente_hasta`` NULL is open ended; an exhausted or expired
  row simply stops matching the listing query, which is flow 4b with nothing
  to sweep;
* creates ``puntos_movimiento`` - the ledger. ``puntos`` is SIGNED so the
  balance is one ``SUM`` and nothing stores it twice. ``uq_puntos_movimiento_pago``
  is the interesting constraint: it makes the accrual of RN-11 idempotent per
  payment, which matters because RF-026 CA-02 replays a charge with the same
  idempotency key as a matter of routine. A redemption leaves ``pago_id``
  NULL and NULL never collides, so the constraint costs redemptions nothing;
* creates ``cupon_canje`` - the entitlement half of a redeemed coupon, pointing
  at the ``promocion`` that carries the discount. Two tables rather than one
  because RF-012 already knows how to apply, freeze and report a coupon
  (RN-04 has one author); what it did NOT know is that a coupon can belong to
  one person and be spendable once, and that is this table;
* registers ``reserva:reprogramar``, ``fidelizacion:leer`` and
  ``fidelizacion:canjear`` and hands them out: rescheduling to the two actors
  RF-015 names (customer and counter), the loyalty programme to the one actor
  RF-032 names (the customer).

No back-fill is needed and none is invented. Every reservation that existed
before this migration has been rescheduled zero times, which the server default
already says, and no payment collected before the programme existed earns
points: RN-11 starts when it starts, and crediting history retroactively would
invent a liability nobody ever promised.

The reschedule notification text is NOT inserted here. ``app/seed.py`` owns the
``plantilla_notificacion`` catalogue and refreshes it on every run, exactly as
migrations ``0007``, ``0008`` and ``0009`` argued. The benefit row of RN-11 is
seeded there too, for the same reason and because its ``stock`` is state a
migration must not reset.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (código, descripción, roles que lo reciben)
PERMISOS_NUEVOS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "reserva:reprogramar",
        "Mover una reserva a otro bloque horario (RF-015).",
        ("cliente", "recepcionista", "administrador"),
    ),
    (
        "fidelizacion:leer",
        "Consultar el saldo de puntos y los beneficios canjeables.",
        ("cliente", "administrador"),
    ),
    (
        "fidelizacion:canjear",
        "Canjear puntos por un beneficio y obtener su cupón.",
        ("cliente", "administrador"),
    ),
)


def upgrade() -> None:
    conexion = op.get_bind()

    # 1. RN-06: the whole allowance, in one integer.
    op.add_column(
        "reserva",
        sa.Column("reprogramaciones_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # 2. RF-032: what the points buy.
    op.create_table(
        "beneficio",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nombre", sa.String(length=80), nullable=False, unique=True),
        sa.Column("descripcion", sa.String(length=400), nullable=True),
        sa.Column("puntos_requeridos", sa.Integer(), nullable=False),
        sa.Column("servicio_id", sa.Integer(), sa.ForeignKey("servicio.id"), nullable=True),
        # NULL = unlimited (RF-032 flow 4b counts down from an integer).
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("vigente_desde", sa.Date(), nullable=False),
        sa.Column("vigente_hasta", sa.Date(), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_beneficio_servicio_id", "beneficio", ["servicio_id"])

    # 3. RN-11: the ledger. The balance is the SUM of this column.
    op.create_table(
        "puntos_movimiento",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("usuario_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=False),
        sa.Column("tipo", sa.String(length=20), nullable=False),
        sa.Column("puntos", sa.Integer(), nullable=False),
        sa.Column("saldo_resultante", sa.Integer(), nullable=False),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "pago_id", sa.Integer(), sa.ForeignKey("pago.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("beneficio_id", sa.Integer(), sa.ForeignKey("beneficio.id"), nullable=True),
        sa.Column("base_centimos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ocurrido_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # One confirmed payment credits points exactly once, enforced by the
        # database and not only by the guard in the service.
        sa.UniqueConstraint("pago_id", name="uq_puntos_movimiento_pago"),
    )
    op.create_index("ix_puntos_movimiento_usuario_id", "puntos_movimiento", ["usuario_id"])
    op.create_index("ix_puntos_movimiento_reserva_id", "puntos_movimiento", ["reserva_id"])
    op.create_index(
        "ix_puntos_movimiento_usuario_fecha",
        "puntos_movimiento",
        ["usuario_id", "ocurrido_en"],
    )

    # 4. RF-032 "Salidas": the coupon a redemption hands the customer.
    op.create_table(
        "cupon_canje",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("usuario_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=False),
        sa.Column("beneficio_id", sa.Integer(), sa.ForeignKey("beneficio.id"), nullable=False),
        sa.Column("codigo", sa.String(length=30), nullable=False, unique=True),
        sa.Column("promocion_id", sa.Integer(), sa.ForeignKey("promocion.id"), nullable=True),
        sa.Column("estado", sa.String(length=20), nullable=False, server_default="emitido"),
        sa.Column("vence_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("usado_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "emitido_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_cupon_canje_usuario_id", "cupon_canje", ["usuario_id"])
    op.create_index("ix_cupon_canje_beneficio_id", "cupon_canje", ["beneficio_id"])
    op.create_index("ix_cupon_canje_promocion_id", "cupon_canje", ["promocion_id"])
    op.create_index("ix_cupon_canje_codigo", "cupon_canje", ["codigo"], unique=True)

    # 5. The three new permissions (principle P5).
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
    """Back to INC-6: no rescheduling, no points, no benefits."""
    conexion = op.get_bind()

    for codigo, _, _ in PERMISOS_NUEVOS:
        conexion.execute(
            sa.text(
                "DELETE FROM rol_permiso WHERE permiso_id IN "
                "(SELECT id FROM permiso WHERE codigo = :codigo)"
            ),
            {"codigo": codigo},
        )
        conexion.execute(sa.text("DELETE FROM permiso WHERE codigo = :codigo"), {"codigo": codigo})

    # The promotions a redemption materialised go with the coupons that own
    # them: leaving 100 % coupons behind with no table to validate them would
    # hand out free washes to anybody who kept a code.
    conexion.execute(
        sa.text(
            "DELETE FROM promocion WHERE id IN "
            "(SELECT promocion_id FROM cupon_canje WHERE promocion_id IS NOT NULL)"
        )
    )

    op.drop_index("ix_cupon_canje_codigo", table_name="cupon_canje")
    op.drop_index("ix_cupon_canje_promocion_id", table_name="cupon_canje")
    op.drop_index("ix_cupon_canje_beneficio_id", table_name="cupon_canje")
    op.drop_index("ix_cupon_canje_usuario_id", table_name="cupon_canje")
    op.drop_table("cupon_canje")

    op.drop_index("ix_puntos_movimiento_usuario_fecha", table_name="puntos_movimiento")
    op.drop_index("ix_puntos_movimiento_reserva_id", table_name="puntos_movimiento")
    op.drop_index("ix_puntos_movimiento_usuario_id", table_name="puntos_movimiento")
    op.drop_table("puntos_movimiento")

    op.drop_index("ix_beneficio_servicio_id", table_name="beneficio")
    op.drop_table("beneficio")

    op.drop_column("reserva", "reprogramaciones_count")
