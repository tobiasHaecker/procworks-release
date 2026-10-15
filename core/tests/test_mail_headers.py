# SPDX-License-Identifier: BUSL-1.1
"""The headers of a notification arrive at the receiver as they were meant.

``EmailMessage`` parses every header value it is given. A value that is no
valid header of its kind is not refused but cut down: the ``Message-ID`` was
built from the outbox key ``<instance|node|2026-10-11T14:12:50+00:00@procworks>``
and ended on the wire after the hour (``<instance|node|2026-10-11T14``) -- the
first ``:`` ended the parse, ``@procworks>`` was gone. Two activations of the
same step within one hour carried the same id, and a receiver that drops
duplicates by ``Message-ID`` could drop the second notification of a loop.

These tests therefore look at the bytes that go out (``as_bytes()`` and a
local SMTP receiver), never only at the field of the message object, and
parse them back the way a receiver does.
"""

from __future__ import annotations

import re
import smtplib
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from email import message_from_bytes, policy
from email.header import decode_header, make_header

import pytest

from procworks.mail_runtime import (
    NO_ENVELOPE_SENDER,
    MailMessage,
    MailOutboxDispatcher,
    SmtpMailSender,
    envelope_sender,
    mail_from_problem,
    smtp_message_id,
)
from procworks.model import MailOutboxState
from procworks.store import InMemoryMailOutboxStore

#: RFC 5322 ``msg-id`` with a ``dot-atom-text`` on both sides (the form this
#: sender promises; no quoted string, no literal).
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_MSG_ID = re.compile(rf"<{_ATOM}(?:\.{_ATOM})*@{_ATOM}(?:\.{_ATOM})*>")

_KEY = "instance_1|act_2|2026-10-11T14:12:50.123456+00:00"


def _sender() -> SmtpMailSender:
    return SmtpMailSender(host="127.0.0.1", port=25, sender="ProcWorks <pw@Mail.Example.org>",
                          use_tls=False)


def _mail(**changes: object) -> MailMessage:
    fields: dict[str, object] = {
        "to": ["a@example.org"], "subject": "s", "body": "b",
        "instance_id": "instance_1", "node_id": "act_2", "schema_id": "s1",
        "message_id": _KEY,
    }
    fields.update(changes)
    return MailMessage.model_validate(fields)


def _displayed_subjects(raw: bytes) -> list[str]:
    """The subject a mail program displays, decoded in two independent ways.

    ``email.policy.default`` (the modern parser) and the legacy route of
    ``decode_header``/``make_header`` over the raw header both decode every
    encoded word, the way a receiving mail program does.

    :param raw: the message bytes as sent.
    :returns: both decoded subjects (should be equal).
    """

    modern = str(message_from_bytes(raw, policy=policy.default)["Subject"])
    legacy_raw = message_from_bytes(raw, policy=policy.compat32)["Subject"]
    legacy = str(make_header(decode_header(str(legacy_raw))))
    return [modern, legacy]


def _wire_header(raw: bytes, name: str) -> str:
    """The unfolded value of header ``name`` exactly as it stands in ``raw``."""

    head = raw.split(b"\n\n", 1)[0].replace(b"\r\n", b"\n")
    unfolded = re.sub(rb"\n[ \t]", b" ", head).decode("ascii")
    values = [line.split(":", 1)[1].strip() for line in unfolded.split("\n")
              if line.lower().startswith(name.lower() + ":")]
    assert len(values) == 1, (name, unfolded)
    return values[0]


# --- the Message-ID on the wire --------------------------------------------------------------


def test_the_message_id_on_the_wire_is_complete_and_valid() -> None:
    """Positiv: the whole id goes out, as an RFC 5322 msg-id with the sender's domain."""

    raw = _sender().build_email(_mail()).as_bytes()
    wire = _wire_header(raw, "Message-ID")
    assert _MSG_ID.fullmatch(wire), wire
    assert wire == smtp_message_id(_KEY, "ProcWorks <pw@Mail.Example.org>")
    assert wire.endswith("@mail.example.org>")
    # A receiver parsing the bytes sees the same id, not a shortened one.
    parsed = message_from_bytes(raw, policy=policy.default)
    assert parsed["Message-ID"] == wire


