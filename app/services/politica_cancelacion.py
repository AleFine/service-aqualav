"""Cancellation penalty policy (RN-05).

RN-05 reads, literally: cancelling **more than two hours** ahead costs
nothing; cancelling later keeps **20 %** of the amount. The MVP shipped the
rule "simplified" - ``PoliticaSinPenalidad`` returned zero for everything -
because there was no charge to keep anything out of. INC-4 brings the charge,
so the real rule replaces it and, exactly as RF-016's "cómo escala" note
promised, **not one line of the cancellation flow changed**: the policy is
still an injected object with one method.

Two decisions worth writing down:

* the penalty is computed over ``reserva.monto_centimos``, which INC-2 made
  the TOTAL of the frozen breakdown. Discounts and add-ons are therefore
  already inside it, so the 20 % is charged on what the customer was actually
  going to pay and not on a catalogue price nobody quoted them;
* the threshold is strict. "Más de dos horas" means two hours exactly is
  already late, which is the reading that never gives the shop a block it
  cannot resell.

``PoliticaSinPenalidad`` stays as a named object - the tests of the shop that
waives penalties during a promotion inject it, and it documents what the MVP
did - but it is no longer what the cancellation gets by default.
"""

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from app.core.horario import desde_bd
from app.models import Reserva
from app.schemas import Dinero
from app.services.tarifa_service import redondear

#: RN-05: "cancelación con MÁS DE 2 H de anticipación: sin penalidad".
ANTICIPACION_SIN_PENALIDAD = timedelta(hours=2)

#: RN-05: "con menos, se retiene el 20 % del monto".
PORCENTAJE_PENALIDAD = 20

#: Denominator of the percentage. Integer arithmetic all the way (P6).
BASE_PORCENTAJE = 100


@runtime_checkable
class PoliticaCancelacion(Protocol):
    """Port: how much the customer forfeits when cancelling at ``momento``."""

    def calcular_penalidad(self, reserva: Reserva, momento: datetime) -> Dinero: ...


class PoliticaSinPenalidad:
    """Free cancellation, always zero. What the MVP did (RN-05 simplified)."""

    def calcular_penalidad(self, reserva: Reserva, momento: datetime) -> Dinero:
        return Dinero.de_centimos(0, reserva.moneda)


class PoliticaRN05:
    """The rule as RN-05 writes it: free over two hours, 20 % under."""

    def __init__(
        self,
        anticipacion_sin_penalidad: timedelta = ANTICIPACION_SIN_PENALIDAD,
        porcentaje: int = PORCENTAJE_PENALIDAD,
    ) -> None:
        self.anticipacion_sin_penalidad = anticipacion_sin_penalidad
        self.porcentaje = porcentaje

    def anticipacion(self, reserva: Reserva, momento: datetime) -> timedelta:
        """How long before the booked block the cancellation arrived."""
        return desde_bd(reserva.inicio) - momento

    def calcular_penalidad(self, reserva: Reserva, momento: datetime) -> Dinero:
        if self.anticipacion(reserva, momento) > self.anticipacion_sin_penalidad:
            return Dinero.de_centimos(0, reserva.moneda)
        # Half-up in the single place a cent may appear or vanish (INC-2).
        retenido = redondear(reserva.monto_centimos * self.porcentaje, BASE_PORCENTAJE)
        return Dinero.de_centimos(retenido, reserva.moneda)


#: Default policy injected by ``reserva_service.cancelar``.
POLITICA_PREDETERMINADA: PoliticaCancelacion = PoliticaRN05()
