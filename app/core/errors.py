"""Uniform error contract for the whole API.

Every 4xx/5xx response has exactly this body::

    {"error": {"codigo": "...", "mensaje": "...", "detalles": [{"campo": "...", "mensaje": "..."}]}}

``mensaje`` is Spanish, user facing, and states both the cause and the
corrective action (RNF-009 M3). ``detalles`` is ``[]`` when not applicable.
"""

import re
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

Detalle = dict[str, Any]


def detalle(campo: str | None, mensaje: str) -> Detalle:
    """Build one entry of the ``detalles`` array."""
    return {"campo": campo, "mensaje": mensaje}


def cuerpo_error(codigo: str, mensaje: str, detalles: list[Detalle] | None = None) -> dict:
    """Build the uniform error body."""
    return {"error": {"codigo": codigo, "mensaje": mensaje, "detalles": list(detalles or [])}}


class AppError(Exception):
    """Base class for every domain error raised by the service layer.

    Services raise these; routers never build error responses by hand.
    """

    codigo: str = "ERROR_INTERNO"
    mensaje: str = "Ocurrió un error inesperado. Inténtalo nuevamente en unos minutos."
    http_status: int = 500

    def __init__(
        self,
        mensaje: str | None = None,
        detalles: list[Detalle] | None = None,
        *,
        codigo: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.codigo = codigo or type(self).codigo
        self.mensaje = mensaje or type(self).mensaje
        self.http_status = http_status or type(self).http_status
        self.detalles: list[Detalle] = list(detalles or [])
        super().__init__(self.mensaje)

    def a_dict(self) -> dict:
        """Serialize to the uniform error body."""
        return cuerpo_error(self.codigo, self.mensaje, self.detalles)

    def a_respuesta(self) -> JSONResponse:
        """Serialize to the final JSON response."""
        return JSONResponse(status_code=self.http_status, content=self.a_dict())


# --------------------------------------------------------------------------
# Authentication and accounts
# --------------------------------------------------------------------------
class CorreoYaRegistrado(AppError):
    codigo = "CORREO_YA_REGISTRADO"
    http_status = 409
    mensaje = (
        "El correo ingresado ya está registrado. Inicia sesión o usa otro correo "
        "para crear la cuenta."
    )


class CredencialesInvalidas(AppError):
    codigo = "CREDENCIALES_INVALIDAS"
    http_status = 401
    mensaje = "El correo o la contraseña son incorrectos. Verifica los datos e inténtalo de nuevo."


class CuentaBloqueada(AppError):
    codigo = "CUENTA_BLOQUEADA"
    http_status = 429
    mensaje = (
        "La cuenta está bloqueada temporalmente por intentos fallidos. "
        "Espera unos minutos antes de volver a intentarlo."
    )


class NoAutenticado(AppError):
    codigo = "NO_AUTENTICADO"
    http_status = 401
    mensaje = "Tu sesión no es válida o expiró. Vuelve a iniciar sesión para continuar."


class PermisoDenegado(AppError):
    codigo = "PERMISO_DENEGADO"
    http_status = 403
    mensaje = (
        "No tienes permisos para realizar esta acción. "
        "Contacta al administrador si la necesitas."
    )


class CuentaDesactivada(AppError):
    """RF-035 CA-01: a deactivated internal account may not sign in.

    Deliberately a 403 and not the 401 of ``CREDENCIALES_INVALIDAS``: the
    credentials WERE right, so telling the worker their account was disabled is
    what lets them ask the administrator instead of resetting a password that
    is not the problem. It reveals nothing a colleague does not already know.
    """

    codigo = "CUENTA_DESACTIVADA"
    http_status = 403
    mensaje = (
        "Tu cuenta está desactivada y no puede iniciar sesión. "
        "Pide al administrador del local que vuelva a activarla."
    )


class CambioDeRolPropioDenegado(AppError):
    """RF-004 flow 3a: nobody may take their own administration away."""

    codigo = "CAMBIO_DE_ROL_PROPIO"
    http_status = 422
    mensaje = (
        "No puedes quitarte a ti mismo la administración de roles: el local quedaría "
        "sin quien administre los accesos. Pide a otro administrador que haga el cambio."
    )


# --------------------------------------------------------------------------
# Vehicles
# --------------------------------------------------------------------------
class PlacaDuplicada(AppError):
    codigo = "PLACA_DUPLICADA"
    http_status = 409
    mensaje = (
        "Ya registraste un vehículo con esa placa. " "Usa otra placa o edita el vehículo existente."
    )


class PlacaInvalida(AppError):
    codigo = "PLACA_INVALIDA"
    http_status = 422
    mensaje = "La placa no tiene un formato válido. Usa el formato ABC-123, A1B-123 o 1234-AB."


# --------------------------------------------------------------------------
# Reservations
# --------------------------------------------------------------------------
class ReservaAnticipacionInsuficiente(AppError):
    codigo = "RESERVA_ANTICIPACION_INSUFICIENTE"
    http_status = 422
    mensaje = (
        "La reserva debe solicitarse con al menos 60 minutos de anticipación. "
        "Elige un bloque más adelante en el día."
    )


class ReservaFueraDeHorario(AppError):
    codigo = "RESERVA_FUERA_DE_HORARIO"
    http_status = 422
    mensaje = (
        "El horario elegido está fuera del horario de atención "
        "(lunes a sábado de 08:00 a 19:00, domingos de 09:00 a 14:00). "
        "Selecciona un bloque dentro de ese rango."
    )


class ReservaBloqueOcupado(AppError):
    codigo = "RESERVA_BLOQUE_OCUPADO"
    http_status = 409
    mensaje = (
        "El bloque horario que elegiste acaba de ocuparse. "
        "Selecciona otro de los bloques disponibles."
    )


class TransicionInvalida(AppError):
    codigo = "TRANSICION_INVALIDA"
    http_status = 422
    mensaje = (
        "La reserva no puede pasar a ese estado desde su estado actual. "
        "Actualiza la vista y elige una de las acciones disponibles."
    )


class RetrasoRequiereConfirmacion(AppError):
    codigo = "RETRASO_REQUIERE_CONFIRMACION"
    http_status = 409
    mensaje = (
        "El cliente llegó con más de 20 minutos de retraso. "
        "Confirma el retraso para registrar el ingreso de todas formas."
    )


# --------------------------------------------------------------------------
# Agenda, bays and assignment (RF-018, RF-020, RF-035)
# --------------------------------------------------------------------------
class FranjaConReservas(AppError):
    """RF-018 flow 4a / CA-02: resolve the bookings before blocking the slot."""

    codigo = "FRANJA_CON_RESERVAS"
    http_status = 409
    mensaje = (
        "La franja que quieres bloquear tiene reservas activas. "
        "Reubícalas o cancélalas antes de aplicar el bloqueo."
    )


class BahiaConReservas(AppError):
    """A bay still holding work cannot be deactivated."""

    codigo = "BAHIA_CON_RESERVAS"
    http_status = 409
    mensaje = (
        "La bahía tiene reservas activas asignadas. "
        "Reubícalas o ciérralas antes de desactivarla."
    )


class SinOperarioDisponible(AppError):
    """RF-020: there is nobody who can execute the service."""

    codigo = "SIN_OPERARIO_DISPONIBLE"
    http_status = 409
    mensaje = (
        "No hay operarios activos a quienes asignar el servicio. "
        "Registra o reactiva un operario antes de asignar."
    )


class OperarioOcupado(AppError):
    """RF-020 flow 3a: the chosen operator already has work in hand."""

    codigo = "OPERARIO_OCUPADO"
    http_status = 409
    mensaje = (
        "El operario elegido ya tiene un servicio en curso. "
        "Confirma la asignación de todas formas o elige a otro operario."
    )


class UsuarioConServiciosEnCurso(AppError):
    """RF-035 flow 4a / CA-02: reassign the work before disabling the account."""

    codigo = "USUARIO_CON_SERVICIOS_EN_CURSO"
    http_status = 409
    mensaje = (
        "El usuario tiene servicios en curso asignados. "
        "Reasígnalos a otra persona antes de desactivar la cuenta."
    )


class LimiteDeBahias(AppError):
    """RE-07: the shop has a fixed number of physical bays."""

    codigo = "LIMITE_DE_BAHIAS"
    http_status = 422
    mensaje = (
        "El local no admite más bahías activas de las que tiene físicamente. "
        "Desactiva una bahía antes de habilitar otra."
    )


class NombreDeBahiaDuplicado(AppError):
    codigo = "NOMBRE_DE_BAHIA_DUPLICADO"
    http_status = 409
    mensaje = "Ya existe una bahía con ese nombre. Usa otro nombre para identificarla."


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------
class PagoPendiente(AppError):
    codigo = "PAGO_PENDIENTE"
    http_status = 422
    mensaje = (
        "La reserva no tiene un pago confirmado. " "Registra el pago antes de entregar el vehículo."
    )


class IdempotencyKeyRequerida(AppError):
    codigo = "IDEMPOTENCY_KEY_REQUERIDA"
    http_status = 400
    mensaje = (
        "Falta la cabecera Idempotency-Key para registrar el pago. "
        "Reintenta la operación enviando una clave única."
    )


# --------------------------------------------------------------------------
# Generic
# --------------------------------------------------------------------------
class RecursoNoEncontrado(AppError):
    codigo = "RECURSO_NO_ENCONTRADO"
    http_status = 404
    mensaje = "No encontramos el recurso solicitado. Verifica los datos e inténtalo de nuevo."


class ErrorDeValidacion(AppError):
    """Raised by services for the same shape Pydantic produces (``VALIDACION``)."""

    codigo = "VALIDACION"
    http_status = 422
    mensaje = (
        "Algunos datos enviados no son válidos. "
        "Revisa los campos marcados y vuelve a intentarlo."
    )


class DatosInvalidos(AppError):
    codigo = "DATOS_INVALIDOS"
    http_status = 422
    mensaje = (
        "Los datos enviados no permiten completar la operación. " "Revísalos e inténtalo de nuevo."
    )


# Every concrete error code exposed by the API, keyed by code.
ERRORES_POR_CODIGO: dict[str, type[AppError]] = {
    clase.codigo: clase
    for clase in (
        CorreoYaRegistrado,
        CredencialesInvalidas,
        CuentaBloqueada,
        CuentaDesactivada,
        NoAutenticado,
        PermisoDenegado,
        CambioDeRolPropioDenegado,
        PlacaDuplicada,
        PlacaInvalida,
        ReservaAnticipacionInsuficiente,
        ReservaFueraDeHorario,
        ReservaBloqueOcupado,
        TransicionInvalida,
        RetrasoRequiereConfirmacion,
        FranjaConReservas,
        BahiaConReservas,
        SinOperarioDisponible,
        OperarioOcupado,
        UsuarioConServiciosEnCurso,
        LimiteDeBahias,
        NombreDeBahiaDuplicado,
        PagoPendiente,
        IdempotencyKeyRequerida,
        RecursoNoEncontrado,
        ErrorDeValidacion,
        DatosInvalidos,
    )
}

# Fallback code for a bare HTTPException raised outside the domain layer.
CODIGOS_POR_ESTADO: dict[int, str] = {
    400: DatosInvalidos.codigo,
    401: NoAutenticado.codigo,
    403: PermisoDenegado.codigo,
    404: RecursoNoEncontrado.codigo,
    422: ErrorDeValidacion.codigo,
}

# Pydantic emits English messages; the ones users actually hit are translated.
_TRADUCCIONES_PYDANTIC: dict[str, str] = {
    "Field required": "Este campo es obligatorio.",
    "Input should be a valid integer": "Debe ser un número entero.",
    "Input should be a valid boolean": "Debe ser verdadero o falso.",
    "Input should be a valid string": "Debe ser un texto.",
    "Input should be a valid datetime": "Debe ser una fecha y hora válida.",
    "Input should be a valid date": "Debe ser una fecha válida.",
    "Input should be a valid list": "Debe ser una lista de valores.",
}

_PREFIJO_VALOR = "Value error, "

# Pydantic renders an enum rejection as "Input should be 'a', 'b' or 'c'", with
# the accepted values inlined. No fixed prefix can capture it, so the values are
# pulled out of the quotes and re-stated in Spanish (RNF-009 M3).
_PREFIJO_ENUM = "Input should be '"
_VALORES_ENTRECOMILLADOS = re.compile(r"'([^']*)'")


def _mensaje_pydantic(mensaje: str) -> str:
    """Turn one Pydantic error message into user facing Spanish copy."""
    if mensaje.startswith(_PREFIJO_VALOR):
        # Custom validators already raise Spanish messages.
        return mensaje[len(_PREFIJO_VALOR) :]
    if mensaje.startswith(_PREFIJO_ENUM):
        valores = _VALORES_ENTRECOMILLADOS.findall(mensaje)
        if valores:
            return f"Valor no válido. Usa uno de: {', '.join(valores)}."
    for ingles, espanol in _TRADUCCIONES_PYDANTIC.items():
        if mensaje.startswith(ingles):
            return espanol
    return mensaje


def _campo_pydantic(loc: tuple) -> str | None:
    """Build the ``campo`` name from a Pydantic error location."""
    partes = [str(parte) for parte in loc if parte not in ("body", "query", "path", "header")]
    return ".".join(partes) if partes else None


def registrar_manejadores(app: FastAPI) -> None:
    """Install the exception handlers that enforce the uniform error body."""

    @app.exception_handler(AppError)
    async def _manejar_app_error(_request, exc: AppError) -> JSONResponse:
        return exc.a_respuesta()

    @app.exception_handler(RequestValidationError)
    async def _manejar_validacion(_request, exc: RequestValidationError) -> JSONResponse:
        detalles = [
            detalle(_campo_pydantic(error.get("loc", ())), _mensaje_pydantic(error.get("msg", "")))
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=cuerpo_error(ErrorDeValidacion.codigo, ErrorDeValidacion.mensaje, detalles),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _manejar_http(_request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            # Already a uniform body (e.g. re-raised by a dependency).
            return JSONResponse(status_code=exc.status_code, content=detail)

        if exc.status_code >= 500:
            codigo = AppError.codigo
            mensaje_defecto = AppError.mensaje
        else:
            codigo = CODIGOS_POR_ESTADO.get(exc.status_code, DatosInvalidos.codigo)
            mensaje_defecto = ERRORES_POR_CODIGO[codigo].mensaje

        mensaje = detail if isinstance(detail, str) and detail else mensaje_defecto
        return JSONResponse(
            status_code=exc.status_code,
            content=cuerpo_error(codigo, mensaje),
            headers=getattr(exc, "headers", None),
        )

    # FastAPI's HTTPException derives from Starlette's, but register it too so
    # the mapping is explicit and independent of that inheritance.
    app.add_exception_handler(HTTPException, _manejar_http)