def test_two_activations_in_the_same_hour_get_different_ids() -> None:
    """Negativ (der Grund): the ids differ although only minutes and seconds differ.

    Before, both arrived as ``<instance_1|act_2|2026-10-11T14`` -- the
    receiver could take the loop's second notification for a duplicate.
    """

    first = _wire_header(_sender().build_email(_mail()).as_bytes(), "Message-ID")
    later = _wire_header(_sender().build_email(
        _mail(message_id="instance_1|act_2|2026-10-11T14:59:01.000001+00:00")).as_bytes(),
        "Message-ID")
    assert first != later
    assert "2026-10-11T14" not in first  # no part of the key that the parser cuts


def test_a_repeated_delivery_carries_the_same_id() -> None:
    """Grenzfall: the at-least-once retry of one activation keeps its id, so the
    receiver can recognise it -- also from a new sender object (restart)."""

    once = _wire_header(_sender().build_email(_mail()).as_bytes(), "Message-ID")
    again = _wire_header(_sender().build_email(_mail()).as_bytes(), "Message-ID")
    assert once == again


def test_no_message_id_without_a_key() -> None:
    """Grenzfall: the best-effort path without a key sends no Message-ID header
    of its own (the receiving server may add one)."""

    raw = _sender().build_email(_mail(message_id="")).as_bytes()
    assert b"message-id" not in raw.lower()


@pytest.mark.parametrize(
    ("sender", "domain"),
    [
        ("pw@example.org", "example.org"),
        ("ProcWorks <PW@Example.ORG>", "example.org"),
        ("pw@localhost", "localhost"),
        ("kein-absender", "procworks.invalid"),        # no domain at all
        ("pw@[192.0.2.10]", "procworks.invalid"),      # literal, no dot-atom
        ("pw@bad_domain.example", "procworks.invalid"),  # no host name
    ],
)
def test_the_right_hand_side_of_the_id(sender: str, domain: str) -> None:
    """The right-hand side is the sender's domain or, if it has none usable,
    a fixed name -- never something the header parser could cut."""

    msg_id = smtp_message_id(_KEY, sender)
    assert _MSG_ID.fullmatch(msg_id), msg_id
    assert msg_id.endswith(f"@{domain}>")


@pytest.mark.parametrize("key", [_KEY, "", "ä|ö|ü", "a\r\nBcc: x@example.org", "x" * 2000])
def test_any_key_gives_a_short_valid_id(key: str) -> None:
    """Grenzfall: whatever the key holds, the id stays a short valid msg-id."""

    msg_id = smtp_message_id(key, "pw@example.org")
    assert _MSG_ID.fullmatch(msg_id)
    assert len(msg_id) < 64  # fits one header line, never folded


# --- the other headers carrying modelled data --------------------------------------------


def test_the_subject_keeps_umlauts_and_special_characters() -> None:
    """Positiv: a subject from model data arrives unchanged after parsing back.

    ``:`` ``|`` ``<>`` and quotes are what cut the Message-ID; the subject is
    an unstructured header and keeps them (umlauts as an encoded word).
    """

    subject = 'Antrag prüfen: Müller | <Eilt> "bitte" ' + "lang " * 30 + "Ende"
    raw = _sender().build_email(_mail(subject=subject)).as_bytes()
    assert all(len(line) <= 998 for line in raw.split(b"\n"))
    parsed = message_from_bytes(raw, policy=policy.default)
    assert parsed["Subject"] == subject


