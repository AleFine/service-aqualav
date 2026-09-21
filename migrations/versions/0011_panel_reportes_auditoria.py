"""Panel, reports and audit trail (INC-8).

Schema for RF-033, RF-034, RF-036 and RNF-014.

The headline of this migration is what it does NOT create. The gap analysis
sketches a ``bitacora_auditoria`` table and, in the same paragraph, says that
``evento_dominio`` "es el ancestro de ``bitacora_auditoria``: se conserva y se
amplía con ``valor_anterior``/``valor_nuevo`` e índices por ``autor_id``,
``entidad`` y ``ocurrido_en`` para los filtros de RF-036". That second sentence
is the one this migration implements, and it is the better of the two:

* the event log is appended INSIDE the business transaction, so RF-036 flow 2a
  ("si falla el registro de auditoría la operación principal se revierte")
  becomes a property of the write path instead of a compensating action that
  somebody has to remember. A separate table filled afterwards could only ever
  be best effort;
* every sensitive operation RF-036 names already writes there - cancellations,
  payments, refunds, price changes, role changes - because P7 has demanded it
  since the MVP. The only one missing is authentications, and those have been
  going to ``intento_login`` since INC-3. The trail is the union of the two;
* copying either into a third table creates a second answer to "what
  happened", and two answers is none.

So:

1. ``evento_dominio`` grows ``valor_anterior`` and ``valor_nuevo`` (JSON, both
   nullable) plus the three composite indexes the RF-036 filters need. No
   back-fill: the events written before today recorded their before/after
   inside ``datos`` under keys of their own (``monto_anterior``,
   ``rol_anterior``...), and rewriting history to look like it always had
   these columns is exactly what an append-only log must not do. From today
   on, a change fills the columns; the events already written keep their
   payload, which the trail still shows in ``detalle``;
2. ``intento_login`` gets ``(usuario_id, ocurrido_en)``, which is the "filtrar
   por usuario y fecha" half of RF-036 on the authentication side. Its
   existing index is on ``(correo, ocurrido_en)`` and answers a different
   question;
3. ``reporte_exportacion`` is created for RF-034 flow 4a, the one genuinely
   new entity: an export that is answered later has to be remembered;
4. ``reporte:leer`` and ``auditoria:leer`` are registered and granted to the
   administrator alone - RF-033, RF-034 and RF-036 name no other human actor.
   Two permissions and not one, on purpose: the audit trail is not a sales
   report, and a future role that should see revenue must not inherit every
   authentication attempt in the shop along with it.

The notification template for the finished export is NOT inserted here.
``app/seed.py`` owns the ``plantilla_notificacion`` catalogue and refreshes it
on every run, exactly as migrations ``0007`` through ``0010`` argued.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: JSONB on PostgreSQL, plain JSON everywhere else - the same variant
#: ``evento_dominio.datos`` has carried since ``0001``.
CUERPO_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

#: (código, descripción, roles que lo reciben)
PERMISOS_NUEVOS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "reporte:leer",
        "Consultar el tablero de indicadores y los reportes exportables (RF-033, RF-034).",
        ("administrador",),
    ),
    (
        "auditoria:leer",
        "Consultar y exportar la bitácora de auditoría (RF-036).",
        ("administrador",),
    ),
)


def upgrade() -> None:
    conexion = op.get_bind()

    # 1. RF-036 CA-01: a change carries both of its sides.
    op.add_column("evento_dominio", sa.Column("valor_anterior", CUERPO_JSON, nullable=True))
    op.add_column("evento_dominio", sa.Column("valor_nuevo", CUERPO_JSON, nullable=True))

    # 2. The three filters RF-036 asks for: user, event type and date.
    op.create_index(
        "ix_evento_dominio_autor_momento", "evento_dominio", ["autor_id", "ocurrido_en"]
    )
    op.create_index(
        "ix_evento_dominio_accion_momento", "evento_dominio", ["accion", "ocurrido_en"]
    )
    op.create_index(
        "ix_evento_dominio_entidad_momento", "evento_dominio", ["entidad", "ocurrido_en"]
    )

    # 3. The authentication half of the same filters.
    op.create_index(
        "ix_intento_login_usuario_momento", "intento_login", ["usuario_id", "ocurrido_en"]
    )

    # 4. RF-034 flow 4a: the export that is answered later.
    op.create_table(
        "reporte_exportacion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tipo", sa.String(length=30), nullable=False),
        sa.Column("formato", sa.String(length=10), nullable=False),
        sa.Column("filtros", CUERPO_JSON, nullable=False, server_default="{}"),
        sa.Column("estado", sa.String(length=20), nullable=False, server_default="pendiente"),
        sa.Column("filas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archivo_key", sa.String(length=300), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column(
            "solicitado_por_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=False
        ),
        sa.Column(
            "solicitado_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("generado_en", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_reporte_exportacion_solicitado_por_id", "reporte_exportacion", ["solicitado_por_id"]
    )
    op.create_index(
        "ix_reporte_exportacion_estado_momento",
        "reporte_exportacion",
        ["estado", "solicitado_en"],
    )
    op.create_index(
        "ix_reporte_exportacion_solicitante",
        "reporte_exportacion",
        ["solicitado_por_id", "solicitado_en"],
    )

    # 5. The two new permissions (principle P5).
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
    """Back to INC-7: no panel, no reports, no audit columns.

    The event rows themselves are never touched. Dropping the two columns
    loses the before/after of whatever was written while they existed, which
    is unavoidable and is why the drop order puts them last: everything else
    can come back, that cannot.
    """
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

    op.drop_index("ix_reporte_exportacion_solicitante", table_name="reporte_exportacion")
    op.drop_index("ix_reporte_exportacion_estado_momento", table_name="reporte_exportacion")
    op.drop_index("ix_reporte_exportacion_solicitado_por_id", table_name="reporte_exportacion")
    op.drop_table("reporte_exportacion")

    op.drop_index("ix_intento_login_usuario_momento", table_name="intento_login")

    op.drop_index("ix_evento_dominio_entidad_momento", table_name="evento_dominio")
    op.drop_index("ix_evento_dominio_accion_momento", table_name="evento_dominio")
    op.drop_index("ix_evento_dominio_autor_momento", table_name="evento_dominio")

    op.drop_column("evento_dominio", "valor_nuevo")
    op.drop_column("evento_dominio", "valor_anterior")
