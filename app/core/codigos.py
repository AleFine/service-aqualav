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


# The QR printed on the reception ticket (RF-019 v1.0). It is a SEPARATE,
# longer token instead of the reservation code so a code read out loud at the
# counter can never be turned into a scannable credential, and so revoking or
# reissuing one never touches the code the customer already knows.
PREFIJO_QR = "AQLQR"
LONGITUD_QR = 12


def generar_codigo_qr() -> str:
    """Return the opaque token the reservation QR encodes, e.g. ``AQLQR-7K2M9Q4XTBWD``."""
    sufijo = "".join(secrets.choice(ALFABETO_CODIGO) for _ in range(LONGITUD_QR))
    return f"{PREFIJO_QR}-{sufijo}"
