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

    # --- Seed ------------------------------------------------------------
    seed_enabled: bool = True
    seed_admin_correo: str = "admin@aqualav.pe"
    seed_admin_password: str = "Admin1234"
    seed_personal_correo: str = "personal@aqualav.pe"
    seed_personal_password: str = "Personal1234"
    seed_cliente_correo: str = "cliente@aqualav.pe"
    seed_cliente_password: str = "Cliente1234"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins as a list, ready for CORSMiddleware."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
