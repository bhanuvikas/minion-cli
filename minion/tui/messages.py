"""Shared Textual Message types for thread-safe TUI event propagation."""

from textual.message import Message


class SlotsUpdated(Message):
    """Posted (thread-safe) when slot state changes; handled by MinionApp."""


class InspectorUpdated(Message):
    """Posted (thread-safe) when subagent registry changes; handled by MinionApp."""
