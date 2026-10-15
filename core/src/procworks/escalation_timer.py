# SPDX-License-Identifier: BUSL-1.1
"""Background timer of the escalation sweep (modelled overdue reactions).

The sweep fires the escalation stages that have become due. It used to run
before every worklist *read* -- over every case, with its schema and
organisation -- so every worklist, and above all the monitoring (one task
read per running case), grew with the stock. Now the API process runs it in
the background at a fixed interval, and reads stay pure reads.

This module only holds the timing: reading the interval from the
environment and a stoppable thread that calls a given pass again and again.
What one pass does (which cases, one unit of work per case, the bound per
pass) lives in :func:`procworks.api._escalation_sweep`.

**Several API processes.** Every process runs its own timer. Two passes may
find the same due case; each escalates it in its own unit of work with the
conditional save of the case, so only the first one stores it -- the other's
unit (event, mail queue entry, raised stage counter) is rolled back and the
case skipped. A stage therefore fires once, without a lock across processes.
A random start offset keeps the timers of several processes from running in
lockstep.
"""

from __future__ import annotations

import logging
import math
import os
import random
import threading
from collections.abc import Callable

from procworks.env_numbers import plain_decimal

#: The environment variable for the interval in seconds.
INTERVAL_VARIABLE = "PROCWORKS_ESCALATION_SWEEP_SECONDS"

#: Default interval: once a minute. Escalation stages are modelled in minutes
#: to days (time values in seconds, typically hours); a stage firing up to a
#: minute late changes nothing that matters, while a pass over the cases of
#: the schemas with escalation rules costs little even at a large stock.
DEFAULT_INTERVAL_SECONDS = 60.0

#: Smallest interval other than 0 (off). A pass every few milliseconds was a
#: busy loop -- about a thousand database transactions a second and most of
#: a CPU core -- for no gain: stages are modelled in minutes to days.
MIN_INTERVAL_SECONDS = 1.0
#: Largest interval (one day); longer would leave stages unfired for days.
MAX_INTERVAL_SECONDS = 86400.0

#: Pause before the next pass when a pass stopped at its bound with more due
#: cases left: the backlog is worked off in bounded steps without waiting a
#: whole interval, and still leaves room for requests in between.
CATCH_UP_PAUSE_SECONDS = 1.0

#: Name of the timer thread (logs, tests, thread dumps).
THREAD_NAME = "procworks-escalation-sweep"

_log = logging.getLogger("procworks.escalation_timer")


def sweep_interval() -> float:
    """The interval of the background sweep from the environment, in seconds.

    ``PROCWORKS_ESCALATION_SWEEP_SECONDS``:

    * unset or empty (Compose passes ``${VAR:-}`` through as empty): the
      default :data:`DEFAULT_INTERVAL_SECONDS`;
    * ``0``: no background sweep in this process -- for a process that should
      not escalate (e.g. when exactly one designated process runs the timer),
      or for tests; ``POST /admin/escalations/sweep`` still works;
    * a plain decimal number of seconds from :data:`MIN_INTERVAL_SECONDS` to
      :data:`MAX_INTERVAL_SECONDS` (``"7.5"`` is allowed).

    :returns: the interval in seconds; ``0.0`` means off.
    :raises ValueError: for anything else -- no plain number
        (:func:`procworks.env_numbers.plain_decimal`: ``1e3``, ``1_0``,
        ``nan`` ...), negative, below one second (a busy loop) or above a
        day -- naming the variable and the bounds, so the start aborts with a
        clear message instead of escalating never or in a busy loop.
    """

    raw = os.environ.get(INTERVAL_VARIABLE)
    if raw is None or not raw.strip():
        return DEFAULT_INTERVAL_SECONDS
    value = plain_decimal(raw)
    if value is not None and (
        value == 0 or MIN_INTERVAL_SECONDS <= value <= MAX_INTERVAL_SECONDS
    ):
        return value
    raise ValueError(
        f"Invalid {INTERVAL_VARIABLE}={raw!r}: expected 0 (off) or a number of seconds "
        f"from {MIN_INTERVAL_SECONDS:g} to {MAX_INTERVAL_SECONDS:g}."
    )


class EscalationTimer:
    """A daemon thread that runs one sweep pass after another.

    :param interval: seconds between two passes (greater than 0).
    :param sweep_pass: one pass; returns ``True`` when it stopped at its bound
        with more due cases left -- then the next pass follows after
        :data:`CATCH_UP_PAUSE_SECONDS` instead of a whole interval.
    :param first_delay: seconds before the first pass; ``None`` draws a random
        offset in ``[0, interval)`` (several processes do not sweep in
        lockstep).
    :param name: name of the thread (default :data:`THREAD_NAME`); the same
        timer also runs other periodic housekeeping, e.g. the removal of
        expired idempotency keys.

    A pass that raises (database outage, a defect) is logged and the timer
    goes on with the next interval -- it must never die silently, nor take
    the API process with it. :meth:`stop` ends the thread: a running pass is
    finished (it is made of short units of work), no new one starts.
    """

    def __init__(
        self,
        interval: float,
        sweep_pass: Callable[[], bool],
        *,
        first_delay: float | None = None,
        name: str = THREAD_NAME,
    ) -> None:
        if not (math.isfinite(interval) and interval > 0):
            raise ValueError(f"interval must be a positive number of seconds, got {interval!r}")
        self._interval = interval
        self._pass = sweep_pass
        # Spreading the processes over the interval, not cryptography.
        self._first_delay = random.uniform(0, interval) if first_delay is None else first_delay
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        """Start the thread (once)."""

        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Ask the thread to end and wait for it (at most ``timeout`` seconds).

        :param timeout: how long to wait for a running pass to finish; a
            daemon thread that is still running after it does not keep the
            process alive.
        """

        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        """Whether the thread is alive."""

        return self._thread.is_alive()

    def _run(self) -> None:
        wait = self._first_delay
        while not self._stopped.wait(wait):
            try:
                more_due = self._pass()
            except Exception:  # noqa: BLE001 -- the timer must survive any failed pass
                _log.exception("escalation sweep pass failed; next pass in %.1fs", self._interval)
                more_due = False
            wait = CATCH_UP_PAUSE_SECONDS if more_due else self._interval
