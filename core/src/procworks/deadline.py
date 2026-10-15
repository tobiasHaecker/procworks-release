# SPDX-License-Identifier: BUSL-1.1
"""A total time limit for one network exchange (all reads and writes together).

Socket timeouts limit each single operation: a peer that sends its answer
one byte every few seconds keeps every read short and the exchange as a
whole runs on without end. Deliveries are reserved only for their time limit
plus a margin; one that outlives the reservation is taken over by another
API process and goes out twice. :class:`Deadline` closes that gap: when the
total time is up it shuts the watched sockets down, which ends a blocked
read or write at once with an error, and the caller reports the attempt as
failed (:meth:`Deadline.raise_if_expired`).
"""

from __future__ import annotations

import socket
import threading
import time
from types import TracebackType


class Deadline:
    """Cut the watched sockets off once ``seconds`` have passed.

    Use as a context manager around the whole exchange and register every
    socket it reads from or writes to with :meth:`watch` -- also a socket
    that replaces another one (a TLS wrapper takes over the descriptor of the
    plain socket, which can no longer be shut down through the old object).

    :param seconds: the total time limit (greater than 0).
    """

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._started = time.monotonic()
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        #: ``True`` once the limit was reached (the sockets are shut down).
        self.expired = False

    def __enter__(self) -> Deadline:
        self._started = time.monotonic()
        self._timer = threading.Timer(self._seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def remaining(self) -> float:
        """Seconds left (at least a tiny positive value, for socket timeouts)."""

        return max(0.001, self._seconds - (time.monotonic() - self._started))

    def watch(self, sock: socket.socket) -> None:
        """Shut ``sock`` down when the limit is reached (at once if it was)."""

        with self._lock:
            self._sockets.append(sock)
            expired = self.expired
        if expired:
            _shut(sock)

    def raise_if_expired(self, cause: BaseException | None = None) -> None:
        """Turn the error of a cut-off exchange into a clear ``TimeoutError``.

        :param cause: the error the exchange ended with (chained).
        :raises TimeoutError: when the limit was reached, naming it.
        """

        if self.expired:
            raise TimeoutError(
                f"no complete answer within {self._seconds:g} s (total time limit)"
            ) from cause

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            sockets = list(self._sockets)
        for sock in sockets:
            _shut(sock)


def _shut(sock: socket.socket) -> None:
    """Shut a socket down in both directions; a closed one is ignored."""

    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
