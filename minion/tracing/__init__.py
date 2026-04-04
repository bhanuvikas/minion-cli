"""Nefario — lightweight observability for minion-cli.

Public API:
    get_tracer()    — returns the active Tracer or a NullTracer (no-op)
    init_tracer()   — creates and registers the session tracer (call from cli.py)
    Tracer          — the real tracer class
    NullTracer      — the no-op tracer class
"""

from .tracer import NullTracer, Tracer, get_tracer, init_tracer

__all__ = ["get_tracer", "init_tracer", "Tracer", "NullTracer"]
