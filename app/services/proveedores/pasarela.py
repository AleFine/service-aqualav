"""Payment gateway port and its simulated implementation (plan section 4).

RF-026 asks for the charge to go "a través de la pasarela en modo sandbox".
There is no sandbox to reach from a student project and there will not be one,
so the port exists and the default implementation is **deterministic by test
card number**: the first four digits decide the outcome, every time, with no
network and no clock involved.

.. code-block:: text

    4111 ...  aprobada
    4000 ...  rechazada (fondos insuficientes)     -> RF-026 flow 3a
    4999 ...  la respuesta se pierde               -> RF-026 flow 3b
    4555 ...  aceptada pero sin liquidar           -> estado "pendiente"
    4222 ...  aprueba el cobro y RECHAZA la reversión -> RF-028 flow 3a

``4999`` is the interesting one. The gateway DOES settle the charge - it
approves it - and the answer never makes it back. That is exactly the shape of
RF-026 flow 3b ("respuesta no recibida por tiempo de espera: el sistema
consulta el estado con la misma clave de idempotencia antes de reintentar"),
and it is the reason :meth:`ProveedorPasarela.consultar` exists: it is the only
way to learn what really happened, and retrying the charge instead would be how
a customer gets billed twice.

The ledger lives in ``transaccion_pasarela``, keyed by the idempotency key, and
a provider built with a session reads and writes it. Same shape as the mail and
push simulations, which persist into ``notificacion``: the PORT is the three
methods, persistence is a property of the INSTANCE.

RNF-013 M3: the card number never enters this module's storage. The caller
tokenizes it with :func:`tokenizar` first and only the token travels.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.config import settings
from app.models import EstadoTransaccion, OperacionPasarela
from app.repositories import pago as pago_repo

logger = logging.getLogger("aqualav.pasarela")

#: How many leading digits of the card identify the test scenario. Four is a
#: BIN prefix, not a card: it cannot be walked back into a number.
LONGITUD_PREFIJO = 4

#: Prefix of every token this module produces.
PREFIJO_TOKEN = "tok"

#: (prefijo) -> (estado del cobro, motivo, se pierde la respuesta, reversión aprobada)
TARJETAS_DE_PRUEBA: dict[str, tuple[str, str | None, bool, bool]] = {
    "4111": (EstadoTransaccion.APROBADA.value, None, False, True),
    "4000": (
        EstadoTransaccion.RECHAZADA.value,
        "La tarjeta fue rechazada por fondos insuficientes.",
        False,
        False,
    ),
    "4999": (EstadoTransaccion.APROBADA.value, None, True, True),
    "4555": (
        EstadoTransaccion.PENDIENTE.value,
        "El emisor aún no confirma la operación.",
        False,
        True,
    ),
    "4222": (EstadoTransaccion.APROBADA.value, None, False, False),
}

#: What an unrecognised prefix gets. Refusing beats silently approving: the
#: simulation is only useful while it is predictable, and a demo that pays with
#: an invented number has to be told why nothing happened.
TARJETA_DESCONOCIDA: tuple[str, str | None, bool, bool] = (
    EstadoTransaccion.RECHAZADA.value,
    "La tarjeta no corresponde a ninguna tarjeta de prueba de la pasarela simulada.",
    False,
    False,
)

#: Why a reversal is refused for ``4222``.
MOTIVO_REVERSION_RECHAZADA = "La pasarela rechazó la reversión de la transacción original."


class PasarelaNoDisponible(Exception):
    """RF-025 flow 3a: the gateway cannot be reached at all right now."""


class TiempoDeEsperaAgotado(Exception):
    """RF-026 flow 3b: the request left and no answer came back.

    It carries the idempotency key because that is the ONLY thing the caller
    may do with it: ask again with the same key before even thinking about
    retrying the charge.
    """

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            "La pasarela no respondió a tiempo a la operación "
            f"«{idempotency_key}»; consulta su estado antes de reintentar."
        )


@dataclass(frozen=True)
class ResultadoPasarela:
    """What the gateway settled for one idempotency key."""

    idempotency_key: str
    estado: str
    referencia_externa: str | None = None
    motivo: str | None = None

    @property
    def aprobada(self) -> bool:
        return self.estado == EstadoTransaccion.APROBADA.value

    @property
    def rechazada(self) -> bool:
        return self.estado == EstadoTransaccion.RECHAZADA.value


@runtime_checkable
class ProveedorPasarela(Protocol):
    """Anything able to move money for a reservation."""

    def disponible(self) -> bool:
        """Whether a charge can be attempted at all (RF-025 flow 3a)."""
        ...

    def cobrar(
        self,
        *,
        idempotency_key: str,
        reserva_id: int,
        monto_centimos: int,
        moneda: str,
        token_tarjeta: str,
        descripcion: str = "",
    ) -> ResultadoPasarela:
        """Charge, or raise :class:`TiempoDeEsperaAgotado` / :class:`PasarelaNoDisponible`."""
        ...

    def consultar(self, idempotency_key: str) -> ResultadoPasarela | None:
        """The settled outcome of that key, or ``None`` if it never reached us."""
        ...

    def reembolsar(
        self,
        *,
        idempotency_key: str,
        reserva_id: int,
        token_tarjeta: str | None,
        monto_centimos: int,
        moneda: str,
        motivo: str,
        referencia_original: str | None = None,
    ) -> ResultadoPasarela:
        """Reverse part or all of a settled charge (RF-028)."""
        ...


def prefijo_de_tarjeta(numero: str) -> str:
    """The four digits that pick the test scenario."""
    digitos = "".join(caracter for caracter in (numero or "") if caracter.isdigit())
    return digitos[:LONGITUD_PREFIJO]


def tokenizar(numero: str) -> str:
    """Turn a card number into the only thing this system is allowed to keep.

    ``tok_<prefijo>_<huella>``. The prefix is the test BIN, which is what keeps
    the simulation deterministic when the reversal comes back months later with
    no card in hand; the fingerprint is a one-way SHA-256 digest. RNF-013 M3 is
    satisfied by construction: there is no operation in this code base that
    turns a token back into a PAN, because none of the digits that identify the
    card are here.
    """
    digitos = "".join(caracter for caracter in (numero or "") if caracter.isdigit())
    huella = hashlib.sha256(digitos.encode("utf-8")).hexdigest()[:16]
    return f"{PREFIJO_TOKEN}_{prefijo_de_tarjeta(digitos)}_{huella}"


def prefijo_de_token(token: str | None) -> str:
    """Read the test prefix back out of a token, for the reversal."""
    partes = (token or "").split("_")
    return partes[1] if len(partes) >= 3 else ""


def _escenario(prefijo: str) -> tuple[str, str | None, bool, bool]:
    return TARJETAS_DE_PRUEBA.get(prefijo, TARJETA_DESCONOCIDA)


def _referencia(operacion: str, idempotency_key: str) -> str:
    """A stable external id. Deterministic, like everything else here."""
    huella = hashlib.sha256(f"{operacion}:{idempotency_key}".encode()).hexdigest()[:12]
    return f"SIM-{operacion[:3].upper()}-{huella}"


class PasarelaSimulada:
    """Deterministic, offline payment gateway. Never opens a socket.

    ``sesion`` binds the instance to ``transaccion_pasarela``, which is the
    gateway's own ledger: the row is what makes the same idempotency key
    produce the same answer for ever, which is the property RF-026 flow 3b
    depends on. An unbound instance still computes the right outcome, it just
    cannot remember it - and :meth:`consultar` then has nothing to answer with.
    """

    def __init__(
        self,
        registro: logging.Logger | None = None,
        *,
        sesion: Session | None = None,
        disponible: bool | None = None,
        nombre: str | None = None,
    ) -> None:
        self._registro = registro or logger
        self._sesion = sesion
        self._disponible = settings.pasarela_disponible if disponible is None else disponible
        self.nombre = nombre or settings.pasarela_nombre
        #: Inspection surface for the suite and for a demo, exactly like
        #: ``CorreoSimulado.enviados``.
        self.cobros: list[str] = []
        self.consultas: list[str] = []
        self.reembolsos: list[str] = []

    # -- port ------------------------------------------------------------
    def disponible(self) -> bool:
        return self._disponible

    def cobrar(
        self,
        *,
        idempotency_key: str,
        reserva_id: int,
        monto_centimos: int,
        moneda: str,
        token_tarjeta: str,
        descripcion: str = "",
    ) -> ResultadoPasarela:
        if not self._disponible:
            raise PasarelaNoDisponible("La pasarela de pagos no está disponible en este momento.")

        self.cobros.append(idempotency_key)
        fila = self._fila(idempotency_key)
        if fila is not None:
            # True idempotency: the same key never charges twice, whatever the
            # caller believes happened the first time.
            if not fila.respuesta.get("entregada", True):
                raise TiempoDeEsperaAgotado(idempotency_key)
            return self._resultado(fila)

        estado, motivo, se_pierde, _ = _escenario(prefijo_de_token(token_tarjeta))
        referencia = (
            _referencia(OperacionPasarela.COBRO.value, idempotency_key)
            if estado != EstadoTransaccion.RECHAZADA.value
            else None
        )
        fila = self._anotar(
            idempotency_key=idempotency_key,
            reserva_id=reserva_id,
            operacion=OperacionPasarela.COBRO.value,
            estado=estado,
            motivo=motivo,
            monto_centimos=monto_centimos,
            moneda=moneda,
            referencia_externa=referencia,
            solicitud={
                "token_tarjeta": token_tarjeta,
                "monto_centimos": monto_centimos,
                "moneda": moneda,
                "descripcion": descripcion,
            },
            entregada=not se_pierde,
        )
        self._registro.info(
            "pasarela cobro clave=%s estado=%s entregada=%s",
            idempotency_key,
            estado,
            not se_pierde,
        )
        if se_pierde:
            # Settled on the gateway, lost on the wire. Only ``consultar`` can
            # tell the caller what actually happened.
            raise TiempoDeEsperaAgotado(idempotency_key)
        if fila is not None:
            return self._resultado(fila)
        return ResultadoPasarela(
            idempotency_key=idempotency_key,
            estado=estado,
            referencia_externa=referencia,
            motivo=motivo,
        )

    def consultar(self, idempotency_key: str) -> ResultadoPasarela | None:
        """RF-026 flow 3b: the same key always answers the same thing.

        Asking also DELIVERS the answer that was lost, so a charge whose reply
        never arrived stops being a mystery after exactly one question.
        """
        self.consultas.append(idempotency_key)
        fila = self._fila(idempotency_key)
        if fila is None:
            return None
        if not fila.respuesta.get("entregada", True):
            pago_repo.marcar_transaccion_entregada(self._sesion, fila)
        self._registro.info("pasarela consulta clave=%s estado=%s", idempotency_key, fila.estado)
        return self._resultado(fila)

    def reembolsar(
        self,
        *,
        idempotency_key: str,
        reserva_id: int,
        token_tarjeta: str | None,
        monto_centimos: int,
        moneda: str,
        motivo: str,
        referencia_original: str | None = None,
    ) -> ResultadoPasarela:
        if not self._disponible:
            raise PasarelaNoDisponible("La pasarela de pagos no está disponible en este momento.")

        self.reembolsos.append(idempotency_key)
        fila = self._fila(idempotency_key)
        if fila is not None:
            return self._resultado(fila)

        _, _, _, reversion_aprobada = _escenario(prefijo_de_token(token_tarjeta))
        estado = (
            EstadoTransaccion.APROBADA.value
            if reversion_aprobada
            else EstadoTransaccion.RECHAZADA.value
        )
        referencia = (
            _referencia(OperacionPasarela.REEMBOLSO.value, idempotency_key)
            if reversion_aprobada
            else None
        )
        fila = self._anotar(
            idempotency_key=idempotency_key,
            reserva_id=reserva_id,
            operacion=OperacionPasarela.REEMBOLSO.value,
            estado=estado,
            motivo=None if reversion_aprobada else MOTIVO_REVERSION_RECHAZADA,
            monto_centimos=monto_centimos,
            moneda=moneda,
            referencia_externa=referencia,
            solicitud={
                "token_tarjeta": token_tarjeta,
                "monto_centimos": monto_centimos,
                "moneda": moneda,
                "motivo": motivo,
                "referencia_original": referencia_original,
            },
            entregada=True,
        )
        self._registro.info("pasarela reembolso clave=%s estado=%s", idempotency_key, estado)
        if fila is not None:
            return self._resultado(fila)
        return ResultadoPasarela(
            idempotency_key=idempotency_key,
            estado=estado,
            referencia_externa=referencia,
            motivo=None if reversion_aprobada else MOTIVO_REVERSION_RECHAZADA,
        )

    # -- ledger ----------------------------------------------------------
    def _fila(self, idempotency_key: str):
        if self._sesion is None:
            return None
        return pago_repo.obtener_transaccion(self._sesion, idempotency_key)

    def _anotar(self, *, entregada: bool, **datos):
        """Persist the settled outcome, when this instance has somewhere to."""
        if self._sesion is None:
            return None
        return pago_repo.crear_transaccion(
            self._sesion,
            respuesta={
                "estado": datos["estado"],
                "referencia_externa": datos["referencia_externa"],
                "motivo": datos["motivo"],
                "entregada": entregada,
            },
            **datos,
        )

    @staticmethod
    def _resultado(fila) -> ResultadoPasarela:
        return ResultadoPasarela(
            idempotency_key=fila.idempotency_key,
            estado=fila.estado,
            referencia_externa=fila.referencia_externa,
            motivo=fila.motivo,
        )


#: Registry of implementations, keyed by ``settings.pasarela_proveedor``.
PROVEEDORES: dict[str, type] = {"simulado": PasarelaSimulada}


def proveedor_pasarela(
    *,
    sesion: Session | None = None,
    disponible: bool | None = None,
) -> ProveedorPasarela:
    """Build the configured gateway. Unknown names fall back to the simulation."""
    clase = PROVEEDORES.get(settings.pasarela_proveedor, PasarelaSimulada)
    return clase(sesion=sesion, disponible=disponible)
