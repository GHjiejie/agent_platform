"""Trace context carries opaque IDs only, never identity or credentials."""

from contextvars import ContextVar

request_id = ContextVar("platform_request_id", default=None)


def audit_details(details=None):
    return {**(details or {}), "request_id": request_id.get()}
