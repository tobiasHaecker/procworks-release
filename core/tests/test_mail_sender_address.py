# SPDX-License-Identifier: BUSL-1.1
"""An unusable sender address is reported once at start, never fatal.

``PROCWORKS_MAIL_FROM`` goes into ``From:`` and ``MAIL FROM:<...>``. A value
like ``procworks`` (no domain) or ``pw@procworks`` (domain not fully
qualified) is no valid address there; mail servers refuse or mark such
mails, and nothing told the operator why. With SMTP configured the start
now logs one warning with the reason -- without the value, which can name a
person -- and goes on.
"""

from __future__ import annotations

import logging

import pytest

from procworks import mail_runtime
from procworks.mail_runtime import create_mail_sender, mail_from_problem

#: A local part that must never appear in the log (stands for a person's name).
_PERSONAL = "erika.musterfrau"


def _configure(monkeypatch: pytest.MonkeyPatch, sender: str | None, *,
               host: str | None = "127.0.0.1") -> None:
    """Set the SMTP environment for one test (``None`` removes the variable)."""

    for name, value in (("PROCWORKS_SMTP_HOST", host), ("PROCWORKS_MAIL_FROM", sender)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.delenv("PROCWORKS_SMTP_EHLO_NAME", raising=False)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.name == "procworks.mail" and r.levelno == logging.WARNING]


@pytest.mark.parametrize(
    "sender",
    ["pw@example.org", "ProcWorks <pw@Mail.Example.org>", f"{_PERSONAL}@firma.de",
     "pw@[192.0.2.10]", "pw@[IPv6:2001:db8::1]", " pw@example.org ",
     # A comment after the address is a valid form; ``From`` and the
     # envelope carry the bare address.
     "pw@example.org (Benachrichtigungen)", '"Müller, Max" <max@firma.example>',
     "<pw@example.org>", "Jörg Bäcker <jb@firma.example>"],
)
def test_a_usable_sender_starts_without_a_warning(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, sender: str) -> None:
    """Positiv: name and address, address literal, blanks around -- no warning."""

    _configure(monkeypatch, sender)
    with caplog.at_level(logging.WARNING, logger="procworks.mail"):
        assert isinstance(create_mail_sender(), mail_runtime.SmtpMailSender)
    assert _warnings(caplog) == []
    assert mail_from_problem(sender) is None


@pytest.mark.parametrize(
    ("sender", "reason"),
    [
        ("procworks", "no single e-mail address"),
        (f"{_PERSONAL}@", "no single e-mail address"),
        ("@example.org", "no single e-mail address"),
        (f"ProcWorks <{_PERSONAL}>", "no single e-mail address"),
        (f"{_PERSONAL}@example.org, x@example.org", "no single e-mail address"),
        (f"{_PERSONAL}@procworks", "not fully qualified"),
        (f"{_PERSONAL}@192.0.2.10", "not fully qualified"),
        (f"ProcWorks <{_PERSONAL}@procworks>", "not fully qualified"),
        # The header parser cannot read these as one address (a blank in the
        # domain, a trailing dot): what would be sent is not the configured
        # address, so the reason is the form, not the domain.
        (f"{_PERSONAL}@exa mple.org", "no single e-mail address"),
        (f"{_PERSONAL}@example.org.", "no single e-mail address"),
        # Doubled angle brackets: ``From:`` and ``MAIL FROM`` would carry only
        # the display name -- unusable, so it must warn.
        (f"Name <<{_PERSONAL}@firma.example>>", "no single e-mail address"),
        # A stray bracket: the domain is fine, the form is not -- never blame
        # the domain for it.
        (f"{_PERSONAL}@firma.example>", "no single e-mail address"),
        (f"Name <{_PERSONAL}@firma.example", "no single e-mail address"),
        (f"{_PERSONAL}@firma.example <x@firma.example>", "no single e-mail address"),
        (f"{_PERSONAL}@firma.example\r\nBcc: x@firma.example", "no single e-mail address"),
        # Umlaut in the local part (not in the name): only sendable over
        # SMTPUTF8 -- in doubt, the form is named.
        ("Müller <müller@firma.example>", "no single e-mail address"),
    ],
)
def test_an_unusable_sender_logs_one_warning_with_the_reason(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        sender: str, reason: str) -> None:
    """Negativ (der Grund): exactly one warning naming why -- the start goes on
    (an SMTP sender is still built), the value never reaches the log."""

    _configure(monkeypatch, sender)
    with caplog.at_level(logging.WARNING, logger="procworks.mail"):
        assert isinstance(create_mail_sender(), mail_runtime.SmtpMailSender)
    records = _warnings(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert reason in message and "PROCWORKS_MAIL_FROM" in message
    assert _PERSONAL not in message and "procworks@" not in message
    assert sender.strip() not in message


@pytest.mark.parametrize("sender", [None, "", "   "])
def test_a_missing_sender_with_smtp_logs_that_nothing_is_sent(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        sender: str | None) -> None:
    """Negativ (der Grund): SMTP host without sender -- one warning, and no mail
    goes out (the no-op sender as before for an unset value)."""

    _configure(monkeypatch, sender)
    with caplog.at_level(logging.WARNING, logger="procworks.mail"):
        create_mail_sender()
    records = _warnings(caplog)
    assert len(records) == 1
    assert "PROCWORKS_MAIL_FROM is not set" in records[0].getMessage()


@pytest.mark.parametrize("sender", [None, "procworks", f"{_PERSONAL}@procworks"])
def test_without_smtp_the_sender_is_not_checked(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        sender: str | None) -> None:
    """Grenzfall: no mail server configured -- nothing to warn about."""

    _configure(monkeypatch, sender, host=None)
    with caplog.at_level(logging.DEBUG, logger="procworks.mail"):
        assert isinstance(create_mail_sender(), mail_runtime.NullMailSender)
    assert _warnings(caplog) == []
