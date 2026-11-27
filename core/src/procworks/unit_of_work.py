# SPDX-License-Identifier: BUSL-1.1
"""One unit of work per change: a single transaction and a conditional save.

Two guarantees for every request that changes something:

1. **Conditional save (optimistic version check).** Every stored case
   (``ProcessInstance``) and model (``ProcessSchema``) carries a version. A
   store saves an object only if the stored version is still the one the
   object was loaded with, and raises :class:`ConcurrentModification`
   otherwise. Without it, two requests that read the same state both saved,
   and the later save silently replaced the earlier one -- two callers got
   200, one change was gone, and the audit showed the same step claimed or
   completed twice.

2. **All or nothing (one transaction).** On a database, every store call
   inside a unit of work uses the one session of the unit; the unit commits
   at its end, or rolls everything back. A case, its audit events and its
   outbox entries are stored together or not at all -- a failure in the
   middle can no longer leave a case without its creation event behind.

:func:`run` executes a change as a unit and **repeats** it on a conflict: the
repetition reads the new state and gives the answer that state deserves
(the step is already claimed, no longer ready, the model no longer a
draft), or simply succeeds when the two changes do not collide. Only a
conflict that persists reaches the caller.

A lock that every writer needs -- the end of the audit chain -- is taken
last: its write is registered with :func:`before_commit` and runs right
before the commit, after all other writes of the unit. It is held only for
the commit itself, and no unit can hold it while waiting for another lock
(no deadlock against units that took a case row first).

A repetition must not repeat what cannot be taken back. Deliveries to the
outside (webhooks, mail, pushes) therefore run **after** the commit
(:func:`after_commit`); an effect that happens inside a unit anyway (an
external write, a delivery called directly) marks the unit as irreversible
(:func:`mark_irreversible`), and a conflict then reaches the caller instead
of being repeated.

The in-memory stores have no transactions. There a unit only carries the
hooks and the bookkeeping, and the caller may pass a ``serialize`` lock so
that units of one process run one after another (a single process is all
the in-memory configuration ever has) -- their saves are still checked.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger("procworks.unit_of_work")

#: How often :func:`run` repeats a unit after a conflict before it gives up.
#: Each repetition reads the latest state, so a conflict only persists when
#: other writers keep changing the same object in the meantime.
DEFAULT_RETRIES = 5


#: SQLSTATE codes with which PostgreSQL aborts a transaction that collided
#: with another one: ``40001`` serialization failure, ``40P01`` deadlock
#: detected. The other transaction went through; repeating this one is the
#: documented remedy -- like a conflict of the conditional save.
_TRANSIENT_SQLSTATES = frozenset({"40001", "40P01"})


def is_transient_collision(exc: BaseException) -> bool:
    """Whether ``exc`` is a database error that a repetition resolves.

    :param exc: any exception.
    :returns: ``True`` for a driver error carrying SQLSTATE ``40001`` or
        ``40P01`` (psycopg 3 names it ``sqlstate``, psycopg 2 ``pgcode``).
    """

    if not isinstance(exc, DBAPIError):
        return False
    orig = exc.orig
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code in _TRANSIENT_SQLSTATES


class ConcurrentModification(Exception):
    """The object was changed by someone else since it was read.

    Raised by a store's ``put`` when the stored version differs from the one
    the object was loaded with, or when a *new* object (never loaded) is put
    under an id that already exists.

    :param kind: what was saved -- ``"instance"`` or ``"schema"``.
    :param key: its id.
    """

    def __init__(self, kind: str, key: str) -> None:
        self.kind = kind
        self.key = key
        super().__init__(
            f"{kind} '{key}' was changed by another request since it was read"
        )


class CommitOutcomeUnknown(Exception):
    """The connection broke off during the commit: saved or not is unknown.

    Raised by the end of a unit (:meth:`UnitOfWork._commit`) when SQLAlchemy
    reports a disconnect while ``COMMIT`` was under way. The server may have
    executed the commit (it had all writes and the ``COMMIT``) or not (the
    command never arrived) -- this process cannot tell. Therefore:

    * :func:`run` never repeats such a unit (a repetition could do the change
      twice);
    * the API answers ``503`` with the code ``DB.outcome-unknown`` and no
      ``Retry-After``: the caller has to check the result before repeating
      the change, or repeat it with the same ``Idempotency-Key`` (the key and
      its answer are committed together with the change).

    The original driver error is the ``__cause__``.
    """

    def __init__(self) -> None:
        super().__init__(
            "the database connection broke off during the commit; "
            "whether the change was saved is unknown"
        )


class UnitOfWork:
    """State of one running unit (see the module documentation).

    Created by :func:`unit_of_work`; code inside reaches it through
    :func:`current`. Not shared between threads at the same time -- one unit
    belongs to one request, which may hop between worker threads, but never
    runs in two of them at once.
    """

    def __init__(self) -> None:
        self._sessions: dict[Engine, Session] = {}
        self._before_commit: dict[str, Callable[[], object]] = {}
        self._after_commit: dict[str, Callable[[], object]] = {}
        #: Per-unit state of stores, keyed by the store object -- e.g. the
        #: audit events a unit has recorded but not yet chained
        #: (:func:`before_commit`). Lives and dies with the unit, so a
        #: repeated unit starts empty.
        self.store_state: dict[object, Any] = {}
        self._on_finish: list[Callable[[bool], None]] = []
        #: Something left the process that a repetition would do twice.
        self.irreversible = False
        #: The first conflict a store reported -- also when the code between
        #: the store and the unit swallowed the exception (best-effort hooks).
        self.conflict: ConcurrentModification | None = None
        #: The first database error -- after it the transaction is unusable.
        self.failure: SQLAlchemyError | None = None

    def session_for(self, engine: Engine) -> Session:
        """The one session (and transaction) of this unit on ``engine``.

        Opened on first use. Every store on the same database shares it, so a
        request holds exactly one connection, and nothing it writes is visible
        to others before the commit.
        """

        session = self._sessions.get(engine)
        if session is None:
            session = Session(engine)
            self._sessions[engine] = session
        return session

    def owns(self, session: Session) -> bool:
        """Whether ``session`` is one of this unit's sessions (then a store
        must flush instead of committing)."""

        return any(own is session for own in self._sessions.values())

    def note_conflict(self, conflict: ConcurrentModification) -> None:
        """Remember the first conflict (stores call this before raising it)."""

        if self.conflict is None:
            self.conflict = conflict

    def note_failure(self, failure: SQLAlchemyError) -> None:
        """Remember the first database error (stores call this before re-raising)."""

        if self.failure is None:
            self.failure = failure

    def _commit(self) -> None:
        """Commit every session of the unit; tell a lost commit from a failed one.

        Each session is flushed first, then the ``before_commit`` steps run
        (:func:`before_commit`), then each session is flushed again and
        committed -- so all writes have reached the server before ``COMMIT``
        is sent. A break before that point is a plain
        failure (the server rolls the open transaction back when the
        connection ends) and propagates unchanged -- the API answers it as an
        outage. Only a break *during* the commit itself is in doubt: the
        ``COMMIT`` may have reached the server and been executed although no
        answer came back (measured with a frozen PostgreSQL, whose
        connection the stall watchdog closed). That case raises
        :class:`CommitOutcomeUnknown`.

        :raises CommitOutcomeUnknown: the connection was lost (SQLAlchemy
            reports a disconnect) while ``COMMIT`` was under way.
        :raises sqlalchemy.exc.SQLAlchemyError: any other failure of flush or
            commit -- among them a commit the server refused with a reason of
            its own (a SQLSTATE such as a serialization failure); then the
            transaction is rolled back for sure.
        """

        for session in list(self._sessions.values()):
            session.flush()
        # The last writes of the unit, after everything else is flushed: a
        # step that takes a lock every writer needs (the end of the audit
        # chain) holds it only from here to the commit -- never while the
        # unit is still working, and always *after* the unit's other locks, so
        # two units can never wait for each other in opposite order.
        for hook in self._before_commit.values():
            hook()
        for session in self._sessions.values():
            session.flush()
            try:
                session.commit()
            except DBAPIError as exc:
                if exc.connection_invalidated:
                    raise CommitOutcomeUnknown() from exc
                raise

    def _rollback(self) -> None:
        for session in self._sessions.values():
            try:
                session.rollback()
            except SQLAlchemyError:  # the connection may be gone already
                logger.warning("rollback of a unit of work failed", exc_info=True)

    def _close(self) -> None:
        for session in self._sessions.values():
            try:
                session.close()
            except SQLAlchemyError:
                logger.warning("closing a unit of work failed", exc_info=True)
        self._sessions.clear()


_current: ContextVar[UnitOfWork | None] = ContextVar("procworks_unit_of_work", default=None)


def current() -> UnitOfWork | None:
    """The unit of work of the running request, or ``None`` outside one."""

    return _current.get()


@contextmanager
def unit_of_work(
    *,
    serialize: threading.Lock | None = None,
    commit_on: tuple[type[BaseException], ...] = (),
) -> Iterator[UnitOfWork]:
    """Run the enclosed block as one unit of work.

    Inside a unit, a nested call joins the outer unit (one transaction per
    request); its end is the outer unit's end.

    The end of an outermost unit, in this order:

    1. a conflict or database error that a store reported is raised, even if
       code in between swallowed it -- the transaction must not be committed
       with a write missing;
    2. commit (or rollback, if the block raised);
    3. the ``on_finish`` hooks run with ``committed`` (always, also after a
       rollback -- e.g. releasing a lock that was held for the unit);
    4. the ``serialize`` lock is released;
    5. after a commit, the ``after_commit`` hooks run (deliveries); their
       errors are logged, never raised -- what was committed stays committed.
       This includes a commit for an exception of ``commit_on``: the
       deliveries run first, then that exception propagates unchanged. A
       rolled-back unit delivers nothing.

    :param serialize: lock held for the whole unit (in-memory stores, see the
        module documentation); ``None`` on a database.
    :param commit_on: exception types that are a *deliberate answer* of the
        block (the API passes ``HTTPException``): the writes made before them
        are kept, as they were before units existed (e.g. a failed attempt of
        an external task is recorded, then the request answers 502), and
        their deliveries run (step 5). Every other exception rolls back.
    :yields: the unit.
    :raises ConcurrentModification: a store refused a stale save.
    """

    outer = current()
    if outer is not None:
        yield outer
        return
    if serialize is not None:
        serialize.acquire()
    uow = UnitOfWork()
    token = _current.set(uow)
    committed = False
    try:
        try:
            try:
                yield uow
            except BaseException as exc:
                # A noted conflict or database error outranks whatever the
                # block made of it (a hook may have swallowed it and the
                # request carried on into another error): the unit is rolled
                # back and repeated, or answered as a database problem.
                noted = uow.conflict or uow.failure
                if noted is not None and noted is not exc:
                    raise noted from exc
                if noted is None and isinstance(exc, commit_on):
                    uow._commit()
                    committed = True
                raise
            if uow.conflict is not None:
                raise uow.conflict
            if uow.failure is not None:
                raise uow.failure
            uow._commit()
            committed = True
        except BaseException:
            if not committed:
                uow._rollback()
            raise
        finally:
            _current.reset(token)
            uow._close()
            for hook in uow._on_finish:
                try:
                    hook(committed)
                except Exception:  # noqa: BLE001 -- a finish hook must not mask the outcome
                    logger.exception("finish hook of a unit of work failed")
            if serialize is not None:
                serialize.release()
    finally:
        # Also when the block ended with a deliberate answer (``commit_on``):
        # what was committed is announced -- an incident recorded before a
        # 502 must reach its subscribers with this request, not whenever an
        # unrelated later request happens to drive the outbox. The answer
        # itself propagates unchanged after the deliveries.
        if committed:
            _run_after_commit(uow)


def _run_after_commit(uow: UnitOfWork) -> None:
    """Run the ``after_commit`` steps of a committed unit, each once.

    Called by :func:`unit_of_work` after the commit, after the ``on_finish``
    hooks and after the ``serialize`` lock was released -- deliveries talk to
    the outside and must not hold up the other writers. Runs whether the block
    ended normally or with an exception of ``commit_on``; never for a unit that
    was rolled back.

    :param uow: the committed unit.
    :returns: nothing; an error of one step is logged and the next step still
        runs (what was committed stays committed, deliveries are best effort,
        and an error here never replaces the unit's own outcome).
    """

    for name, deliver in uow._after_commit.items():
        try:
            deliver()
        except Exception:  # noqa: BLE001 -- the change is committed; deliveries are best effort
            logger.exception("after-commit step %r failed", name)


def run[T](
    change: Callable[[], T],
    *,
    retries: int = DEFAULT_RETRIES,
    serialize: threading.Lock | None = None,
    commit_on: tuple[type[BaseException], ...] = (),
) -> T:
    """Run ``change`` as a unit of work; repeat it after a conflict.

    Each attempt is a fresh unit: it reads the current state, so the
    repetition either succeeds (the changes did not collide) or ends with the
    answer the new state gives. A short random pause before each repetition
    keeps two writers from colliding in lockstep.

    Inside an outer unit, ``change`` simply runs within it: the outer unit
    owns the transaction, and a conflict goes to the outer unit's repetition.

    :param change: the work; it loads, changes and saves through the stores.
    :param retries: repetitions after the first attempt.
    :param serialize: see :func:`unit_of_work`.
    :param commit_on: see :func:`unit_of_work`.
    :returns: what ``change`` returned in the successful attempt.
    :raises ConcurrentModification: the conflict persisted through all
        repetitions, or the unit had an irreversible effect (then it is not
        repeated at all).
    :raises sqlalchemy.exc.DBAPIError: a deadlock or serialization failure
        (:func:`is_transient_collision`) is repeated the same way; when it
        persists it propagates (the API answers 503 ``DB.unavailable``).
    :raises CommitOutcomeUnknown: the connection broke off during the commit;
        never repeated, since the first attempt may have been saved.
    """

    if current() is not None:
        return change()
    attempt = 0
    while True:
        irreversible = False
        try:
            with unit_of_work(serialize=serialize, commit_on=commit_on) as uow:
                try:
                    return change()
                finally:
                    irreversible = uow.irreversible
        except ConcurrentModification:
            if irreversible or attempt >= retries:
                raise
        except DBAPIError as exc:
            # A deadlock or serialization failure: the database rolled this
            # unit back in favour of another one -- repeat it like a conflict.
            if not is_transient_collision(exc) or irreversible or attempt >= retries:
                raise
        attempt += 1
        time.sleep(random.uniform(0.002, 0.02) * attempt)  # jitter, not cryptography


def after_commit(name: str, deliver: Callable[[], object]) -> None:
    """Run ``deliver`` once the current unit has committed -- or now, outside one.

    For effects that leave the process (webhook, mail and push delivery):
    they must neither announce something that is then rolled back nor be
    repeated when the unit is. Registered once per ``name`` and unit, so a
    request that emits ten events still drives the outbox once.

    :param name: identifies the step (later registrations of it are ignored).
    :param deliver: the delivery; errors are logged by the unit.
    """

    uow = current()
    if uow is None:
        deliver()
        return
    uow._after_commit.setdefault(name, deliver)


def before_commit(name: str, write: Callable[[], object]) -> None:
    """Run ``write`` as the last step of the current unit, right before its commit.

    For writes that need a lock every writer of the system needs, such as
    the end of the audit chain: done at the end, the lock is held only for
    the commit itself and is always the unit's last lock (no unit can hold it
    and then wait for a lock another unit holds). ``write`` works in the
    unit's sessions like any store call; an error rolls the unit back like any
    other database error. Registered once per ``name`` and unit; runs only if
    the unit commits (also for an exception of ``commit_on``), never after a
    rollback.

    Outside a unit there is nothing to wait for: ``write`` runs now.

    :param name: identifies the step (later registrations of it are ignored).
    :param write: the write; it must not register further ``before_commit``
        steps.
    """

    uow = current()
    if uow is None:
        write()
        return
    uow._before_commit.setdefault(name, write)


def on_finish(hook: Callable[[bool], None]) -> None:
    """Run ``hook(committed)`` at the end of the current unit -- or now, outside one.

    Runs after the commit or rollback and before :func:`after_commit` steps;
    meant for bookkeeping that must follow the outcome (remember a result
    only if it was committed, release a lock held for the unit).

    :param hook: receives whether the unit committed (``True`` outside a unit).
    """

    uow = current()
    if uow is None:
        hook(True)
        return
    uow._on_finish.append(hook)


def mark_irreversible() -> None:
    """Note that the current unit did something a repetition would do twice.

    Called before a delivery or an external write that runs inside a unit.
    A conflict then reaches the caller (409) instead of being repeated by
    :func:`run`. No-op outside a unit.
    """

    uow = current()
    if uow is not None:
        uow.irreversible = True


def note_conflict(conflict: ConcurrentModification) -> ConcurrentModification:
    """Record ``conflict`` on the current unit and return it for raising.

    Stores call it as ``raise note_conflict(ConcurrentModification(...))``,
    so the unit learns of the conflict even if a best-effort hook in between
    swallows the exception.
    """

    uow = current()
    if uow is not None:
        uow.note_conflict(conflict)
    return conflict


@contextmanager
def store_session(engine: Engine) -> Iterator[Session]:
    """The session a store method works in.

    Inside a unit of work: the unit's session on ``engine`` (no commit of its
    own -- finish with :func:`finish_write`). Outside: a fresh session that the
    method commits itself, as before units existed.

    A database error is recorded on the unit before it propagates, because
    after it the unit's transaction can only be rolled back.
    """

    uow = current()
    if uow is None:
        with Session(engine) as session:
            yield session
        return
    try:
        yield uow.session_for(engine)
    except SQLAlchemyError as exc:
        uow.note_failure(exc)
        raise


def finish_write(session: Session) -> None:
    """End a store write: flush inside a unit (it commits later), else commit."""

    uow = current()
    if uow is not None and uow.owns(session):
        session.flush()
    else:
        session.commit()
