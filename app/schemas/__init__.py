"""Pydantic v2 schemas. Field names mirror the API contract verbatim."""

from app.schemas.auth import LoginIn, RefreshIn, RegistroIn, TokenOut
from app.schemas.common import (
    TAMANIO_PAGINA_DEFECTO,
    TAMANIO_PAGINA_MAXIMO,
    ContenidoError,
    DetalleError,
    Dinero,
    ErrorBody,
    Lista,
    Pagina,
    nombre_de_autor,
)
from app.schemas.disponibilidad import BloqueDisponible, DisponibilidadOut
from app.schemas.estado import EstadoCatalogoOut
from app.schemas.pago import PagoCrear, PagoOut
from app.schemas.reserva import (
    BahiaResumen,
    CambioEstadoIn,
    CancelacionIn,
    CancelacionOut,
    CheckInIn,
    CheckOutIn,
    HistorialItem,
    ReservaCrear,
    ReservaOut,
)
from app.schemas.servicio import ServicioActualizar, ServicioCrear, ServicioOut, ServicioResumen
from app.schemas.usuario import ClienteResumen, UsuarioOut
from app.schemas.vehiculo import VehiculoIn, VehiculoOut, VehiculoResumen

__all__ = [
    "TAMANIO_PAGINA_DEFECTO",
    "TAMANIO_PAGINA_MAXIMO",
    "BahiaResumen",
    "BloqueDisponible",
    "CambioEstadoIn",
    "CancelacionIn",
    "CancelacionOut",
    "CheckInIn",
    "CheckOutIn",
    "ClienteResumen",
    "ContenidoError",
    "DetalleError",
    "Dinero",
    "DisponibilidadOut",
    "ErrorBody",
    "EstadoCatalogoOut",
    "HistorialItem",
    "Lista",
    "LoginIn",
    "Pagina",
    "PagoCrear",
    "PagoOut",
    "RefreshIn",
    "RegistroIn",
    "ReservaCrear",
    "ReservaOut",
    "ServicioActualizar",
    "ServicioCrear",
    "ServicioOut",
    "ServicioResumen",
    "TokenOut",
    "UsuarioOut",
    "VehiculoIn",
    "VehiculoOut",
    "VehiculoResumen",
    "nombre_de_autor",
]
