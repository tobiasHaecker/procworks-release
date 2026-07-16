# SPDX-License-Identifier: BUSL-1.1
"""Lead + feedback relay for the demo broker (deliver, never store).

The broker gates the demo behind a contact form (name, company, e-mail, consent)
and shows a short survey when the visitor ends the demo. To honour data
minimisation (DSGVO), the broker **does not keep a database** of that personal
data -- it **relays** each lead and each feedback to the operator by e-mail and
retains nothing. This module is the transport + formatting for that relay.

Configuration is read from the environment (all optional; when unset the relay
is simply *not configured* and the broker treats a lead as undeliverable):

* ``LEAD_SMTP_HOST`` / ``LEAD_SMTP_PORT`` (default 587) -- SMTP server.
* ``LEAD_SMTP_USER`` / ``LEAD_SMTP_PASSWORD`` -- SMTP auth (a Fly *secret*).
* ``LEAD_SMTP_STARTTLS`` (default "1") -- STARTTLS on the connection.
* ``LEAD_MAIL_FROM`` -- envelope/From address (defaults to the SMTP user).
* ``LEAD_MAIL_TO`` -- where leads/feedback are delivered (the operator inbox).

This is a deployment artifact, not part of the correctness core.
"""

from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Protocol


class MailTransport(Protocol):
    """Minimal e-mail transport the notifier sends through (SMTP or a fake)."""

    def send(self, message: EmailMessage) -> None:
        """Deliver one message; raise on any failure so the caller can react."""


@dataclass
class SmtpTransport:
    """Real :class:`MailTransport` over SMTP (stdlib ``smtplib``).

    Connects, optionally upgrades to STARTTLS, authenticates and sends a single
    message per call. Any transport/auth error propagates as an exception so the
    broker can fail the trial cleanly rather than silently drop a lead.
    """

    host: str
    port: int = 587
    user: str = ""
    password: str = ""
    starttls: bool = True
    timeout: float = 15.0

    def send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(message)


@dataclass
class LeadNotifier:
    """Relays leads and feedback to the operator inbox; keeps nothing.

    ``configured`` is False when there is no recipient or no transport (e.g. the
    local/dev broker without SMTP) -- callers then skip the relay instead of
    failing. All personal data flows *through* this object into one e-mail; it is
    never persisted here, which is the whole point (data minimisation).
    """

    sender: str
    recipient: str
    transport: MailTransport | None

    @property
    def configured(self) -> bool:
        """Whether a real relay target + transport are present."""
        return bool(self.recipient and self.transport)

    def _deliver(self, subject: str, body: str) -> None:
        """Build and send one plain-text message. Raises on transport failure."""
        if self.transport is None:  # pragma: no cover - guarded by ``configured``
            raise RuntimeError("lead notifier has no transport")
        message = EmailMessage()
        message["From"] = self.sender or self.recipient
        message["To"] = self.recipient
        message["Subject"] = subject
        message.set_content(body)
        self.transport.send(message)

    def send_lead(
        self,
        *,
        name: str,
        company: str,
        email: str,
        marketing_consent: bool,
        trial_id: str,
    ) -> None:
        """Relay one demo lead (the pre-demo contact gate). Raises on failure.

        The body doubles as the **consent record**: it captures the UTC time, the
        stated purpose, and whether the optional marketing consent was given. No
        IP address is included (data minimisation). ``trial_id`` lets the operator
        correlate this lead with the later feedback mail from the same session.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        marketing = "ja" if marketing_consent else "nein"
        body = (
            "Neuer Demo-Lead (ProcWorks Testversion)\n"
            f"Eingegangen: {now}\n"
            f"Trial-ID:    {trial_id}\n\n"
            f"Name:        {name}\n"
            f"Firma:       {company}\n"
            f"E-Mail:      {email}\n\n"
            "Einwilligung: Bereitstellung/Betreuung des Testzugangs bestätigt.\n"
            f"Marketing-Kontakt (optional) eingewilligt: {marketing}\n"
        )
        self._deliver(f"ProcWorks Demo-Lead: {company} ({email})", body)

    def send_feedback(self, *, trial_id: str, answers: dict[str, object]) -> None:
        """Relay one post-demo feedback submission. Raises on transport failure.

        ``trial_id`` correlates the feedback with the lead mail from the same
        session (both carry it), so no server-side join/storage is needed.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [
            "Neues Demo-Feedback (ProcWorks Testversion)",
            f"Eingegangen: {now}",
            f"Trial-ID:    {trial_id}",
            "",
        ]
        for key, value in answers.items():
            lines.append(f"{key}: {value}")
        self._deliver(f"ProcWorks Demo-Feedback (Trial {trial_id})", "\n".join(lines) + "\n")


def _truthy(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def create_lead_notifier() -> LeadNotifier:
    """Build the :class:`LeadNotifier` from the environment (see module docstring).

    With ``LEAD_SMTP_HOST`` set, wires a real :class:`SmtpTransport`; otherwise the
    transport is ``None`` and the notifier reports ``configured is False`` so the
    broker skips the relay (local/dev). Never raises -- misconfiguration surfaces
    later as ``configured is False`` or a send error, not at construction.
    """
    host = os.environ.get("LEAD_SMTP_HOST", "").strip()
    user = os.environ.get("LEAD_SMTP_USER", "").strip()
    recipient = os.environ.get("LEAD_MAIL_TO", "").strip()
    sender = os.environ.get("LEAD_MAIL_FROM", "").strip() or user or recipient
    transport: MailTransport | None = None
    if host:
        transport = SmtpTransport(
            host=host,
            port=int(os.environ.get("LEAD_SMTP_PORT", "587")),
            user=user,
            password=os.environ.get("LEAD_SMTP_PASSWORD", ""),
            starttls=_truthy("LEAD_SMTP_STARTTLS", "1"),
        )
    return LeadNotifier(sender=sender, recipient=recipient, transport=transport)
