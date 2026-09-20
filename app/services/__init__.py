"""Business rules.

Services own the transaction, raise :class:`app.core.errors.AppError`
subclasses and never import FastAPI: the same function can be called from a
router, from a management command or from a test with no HTTP in sight.
"""
