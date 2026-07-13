# SPDX-License-Identifier: BUSL-1.1
"""Runtime delivery of modelled e-mail notifications (rule group N).

This is the *boundary* side of the notification feature: when an activity that
carries a :class:`~procworks.model.MailBinding` becomes ready (its node is
activated), this module resolves the concrete recipients, renders the template
and hands a message to a pluggable :class:`MailSender`. It **only reads** the
model and the instance markings -- it holds no correctness logic and never
mutates a schema or an instance, exactly like :mod:`procworks.assignment` and
the webhook dispatcher in :mod:`procworks.outbox`.

The correctness rules N1-N4 (in :mod:`procworks.validator`) have already
guaranteed, *before* the schema was committed, that every possible recipient
has a well-formed address and every template placeholder resolves. This module
can therefore assume its inputs are addressable; it still fails **soft** (a
delivery error is logged, never raised) so a mail problem can never break a
running process -- stability has priority over the notification.

Two delivery paths share the same resolution/rendering primitives:

* :func:`notify_ready_tasks` -- the original *best-effort* path: send once per
  activation transition through a :class:`MailSender`, swallow any failure. Kept
  as a simple primitive for tests and embedders.
* :class:`MailOutboxDispatcher` -- the *durable* production path: enqueue the
  rendered message into a transactional :class:`~procworks.store.MailOutboxStore`
  in the same boundary step that observes the task becoming ready (so nothing is
  lost on a crash), then deliver it with a back-off retry and a dead-letter,
  exactly the pattern of :mod:`procworks.outbox` on the SMTP channel. Enqueue is
  idempotent per activation (:func:`activation_dedup_key`), so a re-observed
  activation is never queued twice, while a loop re-activation yields a fresh
  message. Both paths deliver *at-least-once* behind the same
  :class:`MailSender` interface.
"""

from __future__ import annotations

import logging
import os
import smtplib
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol

from pydantic import BaseModel

from procworks.assignment import eligible_agents
from procworks.model import (
    MailBinding,
    MailOutboxEntry,
    MailOutboxState,
    MailRecipientMode,
    NodeState,
    NodeType,
    OrgModel,
    ProcessInstance,
    ProcessSchema,
    StaffRule,
    StaffRuleKind,
    template_placeholders,
)
from procworks.store import MailOutboxStore

logger = logging.getLogger("procworks.mail")

#: Back-off schedule for a retried notification (mirrors :mod:`procworks.outbox`).
_BACKOFF_BASE_MS = 2000
_BACKOFF_CAP_MS = 300_000


def _no_header_break(value: str) -> str:
    """Strip CR/LF so a value can never inject an extra e-mail header (§8)."""

    return value.replace("\r", " ").replace("\n", " ")


def _backoff_seconds(attempts: int) -> float:
    """Exponential back-off in seconds for the ``attempts``-th failed send."""

    capped = min(_BACKOFF_BASE_MS * (2 ** max(0, attempts - 1)), _BACKOFF_CAP_MS)
    return float(capped) / 1000.0


class MailMessage(BaseModel):
    """A rendered notification ready to be sent.

    ``to`` is the de-duplicated list of recipient addresses; the context fields
    (``instance_id``/``node_id``) are carried for logging and future durable
    delivery, never for the mail body itself (data minimisation).
    """

    to: list[str]
    subject: str
    body: str
    instance_id: str
    node_id: str
    schema_id: str
    #: Stable idempotency token for this activation (the outbox ``dedup_key``).
    #: Emitted as the SMTP ``Message-ID`` so a receiver can de-duplicate an
    #: at-least-once redelivery. Empty for the legacy best-effort path.
    message_id: str = ""


class MailSender(Protocol):
    """Pluggable transport for a rendered notification (analogous to the
    webhook ``Transport``). ``send`` raises on failure; the notifier catches it."""

    def send(self, message: MailMessage) -> None: ...


class NullMailSender:
    """Default sender when no SMTP server is configured: logs and drops.

    Lets the whole feature (modelling, validation, recipient resolution) work
    and be tested without a mail server, and makes an unconfigured deployment a
    no-op rather than an error.
    """

    def send(self, message: MailMessage) -> None:
        logger.info(
            "mail notification (no SMTP configured, dropped): to=%s subject=%r "
            "instance=%s node=%s",
            message.to,
            message.subject,
            message.instance_id,
            message.node_id,
        )


