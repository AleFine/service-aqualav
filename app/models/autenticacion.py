"""Credential lifecycle tables (RF-001, RF-003, RF-005, RF-006).

Four append-mostly tables that turn three promises the MVP could only make on
paper into state the server can actually check:

* ``verificacion_correo`` - RF-001 leaves the account in
  ``pendiente_verificacion`` and RF-006 refuses to apply a new address until it
  is confirmed, so BOTH flows are one row here: the address being confirmed
  travels in ``correo`` and may differ from ``usuario.correo``;
* ``token_recuperacion`` - RF-003, single use and thirty minutes;
* ``token_refresco`` - the revocation list of RF-005. A refresh token is a
  self-contained JWT, so the only way to retire one before it expires is to
  keep its ``jti`` here and look it up;
* ``intento_login`` - the authentication trail RNF-014 asks for, and which
  INC-8 finally reads: the audit log of RF-036 is the union of this table and
  ``evento_dominio``, because an authentication is the one sensitive operation
  the domain event log never carried.

No raw token is ever stored: what is persisted is the SHA-256 of a
``secrets.token_urlsafe`` value, which the mail carries and the database never
sees. A slow KDF buys nothing over 256 bits of entropy, and the comparison
happens on every click.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.usuario import Usuario


def _ahora() -> datetime:
    return datetime.now(UTC)


class VerificacionCorreo(Base):
    """A pending e-mail confirmation (RF-001 step 5, RF-006 flow 3a).

    ``correo`` is the address the token confirms. When it equals the account's
    current address the confirmation activates the account; when it differs it
    is a change of address that RF-006 CA-02 forbids applying before this row
    is consumed.
    """

    __tablename__ = "verificacion_correo"
    __table_args__ = (Index("ix_verificacion_correo_usuario_id", "usuario_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False
    )
    correo: Mapped[str] = mapped_column(String(160), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verificado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_ahora, server_default=func.now()
    )

    usuario: Mapped["Usuario"] = relationship("Usuario", lazy="joined")


class TokenRecuperacion(Base):
    """A password reset token: single use, thirty minutes (RF-003)."""

    __tablename__ = "token_recuperacion"
    __table_args__ = (Index("ix_token_recuperacion_usuario_id", "usuario_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Stamped the first time the token is spent. RF-003 CA-01: from then on
    #: the very same token answers 400, exactly like an expired one.
    usado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_ahora, server_default=func.now()
    )

    usuario: Mapped["Usuario"] = relationship("Usuario", lazy="joined")


class TokenRefresco(Base):
    """One issued refresh token, addressed by its ``jti`` (RF-005).

    This is the revocation list. A refresh token is usable only while its row
    is alive: signing out stamps ``revocado_en`` on one row (CA-01) and a role
    change or a password reset stamps it on every row of the account.
    """

    __tablename__ = "token_refresco"
    __table_args__ = (Index("ix_token_refresco_usuario_revocado", "usuario_id", "revocado_en"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The ``jti`` claim of the JWT. The token itself is never stored.
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False
    )
    #: Free text the client sends so the person can tell their sessions apart.
    dispositivo: Mapped[str | None] = mapped_column(String(120), nullable=True)
    emitido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_ahora, server_default=func.now()
    )
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revocado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    motivo_revocacion: Mapped[str | None] = mapped_column(String(60), nullable=True)

    usuario: Mapped["Usuario"] = relationship("Usuario", lazy="joined")


class IntentoLogin(Base):
    """One authentication attempt, successful or not (RNF-014).

    Insert only. It keeps the e-mail that was typed, not only the account it
    resolved to, so an attempt against an address that does not exist is still
    recorded - which is precisely the shape of an enumeration attack.
    """

    __tablename__ = "intento_login"
    __table_args__ = (
        Index("ix_intento_login_correo_momento", "correo", "ocurrido_en"),
        # RF-036: "filtros por usuario ... y fecha". The index above answers a
        # different question - "what happened to this ADDRESS", which is what
        # the lockout of RNF-012 asks - and cannot serve this one.
        Index("ix_intento_login_usuario_momento", "usuario_id", "ocurrido_en"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    correo: Mapped[str] = mapped_column(String(160), nullable=False)
    usuario_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("usuario.id", ondelete="SET NULL"), nullable=True
    )
    exitoso: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: An error CODE, never a message and never the password (RNF-014).
    motivo: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ocurrido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_ahora, server_default=func.now()
    )