def test_line_breaks_in_the_subject_never_open_a_new_header() -> None:
    """Negativ (der Grund): CR/LF from model data becomes a blank, no ``Bcc`` appears."""

    raw = _sender().build_email(
        _mail(subject="Hallo\r\nBcc: x@example.org\nX-Evil: 1")).as_bytes()
    parsed = message_from_bytes(raw, policy=policy.default)
    assert parsed["Bcc"] is None and parsed["X-Evil"] is None
    assert parsed["Subject"] == "Hallo  Bcc: x@example.org X-Evil: 1"


#: Subjects that contain text in the shape of an encoded word (RFC 2047) --
#: from model data or a value a worker entered (``{betreff}``).
_ENCODED_WORD_SUBJECTS = [
    "=?utf-8?q?Hallo_Welt?= Rest",                      # whole token looks encoded
    "Störung: =?utf-8?q?a=0D=0ABcc:_evil@x.example?=",  # CR/LF hidden in the encoding
    "=?utf-8?b?QQ==?=",                                 # base64 form, nothing else
    "a=?b?=c",                                          # inside a word
    "=?",                                               # only the opening
    "Müller " + "ä" * 60 + " =?x?q?y?= " + "z" * 150,  # long: folded over lines
]


@pytest.mark.parametrize("subject", _ENCODED_WORD_SUBJECTS)
def test_a_subject_that_looks_encoded_arrives_literally(subject: str) -> None:
    """Positiv: the receiver displays exactly the subject of the model.

    ``EmailMessage`` decoded such a text already when the header was set --
    ``=?utf-8?q?Hallo_Welt?= Rest`` arrived as ``Hallo Welt Rest``.
    """

    raw = _sender().build_email(_mail(subject=subject)).as_bytes()
    assert _displayed_subjects(raw) == [subject, subject]
    head = raw.split(b"\n\n", 1)[0]
    assert all(len(line) <= 78 for line in head.split(b"\n"))
    assert head.isascii()


@pytest.mark.parametrize(
    ("subject", "form"),
    [("Modell =?x?= Drucker defekt", "q"), ("äöüäöüäöü =?", "b")],
)
def test_the_own_encoding_picks_the_shorter_form_per_word(subject: str, form: str) -> None:
    """Grenzfall: the own encoding is not always Base64 -- each encoded word
    is ``Q`` or ``B``, whichever is shorter; both decode to the same text."""

    raw = _sender().build_email(_mail(subject=subject)).as_bytes()
    assert _wire_header(raw, "Subject").startswith(f"=?utf-8?{form}?")
    assert _displayed_subjects(raw) == [subject, subject]


def test_an_encoded_line_break_in_the_subject_never_reaches_the_receiver() -> None:
    """Negativ (der Grund): an encoded word with ``=0D=0A`` from instance data
    decoded to CR/LF in the displayed subject -- it now stays literal text,
    and no decoder finds a line break in it."""

    subject = "=?utf-8?q?a=0D=0ABcc:_evil@x.example?="
    raw = _sender().build_email(_mail(subject=f"Störung: {subject}")).as_bytes()
    parsed = message_from_bytes(raw, policy=policy.default)
    assert parsed["Bcc"] is None
    for displayed in _displayed_subjects(raw):
        assert "\r" not in displayed and "\n" not in displayed
        assert displayed == f"Störung: {subject}"


@pytest.mark.parametrize("literal_marker", ["", " =?"])
@pytest.mark.parametrize(
    ("control", "name"),
    [("\t", "TAB"), ("\x00", "NUL"), ("\x1b", "ESC"), ("\x85", "NEL"),
     ("\u2028", "LINE SEPARATOR"), ("\u2029", "PARAGRAPH SEPARATOR"), ("\x7f", "DEL")],
)
def test_control_characters_become_blanks_in_the_displayed_subject(
        control: str, name: str, literal_marker: str) -> None:
    """Negativ: no control character or Unicode line break reaches the
    displayed subject -- on both paths (library encoding, and the own encoding
    for a text with ``=?``)."""

    raw = _sender().build_email(
        _mail(subject=f"a{control}b{literal_marker}")).as_bytes()
    assert _displayed_subjects(raw) == [f"a b{literal_marker}"] * 2, name


