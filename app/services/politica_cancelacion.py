"""Cancellation penalty policy (RN-05, simplified for the MVP).

The MVP charges nothing up front, so there is nothing to withhold: the penalty
is always zero. The rule lives in its own injectable object because that is the
entire point of RF-016's "cómo escala" note - in v0.3 the 20 % rule and the
refund (RF-028) replace this class, and RF-016's flow does not change one line.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.models import Reserva
from app.schemas import Dinero


@runtime_checkable
class PoliticaCancelacion(Protocol):
    """Port: how much the customer forfeits when cancelling at ``momento``."""

    def calcular_penalidad(self, reserva: Reserva, momento: datetime) -> Dinero: ...


class PoliticaSinPenalidad:
    """MVP implementation: free cancellation, always zero (RN-05 simplified)."""

    def calcular_penalidad(self, reserva: Reserva, momento: datetime) -> Dinero:
        return Dinero.de_centimos(0, reserva.moneda)


#: Default policy injected by ``reserva_service.cancelar``.
POLITICA_PREDETERMINADA: PoliticaCancelacion = PoliticaSinPenalidad()
