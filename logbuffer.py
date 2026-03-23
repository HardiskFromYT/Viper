"""Shared in-memory log ring-buffer.

Imported by main.py (to attach the handler) and cli.py (to read entries).
Keeping it in its own module avoids circular imports.
"""
import collections
import logging

_CAPACITY = 2000
buffer: collections.deque = collections.deque(maxlen=_CAPACITY)


class RingHandler(logging.Handler):
    """Appends formatted log records to the shared ring buffer."""
    def emit(self, record: logging.LogRecord):
        try:
            buffer.append(self.format(record))
        except Exception:
            self.handleError(record)
