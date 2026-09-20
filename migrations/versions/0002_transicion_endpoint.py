"""Declare which endpoint owns each transition (P3).

``transicion_estado`` already governs the state machine; these two columns keep
the ownership of a move in the same table, so a state inserted in v0.4 carries
its own rules as data and no code has to enumerate states (principle P3).

``endpoint``          the operation that owns the move. When it is set, the
                      generic ``POST /reservas/{id}/estado`` refuses the move,
                      so the invariants and side effects of that operation
                      (RN-09, RF-024 CA-01, RF-016 CA-03) cannot be skipped.
``marca_fin_servicio``reaching this destination means the service is over, which
                      is what stamps ``reserva.hora_fin_real`` (RF-022 CA-02).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transicion_estado",
        sa.Column("endpoint", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "transicion_estado",
        sa.Column(
            "marca_fin_servicio",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # Backfill for the five states of the MVP. Rows inserted later carry their
    # own values; nothing here is read back by the application as a list.
    op.execute(
        "UPDATE transicion_estado SET endpoint = 'check_in' "
        "WHERE estado_origen = 'confirmada' AND estado_destino = 'en_atencion'"
    )
    op.execute(
        "UPDATE transicion_estado SET endpoint = 'cancelacion' "
        "WHERE estado_destino = 'cancelada'"
    )
    op.execute(
        "UPDATE transicion_estado SET endpoint = 'check_out' WHERE estado_destino = 'entregado'"
    )
    op.execute(
        "UPDATE transicion_estado SET marca_fin_servicio = true "
        "WHERE estado_destino = 'finalizado'"
    )


def downgrade() -> None:
    op.drop_column("transicion_estado", "marca_fin_servicio")
    op.drop_column("transicion_estado", "endpoint")
