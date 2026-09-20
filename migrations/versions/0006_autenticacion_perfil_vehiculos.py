"""Credentials, profile and vehicle lifecycle (INC-3).

Schema for RF-001 (delta), RF-002 (delta), RF-003, RF-005, RF-006, RF-008 and
RN-01.

What it does:

* creates ``verificacion_correo`` - RF-001 leaves a self-registered account in
  ``pendiente_verificacion`` and RF-006 CA-02 refuses to apply a new address
  until it is confirmed. Both are the same row: the address being confirmed
  travels in ``correo`` and may differ from ``usuario.correo``;
* creates ``token_recuperacion`` - RF-003, single use and thirty minutes;
* creates ``token_refresco`` - the revocation list RF-005 needs. A refresh
  token is a self-contained JWT, so the ONLY way to retire one before it
  expires is to keep its ``jti`` here. This is what turns the hook INC-1A left
  in ``auth_service.revocar_tokens_de_refresco`` into a real revocation;
* creates ``intento_login`` - the authentication trail of RNF-014, which
  RF-036 (INC-8) will expose;
* adds the v1.0 columns of ``usuario``: the identity document with a
  uniqueness of its own, the profile picture key, the language and the two
  notification switches (RF-006), plus the consent and deactivation stamps;
* adds ``vehiculo.verificado`` (RN-01), ``verificado_en`` and
  ``desactivado_en`` (RF-008, the deletion is logical);
* registers ``vehiculo:editar``, ``vehiculo:eliminar`` and
  ``vehiculo:verificar`` and grants them.

Two back-fills, both conservative:

* ``consentimiento_privacidad_en`` takes ``creado_en``. Every account that
  exists went through ``RegistroIn``, which has always refused a registration
  without the tick, so the date the account was created IS the date the policy
  was accepted. Leaving it NULL would claim nobody ever consented;
* ``verificado`` is set on the vehicles that already have a reservation. The
  shop has had those cars in a bay: pretending otherwise would lock their
  owners out the day a shop turns ``EXIGIR_VEHICULO_VERIFICADO`` on. A vehicle
  registered and never brought in stays unverified, which is the truth.

Nothing back-fills ``estado_cuenta``: no account was ever left unverified, and
moving live accounts into ``pendiente_verificacion`` would invent a mail nobody
was ever sent.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
# NOTE: the chain uses the short identifiers "0005"/"0006", not the file names.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (código, descripción, roles que lo reciben)
PERMISOS_NUEVOS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "vehiculo:editar",
        "Editar los datos de un vehículo propio.",
        ("cliente", "administrador"),
    ),
    (
        "vehiculo:eliminar",
        "Dar de baja un vehículo propio.",
        ("cliente", "administrador"),
    ),
    (
        "vehiculo:verificar",
        "Verificar que la placa del vehículo corresponde (RN-01).",
        ("recepcionista", "administrador"),
    ),
)

IDIOMA_PREDETERMINADO = "es"


def upgrade() -> None:
    conexion = op.get_bind()

    # 1. E-mail verification (RF-001 step 5, RF-006 flow 3a).
    op.create_table(
        "verificacion_correo",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "usuario_id",
            sa.Integer(),
            sa.ForeignKey("usuario.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("correo", sa.String(length=160), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expira_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verificado_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "creada_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_verificacion_correo_usuario_id", "verificacion_correo", ["usuario_id"])
    op.create_index(
        "ix_verificacion_correo_token_hash", "verificacion_correo", ["token_hash"], unique=True
    )

    # 2. Password recovery (RF-003): single use, thirty minutes.
    op.create_table(
        "token_recuperacion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "usuario_id",
            sa.Integer(),
            sa.ForeignKey("usuario.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expira_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usado_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "creado_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_token_recuperacion_usuario_id", "token_recuperacion", ["usuario_id"])
    op.create_index(
        "ix_token_recuperacion_token_hash", "token_recuperacion", ["token_hash"], unique=True
    )

    # 3. The revocation list of RF-005.
    op.create_table(
        "token_refresco",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("jti", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "usuario_id",
            sa.Integer(),
            sa.ForeignKey("usuario.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dispositivo", sa.String(length=120), nullable=True),
        sa.Column(
            "emitido_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expira_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revocado_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("motivo_revocacion", sa.String(length=60), nullable=True),
    )
    op.create_index("ix_token_refresco_jti", "token_refresco", ["jti"], unique=True)
    op.create_index(
        "ix_token_refresco_usuario_revocado", "token_refresco", ["usuario_id", "revocado_en"]
    )

    # 4. The authentication trail (RNF-014). Insert only.
    op.create_table(
        "intento_login",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("correo", sa.String(length=160), nullable=False),
        sa.Column(
            "usuario_id",
            sa.Integer(),
            sa.ForeignKey("usuario.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("exitoso", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("motivo", sa.String(length=40), nullable=True),
        sa.Column(
            "ocurrido_en", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_intento_login_correo_momento", "intento_login", ["correo", "ocurrido_en"])

    # 5. The v1.0 columns of ``usuario`` (RF-001 delta, RF-006, RNF-018).
    op.add_column("usuario", sa.Column("tipo_documento", sa.String(length=30), nullable=True))
    op.add_column("usuario", sa.Column("numero_documento", sa.String(length=20), nullable=True))
    op.add_column("usuario", sa.Column("foto_perfil_key", sa.String(length=300), nullable=True))
    op.add_column(
        "usuario",
        sa.Column(
            "idioma",
            sa.String(length=5),
            nullable=False,
            server_default=IDIOMA_PREDETERMINADO,
        ),
    )
    op.add_column(
        "usuario",
        sa.Column("notificar_push", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "usuario",
        sa.Column("notificar_correo", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "usuario",
        sa.Column("consentimiento_privacidad_en", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "usuario", sa.Column("desactivado_en", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_usuario_numero_documento", "usuario", ["numero_documento"], unique=True
    )

    # 6. Vehicle lifecycle (RF-008) and RN-01.
    op.add_column(
        "vehiculo",
        sa.Column("verificado", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "vehiculo", sa.Column("verificado_en", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "vehiculo", sa.Column("desactivado_en", sa.DateTime(timezone=True), nullable=True)
    )

    # 7. Back-fills (see the module docstring for why each one is safe).
    conexion.execute(
        sa.text(
            "UPDATE usuario SET consentimiento_privacidad_en = creado_en "
            "WHERE consentimiento_privacidad_en IS NULL"
        )
    )
    conexion.execute(
        sa.text(
            "UPDATE vehiculo SET verificado = true "
            "WHERE id IN (SELECT DISTINCT vehiculo_id FROM reserva)"
        )
    )

    # 8. The three permissions RF-008 and RN-01 need.
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
    """Back to INC-2: no revocation list, no verification, no vehicle lifecycle."""
    conexion = op.get_bind()

    for codigo, _, _ in PERMISOS_NUEVOS:
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

    op.drop_column("vehiculo", "desactivado_en")
    op.drop_column("vehiculo", "verificado_en")
    op.drop_column("vehiculo", "verificado")

    op.drop_index("ix_usuario_numero_documento", table_name="usuario")
    op.drop_column("usuario", "desactivado_en")
    op.drop_column("usuario", "consentimiento_privacidad_en")
    op.drop_column("usuario", "notificar_correo")
    op.drop_column("usuario", "notificar_push")
    op.drop_column("usuario", "idioma")
    op.drop_column("usuario", "foto_perfil_key")
    op.drop_column("usuario", "numero_documento")
    op.drop_column("usuario", "tipo_documento")

    op.drop_index("ix_intento_login_correo_momento", table_name="intento_login")
    op.drop_table("intento_login")

    op.drop_index("ix_token_refresco_usuario_revocado", table_name="token_refresco")
    op.drop_index("ix_token_refresco_jti", table_name="token_refresco")
    op.drop_table("token_refresco")

    op.drop_index("ix_token_recuperacion_token_hash", table_name="token_recuperacion")
    op.drop_index("ix_token_recuperacion_usuario_id", table_name="token_recuperacion")
    op.drop_table("token_recuperacion")

    op.drop_index("ix_verificacion_correo_token_hash", table_name="verificacion_correo")
    op.drop_index("ix_verificacion_correo_usuario_id", table_name="verificacion_correo")
    op.drop_table("verificacion_correo")
