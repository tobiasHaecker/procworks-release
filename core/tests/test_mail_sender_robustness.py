# SPDX-License-Identifier: BUSL-1.1
"""No value of ``PROCWORKS_MAIL_FROM`` can abort the start or the dispatcher.

The sender is read with the header parser of the standard library (the same
reading ``From:`` gets). That parser does not only refuse malformed input
with ``ValueError``: for an address literal without its closing bracket
(``pw@[192.0.2.10``) it fails inside itself with ``AttributeError``
(Python 3.12), for other inputs with ``IndexError``. The start check and the
envelope derivation caught only some of these types -- the API did not start
at all with such a value, although the check promises to warn and go on.

These tests pin the promise for the known forms and then for many random,
broken values (fixed seed, standard library only): every sender function
answers or raises ``ValueError`` with a reason, never anything else.
"""

from __future__ import annotations

import logging
import random
import re
from email import policy

import pytest
from test_mail_headers import _capturing_smtp_server, _mail

from procworks import mail_runtime
from procworks.mail_runtime import (
    UNREADABLE_SENDER,
    MailMessage,
    MailOutboxDispatcher,
    SmtpMailSender,
    create_mail_sender,
    envelope_sender,
    mail_from_problem,
    parse_ehlo_name,
    smtp_message_id,
)
from procworks.model import MailOutboxState
from procworks.store import InMemoryMailOutboxStore

#: A local part that must never appear in a log or an outbox error.
_PERSONAL = "erika.musterfrau"

#: Address literals without their closing bracket: the header parser of the
#: standard library fails on them with ``AttributeError`` (not ``ValueError``).
_BROKEN_LITERALS = [
    f"{_PERSONAL}@[192.0.2.10",
    f"{_PERSONAL}@[",
    "a@[b",
    f"Name <{_PERSONAL}@[192.0.2.10>",
    f"{_PERSONAL}@[IPv6:2001:db8::1",
]

_NO_SINGLE = "no single e-mail address"


def _configure(monkeypatch: pytest.MonkeyPatch, sender: str) -> None:
    """SMTP configured with ``sender`` and no greeting name."""

    monkeypatch.setenv("PROCWORKS_SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("PROCWORKS_MAIL_FROM", sender)
    monkeypatch.delenv("PROCWORKS_SMTP_EHLO_NAME", raising=False)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.name == "procworks.mail" and r.levelno == logging.WARNING]


def test_the_parser_itself_fails_on_these_values_with_other_errors() -> None:
    """Ursache: the standard header parser raises something other than
    ``ValueError`` for these values. Skipped once a Python version reads
    them cleanly (the hardening stays correct)."""

    from email.message import EmailMessage

    other = []
    for value in _BROKEN_LITERALS:
        try:
            EmailMessage()["From"] = value
        except ValueError:
            continue
        except Exception as exc:  # noqa: BLE001 -- the type is what this test records
            other.append(type(exc).__name__)
    if not other:
        pytest.skip("this Python parses broken address literals without failing")
    assert set(other) <= {"AttributeError", "IndexError", "TypeError"}


