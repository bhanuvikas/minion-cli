"""SSE (Server-Sent Events) parser for MCP Streamable HTTP transport.

SSE wire format (RFC):
    Each event is a block of field lines terminated by a blank line.

        id: 42
        event: message
        data: {"jsonrpc":"2.0",...}

    Fields:
        id:    — sets the last-event-id (used for reconnection via Last-Event-ID header)
        event: — event type, default "message"
        data:  — payload; multiple data: lines are joined with newline
        retry: — reconnection time hint (ignored here)
        :      — comment line, ignored

A blank line dispatches the accumulated event. Lines with no colon are
treated as a field with an empty value per spec (also ignored here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional


@dataclass
class SSEEvent:
    """A single dispatched Server-Sent Event."""
    data: str                    # joined data payload (may be multi-line)
    id: Optional[str] = None     # last-event-id value, or None if not set
    event: str = "message"       # event type, default per spec


class SSEParser:
    """Stateful line-by-line SSE parser.

    Feed lines one at a time via feed(). A blank line triggers dispatch and
    returns an SSEEvent if at least one data: line was seen; otherwise None.
    The parser resets its buffer after each dispatch.

    Usage (manual):
        parser = SSEParser()
        for raw_line in source:
            event = parser.feed(raw_line.rstrip("\\r\\n"))
            if event:
                process(event)

    Usage (iterator helper):
        for event in SSEParser.iter_events(http_response):
            process(event)
    """

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._id: Optional[str] = None
        self._event: str = "message"
        self._data_parts: list[str] = []

    def feed(self, line: str) -> Optional[SSEEvent]:
        """Feed one decoded, stripped line. Returns SSEEvent on blank line dispatch."""
        if line == "":
            # Blank line: dispatch event if any data was accumulated
            if self._data_parts:
                event = SSEEvent(
                    data="\n".join(self._data_parts),
                    id=self._id,
                    event=self._event,
                )
                self._reset()
                return event
            self._reset()
            return None

        if ":" in line:
            field_name, _, value = line.partition(":")
            value = value.lstrip(" ")  # single leading space is stripped per spec
            if field_name == "data":
                self._data_parts.append(value)
            elif field_name == "id":
                self._id = value
            elif field_name == "event":
                self._event = value
            # retry: and unknown fields are intentionally ignored
        # Lines with no colon are also ignored (field name with empty value)
        return None

    @staticmethod
    def iter_events(response: Iterator[bytes]) -> Iterator[SSEEvent]:
        """Yield SSEEvent objects from any bytes line iterator (e.g. HTTPResponse).

        Decodes each line as UTF-8, strips \\r\\n, and feeds to a fresh parser.
        Stops when the iterator is exhausted.
        """
        parser = SSEParser()
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            event = parser.feed(line)
            if event is not None:
                yield event
