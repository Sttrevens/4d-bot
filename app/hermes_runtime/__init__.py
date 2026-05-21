"""Hermes Agent OS runtime adapter.

This package is the only production boundary allowed to know about Hermes
runtime concepts. Business code calls the typed contract here instead of
importing upstream Hermes internals directly.
"""

from app.hermes_runtime.types import RuntimeRequest, RuntimeResponse

__all__ = ["RuntimeRequest", "RuntimeResponse"]
