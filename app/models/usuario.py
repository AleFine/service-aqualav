"""Application user (replaces the template's ``users`` table)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.calidad import Calificable
from app.models.enums import EstadoCuenta, Idioma

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.bahia import Bahia
    from app.models.reserva import Reserva
    from app.models.rol import Rol
    from app.models.vehiculo import Vehiculo


class Usuario(Calificable, Base):
    __tablename__ = "usuario"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombres: Mapped[str] = mapped_column(String(80), nullable=False)
    apellidos: Mapped[str] = mapped_column(String(80), nullable=False)
    correo: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    telefono: Mapped[str] = mapped_column(String(20), nullable=False)
    #: RF-001 v1.0: the identity document, with a uniqueness of its own on the
    #: number. Nullable because the accounts RF-035 creates for the staff are
    #: not asked for one, and because every account that predates v1.0 has none.
    tipo_documento: Mapped[str | None] = mapped_column(String(30), nullable=True)
    numero_documento: Mapped[str | None] = mapped_column(
        String(20), unique=True, index=True, nullable=True
    )
    hash_password: Mapped[str] = mapped_column(String(255), nullable=False)
    rol_id: Mapped[int] = mapped_column(Integer, ForeignKey("rol.id"), nullable=False)
    #: RF-035: the bay an operator usually works in. It only PRE-SELECTS the
    #: suggestion of RF-020; it never reserves the bay for them.
    bahia_habitual_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bahia.id"), nullable=True
    )
    #: RF-006: the profile picture, as a key of the object storage. It is the
    #: key and not the bytes: INC-6 brings ``ProveedorAlmacenamiento`` and the
    #: endpoint that serves it, and the column already holds what it will read.
    foto_perfil_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: RF-006 / RF-029: preferences. The language picks the notification
    #: template; the two switches decide which channels INC-5 may use. A
    #: customer who turns push off still gets the e-mail (RF-029).
    idioma: Mapped[str] = mapped_column(
        String(5), nullable=False, default=Idioma.ES.value, server_default=Idioma.ES.value
    )
    notificar_push: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    notificar_correo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    #: RNF-018: when the person accepted the privacy policy. ``RegistroIn``
    #: already refuses a registration without the tick; this is the receipt.
    consentimiento_privacidad_en: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When the account stopped being usable (RF-035, RF-008 for the person).
    desactivado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estado_cuenta: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=EstadoCuenta.ACTIVA.value,
        server_default=EstadoCuenta.ACTIVA.value,
    )
    #: RF-031: "promedio del OPERARIO actualizado", the other input RF-033
    #: reports on. It stays zero for everybody who never worked a service,
    #: which is exactly what ``calificacion_promedio`` reads back as ``None``.
    calificaciones_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    calificaciones_suma: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    intentos_fallidos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    bloqueado_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    rol: Mapped["Rol"] = relationship("Rol", back_populates="usuarios", lazy="joined")
    bahia_habitual: Mapped[Optional["Bahia"]] = relationship("Bahia", lazy="joined")
    vehiculos: Mapped[list["Vehiculo"]] = relationship(
        "Vehiculo", back_populates="usuario", cascade="all, delete-orphan"
    )
    reservas: Mapped[list["Reserva"]] = relationship(
        "Reserva", back_populates="usuario", foreign_keys="Reserva.usuario_id"
    )

    @property
    def nombre_completo(self) -> str:
        """``"Ana Torres"`` - used as the ``autor`` label in payloads."""
        return f"{self.nombres} {self.apellidos}".strip()
