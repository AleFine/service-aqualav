"""Password policy validator (RNF-012 / RNF-009 M1).

The exact same rules are mirrored by the mobile client so the user never
reaches the server with a password the client could have rejected.
"""

LONGITUD_MINIMA = 8

# Human readable description of every unmet rule, in Spanish (RNF-009 M3).
MENSAJE_LONGITUD = f"Debe tener al menos {LONGITUD_MINIMA} caracteres."
MENSAJE_MAYUSCULA = "Debe incluir al menos una letra mayúscula."
MENSAJE_MINUSCULA = "Debe incluir al menos una letra minúscula."
MENSAJE_DIGITO = "Debe incluir al menos un número."


def validar_politica(password: str) -> list[str]:
    """Return the descriptions of every policy criterion the password fails.

    An empty list means the password is acceptable.
    """
    password = password or ""
    faltantes: list[str] = []

    if len(password) < LONGITUD_MINIMA:
        faltantes.append(MENSAJE_LONGITUD)
    if not any(caracter.isupper() for caracter in password):
        faltantes.append(MENSAJE_MAYUSCULA)
    if not any(caracter.islower() for caracter in password):
        faltantes.append(MENSAJE_MINUSCULA)
    if not any(caracter.isdigit() for caracter in password):
        faltantes.append(MENSAJE_DIGITO)

    return faltantes


def cumple_politica(password: str) -> bool:
    """Convenience predicate over :func:`validar_politica`."""
    return not validar_politica(password)
