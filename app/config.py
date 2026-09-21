"""Application settings, loaded from environment variables / .env."""

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
    def cors_origin_list(self) -> list[str]:
        """CORS origins as a list, ready for CORSMiddleware."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
