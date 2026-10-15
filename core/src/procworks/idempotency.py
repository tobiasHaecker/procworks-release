# SPDX-License-Identifier: BUSL-1.1
"""Reservation of ``Idempotency-Key`` values -- in memory or in the database.

A mutating ``/v1`` call may carry an ``Idempotency-Key``. The first request
with a key runs the operation; a repetition of the *same* request does not
run it again but answers with the current state of the object the first
request created or changed, with the same status -- so a network retry can
never start a second instance or complete a task twice. Failures are never
kept -- the key stays free for a retry.

**Only a reference is kept (data minimisation).** For a key the stores keep
the kind-free id of the object the first request returned and its HTTP
status -- never the answer itself (it contained the whole case with its
data). A replay reads the object anew (:func:`procworks.api._idempotent`).

**Keyed digests.** Neither the identity, nor the key, nor the request are
stored in plain: all three go into an HMAC-SHA256 with a server secret
(:func:`keyed_digest`). An unsalted hash would let anyone with a copy of the
table confirm a guessed key or body. The secret comes from
``PROCWORKS_IDEMPOTENCY_SECRET`` (:func:`secret_from_env`) when it is set --
then even a database copy alone confirms nothing. Without it, the database
store uses a random secret created once per installation and kept in the
database (table ``server_secret``, created by the migration), so every API
process uses the same one; the memory store a random one per process.

Two backends with the same contract (:class:`IdempotencyStore`):

* :class:`InMemoryIdempotencyStore` -- one API process. A key is held by a
  lock until the unit of work of its request has ended; a concurrent request
  with the same key waits and then replays. The reference is kept only if
  the unit committed.
* ``procworks.db.SqlAlchemyIdempotencyStore`` -- with ``DATABASE_URL``, for
  any number of API processes. The key is reserved by inserting a row inside
  the transaction of the request; the reference is written into the same
  row in the same transaction.

Kept for :data:`TTL_SECONDS` (24 hours); after that a key is free again.
Expired entries are removed regularly by the API's housekeeping timer
(:meth:`IdempotencyStore.remove_expired`, every
:data:`CLEANUP_INTERVAL_SECONDS`), also when no new key arrives.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

#: How long a remembered reference replays (seconds). Long enough for any
#: retry of an integration (a worker that was down over night retries in the
#: morning), short enough that the table does not grow without bound.
TTL_SECONDS = 24 * 3600

#: Seconds between two removals of expired entries by the housekeeping timer.
CLEANUP_INTERVAL_SECONDS = 3600.0

#: The environment variable with the server secret of the keyed digests.
SECRET_VARIABLE = "PROCWORKS_IDEMPOTENCY_SECRET"

#: Smallest accepted secret length (characters) -- e.g. ``openssl rand -hex 32``
#: gives 64. A short secret could be guessed and would defeat the purpose.
MIN_SECRET_LENGTH = 32


def secret_from_env() -> bytes | None:
    """The server secret from :data:`SECRET_VARIABLE`, or ``None`` when unset.

    Empty counts as unset (Compose passes ``${VAR:-}`` through as empty).
    Leading and trailing blanks are ignored. Set it on **all** API processes
    or on none -- processes with different secrets do not recognise each
    other's keys.

    :returns: the secret as bytes, or ``None``.
    :raises ValueError: for a secret shorter than :data:`MIN_SECRET_LENGTH`;
        the start aborts with the variable's name (the value is never shown).
    """

    raw = os.environ.get(SECRET_VARIABLE)
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if len(value) < MIN_SECRET_LENGTH:
        raise ValueError(
            f"Invalid {SECRET_VARIABLE}: expected at least {MIN_SECRET_LENGTH} characters "
            "(for example the output of `openssl rand -hex 32`)."
        )
    return value.encode("utf-8")


def keyed_digest(secret: bytes, *parts: str) -> str:
    """HMAC-SHA256 of ``parts`` (joined by the unit separator) under ``secret``.

    :param secret: the server secret.
    :param parts: the values to digest (identity and key, or a request
        fingerprint).
    :returns: 64 hex characters -- fixed length whatever a client sends.
    """

    return hmac.new(secret, "\x1f".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Replay:
    """What an earlier, committed request with the same key left behind.

    :ivar same_request: whether it was the same request (endpoint, path
        parameters, body); otherwise the key is reused for another one.
    :ivar object_id: id of the object its answer showed (an instance or an
        external task).
    :ivar status: its HTTP status.
    """

    same_request: bool
    object_id: str
    status: int


class Reservation(Protocol):
    """One request's hold on one key (see :meth:`IdempotencyStore.reserve`).

    :ivar previous: the :class:`Replay` of an earlier, committed request
        with this key, or ``None`` when this request holds the key fresh and
        must run the operation.
    """

    previous: Replay | None

    def remember(self, object_id: str, status: int) -> None:
        """Keep the reference -- effective only if the unit commits."""

    def abandon(self) -> None:
        """The operation failed: the key must stay free (nothing is kept)."""

    def close(self, committed: bool) -> None:
        """End of the unit of work (always called once, also after a rollback)."""


class IdempotencyStore(Protocol):
    """Where idempotency keys are reserved and their references kept."""

    def reserve(self, subject: str, key: str, fingerprint: str) -> Reservation:
        """Reserve ``(subject, key)`` for the request ``fingerprint``.

        May wait while another request holds the same key. Must be called
        inside the request's unit of work; the caller registers
        :meth:`Reservation.close` for the end of the unit.

        :param subject: the caller's identity (keys are per identity).
        :param key: the header value.
        :param fingerprint: digest of the request (endpoint, path, body).
        :returns: the reservation; its ``previous`` decides replay or run.
        """
        ...

    def remove_expired(self) -> int:
        """Remove expired entries; returns how many were removed."""
        ...


@dataclass
class _Entry:
    fingerprint: str
    object_id: str
    status: int
    expires: float


@dataclass
class _MemoryReservation:
    """In-memory hold: the per-key lock, released at the end of the unit."""

    store: InMemoryIdempotencyStore
    name: str
    fingerprint: str
    release: Callable[[], None]
    previous: Replay | None
    _pending: list[tuple[str, int]] = field(default_factory=list)

    def remember(self, object_id: str, status: int) -> None:
        self._pending[:] = [(object_id, status)]

    def abandon(self) -> None:
        self._pending.clear()

    def close(self, committed: bool) -> None:
        try:
            if committed and self._pending:
                object_id, status = self._pending[0]
                self.store._keep(self.name, self.fingerprint, object_id, status)
        finally:
            self.release()


class InMemoryIdempotencyStore:
    """Keyed digest of ``(subject, key)`` -> reference, in one process, with expiry.

    Concurrent first requests with the same key: each key gets a lock that
    the first request keeps until its unit of work has ended, so the second
    one waits and then replays instead of running the operation a second
    time. Keys protect within this process only; with ``DATABASE_URL`` the
    database store protects across processes.

    :param ttl_seconds: how long a reference replays (default :data:`TTL_SECONDS`).
    :param clock: monotonic seconds (tests pass their own).
    :param secret: the HMAC secret; default :func:`secret_from_env` or, when
        unset, a random secret of this process (nothing outlives it anyway).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        secret: bytes | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._secret = secret or secret_from_env() or os.urandom(32)
        self._seen: dict[str, _Entry] = {}
        #: Per key: its lock and how many requests use it right now (removed
        #: again at zero, so the table does not grow with every key ever seen).
        self._holders: dict[str, tuple[threading.Lock, int]] = {}
        self._table_lock = threading.Lock()

    def reserve(self, subject: str, key: str, fingerprint: str) -> Reservation:
        """Wait until no other request works on the key, then hold it.

        :returns: the reservation; ``previous`` is the unexpired reference
            kept for the key, if any.
        """

        name = keyed_digest(self._secret, subject, key)
        request_digest = keyed_digest(self._secret, fingerprint)
        with self._table_lock:
            lock, users = self._holders.get(name, (threading.Lock(), 0))
            self._holders[name] = (lock, users + 1)
        lock.acquire()

        def release() -> None:
            lock.release()
            with self._table_lock:
                current, remaining = self._holders[name]
                if remaining <= 1:
                    del self._holders[name]
                else:
                    self._holders[name] = (current, remaining - 1)

        with self._table_lock:
            entry = self._seen.get(name)
            if entry is not None and entry.expires <= self._clock():
                del self._seen[name]
                entry = None
        previous = (
            Replay(
                hmac.compare_digest(entry.fingerprint, request_digest),
                entry.object_id,
                entry.status,
            )
            if entry is not None
            else None
        )
        return _MemoryReservation(self, name, request_digest, release, previous)

    def _keep(self, name: str, fingerprint: str, object_id: str, status: int) -> None:
        """Remember a committed reference."""

        with self._table_lock:
            self._seen[name] = _Entry(fingerprint, object_id, status, self._clock() + self._ttl)

    def remove_expired(self) -> int:
        """Drop expired entries.

        :returns: how many were dropped.
        """

        now = self._clock()
        with self._table_lock:
            stale = [n for n, e in self._seen.items() if e.expires <= now]
            for name in stale:
                del self._seen[name]
        return len(stale)


def create_idempotency_store() -> IdempotencyStore:
    """Build the store from the environment: the database with ``DATABASE_URL``
    (effective across API processes), otherwise in memory.

    :raises ValueError: for an invalid :data:`SECRET_VARIABLE` (the start
        aborts with its name).
    """

    secret_from_env()  # validate early, also for the database store
    url = os.environ.get("DATABASE_URL")
    if url:
        from procworks.db import SqlAlchemyIdempotencyStore

        return SqlAlchemyIdempotencyStore(url, create_tables=True)
    return InMemoryIdempotencyStore()
