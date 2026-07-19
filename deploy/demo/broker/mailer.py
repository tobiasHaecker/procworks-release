# SPDX-License-Identifier: BUSL-1.1
"""Lead + feedback relay and visitor welcome mail for the demo broker.

The broker gates the demo behind a contact form (name, company, e-mail, consent)
and shows a short survey when the visitor ends the demo. To honour data
minimisation (DSGVO), the broker **does not keep a database** of that personal
data -- it **relays** each lead and each feedback to the operator by e-mail and
retains nothing. This module is the transport + formatting for that relay.

It also builds the **welcome mail to the visitor** (:meth:`LeadNotifier.send_welcome`):
the link back into their own demo plus how long it is kept. Two different jobs
in one module on purpose -- both are "format one message, hand it to SMTP".

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

import html
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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

    def send_welcome(
        self,
        *,
        name: str,
        email: str,
        url: str,
        ttl_seconds: int,
        marketing_consent: bool,
        site_url: str = "https://procworks.de",
        started_at: datetime | None = None,
    ) -> None:
        """Send the visitor their own demo link + how long it is kept. Raises on failure.

        This is the only mail that goes **to the visitor** rather than to the
        operator inbox, so it differs from :meth:`send_lead` in three ways:

        * ``To`` is the visitor; ``Reply-To`` is the operator inbox, so "just
          reply to this mail" actually reaches a person.
        * It is ``multipart/alternative`` (text + HTML). The text part is not a
          courtesy -- an HTML-only mail scores badly with spam filters.
        * Failure is the **caller's** to absorb: by the time this is sent the
          demo already runs and the visitor is being redirected into it, so a
          dead SMTP connection must not undo a working trial (contrast
          :meth:`send_lead`, which is fail-closed because a demo without a
          delivered lead must not exist).

        The soliciting block is rendered **only** with ``marketing_consent``. The
        mandatory consent on the contact form covers providing and supporting the
        test access (Art. 6 (1) (b)) -- that carries the link, the validity and
        the getting-started hints, which is what the visitor asked for. Actual
        advertising rides on the separate optional opt-in (Art. 6 (1) (a)) that
        the form promises not to couple to the demo (Kopplungsverbot).

        Args:
            name: Visitor's name from the contact gate (used for the salutation).
            email: Visitor's address -- the recipient.
            url: The visitor's own demo URL.
            ttl_seconds: Hard lifetime of the instance; <= 0 means no reaping.
            marketing_consent: The optional opt-in from the contact form.
            site_url: Marketing site base for the footer links (no trailing slash).
            started_at: Provisioning time; defaults to now (UTC). Injectable so
                the rendered deadline is deterministic in tests.

        Raises:
            RuntimeError: When the notifier has no transport.
            Exception: Whatever the transport raises on a delivery failure.
        """
        if self.transport is None:  # pragma: no cover - guarded by ``configured``
            raise RuntimeError("lead notifier has no transport")
        start = started_at or datetime.now(timezone.utc)
        # With reaping disabled there is no deadline to name; the phrase then
        # reads "unbegrenzt" and the date simply repeats the start.
        expires_at = start + timedelta(seconds=ttl_seconds) if ttl_seconds > 0 else start
        first = _first_name(name)
        site = site_url.rstrip("/")
        parts = {
            "first_name": first,
            "url": url,
            "expiry": _format_expiry(expires_at),
            "ttl_phrase": _humanize_duration(ttl_seconds),
            "site_url": site,
            "marketing_consent": marketing_consent,
        }
        message = EmailMessage()
        message["From"] = self.sender or self.recipient
        message["To"] = email
        if self.recipient:
            message["Reply-To"] = self.recipient
        message["Subject"] = _welcome_subject(first)
        # set_content + add_alternative in this order yields multipart/alternative
        # with text/plain FIRST -- the order clients use to pick the richest part.
        message.set_content(_welcome_text(**parts))  # type: ignore[arg-type]
        message.add_alternative(_welcome_html(**parts), subtype="html")  # type: ignore[arg-type]
        self.transport.send(message)

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


# ---------------------------------------------------------------------------
# Welcome mail to the visitor -- design tokens + rendering
# ---------------------------------------------------------------------------

#: The ProcWorks brand palette, mirrored from ``site/styles.css`` (:root). Kept
#: here as literals because an e-mail cannot load a stylesheet: every rule has to
#: be inlined per element. When the website palette changes, change it here too.
BRAND = {
    "bg": "#0f1420",       # page backdrop (--bg)
    "card": "#161d2e",     # elevated surface (--bg-elev)
    "card2": "#1d2740",    # second elevation (--bg-elev2)
    "line": "#2a3550",     # hairline borders (--line)
    "txt": "#e6ebf5",      # primary text (--txt)
    "dim": "#9aa6bf",      # secondary text (--txt-dim)
    "accent": "#4f86ff",   # primary accent (--accent)
    "accent2": "#8a5bff",  # gradient partner (--accent-2)
    "green": "#2fbf71",    # success/validity (--green)
}

#: Font stack of the website (``site/styles.css`` body). Web fonts are avoided on
#: purpose -- many clients strip ``@font-face``, and a system stack renders
#: identically everywhere.
#:
#: The inner quoting MUST stay single: this string is interpolated into
#: ``style="..."`` attributes, so a double-quoted ``"Segoe UI"`` would terminate
#: the attribute and shred the markup.
FONT = "'Segoe UI', system-ui, -apple-system, Roboto, Arial, sans-serif"


def _humanize_duration(seconds: int) -> str:
    """Render a TTL as a short German phrase ("2 Stunden", "90 Minuten").

    Whole hours read as hours, anything else as minutes -- the demo TTL is a
    round number in practice, and an exact "1 Stunde 47 Minuten" would only make
    the promise look more precise than it is.

    Args:
        seconds: Lifetime in seconds; values <= 0 mean "no hard limit".

    Returns:
        A human phrase for the mail body.
    """
    if seconds <= 0:
        return "unbegrenzt"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return "1 Stunde" if hours == 1 else f"{hours} Stunden"
    minutes = max(1, round(seconds / 60))
    return "1 Minute" if minutes == 1 else f"{minutes} Minuten"


def _format_expiry(expires_at: datetime) -> str:
    """Format the hard-TTL deadline in German local time, with the zone named.

    Falls back to UTC when the container carries no tz database (``python:slim``
    images do not always ship ``tzdata``). Naming the zone matters: a bare
    "18:40" in the wrong zone would send the visitor back to a destroyed demo.

    Args:
        expires_at: Timezone-aware UTC deadline.

    Returns:
        e.g. ``"19.07.2026 um 18:40 Uhr (MESZ)"``.
    """
    try:
        local = expires_at.astimezone(ZoneInfo("Europe/Berlin"))
        zone = "MESZ" if local.dst() else "MEZ"
    except (ZoneInfoNotFoundError, KeyError):  # pragma: no cover - image without tzdata
        local, zone = expires_at.astimezone(timezone.utc), "UTC"
    return f"{local.strftime('%d.%m.%Y')} um {local.strftime('%H:%M')} Uhr ({zone})"


def _first_name(name: str) -> str:
    """Return the given name for the salutation, or "" when unusable."""
    return name.strip().split(" ")[0] if name.strip() else ""


def _welcome_subject(first_name: str) -> str:
    """Subject line of the welcome mail (personalised when a name is known)."""
    if first_name:
        return f"{first_name}, Ihre ProcWorks-Testumgebung ist bereit"
    return "Ihre ProcWorks-Testumgebung ist bereit"


def _welcome_text(
    *,
    first_name: str,
    url: str,
    expiry: str,
    ttl_phrase: str,
    site_url: str,
    marketing_consent: bool,
) -> str:
    """Plain-text alternative of the welcome mail.

    Never optional: a text/plain part is what makes the message readable in
    text-only clients and materially improves spam scoring -- an HTML-only mail
    is a classic reason a well-designed campaign lands in the junk folder.

    Args:
        first_name: Salutation name, "" for the neutral form.
        url: The visitor's own demo URL.
        expiry: Preformatted deadline (see :func:`_format_expiry`).
        ttl_phrase: Preformatted lifetime (see :func:`_humanize_duration`).
        site_url: Marketing site base URL (links in the footer).
        marketing_consent: Whether the optional opt-in was ticked; only then is
            the soliciting block appended (see :meth:`LeadNotifier.send_welcome`).

    Returns:
        The complete text body.
    """
    hello = f"Hallo {first_name}," if first_name else "Hallo,"
    blocks = [
        hello,
        "",
        "Ihre persoenliche ProcWorks-Testumgebung laeuft. Sie koennen sie jederzeit",
        "ueber diesen Link wieder oeffnen:",
        "",
        f"    {url}",
        "",
        f"Verfuegbar bis: {expiry}",
        f"Laufzeit: {ttl_phrase} ab dem Start.",
        "",
        "Danach wird die Umgebung automatisch und vollstaendig geloescht -- mit allen",
        "Daten, die Sie darin angelegt haben. Es ist eine Wegwerf-Demo: Sie koennen",
        "nichts kaputt machen.",
        "",
        "Sie werden automatisch als Modelliererin angemeldet. Ueber die Rollen-Box",
        "oben wechseln Sie ohne Passwort zwischen Modellierer, Bearbeiter, Leser",
        "und Administrator.",
        "",
        "Drei Dinge, die sich in zehn Minuten lohnen:",
        "  1. Einen Schritt in den Prozess einfuegen -- das Modell bleibt garantiert",
        "     ausfuehrbar, ohne Aufraeumen.",
        "  2. Bewusst einen Fehler bauen (z. B. Daten lesen, bevor sie geschrieben",
        "     werden) -- ProcWorks lehnt die Aenderung mit Begruendung ab.",
        "  3. Den Prozess starten und in 'Meine Aufgaben' abarbeiten.",
        "",
        "Fragen? Antworten Sie einfach auf diese E-Mail.",
    ]
    if marketing_consent:
        blocks += [
            "",
            "--",
            "Uebrigens: ProcWorks laeuft auch auf Ihrem eigenen Server -- On-Premise,",
            "ohne Cloud-Zwang. Wenn Sie moechten, zeigen wir Ihnen in 30 Minuten,",
            "wie sich Ihre Prozesse damit abbilden lassen. Einfach auf diese Mail",
            "antworten.",
            "Sie erhalten diesen Hinweis, weil Sie beim Start der Testversion der",
            "optionalen Kontaktaufnahme zugestimmt haben. Ein formloser Widerspruch",
            "per Antwort auf diese Mail genuegt.",
        ]
    blocks += [
        "",
        "--",
        f"ProcWorks -- {site_url}",
        f"Impressum: {site_url}/impressum.html",
        f"Datenschutz: {site_url}/datenschutz.html",
        "",
        "Sie erhalten diese E-Mail, weil ueber diese Adresse eine ProcWorks-",
        "Testversion gestartet wurde.",
    ]
    return "\n".join(blocks) + "\n"


def _welcome_html(
    *,
    first_name: str,
    url: str,
    expiry: str,
    ttl_phrase: str,
    site_url: str,
    marketing_consent: bool,
) -> str:
    """HTML part of the welcome mail, styled like procworks.de.

    E-mail HTML is not web HTML. The constraints that shape this markup:

    * **Tables, not flex/grid** -- Outlook (Word rendering engine) supports
      neither, and a broken layout in the biggest business client is not a
      trade-off worth making.
    * **Inline styles only** -- Gmail strips ``<style>`` blocks in forwarded and
      clipped messages, so every rule sits on its element.
    * **No images** -- the logo is a styled table cell with the letter "P", not a
      hosted PNG. Most clients block remote images by default, so an image-based
      header would arrive as a grey box; this one always renders. It also means
      the mail needs no asset hosting and no tracking pixel.
    * **Gradients degrade** -- the accent gradient rides on ``background-image``
      over a solid ``bgcolor``. Clients that ignore it (Outlook) still get the
      solid ProcWorks blue rather than a transparent hole.
    * **Dark by design** -- the brand is dark, and ``color-scheme``/
      ``supported-color-schemes`` stop iOS/Apple Mail from re-inverting it.

    Args (see :func:`_welcome_text`): identical inputs, HTML-escaped here.

    Returns:
        The complete HTML body.
    """
    c = BRAND
    safe_url = html.escape(url, quote=True)
    hello = f"Hallo {html.escape(first_name)}," if first_name else "Hallo,"

    def section(inner: str) -> str:
        """Wrap content in a full-width padded row of the card."""
        return f'<tr><td style="padding:0 32px;">{inner}</td></tr>'

    p_style = f"margin:0 0 16px;font:400 16px/1.65 {FONT};color:{c['txt']};"
    dim_style = f"margin:0 0 16px;font:400 15px/1.6 {FONT};color:{c['dim']};"

    # The three "worth trying" items -- the actual product story, told as things
    # the reader can do rather than as feature bullets.
    steps = [
        (
            "Einen Schritt einf&uuml;gen",
            "Ziehen Sie eine Aktivit&auml;t in den Prozess. Das Modell bleibt "
            "garantiert ausf&uuml;hrbar &ndash; ohne Nacharbeit.",
        ),
        (
            "Absichtlich einen Fehler bauen",
            "Lesen Sie Daten, bevor sie geschrieben werden. ProcWorks lehnt die "
            "&Auml;nderung ab und sagt Ihnen genau, warum.",
        ),
        (
            "Den Prozess ausf&uuml;hren",
            "Starten Sie eine Instanz und arbeiten Sie sie unter &bdquo;Meine "
            "Aufgaben&ldquo; ab &ndash; dasselbe Modell, live.",
        ),
    ]
    step_rows = "".join(
        f'<tr>'
        f'<td width="34" valign="top" style="padding:0 0 18px;">'
        f'<div style="width:24px;height:24px;border-radius:12px;'
        f'background-color:{c["card2"]};border:1px solid {c["line"]};'
        f'font:700 12px/24px {FONT};color:{c["accent"]};text-align:center;">{i}</div>'
        f'</td>'
        f'<td valign="top" style="padding:0 0 18px;">'
        f'<div style="font:600 15px/1.4 {FONT};color:{c["txt"]};padding-bottom:3px;">{title}</div>'
        f'<div style="font:400 14px/1.6 {FONT};color:{c["dim"]};">{body}</div>'
        f'</td></tr>'
        for i, (title, body) in enumerate(steps, start=1)
    )

    # Soliciting block -- ONLY with the optional opt-in from the contact form.
    # The mandatory consent covers providing the test access (Art. 6 (1) (b));
    # advertising rides on the separate, optional consent (Art. 6 (1) (a)), which
    # the form promises not to couple to the demo.
    promo = ""
    if marketing_consent:
        promo = section(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin:8px 0 28px;border-collapse:separate;">'
            f'<tr><td style="padding:22px 24px;background-color:{c["card2"]};'
            f'border:1px solid {c["line"]};border-radius:14px;">'
            f'<div style="font:700 16px/1.4 {FONT};color:{c["txt"]};padding-bottom:8px;">'
            f'Wie sieht das mit <em>Ihren</em> Prozessen aus?</div>'
            f'<div style="font:400 15px/1.65 {FONT};color:{c["dim"]};padding-bottom:14px;">'
            f'ProcWorks l&auml;uft auch auf Ihrem eigenen Server &ndash; On-Premise, ohne '
            f'Cloud-Zwang. In 30 Minuten zeigen wir Ihnen, wie sich Ihre Abl&auml;ufe '
            f'damit abbilden lassen. Antworten Sie einfach auf diese E-Mail.</div>'
            f'<div style="font:400 12px/1.55 {FONT};color:{c["dim"]};opacity:.75;">'
            f'Sie erhalten diesen Hinweis, weil Sie beim Start der Testversion der '
            f'optionalen Kontaktaufnahme zugestimmt haben. Ein formloser Widerspruch '
            f'per Antwort auf diese Mail gen&uuml;gt.</div>'
            f'</td></tr></table>'
        )

    return f"""\
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="dark light" />
<meta name="supported-color-schemes" content="dark light" />
<title>{html.escape(_welcome_subject(first_name))}</title>
</head>
<body style="margin:0;padding:0;background-color:{c['bg']};">
<!-- Preheader: the grey preview line in the inbox. Hidden in the body itself. -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">
Ihr Link zur Testumgebung &ndash; verf&uuml;gbar bis {html.escape(expiry)}.
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       bgcolor="{c['bg']}" style="background-color:{c['bg']};">
<tr><td align="center" style="padding:32px 12px;">