#: The bidirectional embedding, override and isolate controls: with them a
#: value from instance data turns the displayed order of the subject around
#: (a file extension or a name appears different from what it is).
_BIDI_CONTROLS = [chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))]


@pytest.mark.parametrize("literal_marker", ["", " =?"])
@pytest.mark.parametrize("control", _BIDI_CONTROLS, ids=lambda c: f"U+{ord(c):04X}")
def test_bidi_controls_become_blanks_in_the_displayed_subject(
        control: str, literal_marker: str) -> None:
    """Negativ (der Grund): no bidi control reaches the displayed subject --
    ``Rechnung<RLO>fdp.exe`` would be shown as ``Rechnungexe.pdf``. One blank
    each, on both encoding paths."""

    raw = _sender().build_email(
        _mail(subject=f"Rechnung{control}fdp.exe{literal_marker}")).as_bytes()
    assert _displayed_subjects(raw) == [f"Rechnung fdp.exe{literal_marker}"] * 2


@pytest.mark.parametrize("literal_marker", ["", " =?"])
@pytest.mark.parametrize(
    "kept",
    [
        "\U0001F468‍\U0001F469‍\U0001F467",  # family: joined by ZERO WIDTH JOINER
        "\U0001F3F3️‍\U0001F308",            # flag with variation selector + ZWJ
        "a​b",                                    # ZERO WIDTH SPACE
        "a‎b‏c",                             # LRM/RLM: marks, no embedding
        "a\u061cb",                                # ARABIC LETTER MARK: like LRM/RLM
    ],
    ids=["zwj-family", "zwj-flag", "zwsp", "lrm-rlm", "alm"],
)
def test_other_format_characters_stay_in_the_subject(kept: str, literal_marker: str) -> None:
    """Grenzfall: only the bidi controls go -- every other format character
    (``Cf``) stays, else emoji sequences joined by U+200D fall apart. The
    single marks U+200E/U+200F and U+061C (Arabic letter mark) open no
    embedding and stay as well."""

    raw = _sender().build_email(_mail(subject=f"x {kept}{literal_marker}")).as_bytes()
    assert _displayed_subjects(raw) == [f"x {kept}{literal_marker}"] * 2


@pytest.mark.parametrize("subject", ["s", "Frage?= ja", "a = b ? c", "Gleich=", "?= ?"])
def test_a_subject_without_an_opening_stays_plain_on_the_wire(subject: str) -> None:
    """Grenzfall: without ``=?`` nothing can be read as an encoded word -- the
    plain ASCII subject goes out unencoded, as before."""

    raw = _sender().build_email(_mail(subject=subject)).as_bytes()
    assert _wire_header(raw, "Subject") == subject
    assert _displayed_subjects(raw) == [subject, subject]


def test_the_message_object_returns_the_literal_subject() -> None:
    """Grenzfall: reading the header from the built message gives the text
    itself, also for the own encoding."""

    subject = "=?utf-8?q?Hallo_Welt?= Rest"
    assert _sender().build_email(_mail(subject=subject))["Subject"] == subject


def test_every_recipient_arrives_in_to() -> None:
    """Positiv: all addresses, also with ``+`` and subdomains, stand in ``To``."""

    to = ["a@example.org", "b.c+d@sub.example.org", "Grossbuchstaben@Example.ORG"]
    raw = _sender().build_email(_mail(to=to)).as_bytes()
    parsed = message_from_bytes(raw, policy=policy.default)
    assert [a.addr_spec for a in parsed["To"].addresses] == to


# --- through a real SMTP session ---------------------------------------------------------