class SmtpMailSender:
    """Sends a notification over SMTP (STARTTLS by default).

    Configuration is *operational* (constructor arguments fed from environment
    variables by :func:`create_mail_sender`), never part of the process model:
    credentials must not live in a schema (mirrors the webhook secret store).
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._timeout = timeout

    def send(self, message: MailMessage) -> None:
        """Build and transmit the e-mail. Raises on any SMTP/transport error."""

        email = EmailMessage()
        email["From"] = self._sender
        # Guard against e-mail header injection: no CR/LF may reach a header,
        # even though N1 already rejects malformed addresses at modelling time.
        email["To"] = ", ".join(_no_header_break(addr) for addr in message.to)
        email["Subject"] = _no_header_break(message.subject)
        if message.message_id:
            # A stable Message-ID lets the receiver de-duplicate an at-least-once
            # redelivery of the same activation (idempotency, mirrors outbox.py).
            email["Message-ID"] = f"<{_no_header_break(message.message_id)}@procworks>"
        email.set_content(message.body or message.subject)
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password or "")
            smtp.send_message(email)


def create_mail_sender() -> MailSender:
    """Build the process-wide mail sender from the environment.

    Returns an :class:`SmtpMailSender` when ``PROCWORKS_SMTP_HOST`` and
    ``PROCWORKS_MAIL_FROM`` are set, otherwise a :class:`NullMailSender` (the
    feature stays fully modellable and validated, mails are just not sent).
    """

    host = os.getenv("PROCWORKS_SMTP_HOST")
    sender = os.getenv("PROCWORKS_MAIL_FROM")
    if not host or not sender:
        return NullMailSender()
    return SmtpMailSender(
        host=host,
        port=int(os.getenv("PROCWORKS_SMTP_PORT", "587")),
        sender=sender,
        username=os.getenv("PROCWORKS_SMTP_USER") or None,
        password=os.getenv("PROCWORKS_SMTP_PASSWORD") or None,
        use_tls=os.getenv("PROCWORKS_SMTP_TLS", "1") not in ("0", "false", "False"),
    )


# --------------------------------------------------------------------------- #
# Trigger detection and message construction (pure reads)
# --------------------------------------------------------------------------- #
def newly_ready_mail_nodes(
    schema: ProcessSchema,
    before_states: dict[str, NodeState] | None,
    after: ProcessInstance,
) -> list[str]:
    """Ids of ACTIVITY nodes with a mail binding that *just* became ready.

    A node qualifies when it is ACTIVATED in ``after`` but was not ACTIVATED in
    ``before_states`` (``None`` == a fresh instance, everything counts as newly
    activated). Diffing the transition -- rather than scanning the current state
    -- means a node re-entered by a loop is notified again on each activation,
    while the same transition is never counted twice.
    """

    result: list[str] = []
    for node_id, state in after.node_states.items():
        if state is not NodeState.ACTIVATED:
            continue
        if before_states is not None and before_states.get(node_id) is NodeState.ACTIVATED:
            continue
        node = schema.nodes.get(node_id)
        if node is None or node.type is not NodeType.ACTIVITY:
            continue
        if node_id not in schema.mail_bindings:
            continue
        result.append(node_id)
    return result


def render_template(template: str, instance: ProcessInstance) -> str:
    """Substitute ``{element_id}`` placeholders with the instance's values.

    N4 has guaranteed every placeholder refers to an INSTANCE element written
    before the node, so the value is present; a missing value degrades to an
    empty string rather than raising (soft failure).
    """

    text = template
    for ref in template_placeholders(template):
        value = instance.data_values.get(ref)
        text = text.replace("{" + ref + "}", "" if value is None else str(value))
    return text


def build_message(
    schema: ProcessSchema,
    node_id: str,
    binding: MailBinding,
    instance: ProcessInstance,
    absent_agents: frozenset[str] = frozenset(),
) -> MailMessage | None:
    """Resolve recipients and render the template, or ``None`` if no recipient.

    A ``None`` result (e.g. an org edit left a concrete instance with no
    currently addressable recipient) is skipped silently by the notifier -- N3
    guarantees the *modelled* recipient set is addressable, but a live instance
    could momentarily resolve to nobody. ``absent_agents`` is passed through so a
    covering deputy is notified during an absence.
    """

    recipients = _resolve_recipients(schema, node_id, binding, instance, absent_agents)
    if not recipients:
        return None
    return MailMessage(
        to=recipients,
        subject=render_template(binding.subject, instance),
        body=render_template(binding.body, instance),
        instance_id=instance.id,
        node_id=node_id,
        schema_id=instance.schema_id,
    )


def notify_ready_tasks(
    schema: ProcessSchema,
    before_states: dict[str, NodeState] | None,
    after: ProcessInstance,
    sender: MailSender,
) -> list[MailMessage]:
    """Send a notification for every task that just became ready. Best-effort.

    Returns the messages that were handed to the sender without error (useful
    for tests and future auditing). A delivery failure is logged and swallowed
    so it never propagates into the process step that triggered it.
    """

    sent: list[MailMessage] = []
    for node_id in newly_ready_mail_nodes(schema, before_states, after):
        binding = schema.mail_bindings[node_id]
        message = build_message(schema, node_id, binding, after)
        if message is None:
            continue
        try:
            sender.send(message)
            sent.append(message)
        except Exception:  # noqa: BLE001 -- soft failure: never break the step
            logger.exception(
                "mail notification failed for node %s of instance %s",
                node_id,
                after.id,
            )
    return sent


def _resolve_recipients(
    schema: ProcessSchema,
    node_id: str,
    binding: MailBinding,
    instance: ProcessInstance,
    absent_agents: frozenset[str] = frozenset(),
) -> list[str]:
    """Concrete recipient addresses for a binding, de-duplicated, order-stable.

    ``absent_agents`` is threaded to the eligibility resolution so a deputy that
    stands in for an absent agent is also notified (mirrors the worklist).
    """

    org = schema.org_model
    if binding.mode is MailRecipientMode.TO_ELIGIBLE_AGENTS:
        agent_ids = eligible_agents(
            schema,
            node_id,
            instance,
            include_deputies=binding.include_deputies,
            absent_agents=absent_agents,
        )
        addresses: list[str] = []
        for aid in sorted(agent_ids):
            agent = org.agents.get(aid)
            if agent is not None and agent.email:
                addresses.append(agent.email)
        return _dedupe(addresses)

    rule = schema.staff_rules.get(node_id)
    if rule is None:
        return []
    return _dedupe(_group_mailboxes(org, rule))


def _group_mailboxes(org: OrgModel, rule: StaffRule, *, positive: bool = True) -> list[str]:
    """Group mailboxes the rule positively addresses (mirrors N3's ``_group_refs``).

    An ``EXCEPT`` right operand is a subtraction and is not notified.
    """

    boxes: list[str] = []
    if rule.kind is StaffRuleKind.ROLE and rule.ref is not None and positive:
        role = org.roles.get(rule.ref)
        if role is not None and role.mailbox:
            boxes.append(role.mailbox)
    elif rule.kind is StaffRuleKind.ORG_UNIT and rule.ref is not None and positive:
        unit = org.org_units.get(rule.ref)
        if unit is not None and unit.mailbox:
            boxes.append(unit.mailbox)
    elif rule.kind is StaffRuleKind.EXCEPT:
        if rule.operands:
            boxes += _group_mailboxes(org, rule.operands[0], positive=positive)
        for operand in rule.operands[1:]:
            boxes += _group_mailboxes(org, operand, positive=False)
    else:  # AND / OR / (non-positive leaf)
        for operand in rule.operands:
            boxes += _group_mailboxes(org, operand, positive=positive)
    return boxes


def _dedupe(items: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication of an iterable of addresses."""

    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)


