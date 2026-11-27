# SPDX-License-Identifier: BUSL-1.1
"""The SMTP greeting never waits for a name lookup of this host.

Without an explicit name ``smtplib`` calls ``socket.getfqdn()`` for its
``EHLO``/``HELO`` while the connection object is built. That is a blocking
lookup at the system resolver which no socket timeout and no total deadline
can cut off: on a host with a slow resolver every send waited 30 s before the
first byte -- the mail tests failed their time windows, and in operation a
session could hang past its total limit. It also put the internal host name
into the greeting.

Now the greeting carries the configured ``PROCWORKS_SMTP_EHLO_NAME`` or, by
default, the address literal of the local end of the connection (RFC 5321,
2.3.5 / 4.1.4: the literal for a client without a meaningful name).

Only local sockets are used; any lookup of this host's name fails the test.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from procworks import mail_runtime
from procworks.mail_runtime import (
    MailMessage,
    SmtpMailSender,
    address_literal,
    create_mail_sender,
    parse_ehlo_name,
)


def _forbidden_lookup(*_args: object, **_kwargs: object) -> str:
    raise AssertionError("the SMTP sender looked up the name of this host")


@pytest.fixture
def no_host_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every way ``smtplib`` finds the local name fails loudly."""

    monkeypatch.setattr(socket, "getfqdn", _forbidden_lookup)
    monkeypatch.setattr(socket, "gethostname", _forbidden_lookup)
    monkeypatch.setattr(socket, "gethostbyname", _forbidden_lookup)
    monkeypatch.setattr(socket, "gethostbyaddr", _forbidden_lookup)


