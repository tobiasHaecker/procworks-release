# SPDX-License-Identifier: BUSL-1.1
"""Allocation of the readable ids ``<prefix>_<n>`` (instances, schemas, nodes, ...).

Why this module exists (Nachtest 2026-09-27, NT-01): the ids used to come from
three process-local ``itertools.count(1)`` counters. Every restart of the API
began again at ``1`` -- and because the stores save by *upsert*, the first new
process instance after a restart silently **replaced** the stored
``instance_1``, the first new schema replaced ``schema_1``. With PostgreSQL that
meant data loss on every update or server reboot, without any error.

Two measures close it, and both are needed:

1. :func:`reserve_existing_ids` lifts every sequence past the highest number
   already in the stores. The API calls it once at start-up (``_lifespan``), so
   a restarted process continues *after* the persisted state instead of at 1.
2. A **guard** per globally stored prefix (:meth:`IdSequence.guard`): before an
   id of that prefix is handed out, the guard asks the store whether it is
   taken and the sequence skips it. That also covers what the start-up lift
   cannot see -- e.g. a second API replica that created ids in the meantime.

Ids that only live *inside* one schema or org model (``act_``, ``data_``,
``role_`` ...) are covered by measure 1; a residual clash there is still caught
by validate-before-commit (K2) and never persisted.

The id format is deliberately unchanged (``instance_14``, ``act_3``): it is
visible in the UI, the API and BPMN exports, and existing data keeps its ids.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any

#: A readable generated id: lower-case words joined by ``_``, ending in ``_<n>``.
#: Only strings of this shape count when the stores are scanned; free text such
#: as labels or data values (``"B-1234"``) never matches.
_GENERATED_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z][a-z0-9]*)*_(\d{1,12})$")


class IdSequence:
    """Thread-safe allocator of ``<prefix>_<n>`` ids with optional store guards.

    One sequence may serve several prefixes (the model operations share one
    counter for ``schema_``, ``act_``, ``data_`` ...), exactly like the counter
    it replaces, so the numbering of existing deployments stays familiar.

    Thread safety: FastAPI runs sync endpoints in a thread pool, so two
    requests may allocate at the same time. The counter is advanced under a
    lock; the guard (a store lookup, possibly I/O) runs *outside* it.
    """

    def __init__(self) -> None:
        self._next = 1
        self._lock = threading.Lock()
        self._guards: dict[str, Callable[[str], bool]] = {}

    def guard(self, prefix: str, is_taken: Callable[[str], bool] | None) -> None:
        """Register (or with ``None`` remove) the store check for ``prefix``.

        :param prefix: the id prefix the guard applies to, e.g. ``"instance"``.
        :param is_taken: returns ``True`` when the store already holds the id.
            It must not raise for unknown ids; a raising guard aborts the
            allocation (the request fails instead of overwriting data).
        """

        with self._lock:
            if is_taken is None:
                self._guards.pop(prefix, None)
            else:
                self._guards[prefix] = is_taken

    def new(self, prefix: str) -> str:
        """Return a fresh ``<prefix>_<n>`` id that no registered guard reports taken.

        Skipped numbers are simply lost (the counter never goes backwards), which
        keeps the loop short and the ids monotonic within one process.
        """

        while True:
            with self._lock:
                number = self._next
                self._next += 1
                is_taken = self._guards.get(prefix)
            candidate = f"{prefix}_{number}"
            if is_taken is None or not is_taken(candidate):
                return candidate

    def advance_past(self, ids: Iterable[str]) -> None:
        """Move the counter behind the highest ``_<n>`` suffix among ``ids``.

        Strings that are not shaped like a generated id are ignored; the
        counter only ever moves forward, so calling this repeatedly (or with
        unrelated ids) is harmless.
        """

        highest = max_generated_number(ids)
        with self._lock:
            if highest >= self._next:
                self._next = highest + 1

    def peek(self) -> int:
        """Return the number the next allocation would try (for tests/diagnosis)."""

        with self._lock:
            return self._next

    def reset_for_tests(self, next_number: int = 1) -> None:
        """Set the counter back, **only** to simulate a fresh process in tests."""

        with self._lock:
            self._next = next_number


#: Process instances (``instance_<n>``), used by :mod:`procworks.execution`.
INSTANCE_IDS = IdSequence()
#: Everything the model operations create (schemas, nodes, data elements, form
#: fields, connectors, templates, embedded org entries ...), :mod:`procworks.operations`.
MODEL_IDS = IdSequence()
#: Shared organisation models and their roles/units/agents, :mod:`procworks.org`.
ORG_IDS = IdSequence()

_ALL_SEQUENCES = (INSTANCE_IDS, MODEL_IDS, ORG_IDS)


def max_generated_number(ids: Iterable[str]) -> int:
    """Return the largest ``_<n>`` suffix among generated-looking ids (0 if none)."""

    highest = 0
    for value in ids:
        match = _GENERATED_ID.match(value)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def generated_strings(value: Any) -> Iterable[str]:
    """Yield every string (keys included) of a JSON-like structure.

    Used on ``model_dump(mode="json")`` of stored schemas, org models and
    templates: node ids, element ids, field ids ... sit in many different
    places, and scanning every string is simpler and safer than listing them.
    Over-matching only lifts the counter further, which is harmless.
    """

    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from generated_strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from generated_strings(item)


def reserve_existing_ids(
    *,
    instance_ids: Iterable[str] = (),
    documents: Iterable[Any] = (),
) -> int:
    """Lift **every** sequence past the ids already persisted; return the new floor.

    :param instance_ids: ids of stored process instances.
    :param documents: stored schemas, org models, templates -- pydantic models
        (dumped here) or plain JSON-like data; every string inside counts.
    :returns: the highest number found (0 for empty stores).

    All sequences get the same floor on purpose: which counter produced a
    given ``x_<n>`` in the past is not recorded, and a higher floor never hurts.
    """

    highest = max_generated_number(instance_ids)
    for document in documents:
        if document is None:  # vanished between list_ids() and get()
            continue
        dump = document.model_dump(mode="json") if hasattr(document, "model_dump") else document
        highest = max(highest, max_generated_number(generated_strings(dump)))
    for sequence in _ALL_SEQUENCES:
        sequence.advance_past([f"id_{highest}"] if highest else [])
    return highest
