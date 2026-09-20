"""Password policy validator (RNF-012 / RNF-009 M1).

The exact same rules are mirrored by the mobile client so the user never
reaches the server with a password the client could have rejected.
"""

import secrets

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


# Alphabet of a generated temporary password (RF-035): the ambiguous glyphs are
# out because the worker reads it from an e-mail and types it once.
ALFABETO_MAYUSCULAS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
ALFABETO_MINUSCULAS = "abcdefghijkmnopqrstuvwxyz"
ALFABETO_DIGITOS = "23456789"
LONGITUD_TEMPORAL = 12


def generar_password_temporal() -> str:
    """Return a random password that satisfies :func:`validar_politica` by construction.

    RF-035: the administrator never chooses the worker's password; the system
    generates it and the mail provider delivers it. One character of each
    required class is placed first and then the whole string is shuffled, so
    the policy holds without a retry loop.
    """
    alfabeto = ALFABETO_MAYUSCULAS + ALFABETO_MINUSCULAS + ALFABETO_DIGITOS
    caracteres = [
        secrets.choice(ALFABETO_MAYUSCULAS),
        secrets.choice(ALFABETO_MINUSCULAS),
        secrets.choice(ALFABETO_DIGITOS),
    ]
    caracteres += [secrets.choice(alfabeto) for _ in range(LONGITUD_TEMPORAL - len(caracteres))]
    secrets.SystemRandom().shuffle(caracteres)
    return "".join(caracteres)