@contextmanager
def _capturing_smtp_server(messages: list[bytes],
                           envelope: list[bytes] | None = None) -> Iterator[int]:
    """A minimal local SMTP server that stores the raw ``DATA`` of each mail.

    :param messages: filled with the received message bytes (dot-unstuffed).
    :param envelope: if given, filled with the argument of each ``MAIL FROM``
        command exactly as it arrives (``<pw@example.org>``, CRLF stripped;
        the verb is case-insensitive, ``smtplib`` writes ``mail FROM:``) --
        the envelope sender on the wire, which no header of the message shows.
    :yields: the port (bound to 127.0.0.1).
    """

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn, conn.makefile("rb") as reader:
            conn.settimeout(5)
            try:
                conn.sendall(b"220 test.example ESMTP\r\n")
                data: list[bytes] | None = None
                for raw in reader:
                    if data is not None:
                        if raw == b".\r\n":
                            messages.append(b"".join(data))
                            data = None
                            conn.sendall(b"250 2.0.0 accepted\r\n")
                        else:
                            data.append(raw[1:] if raw.startswith(b"..") else raw)
                        continue
                    command = raw[:4].upper()
                    if command == b"MAIL" and envelope is not None:
                        envelope.append(raw.rstrip(b"\r\n").partition(b":")[2])
                    if command == b"DATA":
                        data = []
                        conn.sendall(b"354 go ahead\r\n")
                    elif command == b"QUIT":
                        conn.sendall(b"221 bye\r\n")
                        return
                    else:
                        conn.sendall(b"250 ok\r\n")
            except OSError:
                return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        listener.close()
        thread.join(5)


def test_the_message_id_received_over_smtp_is_the_full_id() -> None:
    """Positiv, auf der Leitung: what the SMTP receiver gets is the full id."""

    received: list[bytes] = []
    with _capturing_smtp_server(received) as port:
        SmtpMailSender(host="127.0.0.1", port=port, sender="pw@example.org", use_tls=False,
                       timeout=2, total_timeout=5, ehlo_name="mail.example.org").send(_mail())
    assert len(received) == 1
    wire = _wire_header(received[0], "Message-ID")
    assert wire == smtp_message_id(_KEY, "pw@example.org")
    assert _MSG_ID.fullmatch(wire)


def test_a_subject_that_looks_encoded_arrives_literally_over_smtp() -> None:
    """Positiv, auf der Leitung: the SMTP receiver decodes the literal subject,
    without CR/LF, and finds no extra header."""

    subject = "Störung: =?utf-8?q?a=0D=0ABcc:_evil@x.example?="
    received: list[bytes] = []
    with _capturing_smtp_server(received) as port:
        SmtpMailSender(host="127.0.0.1", port=port, sender="pw@example.org", use_tls=False,
                       timeout=2, total_timeout=5, ehlo_name="mail.example.org").send(
            _mail(subject=subject))
    assert len(received) == 1
    assert _displayed_subjects(received[0]) == [subject, subject]
    assert message_from_bytes(received[0], policy=policy.default)["Bcc"] is None


# --- the envelope sender (MAIL FROM) on the wire -----------------------------------------
#
# ``smtplib.SMTP.send_message`` without ``from_addr`` reads the ``From`` header
# with ``email.utils.getaddresses``; since Python 3.12 that parser returns an
# empty address for a domain in brackets, and the mail went out with the null
# sender ``MAIL FROM:<>`` (the sender of a bounce). The sender therefore names
# the envelope itself.

#: Ordinary senders: the envelope must stay exactly what ``smtplib`` derived
#: itself before (display name with umlaut, quoted name with comma, comment).
_ORDINARY_SENDERS = [
    "pw@example.org",
    "ProcWorks <pw@Mail.Example.org>",
    '"Müller, Max" <max@firma.example>',
    "Jörg Bäcker <jb@firma.example>",
    "pw@example.org (Benachrichtigungen)",
    "<pw@example.org>",
]

#: Address literals with the envelope they must produce.
_LITERAL_SENDERS = [
    ("pw@[192.0.2.10]", "pw@[192.0.2.10]"),
    ("Name <pw@[192.0.2.10]>", "pw@[192.0.2.10]"),
    ("pw@[IPv6:2001:db8::1]", "pw@[IPv6:2001:db8::1]"),
    ("Prüfstelle <pw@[IPv6:2001:db8::1]>", "pw@[IPv6:2001:db8::1]"),
]

