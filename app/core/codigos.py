"""Reservation code generation (RF-014: unique ``AQL-XXXXXX`` code)."""

import secrets

PREFIJO_RESERVA = "AQL"
LONGITUD_SUFIJO = 6

# Uppercase letters + digits, minus the visually ambiguous ones (I, O, 0, 1).
# The code is read out loud and typed by hand at the counter (RF-019).
ALFABETO_CODIGO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generar_codigo_reserva() -> str:
    """Return a cryptographically random reservation code, e.g. ``AQL-7K2M9Q``."""
    sufijo = "".join(secrets.choice(ALFABETO_CODIGO) for _ in range(LONGITUD_SUFIJO))
    return f"{PREFIJO_RESERVA}-{sufijo}"
