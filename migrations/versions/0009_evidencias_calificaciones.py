"""Service evidence and service rating (INC-6).

Schema for RF-023, RF-031 and RN-10.

What it does:

* creates ``evidencia`` - the photographs of RF-023. Its interesting column is
  ``estado_carga``: flow 4a says a failed upload leaves the evidence pending
  with an automatic retry, so the row has to be able to exist BEFORE its bytes
  do. ``objeto_key`` is therefore nullable and points into the object store,
  never at a path, exactly like ``comprobante.archivo_key``;
* ``uq_evidencia_reserva_referencia`` is what makes the retry of CA-02 safe:
  ``referencia_cliente`` is the device's own id for the picture, so replaying
  the same upload completes the same row instead of adding a seventh
  photograph. NULL never collides, so a client that sends no reference simply
  gets a new row every time;
* creates ``calificacion`` - RF-031, with ``reserva_id`` UNIQUE, which is RN-10
  read literally: one rating per service. ``operario_id`` is a SNAPSHOT, not a
  live lookup: ``asignacion_servicio`` is deleted the moment the delivery makes
  the reservation terminal, so by the time the customer rates, who worked on it
  only survives here and in ``evento_dominio``;
* widens ``servicio`` and ``usuario`` with ``calificaciones_count`` and
  ``calificaciones_suma``. Sum and count rather than a rounded average: the two
  integers are exact, the average derived from them is exact, and recomputing
  them from ``calificacion`` after every rating makes drift impossible. They
  are the input RF-033 (INC-8) reports on;
* registers ``archivo:subir``, ``evidencia:registrar`` and
  ``calificacion:crear`` and hands them out. The evidence belongs to the two
  actors RF-023 names (counter and bay); the rating to the one RF-031 names
  (the customer); uploading a file to everybody, because a profile picture
  (RF-006) is as much a file as a photograph of a bumper.

No back-fill is needed and none is invented. Every reservation delivered before
this migration has no ``reserva.calificacion_habilitada`` event with a
``vence_en`` in it, so its window reads as "opened when the event was written"
and, for the ones delivered more than a week ago, as closed - which is what
RN-10 says about them anyway. Inventing ratings, or reopening windows that
expired before the feature existed, would be worse than leaving them alone.

The notification texts of the new ``calificacion`` event are NOT inserted here.
``app/seed.py`` owns that catalogue and refreshes it on every run, exactly as
migrations ``0007`` and ``0008`` argued.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (código, descripción, roles que lo reciben)
PERMISOS_NUEVOS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "archivo:subir",
        "Subir un archivo al almacenamiento (foto de perfil, imagen de servicio, evidencia).",
        ("cliente", "recepcionista", "operario", "administrador"),
    ),
    (
        "evidencia:registrar",
        "Registrar fotografías de evidencia de un servicio (RF-023).",
        ("recepcionista", "operario", "administrador"),
    ),
    (
        "calificacion:crear",
        "Calificar un servicio entregado propio (RF-031).",
        ("cliente", "administrador"),
    ),
)

#: (tabla, columna) pairs added to both rating aggregates.
COLUMNAS_CALIFICACION: tuple[str, ...] = ("calificaciones_count", "calificaciones_suma")
TABLAS_CALIFICABLES: tuple[str, ...] = ("servicio", "usuario")


def upgrade() -> None:
    conexion = op.get_bind()

    # 1. RF-023: the photographs, with their upload state (flow 4a).
    op.create_table(
        "evidencia",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("momento", sa.String(length=10), nullable=False),
        sa.Column("objeto_key", sa.String(length=300), nullable=True),
        sa.Column("mime", sa.String(length=100), nullable=True),
        sa.Column("tamano_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("observacion", sa.String(length=500), nullable=True),
        sa.Column("autor_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=False),
        sa.Column("estado_carga", sa.String(length=20), nullable=False, server_default="pendiente"),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("referencia_cliente", sa.String(length=80), nullable=True),
        sa.Column(
            "registrada_en",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("subida_en", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "reserva_id", "referencia_cliente", name="uq_evidencia_reserva_referencia"
        ),
    )
    op.create_index("ix_evidencia_reserva_id", "evidencia", ["reserva_id"])

    # 2. RF-031 / RN-10: one rating per service.
    op.create_table(
        "calificacion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey("reserva.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("usuario_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=False),
        sa.Column("operario_id", sa.Integer(), sa.ForeignKey("usuario.id"), nullable=True),
        sa.Column("servicio_id", sa.Integer(), sa.ForeignKey("servicio.id"), nullable=False),
        sa.Column("puntuacion", sa.Integer(), nullable=False),
        sa.Column("comentario", sa.String(length=500), nullable=True),
        sa.Column(
            "creada_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("puntuacion BETWEEN 1 AND 5", name="ck_calificacion_puntuacion"),
    )
    op.create_index("ix_calificacion_reserva_id", "calificacion", ["reserva_id"], unique=True)
    op.create_index("ix_calificacion_usuario_id", "calificacion", ["usuario_id"])
    op.create_index("ix_calificacion_operario_id", "calificacion", ["operario_id"])
    op.create_index("ix_calificacion_servicio_id", "calificacion", ["servicio_id"])

    # 3. The running averages RF-031 keeps updated and RF-033 will read.
    for tabla in TABLAS_CALIFICABLES:
        for columna in COLUMNAS_CALIFICACION:
            op.add_column(
                tabla,
                sa.Column(columna, sa.Integer(), nullable=False, server_default="0"),
            )

    # 4. The three new permissions (principle P5).
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
    """Back to INC-4: no evidence, no ratings, no generic object endpoint."""
    conexion = op.get_bind()

    for codigo, _, _ in PERMISOS_NUEVOS:
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

    for tabla in TABLAS_CALIFICABLES:
        for columna in COLUMNAS_CALIFICACION:
            op.drop_column(tabla, columna)

    op.drop_index("ix_calificacion_servicio_id", table_name="calificacion")
    op.drop_index("ix_calificacion_operario_id", table_name="calificacion")
    op.drop_index("ix_calificacion_usuario_id", table_name="calificacion")
    op.drop_index("ix_calificacion_reserva_id", table_name="calificacion")
    op.drop_table("calificacion")

    op.drop_index("ix_evidencia_reserva_id", table_name="evidencia")
    op.drop_table("evidencia")