@pytest.mark.parametrize("sender", _BROKEN_LITERALS)
def test_a_broken_address_literal_never_aborts_the_start(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, sender: str) -> None:
    """Negativ (der Grund): the start goes on with exactly one warning *no
    single e-mail address*, without the value."""

    _configure(monkeypatch, sender)
    with caplog.at_level(logging.WARNING, logger="procworks.mail"):
        assert isinstance(create_mail_sender(), SmtpMailSender)
    records = _warnings(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert _NO_SINGLE in message and "PROCWORKS_MAIL_FROM" in message
    assert _PERSONAL not in message and sender not in message


@pytest.mark.parametrize("sender", _BROKEN_LITERALS)
def test_a_broken_address_literal_has_no_envelope(sender: str) -> None:
    """Negativ (der Grund): no envelope can be named, the start check says why."""

    assert envelope_sender(sender) is None
    problem = mail_from_problem(sender)
    assert problem is not None and _NO_SINGLE in problem


@pytest.mark.parametrize("sender", _BROKEN_LITERALS)
def test_a_broken_address_literal_is_never_sent(sender: str) -> None:
    """Negativ (der Grund), auf der Leitung: the send fails with the reason
    before any connection -- nothing reaches the receiver, least of all
    ``MAIL FROM:<>`` (``smtplib`` reads no address from these values)."""

    envelope: list[bytes] = []
    messages: list[bytes] = []
    with _capturing_smtp_server(messages, envelope) as port:
        with pytest.raises(ValueError) as exc:
            SmtpMailSender(host="127.0.0.1", port=port, sender=sender, use_tls=False,
                           timeout=2, total_timeout=5,
                           ehlo_name="mail.example.org").send(_mail())
    assert str(exc.value) == UNREADABLE_SENDER
    assert envelope == [] and messages == []
    assert _PERSONAL not in str(exc.value)


def test_a_broken_address_literal_fails_in_the_outbox_with_the_reason() -> None:
    """Negativ (der Grund), Outbox: a failed attempt (retried later) with the
    reason, never the value, and the dispatcher goes on."""

    store = InMemoryMailOutboxStore()
    dispatcher = MailOutboxDispatcher(store, now=lambda: 100.0)
    entry = dispatcher.enqueue(_mail(), "key-1")
    assert entry is not None
    sender = SmtpMailSender(host="127.0.0.1", port=9, sender=_BROKEN_LITERALS[0],
                            use_tls=False, timeout=1, total_timeout=2)
    results = dispatcher.dispatch_pending(sender)
    assert len(results) == 1 and not results[0].delivered and not results[0].dead
    stored = store.get_entry(entry.id)
    assert stored.state is MailOutboxState.FAILED
    assert stored.last_error == UNREADABLE_SENDER


@pytest.mark.parametrize("recipient", ["a@[b", "x@[192.0.2.10"])
def test_an_unreadable_recipient_fails_with_a_reason(recipient: str) -> None:
    """Negativ (der Grund): a recipient the header parser cannot read gives a
    reason without the value, not the parser's internal error text."""

    with pytest.raises(ValueError) as exc:
        SmtpMailSender(host="127.0.0.1", port=25, sender="pw@example.org",
                       use_tls=False).build_email(_mail(to=[recipient]))
    assert str(exc.value) == mail_runtime.UNREADABLE_RECIPIENT
    assert recipient not in str(exc.value)


@pytest.mark.parametrize(
    ("sender", "reason"),
    [("pw@]", _NO_SINGLE), ("pw@[1.2.3.4]]", _NO_SINGLE),
     ("pw@[]", "not fully qualified")],
)
def test_other_bracket_forms_keep_their_reading(sender: str, reason: str) -> None:
    """Grenzfall: bracket forms the parser reads without failing keep their
    reason (an empty literal is a clean address with an unusable domain)."""

    problem = mail_from_problem(sender)
    assert problem is not None and reason in problem


# --------------------------------------------------------------------------- #
# Property test over random, broken values
# --------------------------------------------------------------------------- #
#: Characters that steer the address grammar, plus line breaks, controls,
#: umlauts, bidi controls and encoded-word markers.
_ALPHABET = (
    "ab.@[]<>\"(),:;\\ \t\r\n%=?IPv6:0129äß\x00\x07\x7f‮ " + "\U0001f600"
)

#: Fragments that put the parser into its rarer states.
_FRAGMENTS = [
    "pw@", "@", "[", "]", "<", ">", "\"", "(", ")", "IPv6:", "192.0.2.10",
    "2001:db8::1", "example.org", "Name ", ",", ";", ":", "=?utf-8?q?", "?=",
    "\\", "\r\n", " ", "<<", ">>", "[IPv6:", "%en0", "ä",
]

_SEED = 20261011
_ROUNDS = 1500

_MSG_ID = re.compile(r"<[0-9a-f]{32}@[A-Za-z0-9.-]+>")


def _random_values(seed: int, rounds: int) -> list[str]:
    """Deterministic random values: character soup and fragment chains."""

    rng = random.Random(seed)
    values = []
    for _ in range(rounds):
        if rng.random() < 0.5:
            values.append("".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 24))))
        else:
            values.append("".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(1, 8))))
    return values


def test_no_sender_value_raises_anything_but_a_reason() -> None:
    """Eigenschaft: for every value the start check answers (``str``/``None``,
    never the value itself in the reason), the envelope is ``str``/``None``,
    the sender is built, ``build_email`` either builds a mail the transport
    can serialise or raises ``ValueError``, the ``Message-ID`` is always
    valid, and ``parse_ehlo_name`` raises only ``ValueError``."""

    for value in _random_values(_SEED, _ROUNDS) + _BROKEN_LITERALS:
        problem = mail_from_problem(value)
        assert problem is None or isinstance(problem, str), repr(value)
        envelope = envelope_sender(value)
        assert envelope is None or isinstance(envelope, str), repr(value)
        sender = SmtpMailSender(host="127.0.0.1", port=25, sender=value, use_tls=False)
        try:
            email = sender.build_email(_mail())
        except ValueError:
            pass
        else:
            email.as_bytes(policy=policy.SMTPUTF8)
            mail_runtime._smtplib_envelope(email)
        assert _MSG_ID.fullmatch(smtp_message_id("k", value)), repr(value)
        try:
            parse_ehlo_name(value)
        except ValueError:
            pass


def test_no_recipient_value_raises_anything_but_a_reason() -> None:
    """Eigenschaft: random recipients never make ``build_email`` raise
    anything but ``ValueError`` (the outbox records the reason)."""

    sender = SmtpMailSender(host="127.0.0.1", port=25, sender="pw@example.org",
                            use_tls=False)
    for value in _random_values(_SEED + 1, _ROUNDS // 3):
        message = MailMessage(to=[value], subject="s", body="b", instance_id="i",
                              node_id="n", schema_id="s", message_id="k")
        try:
            sender.build_email(message)
        except ValueError:
            pass


def test_no_sender_value_aborts_the_start(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Eigenschaft: ``create_mail_sender`` returns for every value an
    environment can hold (no NUL) and logs at most one warning."""

    for value in _random_values(_SEED + 2, _ROUNDS // 3) + _BROKEN_LITERALS:
        value = value.replace("\x00", "")
        _configure(monkeypatch, value)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="procworks.mail"):
            create_mail_sender()
        assert len(_warnings(caplog)) <= 1, repr(value)
