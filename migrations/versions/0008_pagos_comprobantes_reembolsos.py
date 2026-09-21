"""Payments through a gateway, receipts and refunds (INC-4).

Schema for RF-025, RF-026, RF-027, RF-028, RN-05 and RN-08.

What it does:

* creates ``transaccion_pasarela`` - what was asked of the payment gateway and
  what it answered, keyed by the idempotency key. It is the table RF-026 flow
  3b stands on: when the reply never arrives the state is looked up with the
  SAME key instead of charging the customer twice;
* creates ``comprobante`` - the correlative receipt of RF-027. The unique
  constraint over ``(serie, numero_correlativo)`` is what makes the numbering a
  series and not a suggestion, and ``archivo_key`` points into the object
  store, never at a path;
* creates ``reembolso`` - the reversal of RF-028, including the one the gateway
  refused, which flow 3a says must survive as ``pendiente_manual``;
* widens ``pago`` with ``saldo_centimos`` (what is left to give back),
  ``pasarela``, ``token_tarjeta`` (**never the PAN**, RNF-013 M3) and
  ``motivo_rechazo``. ``estado`` keeps its ``String`` type on purpose: the six
  values of v1.0 are DATA, exactly like the reservation states, so growing the
  set never needs a type migration;
* widens ``reserva`` with ``expira_en`` (RF-014 flow 2a, the fifteen minutes)
  and ``penalidad_centimos`` (RN-05);
* registers ``pago:en_linea`` and ``pago:reembolsar``, and **moves the
  transition ``pendiente_pago -> confirmada`` onto ``pago:en_linea``**. That
  move is completed by the CUSTOMER paying their own booking; leaving it on
  ``pago:registrar`` would have meant handing every customer the counter's
  charging permission to let them do it (principle P5).

Three back-fills:

* ``pago.saldo_centimos`` takes ``monto_centimos`` for every payment that was
  collected and zero for any other. Leaving it at zero for all of them would
  make every historical payment look like it had already been refunded;
* ``reserva.penalidad_centimos`` takes zero, which is what the MVP's
  ``PoliticaSinPenalidad`` charged - RN-05 is not applied retroactively to
  cancellations that were free when they happened;
* ``reserva.expira_en`` stays NULL: nothing was ever born waiting for an online
  payment, so nothing has a window to expire.

The notification texts of the two new events (``comprobante`` and
``reembolso``) are NOT inserted here. ``app/seed.py`` owns that catalogue and
refreshes it on every run, exactly as migration ``0007`` argued.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (código, descripción, roles que lo reciben)
PERMISOS_NUEVOS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "pago:en_linea",
        "Elegir la modalidad de pago de una reserva propia y pagarla por la pasarela.",
        ("cliente", "recepcionista", "administrador"),
    ),
    (
        "pago:reembolsar",
        "Anular o reembolsar un pago registrado.",
        ("administrador",),
    ),
)

#: (estado_origen, estado_destino, permiso nuevo)
PERMISOS_DE_TRANSICION: tuple[tuple[str, str, str], ...] = (
    ("pendiente_pago", "confirmada", "pago:en_linea"),
)

#: Payment states that mean the money actually arrived (``ESTADOS_PAGO_COBRADO``).
ESTADOS_COBRADOS: tuple[str, ...] = ("confirmado", "reembolsado_parcial")


def upgrade() -> None:
    conexion = op.get_bind()

    # 1. RF-026: the gateway ledger, keyed by the idempotency key.
    op.create_table(
        "transaccion_pasarela",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "pago_id", sa.Integer(), sa.ForeignKey("pago.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False, unique=True),
        sa.Column("operacion", sa.String(length=20), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("monto_centimos", sa.Integer(), nullable=False),
        sa.Column("moneda", sa.String(length=3), nullable=False, server_default="PEN"),
        sa.Column("referencia_externa", sa.String(length=120), nullable=True),
        sa.Column("motivo", sa.String(length=300), nullable=True),
        sa.Column("solicitud", sa.JSON(), nullable=False),
        sa.Column("respuesta", sa.JSON(), nullable=False),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ocurrido_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_transaccion_pasarela_pago_id", "transaccion_pasarela", ["pago_id"])
    op.create_index("ix_transaccion_pasarela_reserva_id", "transaccion_pasarela", ["reserva_id"])
    op.create_index(
        "ix_transaccion_pasarela_idempotency_key",
        "transaccion_pasarela",
        ["idempotency_key"],
        unique=True,
    )

    # 2. RF-027: the correlative receipt.
    op.create_table(
        "comprobante",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "pago_id",
            sa.Integer(),
            sa.ForeignKey("pago.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("serie", sa.String(length=4), nullable=False),
        sa.Column("numero_correlativo", sa.Integer(), nullable=False),
        sa.Column("monto_centimos", sa.Integer(), nullable=False),
        sa.Column("moneda", sa.String(length=3), nullable=False, server_default="PEN"),
        sa.Column("medio_pago", sa.String(length=20), nullable=False),
        sa.Column("archivo_key", sa.String(length=200), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False, server_default="emitido"),
        sa.Column(
            "emitido_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("serie", "numero_correlativo", name="uq_comprobante_serie_numero"),
    )
    op.create_index("ix_comprobante_reserva_id", "comprobante", ["reserva_id"])

    # 3. RF-028: the reversal, processed or waiting for a human.
    op.create_table(
        "reembolso",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "pago_id", sa.Integer(), sa.ForeignKey("pago.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("tipo", sa.String(length=20), nullable=False),
        sa.Column("monto_centimos", sa.Integer(), nullable=False),
        sa.Column("moneda", sa.String(length=3), nullable=False, server_default="PEN"),
        sa.Column("motivo", sa.String(length=300), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False, server_default="procesado"),
        sa.Column("referencia_externa", sa.String(length=120), nullable=True),
        sa.Column("detalle", sa.String(length=300), nullable=True),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False, unique=True),
        sa.Column("autor_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=True),
        sa.Column(
            "registrado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_reembolso_pago_id", "reembolso", ["pago_id"])
    op.create_index("ix_reembolso_idempotency_key", "reembolso", ["idempotency_key"], unique=True)

    # 4. ``pago`` grows the gateway half (RF-026, RF-028, RNF-013).
    op.add_column(
        "pago", sa.Column("saldo_centimos", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("pago", sa.Column("pasarela", sa.String(length=40), nullable=True))
    op.add_column("pago", sa.Column("token_tarjeta", sa.String(length=60), nullable=True))
    op.add_column("pago", sa.Column("motivo_rechazo", sa.String(length=300), nullable=True))

    # 5. ``reserva`` grows the fifteen minute window and the penalty.
    op.add_column("reserva", sa.Column("expira_en", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "reserva",
        sa.Column("penalidad_centimos", sa.Integer(), nullable=False, server_default="0"),
    )

    # 6. Back-fills (see the module docstring for why each one is safe).
    conexion.execute(
        sa.text(
            "UPDATE pago SET saldo_centimos = monto_centimos "
            "WHERE estado IN :estados"
        ).bindparams(sa.bindparam("estados", value=ESTADOS_COBRADOS, expanding=True))
    )

    # 7. The two new permissions (principle P5).
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

    # 8. EXTENSION POINT P3: the move the online payment owns changes hands.
    for origen, destino, permiso in PERMISOS_DE_TRANSICION:
        conexion.execute(
            sa.text(
                "UPDATE transicion_estado SET permiso_requerido = :permiso "
                "WHERE estado_origen = :origen AND estado_destino = :destino"
            ),
            {"permiso": permiso, "origen": origen, "destino": destino},
        )


def downgrade() -> None:
    """Back to INC-5: one payment table, no gateway, no receipts, no refunds."""
    conexion = op.get_bind()

    for origen, destino, _ in PERMISOS_DE_TRANSICION:
        conexion.execute(
            sa.text(
                "UPDATE transicion_estado SET permiso_requerido = 'pago:registrar' "
                "WHERE estado_origen = :origen AND estado_destino = :destino"
            ),
            {"origen": origen, "destino": destino},
        )

    for codigo, _, _ in PERMISOS_NUEVOS:
        conexion.execute(
            sa.text(
                "DELETE FROM rol_permiso WHERE permiso_id = "
                "(SELECT id FROM permiso WHERE codigo = :codigo)"
            ),
            {"codigo": codigo},
        )
        conexion.execute(sa.text("DELETE FROM permiso WHERE codigo = :codigo"), {"codigo": codigo})

    op.drop_column("reserva", "penalidad_centimos")
    op.drop_column("reserva", "expira_en")
    op.drop_column("pago", "motivo_rechazo")
    op.drop_column("pago", "token_tarjeta")
    op.drop_column("pago", "pasarela")
    op.drop_column("pago", "saldo_centimos")

    op.drop_index("ix_reembolso_idempotency_key", table_name="reembolso")
    op.drop_index("ix_reembolso_pago_id", table_name="reembolso")
    op.drop_table("reembolso")

    op.drop_index("ix_comprobante_reserva_id", table_name="comprobante")
    op.drop_table("comprobante")

    op.drop_index("ix_transaccion_pasarela_idempotency_key", table_name="transaccion_pasarela")
    op.drop_index("ix_transaccion_pasarela_reserva_id", table_name="transaccion_pasarela")
    op.drop_index("ix_transaccion_pasarela_pago_id", table_name="transaccion_pasarela")
    op.drop_table("transaccion_pasarela")