<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
       style="width:600px;max-width:100%;background-color:{c['card']};
              border:1px solid {c['line']};border-radius:16px;overflow:hidden;">

  <!-- Brand band. bgcolor carries clients that drop the gradient. -->
  <tr><td bgcolor="{c['accent']}"
          style="background-color:{c['accent']};
                 background-image:linear-gradient(135deg,{c['accent']},{c['accent2']});
                 padding:22px 32px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
      <td width="34" style="padding-right:11px;">
        <div style="width:34px;height:34px;border-radius:8px;
                    background-color:rgba(255,255,255,.18);
                    font:800 15px/34px {FONT};color:#ffffff;text-align:center;">P</div>
      </td>
      <td style="font:700 18px/34px {FONT};color:#ffffff;">ProcWorks</td>
    </tr></table>
  </td></tr>

  <tr><td style="padding:34px 32px 0;">
    <h1 style="margin:0 0 6px;font:700 27px/1.25 {FONT};color:{c['txt']};">
      Ihre Testumgebung steht bereit</h1>
    <div style="font:400 15px/1.5 {FONT};color:{c['accent']};padding-bottom:22px;">
      Correctness by Construction &ndash; zum Anfassen</div>
  </td></tr>

  {section(f'<p style="{p_style}">{hello}</p>')}
  {section(
      f'<p style="{p_style}">wir haben eine ProcWorks-Instanz nur f&uuml;r Sie '
      f'gestartet. &Uuml;ber diesen Link kommen Sie jederzeit zur&uuml;ck &ndash; '
      f'ohne Anmeldung, ohne Installation.</p>'
  )}

  <!-- Call to action. The bulletproof pattern: a table cell with bgcolor whose
       anchor fills it, so the whole block stays clickable without CSS support. -->
  {section(
      f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
      f'style="margin:6px 0 24px;">'
      f'<tr><td bgcolor="{c["accent"]}" style="background-color:{c["accent"]};'
      f'background-image:linear-gradient(135deg,{c["accent"]},{c["accent2"]});'
      f'border-radius:11px;">'
      f'<a href="{safe_url}" target="_blank" rel="noopener" '
      f'style="display:inline-block;padding:15px 34px;font:700 16px/1 {FONT};'
      f'color:#ffffff;text-decoration:none;">Testumgebung &ouml;ffnen &rarr;</a>'
      f'</td></tr></table>'
  )}

  <!-- Validity. The reason this mail exists at all: what the visitor gets and
       for how long, stated before they have to ask. -->
  {section(
      f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
      f'border="0" style="margin:0 0 26px;border-collapse:separate;">'
      f'<tr><td style="padding:18px 22px;background-color:{c["card2"]};'
      f'border:1px solid {c["line"]};border-left:3px solid {c["green"]};'
      f'border-radius:12px;">'
      f'<div style="font:700 13px/1.4 {FONT};color:{c["green"]};'
      f'letter-spacing:.06em;text-transform:uppercase;padding-bottom:7px;">'
      f'So lange ist Ihre Umgebung reserviert</div>'
      f'<div style="font:600 16px/1.5 {FONT};color:{c["txt"]};padding-bottom:5px;">'
      f'Verf&uuml;gbar bis {html.escape(expiry)}</div>'
      f'<div style="font:400 14px/1.6 {FONT};color:{c["dim"]};">'
      f'Das sind {html.escape(ttl_phrase)} ab dem Start. Danach wird die Umgebung '
      f'automatisch und vollst&auml;ndig gel&ouml;scht &ndash; samt allem, was Sie '
      f'darin angelegt haben. Es ist eine Wegwerf-Demo: Sie k&ouml;nnen nichts '
      f'kaputt machen.</div>'
      f'</td></tr></table>'
  )}

  {section(
      f'<div style="font:700 17px/1.4 {FONT};color:{c["txt"]};padding-bottom:14px;">'
      f'Drei Dinge, die sich in zehn Minuten lohnen</div>'
      f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
      f'border="0">{step_rows}</table>'
  )}

  {section(
      f'<p style="{dim_style}">Sie sind automatisch als Modelliererin angemeldet. '
      f'&Uuml;ber die Rollen-Box oben wechseln Sie ohne Passwort zwischen '
      f'Modellierer, Bearbeiter, Leser und Administrator &ndash; jede Rolle sieht '
      f'ihre eigene Oberfl&auml;che.</p>'
  )}

  {promo}

  {section(
      f'<div style="border-top:1px solid {c["line"]};margin:6px 0 0;"></div>'
  )}
  <tr><td style="padding:20px 32px 30px;">
    <div style="font:400 14px/1.6 {FONT};color:{c['dim']};padding-bottom:14px;">
      Fragen? Antworten Sie einfach auf diese E-Mail &ndash; da sitzt ein Mensch.</div>
    <div style="font:400 13px/1.7 {FONT};color:{c['dim']};">
      <a href="{html.escape(site_url, quote=True)}" target="_blank" rel="noopener"
         style="color:{c['accent']};text-decoration:none;">procworks.de</a>
      &nbsp;&middot;&nbsp;
      <a href="{html.escape(site_url, quote=True)}/impressum.html" target="_blank"
         rel="noopener" style="color:{c['dim']};text-decoration:underline;">Impressum</a>
      &nbsp;&middot;&nbsp;
      <a href="{html.escape(site_url, quote=True)}/datenschutz.html" target="_blank"
         rel="noopener" style="color:{c['dim']};text-decoration:underline;">Datenschutz</a>
    </div>
    <div style="font:400 12px/1.6 {FONT};color:{c['dim']};opacity:.7;padding-top:10px;">
      Sie erhalten diese E-Mail, weil &uuml;ber diese Adresse eine
      ProcWorks-Testversion gestartet wurde.</div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>
"""


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