#: Senders without a single address that ``smtplib`` still reads an envelope
#: from: they go out exactly as before (the start check warns; some internal
#: relays accept ``<procworks>`` and add their domain).
_SENDERS_AS_BEFORE = [
    ("procworks", "procworks"),
    ("ProcWorks <pw>", "pw"),
    ("Name <<pw@firma.example>>", "Name"),
    ("a@example.org, x@example.org", "a@example.org"),
]

#: Senders for which ``smtplib`` would send the null sender ``MAIL FROM:<>``.
_NULL_SENDERS = ["pw@", "@example.org", "pw@example.org.", "", "   "]

#: Every sender without a single address with local part and domain.
_SENDERS_WITHOUT_ENVELOPE = (
    [s for s, _ in _SENDERS_AS_BEFORE] + _NULL_SENDERS
    + ["pw@example.org\r\nBcc: x@example.org"]
)


def _mail_from_over_smtp(sender: str) -> list[bytes]:
    """The ``MAIL FROM`` arguments a local receiver gets for one notification.

    :param sender: the configured sender of :class:`SmtpMailSender`.
    :returns: the raw arguments (``<address>``; normally exactly one).
    """

    envelope: list[bytes] = []
    with _capturing_smtp_server([], envelope) as port:
        SmtpMailSender(host="127.0.0.1", port=port, sender=sender, use_tls=False,
                       timeout=2, total_timeout=5, ehlo_name="mail.example.org").send(_mail())
    return envelope


def _mail_from_of_plain_smtplib(sender: str) -> list[bytes]:
    """The ``MAIL FROM`` arguments when ``smtplib`` derives the envelope itself.

    Sends the very message :class:`SmtpMailSender` builds, but through
    ``send_message`` **without** ``from_addr`` -- the behaviour before the
    sender named the envelope.

    :param sender: the configured sender.
    :returns: the raw arguments.
    """

    sender_obj = SmtpMailSender(host="127.0.0.1", port=25, sender=sender, use_tls=False)
    email = sender_obj.build_email(_mail())
    envelope: list[bytes] = []
    with _capturing_smtp_server([], envelope) as port:
        with smtplib.SMTP("127.0.0.1", port, local_hostname="mail.example.org",
                          timeout=2) as smtp:
            smtp.send_message(email)
    return envelope


@pytest.mark.parametrize("sender", _ORDINARY_SENDERS)
def test_an_ordinary_sender_keeps_its_envelope(sender: str) -> None:
    """Positiv, auf der Leitung: the envelope is the bare address and the same
    as ``smtplib`` derived it itself -- nothing changes for ordinary senders."""

    wire = _mail_from_over_smtp(sender)
    expected = envelope_sender(sender)
    assert expected is not None and expected.isascii() and " " not in expected
    assert wire == [f"<{expected}>".encode()]
    assert wire == _mail_from_of_plain_smtplib(sender)


@pytest.mark.parametrize(("sender", "address"), _LITERAL_SENDERS)
def test_an_address_literal_is_the_envelope_sender(sender: str, address: str) -> None:
    """Positiv, auf der Leitung (Grenzfall Adressliteral IPv4/IPv6, auch mit
    Anzeigename und Umlaut): ``MAIL FROM`` carries the literal, never the
    null sender."""

    assert envelope_sender(sender) == address
    assert _mail_from_over_smtp(sender) == [f"<{address}>".encode()]


@pytest.mark.parametrize(("sender", "address"), _LITERAL_SENDERS)
def test_plain_smtplib_sends_an_address_literal_as_the_null_sender(
        sender: str, address: str) -> None:
    """Ursache, auf der Leitung: without an explicit envelope ``smtplib`` sends
    ``MAIL FROM:<>`` for an address literal. Skipped once a Python version
    reads the literal itself (then this cause is gone; the explicit envelope
    stays correct)."""

    wire = _mail_from_of_plain_smtplib(sender)
    if wire == [f"<{address}>".encode()]:
        pytest.skip("this Python derives the address literal itself")
    assert wire == [b"<>"]