@contextmanager
def _recording_smtp_server(greetings: list[str]) -> Iterator[int]:
    """A minimal SMTP server for one mail that records each ``EHLO``/``HELO`` argument.

    :param greetings: filled with the argument of every greeting received.
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
                in_data = False
                for raw in reader:
                    line = raw.rstrip(b"\r\n")
                    if in_data:
                        if line == b".":
                            in_data = False
                            conn.sendall(b"250 2.0.0 accepted\r\n")
                        continue
                    command = line[:4].upper()
                    if command in (b"EHLO", b"HELO"):
                        greetings.append(line[5:].decode("ascii"))
                        conn.sendall(b"250 test.example\r\n")
                    elif command == b"DATA":
                        in_data = True
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


def _mail() -> MailMessage:
    return MailMessage(to=["a@example.org"], subject="s", body="b",
                       instance_id="i1", node_id="n1", schema_id="s1")


def _send(sender: SmtpMailSender) -> None:
    sender.send(_mail())


# --- the greeting on the wire ---------------------------------------------------------------


def test_a_send_never_looks_up_the_host_name(no_host_lookup: None) -> None:
    """Wächter: with every local name lookup failing, the mail still goes out.

    The default greeting is the literal of the local address (127.0.0.1 here),
    not a host name.
    """

    greetings: list[str] = []
    with _recording_smtp_server(greetings) as port:
        _send(SmtpMailSender(host="127.0.0.1", port=port, sender="pw@example.org",
                             use_tls=False, timeout=2, total_timeout=5))
    assert greetings == ["[127.0.0.1]"]


def test_the_configured_name_is_the_greeting(
    no_host_lookup: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    greetings: list[str] = []
    with _recording_smtp_server(greetings) as port:
        monkeypatch.setenv("PROCWORKS_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("PROCWORKS_SMTP_PORT", str(port))
        monkeypatch.setenv("PROCWORKS_MAIL_FROM", "pw@example.org")
        monkeypatch.setenv("PROCWORKS_SMTP_TLS", "0")
        monkeypatch.setenv("PROCWORKS_SMTP_EHLO_NAME", " mail.example.org ")
        sender = create_mail_sender()
        assert isinstance(sender, SmtpMailSender)
        _send(sender)
    assert greetings == ["mail.example.org"]


@pytest.mark.parametrize("unset", ["", "   "])
def test_an_empty_setting_means_the_address_literal(
    no_host_lookup: None, monkeypatch: pytest.MonkeyPatch, unset: str
) -> None:
    """Grenzfall: Compose passes an unset variable as the empty string."""

    greetings: list[str] = []
    with _recording_smtp_server(greetings) as port:
        monkeypatch.setenv("PROCWORKS_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("PROCWORKS_SMTP_PORT", str(port))
        monkeypatch.setenv("PROCWORKS_MAIL_FROM", "pw@example.org")
        monkeypatch.setenv("PROCWORKS_SMTP_TLS", "0")
        monkeypatch.setenv("PROCWORKS_SMTP_EHLO_NAME", unset)
        sender = create_mail_sender()
        assert isinstance(sender, SmtpMailSender)
        _send(sender)
    assert greetings == ["[127.0.0.1]"]


def test_a_wrong_setting_stops_the_start_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negativ: the start aborts naming the variable and the expected forms."""

    monkeypatch.setenv("PROCWORKS_SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("PROCWORKS_MAIL_FROM", "pw@example.org")
    monkeypatch.setenv("PROCWORKS_SMTP_EHLO_NAME", "procworks")
    with pytest.raises(ValueError, match=r"Invalid PROCWORKS_SMTP_EHLO_NAME='procworks'.*"
                                         r"fully qualified domain name.*address literal"):
        create_mail_sender()


def test_without_smtp_the_setting_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grenzfall: no mail server, no greeting -- the start is not blocked."""

    monkeypatch.delenv("PROCWORKS_SMTP_HOST", raising=False)
    monkeypatch.setenv("PROCWORKS_SMTP_EHLO_NAME", "procworks")
    assert isinstance(create_mail_sender(), mail_runtime.NullMailSender)


# --- which names are accepted ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mail.example.org", "mail.example.org"),
        ("Mail-1.Example.ORG", "Mail-1.Example.ORG"),
        ("a.b", "a.b"),
        ("[192.0.2.10]", "[192.0.2.10]"),
        ("[IPv6:2001:DB8:0:0::1]", "[IPv6:2001:db8::1]"),
        ("[ipv6:::1]", "[IPv6:::1]"),
        (("a" * 63) + ".example.org", ("a" * 63) + ".example.org"),
    ],
)
def test_valid_greeting_names(raw: str, expected: str) -> None:
    assert parse_ehlo_name(raw) == expected


def test_a_domain_of_exactly_255_characters_is_accepted() -> None:
    """Grenzfall: the RFC 5321 upper bound itself; one character more is refused
    although every label stays within 63 characters."""

    name = ".".join(["c" * 63, "d" * 63, "e" * 63, "f" * 59, "org"])
    assert len(name) == 255
    assert parse_ehlo_name(name) == name
    longer = ".".join(["c" * 63, "d" * 63, "e" * 63, "f" * 60, "org"])
    assert len(longer) == 256
    with pytest.raises(ValueError, match="fully qualified domain name"):
        parse_ehlo_name(longer)


@pytest.mark.parametrize(
    "raw",
    [
        "",                              # nothing
        "procworks",                     # single label: not fully qualified
        "mail.example.org.",             # trailing dot (empty label)
        "-mail.example.org",             # leading hyphen
        "mail-.example.org",             # trailing hyphen
        "mail_relay.example.org",        # underscore
        "mail example.org",              # blank
        "mail.example.org\r\nRSET",      # would inject a command
        ("a" * 64) + ".example.org",     # label over 63 characters
        "[999.1.1.1]",                   # no IPv4 address
        "[192.0.2.010]",                 # ambiguous leading zero
        "[IPv6:192.0.2.1]",              # IPv4 marked as IPv6
        "[2001:db8::1]",                 # IPv6 without its tag
        "[IPv6:2001:db8::1",             # unclosed literal
        "[]",                            # empty literal
    ],
)
def test_invalid_greeting_names_are_rejected_with_the_reason(raw: str) -> None:
    with pytest.raises(ValueError, match=r"Invalid PROCWORKS_SMTP_EHLO_NAME=.*expected the "
                                         r"fully qualified domain name of this host"):
        parse_ehlo_name(raw)


@pytest.mark.parametrize(
    ("raw", "literal"),
    [
        ("[IPv6:fe80::1%en0]", "[IPv6:fe80::1]"),        # a zone means nothing to the server
        ("[IPv6:fe80::1%a b]", "[IPv6:fe80::1]"),        # ... and would put a blank in the command
        ("[IPv6:fe80::1%x\r\nRSET]", "[IPv6:fe80::1]"),  # ... or a second command
        ("[IPv6:fe80::1%a]x]", "[IPv6:fe80::1]"),        # ... or a closing bracket inside
        ("[IPv6:fe80::1%]", "[IPv6:fe80::1]"),           # empty zone
        ("[ipv6:FE80:0::1%en0]", "[IPv6:fe80::1]"),      # hint in canonical spelling
        ("[192.0.2.10%eth0]", "[192.0.2.10]"),           # IPv4 knows no zone either
    ],
)
def test_an_ipv6_literal_with_a_zone_is_rejected(raw: str, literal: str) -> None:
    """Negativ (der Grund): the zone is named; the literal without it is the hint.

    The IPv6 parser accepts a zone after ``%`` with any characters and keeps
    it in the canonical spelling -- the start succeeded, and every single
    mail failed later on the command line. The message ends with exactly the
    literal to write, the way the hint for ``192.0.2.10`` does.
    """

    with pytest.raises(ValueError) as caught:
        parse_ehlo_name(raw)
    message = str(caught.value)
    assert message.startswith(f"Invalid PROCWORKS_SMTP_EHLO_NAME={raw!r}")
    assert message.endswith(f"an address literal carries no zone -- write {literal}.")


@pytest.mark.parametrize(
    "raw",
    [
        "[IPv6:fe80::zz%en0]",           # no address before the zone
        "[IPv6:%en0]",                   # nothing before the zone
        "[IPv6:192.0.2.1%en0]",          # IPv4 under the IPv6 tag
        "[999.1.1.1%eth0]",              # no IPv4 address
    ],
)
def test_a_zone_after_an_invalid_address_gets_the_general_hint(raw: str) -> None:
    """Grenzfall: without a valid address before ``%`` there is no literal to
    suggest -- the message only says to drop the zone and never names a
    made-up literal."""

    with pytest.raises(ValueError) as caught:
        parse_ehlo_name(raw)
    message = str(caught.value)
    assert message.endswith(
        "an address literal carries no zone -- write it without the part from '%' on."
    )
    assert "write [" not in message


def test_the_zone_hint_carries_no_control_character_of_the_value() -> None:
    """Negativ: the suggested literal comes from the parsed address only -- CR/LF
    of the zone never reach the message outside the ``repr`` of the value."""

    raw = "[IPv6:fe80::1%x\r\nRSET]"
    with pytest.raises(ValueError) as caught:
        parse_ehlo_name(raw)
    message = str(caught.value)
    assert "\r" not in message and "\n" not in message
    hint = message.split(" carries no zone -- ", 1)[1]
    assert hint == "write [IPv6:fe80::1]."


def test_the_ipv6_literal_without_its_zone_is_accepted() -> None:
    """Grenzfall: the same address without the zone is a valid literal."""

    assert parse_ehlo_name("[IPv6:fe80::1]") == "[IPv6:fe80::1]"


def test_a_zone_in_the_setting_stops_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negativ: with a zone in the setting the start aborts, not every mail."""

    monkeypatch.setenv("PROCWORKS_SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("PROCWORKS_MAIL_FROM", "pw@example.org")
    monkeypatch.setenv("PROCWORKS_SMTP_EHLO_NAME", "[IPv6:fe80::1%x\nRSET]")
    with pytest.raises(ValueError, match="carries no zone"):
        create_mail_sender()


@pytest.mark.parametrize(
    ("raw", "hint"),
    [
        ("192.0.2.10", r"write the address literal \[192\.0\.2\.10\]"),
        ("10.0.0.1", r"write the address literal \[10\.0\.0\.1\]"),
        ("1.2", r"the last label must not be all digits"),
        ("mail.123", r"the last label must not be all digits"),
        ("999.1.1.1", r"the last label must not be all digits"),
    ],
)
def test_a_numeric_name_is_rejected_with_the_literal_as_hint(raw: str, hint: str) -> None:
    """Negativ (der Grund): a name whose top-level label is all digits is no
    host name (RFC 1123 2.1, RFC 3696 2) -- meant was almost surely the
    address literal, and the message says so."""

    with pytest.raises(ValueError, match=r"Invalid PROCWORKS_SMTP_EHLO_NAME=.*" + hint):
        parse_ehlo_name(raw)


@pytest.mark.parametrize("raw", ["123.example.org", "1.example.org", "mail.example.c0m",
                                 "mail.example.x1"])
def test_digits_below_the_top_level_are_still_a_name(raw: str) -> None:
    """Grenzfall: only an all-digit top-level label is refused; digit labels
    below it and a top level mixing letters and digits stay valid."""

    assert parse_ehlo_name(raw) == raw


def test_the_sender_rejects_a_wrong_name_when_built() -> None:
    """Negativ: a wrong name can not even be put into a sender."""

    with pytest.raises(ValueError, match="PROCWORKS_SMTP_EHLO_NAME"):
        SmtpMailSender(host="h", port=25, sender="s@example.org", ehlo_name="bad name")


@pytest.mark.parametrize(
    ("address", "literal"),
    [
        ("192.0.2.10", "[192.0.2.10]"),
        ("2001:db8::1", "[IPv6:2001:db8::1]"),
        ("fe80::1%en0", "[IPv6:fe80::1]"),
        ("::ffff:192.0.2.10", "[192.0.2.10]"),
        ("kein-ip", "[127.0.0.1]"),
    ],
)
def test_the_address_literal_of_the_local_end(address: str, literal: str) -> None:
    assert address_literal(address) == literal
