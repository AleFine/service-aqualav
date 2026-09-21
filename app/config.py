"""Application settings, loaded from environment variables / .env."""

import tempfile
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the AquaLav API."""

    # --- Persistence -----------------------------------------------------
    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/aqualav"

    # --- Security (RNF-012) ---------------------------------------------
    secret_key: str = "test-secret-key-change-me-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # --- HTTP ------------------------------------------------------------
    # Comma separated list of allowed origins. "*" is for development only.
    cors_origins: str = "*"

    # --- Domain ----------------------------------------------------------
    # Every business rule (RN-02, RN-07) is evaluated in this timezone.
    zona_horaria: str = "America/Lima"

    # RF-001: how long the e-mail verification link stays usable. It is not a
    # credential, so a generous window costs nothing and saves a resend.
    verificacion_correo_expira_horas: int = 24
    # RF-003 CA-02: THIRTY MINUTES, and the requirement says so literally.
    # It is a setting only so a demo can shorten it, never to relax it.
    recuperacion_expira_minutos: int = 30
    # Where the mobile app picks the link up. Only the simulated mail body
    # uses it, so any value boots the API.
    url_base_app: str = "https://aqualav.pe/app"

    # RN-01 v1.0 reads "at least one vehicle registered AND VERIFIED". The rule
    # is implemented (``vehiculo.verificado`` plus the counter's endpoint) and
    # this switch decides whether booking DEMANDS it. It ships off because no
    # requirement describes how a vehicle gets verified before its first visit:
    # with it on, a brand new customer could not book the appointment that
    # would let the counter verify their car (RN-01 vs RF-019 deadlock).
    exigir_vehiculo_verificado: bool = False

    # --- Simulated external providers (plan section 4) -------------------
    # Every option here ships with a working default so the API boots with no
    # ``.env`` at all; only "simulado" exists today and it never uses the
    # network. An unknown value falls back to the simulation on purpose.
    correo_proveedor: str = "simulado"
    # RF-029: the push provider. Same registry pattern as the mail one; the
    # simulation refuses a token that is not in ``dispositivo``.
    push_proveedor: str = "simulado"

    # RF-025 / RF-026: the payment gateway. The simulation is deterministic by
    # TEST CARD NUMBER (see ``app/services/proveedores/pasarela.py``) and never
    # opens a socket.
    pasarela_proveedor: str = "simulado"
    # RF-025 flow 3a: "pasarela no disponible -> se ofrece continuar con pago
    # presencial". There is no real gateway to go down, so the outage is a
    # switch. It ships UP; turning it off is how a demo shows the fallback.
    pasarela_disponible: bool = True
    # Name recorded in ``pago.pasarela`` so the reversal knows who to ask.
    pasarela_nombre: str = "simulada"

    # RF-027 / RF-023 / RF-006: the object store. Local filesystem, as the
    # plan's section 4 asks. Empty means "a directory named ``aqualav`` inside
    # the system temporary directory", which is writable everywhere and keeps
    # the checkout clean; set it to any path to keep the files around.
    almacenamiento_proveedor: str = "simulado"
    almacenamiento_directorio: str = ""
    # RF-023 flow 3a + RNF-004 M4: "la app comprime las imagenes antes de
    # subirlas (max. 1 MB)". The compression is the mobile client's job; the
    # backend's job is to REFUSE what arrives over the limit and say why, so
    # the app knows whether to compress again or ask for another picture.
    # Kilobytes, so a demo can lower it without editing code.
    archivo_tamano_maximo_kb: int = 1024

    # RF-027: the PDF generator. The simulation writes a real, minimal PDF 1.4
    # by hand - no reportlab, no dependency at all.
    documentos_proveedor: str = "simulado"
    # RF-027 CA-01: the correlative series the receipt numbers belong to.
    comprobante_serie: str = "B001"

    # RF-014 flow 2a: "la reserva se mantiene en estado Pendiente de pago
    # durante 15 MINUTOS". The requirement says fifteen literally; it is a
    # setting so a demo can shorten it, never to relax it.
    pago_en_linea_ventana_minutos: int = 15

    # RF-030 / plan section 4: the background sweep. It ships ON because a
    # reminder nobody runs is not a reminder, and it is switchable because the
    # sweep is always reachable through ``POST /interno/planificador`` - which
    # is how the test suite runs it, with the loop off.
    planificador_habilitado: bool = True
    planificador_intervalo_segundos: int = 300

    # --- Seed ------------------------------------------------------------
    seed_enabled: bool = True
    seed_admin_correo: str = "admin@aqualav.pe"
    seed_admin_password: str = "Admin1234"
    # RF-004 v1.0: ``personal`` is split, so the demo staff is two accounts.
    seed_recepcion_correo: str = "recepcion@aqualav.pe"
    seed_recepcion_password: str = "Recepcion1234"
    seed_operario_correo: str = "operario@aqualav.pe"
    seed_operario_password: str = "Operario1234"
    seed_cliente_correo: str = "cliente@aqualav.pe"
    seed_cliente_password: str = "Cliente1234"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def directorio_almacenamiento(self) -> Path:
        """Where the simulated object store keeps its files.

        A working default matters more than a pretty one here: the plan forbids
        ``.env``, so an unset value has to resolve to a directory that exists
        and is writable on any machine the project is cloned to.
        """
        if self.almacenamiento_directorio.strip():
            return Path(self.almacenamiento_directorio.strip())
        return Path(tempfile.gettempdir()) / "aqualav-almacenamiento"

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins as a list, ready for CORSMiddleware."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