@pytest.mark.parametrize(("sender", "address"), _SENDERS_AS_BEFORE)
def test_a_sender_without_a_single_address_goes_out_as_before(
        sender: str, address: str) -> None:
    """Grenzfall, auf der Leitung: no single address, but ``smtplib`` reads an
    envelope -- the mail goes out with exactly that envelope, as before (no
    stop of the mail traffic after an update; the start check warns)."""

    assert envelope_sender(sender) is None
    wire = _mail_from_over_smtp(sender)
    assert wire == [f"<{address}>".encode()]
    assert wire == _mail_from_of_plain_smtplib(sender)


@pytest.mark.parametrize("sender", _NULL_SENDERS)
def test_plain_smtplib_sends_these_senders_as_the_null_sender(sender: str) -> None:
    """Ursache, auf der Leitung: for these values ``smtplib`` itself sends
    ``MAIL FROM:<>`` -- the only case the sender refuses."""

    assert _mail_from_of_plain_smtplib(sender) == [b"<>"]


@pytest.mark.parametrize("sender", _NULL_SENDERS)
def test_a_sender_that_would_be_the_null_sender_is_never_sent(sender: str) -> None:
    """Negativ (der Grund): neither the sender nor ``smtplib`` finds an
    address -- the send fails with the reason before any connection; nothing
    reaches the receiver, least of all ``MAIL FROM:<>``. The reason never
    contains the value."""

    assert envelope_sender(sender) is None
    envelope: list[bytes] = []
    messages: list[bytes] = []
    with _capturing_smtp_server(messages, envelope) as port:
        with pytest.raises(ValueError) as exc:
            SmtpMailSender(host="127.0.0.1", port=port, sender=sender, use_tls=False,
                           timeout=2, total_timeout=5,
                           ehlo_name="mail.example.org").send(_mail())
    assert str(exc.value) == NO_ENVELOPE_SENDER
    assert "MAIL FROM:<>" in str(exc.value) and "local-part@domain" in str(exc.value)
    assert envelope == [] and messages == []
    if sender.strip():
        assert sender.strip() not in str(exc.value)


@pytest.mark.parametrize("sender", _SENDERS_WITHOUT_ENVELOPE)
def test_the_start_check_warns_about_every_sender_without_an_envelope(sender: str) -> None:
    """Negativ (der Grund): every value the sender refuses to send is named by
    the start check as *no single address* (or as not set)."""

    problem = mail_from_problem(sender)
    assert problem is not None
    assert ("no single e-mail address" in problem) or (
        not sender.strip() and "is not set" in problem)


@pytest.mark.parametrize("sender", [s for s, _ in _LITERAL_SENDERS] + _ORDINARY_SENDERS)
def test_a_sender_the_start_check_accepts_always_has_an_envelope(sender: str) -> None:
    """Grenzfall: what the start check accepts is always sendable."""

    assert mail_from_problem(sender) is None
    assert envelope_sender(sender) is not None


def test_a_null_sender_fails_in_the_outbox_with_the_reason() -> None:
    """Negativ (der Grund), Outbox: the attempt counts as failed (retried later),
    the entry carries the reason, never the value and never a success."""

    store = InMemoryMailOutboxStore()
    dispatcher = MailOutboxDispatcher(store, now=lambda: 100.0)
    entry = dispatcher.enqueue(_mail(), _KEY)
    assert entry is not None
    sender = SmtpMailSender(host="127.0.0.1", port=9, sender="pw@",
                            use_tls=False, timeout=1, total_timeout=2)
    results = dispatcher.dispatch_pending(sender)
    assert len(results) == 1 and not results[0].delivered and not results[0].dead
    stored = store.get_entry(entry.id)
    assert stored.state is MailOutboxState.FAILED
    assert stored.last_error == NO_ENVELOPE_SENDER
