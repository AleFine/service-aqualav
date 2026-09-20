"""State catalogue payload (EXTENSION POINT P3)."""

from pydantic import BaseModel, ConfigDict


class EstadoCatalogoOut(BaseModel):
    """One state of ``transicion_estado``, as the aggregate screens need it.

    Every field is DERIVED from the transition table, so inserting a row is all
    it takes for a new state to reach the mobile app (RF-021 CA-03).
    """

    model_config = ConfigDict(from_attributes=True)

    #: The exact string stored in ``reserva.estado`` and emitted by the API.
    codigo: str
    #: No declared move leaves it, so a reservation there is done.
    terminal: bool
    #: It belongs to the longest declared path: the main flow of a service, as
    #: opposed to an exception branch such as cancelling. The timeline renders
    #: the steps still ahead from these.
    principal: bool
    #: Display order: topological, main flow first. A hint for the UI, never a
    #: business rule.
    orden: int
