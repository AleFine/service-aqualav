"""Hardening pass over the closed backend (INC-8B).

Two things an existing installation cannot get from a code change alone.

1. **RF-015: the receptionist may see the free blocks.** The requirement names
   the Customer AND the Receptionist as its actors, and step 3 of its flow is
   "el sistema muestra los bloques disponibles alternativos". The counter held
   ``reserva:reprogramar`` and ``agenda:leer`` - which answers "what is taken"
   - but not ``disponibilidad:leer``, which answers "what is free", so
   ``GET /disponibilidad`` replied 403 to the very actor the requirement puts
   in front of it. ``app/seed.py`` grants it from now on; this grants it to
   the databases that already exist.

2. **``transaccion_pasarela.solicitud`` / ``.respuesta`` become ``jsonb`` on
   PostgreSQL.** The ORM has declared them
   ``JSON().with_variant(JSONB, "postgresql")`` since INC-4 - the same variant
   ``evento_dominio.datos`` has carried since ``0001`` - but migration ``0008``
   created them as plain ``json``. Nothing was broken by it (``json`` stores
   and returns the same document) yet the two descriptions of the schema
   disagreed, and a drift nobody can see is the kind that gets discovered by a
   query plan. ``jsonb`` is also the only one of the two that can be indexed
   and compared, which is what a gateway payload eventually gets asked for.

   The conversion is guarded by dialect because it is a PostgreSQL fact:
   SQLite has no ``json`` type at all (both render as ``TEXT`` affinity there,
   which is why ``tests/test_migraciones.py`` cannot see this particular
   drift and says so), so on SQLite this step is correctly a no-op rather
   than a workaround.

The other two drifts the audit found - ``factor_tipo_vehiculo``
``factor_milesimas`` and ``intento_login.exitoso`` carrying a ``DEFAULT`` in
the migration and none in the ORM - are fixed on the ORM side instead: the
migrations already say the right thing and section 1.8 of the plan forbids
editing one that shipped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (role, permission) grants this migration adds. Written as data so the
#: downgrade is the same list read backwards and cannot drift from it.
CONCESIONES: tuple[tuple[str, str], ...] = (("recepcionista", "disponibilidad:leer"),)

#: The two gateway payload columns and how each dialect spells "a document".
COLUMNAS_JSON: tuple[str, ...] = ("solicitud", "respuesta")


def _conceder(conexion: sa.engine.Connection, rol: str, codigo: str) -> None:
    """Grant ``codigo`` to ``rol``, idempotently, by NAME lookup.

    Naming a role here is exactly what principle P5 allows a data migration to
    do and forbids everywhere else: this is the table that TEACHES the system
    which permissions a role has, so it is the one place where the answer
    cannot itself be looked up.
    """
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


def upgrade() -> None:
    conexion = op.get_bind()

    # 1. RF-015: the counter can see a free block, not only a busy one.
    for rol, codigo in CONCESIONES:
        _conceder(conexion, rol, codigo)

    # 2. Align the gateway payload columns with the ORM (PostgreSQL only).
    if conexion.dialect.name == "postgresql":
        for columna in COLUMNAS_JSON:
            op.alter_column(
                "transaccion_pasarela",
                columna,
                existing_type=sa.JSON(),
                type_=postgresql.JSONB(),
                existing_nullable=False,
                postgresql_using=f"{columna}::jsonb",
            )


def downgrade() -> None:
    """Back to INC-8: plain ``json`` payloads and a counter that cannot look.

    The grant is removed only for the exact pair this migration inserted. A
    shop that later granted the same permission on purpose loses it here,
    which is the price of a reversible grant and the reason the pair is
    spelled out instead of deleting by permission code alone.
    """
    conexion = op.get_bind()

    if conexion.dialect.name == "postgresql":
        for columna in COLUMNAS_JSON:
            op.alter_column(
                "transaccion_pasarela",
                columna,
                existing_type=postgresql.JSONB(),
                type_=sa.JSON(),
                existing_nullable=False,
                postgresql_using=f"{columna}::json",
            )

    for rol, codigo in CONCESIONES:
        conexion.execute(
            sa.text(
                "DELETE FROM rol_permiso WHERE rol_id IN "
                "(SELECT id FROM rol WHERE nombre = :rol) AND permiso_id IN "
                "(SELECT id FROM permiso WHERE codigo = :codigo)"
            ),
            {"rol": rol, "codigo": codigo},
        )
