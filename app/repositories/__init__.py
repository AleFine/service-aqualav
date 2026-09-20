"""Pure data access.

Every function in this package takes a :class:`~sqlalchemy.orm.Session` as its
first argument, returns model instances (or ``None``) and knows nothing about
HTTP: no FastAPI import, no ``HTTPException``, no ``AppError``. Deciding what
an empty result means is the service layer's job, not this one's.

Repositories do not commit either; the service that opened the unit of work
owns the transaction.
"""