# --------------------------------------------------------------------------- #
# Durable mail outbox (transactional, retrying delivery for rule group N)
# --------------------------------------------------------------------------- #
def activation_dedup_key(instance: ProcessInstance, node_id: str) -> str:
    """Idempotency key for *this* activation of ``node_id`` in ``instance``.

    Combines the instance id, the node id and the recorded activation instant
    (``instance.node_activated_at``, stamped at the API boundary). Two
    observations of the *same* activation transition therefore resolve to the
    same key and are enqueued at most once, while a loop re-activation carries a
    later activation instant -> a distinct key -> a fresh notification. Falls back
    to the current wall-clock when no stamp is present (best-effort uniqueness).
    """

    stamp = instance.node_activated_at.get(node_id)
    marker = stamp.isoformat() if stamp is not None else datetime.now().isoformat()
    return f"{instance.id}|{node_id}|{marker}"


@dataclass(frozen=True)
class MailDispatchResult:
    """Outcome of one send attempt made by :meth:`MailOutboxDispatcher.dispatch`.

    ``delivered`` is true when this attempt reached the SMTP server; ``dead`` is
    true when it exhausted its retry budget. Both false means a transient failure
    that will be retried after a back-off. The API boundary turns ``delivered`` /
    ``dead`` into the ``mail.sent`` / ``mail.failed`` audit events (metadata only).
    """

    entry: MailOutboxEntry
    delivered: bool
    dead: bool


class MailOutboxDispatcher:
    """Owns the durable queue and retrying delivery of modelled notifications.

    The SMTP-channel sibling of :class:`~procworks.outbox.OutboxDispatcher`:
    :meth:`enqueue` writes a rendered message to the store (idempotent per
    activation), :meth:`dispatch_pending` attempts every due entry through the
    :class:`MailSender` and applies the back-off / dead-letter lifecycle. It is a
    boundary component -- it never touches the pure engine or the validator.
    """

    def __init__(
        self,
        store: MailOutboxStore,
        *,
        now: Callable[[], float] | None = None,
        max_attempts: int = 5,
    ) -> None:
        self._store = store
        self._now = now or time.time
        self._max_attempts = max_attempts

    def enqueue(
        self, message: MailMessage, dedup_key: str
    ) -> MailOutboxEntry | None:
        """Queue ``message`` for durable delivery, or ``None`` if already queued.

        Idempotent: if an entry with ``dedup_key`` already exists (the same
        activation was observed before, e.g. after a crash/replay), nothing is
        written and ``None`` is returned. The rendered recipients/subject/body are
        snapshotted so a later org or data change cannot alter a queued message.
        """

        existing = self._store.find_by_dedup_key(dedup_key)
        if existing is not None:
            return None
        now = self._now()
        entry = MailOutboxEntry(
            id=f"mo_{uuid.uuid4().hex}",
            dedup_key=dedup_key,
            instance_id=message.instance_id,
            node_id=message.node_id,
            schema_id=message.schema_id,
            recipients=list(message.to),
            subject=message.subject,
            body=message.body,
            max_attempts=self._max_attempts,
            next_attempt_at=now,
            created_at=now,
        )
        return self._store.put_entry(entry)

    def dispatch_pending(self, sender: MailSender) -> list[MailDispatchResult]:
        """Attempt delivery of every due entry; return the per-entry outcomes.

        Due = ``PENDING``/``FAILED`` with ``next_attempt_at`` in the past. Each is
        sent through ``sender``; success -> ``SENT``, transient failure ->
        ``FAILED`` with a back-off, exhausted budget -> ``DEAD`` (dead-letter).
        The ``sender`` is passed per call so callers can supply the current
        process-wide sender (and tests can inject a collector) without the
        dispatcher holding a stale reference. Never raises -- a delivery problem
        is recorded on the entry, never propagated into the triggering step.
        """

        now = self._now()
        results: list[MailDispatchResult] = []
        for entry in self._store.list_entries():
            if entry.state not in (MailOutboxState.PENDING, MailOutboxState.FAILED):
                continue
            if entry.next_attempt_at > now:
                continue
            results.append(self._deliver(entry, sender, now))
        return results

    def _deliver(
        self, entry: MailOutboxEntry, sender: MailSender, now: float
    ) -> MailDispatchResult:
        """Make one send attempt for ``entry`` and persist the new lifecycle state."""

        message = MailMessage(
            to=list(entry.recipients),
            subject=entry.subject,
            body=entry.body,
            instance_id=entry.instance_id,
            node_id=entry.node_id,
            schema_id=entry.schema_id,
            message_id=entry.dedup_key,
        )
        attempt = entry.attempts + 1
        error: str | None = None
        try:
            sender.send(message)
            ok = True
        except Exception as exc:  # noqa: BLE001 -- transport failures are recorded, not raised
            ok = False
            error = str(exc)

        entry.attempts = attempt
        entry.last_error = error
        delivered = ok
        dead = False
        if ok:
            entry.state = MailOutboxState.SENT
            entry.next_attempt_at = now
        elif attempt >= entry.max_attempts:
            entry.state = MailOutboxState.DEAD
            entry.next_attempt_at = now
            dead = True
            logger.error(
                "mail notification dead-lettered after %d attempts "
                "(instance=%s node=%s, recipients=%d)",
                attempt,
                entry.instance_id,
                entry.node_id,
                len(entry.recipients),
            )
        else:
            entry.state = MailOutboxState.FAILED
            entry.next_attempt_at = now + _backoff_seconds(attempt)
        self._store.put_entry(entry)
        return MailDispatchResult(entry=entry, delivered=delivered, dead=dead)


def enqueue_ready_tasks(
    schema: ProcessSchema,
    before_states: dict[str, NodeState] | None,
    after: ProcessInstance,
    dispatcher: MailOutboxDispatcher,
    absent_agents: frozenset[str] = frozenset(),
) -> list[MailOutboxEntry]:
    """Durably queue a notification for every task that just became ready.

    The durable counterpart of :func:`notify_ready_tasks`: for each node that
    transitioned to ``ACTIVATED`` and carries a mail binding, it resolves the
    recipients, renders the template and enqueues the message idempotently per
    activation. Returns the entries that were newly queued (skipping already-queued
    activations and bindings that momentarily resolve to no recipient).
    ``absent_agents`` is threaded through so a covering deputy is notified.
    """

    entries: list[MailOutboxEntry] = []
    for node_id in newly_ready_mail_nodes(schema, before_states, after):
        binding = schema.mail_bindings[node_id]
        message = build_message(schema, node_id, binding, after, absent_agents)
        if message is None:
            continue
        entry = dispatcher.enqueue(message, activation_dedup_key(after, node_id))
        if entry is not None:
            entries.append(entry)
    return entries
