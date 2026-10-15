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

import hashlib
import ipaddress
import logging
import os
import re
import smtplib
import socket
import time
import unicodedata
import uuid
from collections.abc import Buffer, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from email.header import Header
from email.message import EmailMessage
from email.policy import Policy
from email.utils import getaddresses, parseaddr
from typing import Any, Protocol

from pydantic import BaseModel

from procworks import unit_of_work
from procworks.assignment import eligible_agents
from procworks.deadline import Deadline
from procworks.model import (
    DUE_MAIL_OUTBOX_STATES,
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
    """Strip CR/LF so a value can never inject an extra e-mail header."""

    return value.replace("\r", " ").replace("\n", " ")


#: The bidirectional embedding/override (U+202A-U+202E) and isolate
#: (U+2066-U+2069) controls. With one of them a value from instance data
#: reverses the displayed order of the rest of the subject -- a file
#: extension or a name then appears as something it is not. They are format
#: characters (``Cf``) like U+200D, which must stay (it joins emoji
#: sequences), so they are listed one by one instead of by category.
_BIDI_CONTROLS = frozenset(
    chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))
)


def _subject_text(value: str) -> str:
    """The subject as the receiver shall display it: no control character.

    Every control character (Unicode category ``Cc``: CR, LF, TAB, NUL, ESC,
    NEL ...), the Unicode line and paragraph separators (``Zl``/``Zp``) and
    the bidi controls (:data:`_BIDI_CONTROLS`) become a blank, one blank each
    -- so ``"a\\r\\nb"`` keeps its two blanks as before. Nothing else
    changes (umlauts, ``:``, ``|``, quotes, ``=?``); in particular every other
    format character stays: U+200D (zero width joiner) and the variation
    selectors build emoji sequences, U+200E/U+200F (LRM/RLM) and U+061C
    (Arabic letter mark, ALM) are single direction marks that open no
    embedding -- they act like a strongly directed letter and cannot reverse
    the order of what follows, so they stay.

    Because :class:`_LiteralSubject` keeps the text from being decoded again,
    this is also what the receiver shows after decoding: no CR/LF can come
    back in through an encoded word in the model or instance data.

    :param value: the rendered subject (from model and instance data).
    :returns: ``value`` with each such character replaced by ``" "``; an
        empty string stays empty.
    """

    return "".join(
        " " if ch in _BIDI_CONTROLS or unicodedata.category(ch) in ("Cc", "Zl", "Zp")
        else ch
        for ch in value
    )


class _LiteralSubject(str):
    """A ``Subject`` header that arrives exactly as its text, never decoded.

    ``EmailMessage`` parses a header value it is given and decodes every
    encoded word in it (RFC 2047, ``=?utf-8?q?...?=``) -- it has no literal
    mode. A subject from model or instance data that merely *looks* like an
    encoded word therefore lost its text, and one with ``=0D=0A`` inside put
    CR/LF into the subject the receiver displays (no extra header line on the
    wire, but the "no line break in the subject" promise was gone).

    An instance of this class is a header object in the sense of
    :mod:`email.policy`: it has a ``name`` and a ``fold`` method, so
    ``EmailMessage`` stores it unparsed and the generator calls :meth:`fold`
    for the wire form. Reading ``email["Subject"]`` returns it; as a ``str``
    it equals the literal text.

    Used only for a subject containing ``=?`` (see
    :meth:`SmtpMailSender.build_email`); without ``=?`` no decoder sees an
    encoded word, and the library's own encoding stays in place.
    """

    name = "Subject"

    def fold(self, *, policy: Policy) -> str:
        """The complete header line(s) for the wire.

        The whole text is encoded as UTF-8 encoded words (RFC 2047), so a
        ``=?`` of the text sits inside the encoding and every decoder --
        RFC 2047 ignores the folding blank between two encoded words --
        restores exactly the text. Lines are folded to at most 78
        characters, a multi-byte character is never split.

        The encoding is chosen per encoded word by :class:`email.header.Header`
        (charset ``utf-8`` encodes "shortest"): ``Q`` (quoted-printable) or
        ``B`` (Base64), whichever is shorter -- plain ASCII text usually
        becomes ``Q``, text with umlauts usually ``B``, and one subject can
        mix both over its lines. Decoders read either form alike.

        :param policy: the generator's policy; only ``linesep`` is used
            (``"\\r\\n"`` for SMTP, ``"\\n"`` for ``as_bytes()``).
        :returns: ``"Subject: =?utf-8?q?...?="`` or ``=?utf-8?b?...?=`` plus
            ``linesep``, continuation lines starting with a blank.
        """

        encoded = Header(str(self), "utf-8", header_name=self.name).encode(
            linesep=policy.linesep
        )
        return f"{self.name}: {encoded}{policy.linesep}"


#: How long a send is reserved for one dispatcher. An SMTP session (connect,
#: greeting, login, transfer) runs several operations with their own timeout,
#: so the lease is generous: a send still in progress is never taken over by a
#: dispatch in another request or API process.
_SEND_LEASE_SECONDS = 300.0

#: Total time limit of one SMTP session (connect, greeting, STARTTLS, login,
#: transfer together), well below :data:`_SEND_LEASE_SECONDS`. Each step has
#: its own socket timeout as well, but a server that answers drop by drop
#: keeps every single read short -- without a total limit the session outlived
#: the reservation, and another dispatcher sent the same mail again.
SMTP_TOTAL_SECONDS = 120.0

#: Time limit for the server's answer after the message itself (the reply to
#: the terminating ``.``), within :data:`SMTP_TOTAL_SECONDS`. Every other step
#: has the short step limit (10 s), but many mail servers check the content
#: (virus, spam) before they confirm -- with 10 s such a send failed and was
#: repeated although the server would have accepted it.
SMTP_DATA_REPLY_SECONDS = 60.0

#: The bytes that end the message in the SMTP ``DATA`` phase (``smtplib``
#: sends the dot-quoted message with it in one ``send``; no other command
#: ends this way).
_DATA_TERMINATOR = b"\r\n.\r\n"

#: Environment variable with the name this host gives in the SMTP greeting
#: (``EHLO``/``HELO``), see :func:`parse_ehlo_name`.
SMTP_EHLO_NAME_ENV = "PROCWORKS_SMTP_EHLO_NAME"

#: One label of a domain name in the greeting (RFC 5321 ``sub-domain``:
#: letter or digit, then letters, digits and hyphens, not ending in a hyphen),
#: at most 63 characters (RFC 1035).
_EHLO_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")

#: Upper bound of a domain in the greeting (RFC 5321, 4.5.3.1.2).
_EHLO_DOMAIN_MAX = 255


def parse_ehlo_name(raw: str) -> str:
    """Check the name for the SMTP greeting and return it in canonical form.

    RFC 5321 (2.3.5, 4.1.1.1) allows exactly two forms after ``EHLO``: the
    fully qualified domain name of the sending host, or -- if it has no
    meaningful name -- an address literal. Both are accepted here and nothing
    else, so no value can break the command line (CR/LF, blanks) or claim a
    form the server must refuse.

    :param raw: the configured value; surrounding blanks are ignored.
    :returns: a domain (``mail.example.org``, at least two labels, case kept)
        or an address literal (``[192.0.2.10]``, ``[IPv6:2001:db8::1]``,
        the address in canonical spelling).
    :raises ValueError: for anything else -- an empty value, a single label
        (``procworks``: not fully qualified), a label with a leading or
        trailing hyphen or over 63 characters, a domain over 255 characters,
        an underscore, a trailing dot, a malformed or mismatched literal
        (``[IPv6:192.0.2.1]``). The message names the value, so a start with a
        wrong setting aborts with a hint the operator can act on. Two cases
        get their own hint:

        * an IPv6 literal with a zone (``[IPv6:fe80::1%en0]``): RFC 5321 has
          no zone in a literal, and :class:`ipaddress.IPv6Address` accepts
          *any* characters after ``%`` (blanks, ``]``, CR/LF) and keeps them
          in the canonical spelling -- the start succeeded and every mail
          failed on the command line. Any ``%`` is refused, also an empty
          zone; the hint names the literal without it in canonical
          spelling (``[IPv6:fe80::1]``) when the part before ``%`` is a
          valid address, otherwise it says to drop the part from ``%`` on.
        * a name whose last label is all digits (``192.0.2.10``, ``1.2``):
          no host name (RFC 1123 2.1, RFC 3696 2: a top-level label is never
          all-numeric), meant was almost surely the address literal; for a
          dotted IPv4 address the hint spells it (``[192.0.2.10]``). Digit
          labels below the top level (``123.example.org``) stay valid.
    """

    value = raw.strip()
    problem = (
        f"Invalid {SMTP_EHLO_NAME_ENV}={raw!r}: expected the fully qualified "
        "domain name of this host (e.g. mail.example.org) or an address "
        "literal (e.g. [192.0.2.10] or [IPv6:2001:db8::1])."
    )
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1]
        if "%" in inner:
            raise ValueError(_zone_hint(problem, inner))
        literal = _address_literal(inner)
        if literal is None:
            raise ValueError(problem)
        return literal
    labels = value.split(".")
    if (
        len(value) > _EHLO_DOMAIN_MAX
        or len(labels) < 2
        or not all(_EHLO_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError(problem)
    if labels[-1].isdigit():
        if _is_ipv4(value):
            raise ValueError(
                f"{problem[:-1]}; {value} is an IP address, not a name -- write "
                f"the address literal [{value}]."
            )
        raise ValueError(
            f"{problem[:-1]}; the last label must not be all digits (no host "
            "name), for an IP address write the address literal."
        )
    return value


def _address_literal(inner: str) -> str | None:
    """The address literal for the text between the brackets, or ``None``.

    :param inner: what stands between ``[`` and ``]`` -- ``IPv6:<address>``
        (prefix in any case) or a dotted IPv4 address.
    :returns: the literal in canonical spelling (``[IPv6:2001:db8::1]``,
        ``[192.0.2.10]``), or ``None`` if ``inner`` is no valid address of the
        form it claims (``IPv6:192.0.2.1``, ``999.1.1.1``, empty text).
        Built only from the parsed address, never from ``inner`` itself, so
        the result never carries a character of the raw value. A zone
        (``%``) is the caller's business: the IPv6 parser would accept and
        keep it.
    """

    try:
        if inner[:5].lower() == "ipv6:":
            return f"[IPv6:{ipaddress.IPv6Address(inner[5:]).compressed}]"
        return f"[{ipaddress.IPv4Address(inner)}]"
    except ValueError:
        return None


def _zone_hint(problem: str, inner: str) -> str:
    """The message for an address literal with a zone (``%``).

    :param problem: the general message of :func:`parse_ehlo_name` (ends in
        ``.``).
    :param inner: the text between the brackets; it contains ``%``.
    :returns: the general message plus the reason. If the part before the
        first ``%`` is a valid address, the hint spells the literal without
        the zone (``write [IPv6:fe80::1]``) -- canonical, from the parsed
        address only, so control characters or blanks of the zone never reach
        the message. Otherwise (``[IPv6:nonsense%en0]``) it only says to drop
        the part from ``%`` on; the raw value is already named (``repr``) in
        ``problem``.
    """

    base = f"{problem[:-1]}; an address literal carries no zone -- "
    literal = _address_literal(inner.split("%", 1)[0])
    if literal is None:
        return base + "write it without the part from '%' on."
    return base + f"write {literal}."


def _is_ipv4(text: str) -> bool:
    """Whether ``text`` is an IPv4 address in dotted form (``192.0.2.10``).

    :param text: a candidate already known to consist of valid labels.
    :returns: ``True`` exactly when :class:`ipaddress.IPv4Address` accepts it
        (four decimal parts 0-255, no leading zero); ``False`` otherwise.
    """

    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


#: Right-hand side of a ``Message-ID`` when the sender address has no usable
#: domain. ``.invalid`` is reserved (RFC 2606) and can never name a real host.
_MESSAGE_ID_FALLBACK_DOMAIN = "procworks.invalid"

#: Hex digits of the key digest on the left of a ``Message-ID`` (128 bits:
#: no two activations collide in practice, the header stays on one line).
_MESSAGE_ID_DIGEST_HEX = 32


def smtp_message_id(key: str, sender: str) -> str:
    """The ``Message-ID`` header value for the outbox key ``key``.

    The key (:func:`activation_dedup_key`, ``instance|node|ISO instant``) is
    no valid ``msg-id``: ``EmailMessage`` parses the header and silently drops
    everything from the first ``:`` of the timestamp on, together with the
    domain -- two activations of a step within one hour then carried the same
    id, and a receiver that drops duplicates by ``Message-ID`` could drop the
    second notification of a loop. The id is therefore built only from
    characters every parser keeps (RFC 5322 ``dot-atom-text`` on both sides).

    :param key: the idempotency key of one activation; any text (also empty,
        non-ASCII or with CR/LF -- it is only hashed, never copied).
    :param sender: the configured sender (``PROCWORKS_MAIL_FROM``, with or
        without a display name); its domain becomes the right-hand side.
    :returns: ``<32 hex digits of SHA-256(key)@domain>``. The same key and
        sender always give the same id (a repeated delivery of one activation
        is recognisable as a duplicate), different keys different ids (a loop
        re-activation is a new mail). The domain is lowercased; without a
        usable domain in ``sender`` (none, an address literal, a label that
        is no host name) it is ``procworks.invalid``.
    """

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_MESSAGE_ID_DIGEST_HEX]
    _local, at, domain = parseaddr(sender)[1].rpartition("@")
    domain = domain.lower()
    if (
        not at
        or len(domain) > _EHLO_DOMAIN_MAX
        or not all(_EHLO_LABEL.fullmatch(label) for label in domain.split("."))
    ):
        domain = _MESSAGE_ID_FALLBACK_DOMAIN
    return f"<{digest}@{domain}>"


def _read_from_header(value: str) -> Any | None:
    """The ``From`` header the standard parser makes of ``value``, or ``None``.

    The single boundary to the header parser for the sender. The parser does
    not only refuse input with ``ValueError`` (a line break): for an address
    literal without its closing bracket (``pw@[192.0.2.10``, ``Name
    <pw@[``) it fails inside itself with ``AttributeError`` (Python 3.12),
    for other broken input with ``IndexError`` -- and the set of types is no
    documented contract. Catching a list of types once let such a value abort
    the start (the start check must only warn). **Every** exception of the
    parser therefore means "no readable address"; nothing else runs inside
    the ``try``, so no error of ProcWorks' own is hidden.

    :param value: the stripped, non-empty configured sender.
    :returns: the parsed header (``.addresses``, ``.defects``), or ``None``
        when the parser raises anything at all while setting or reading it.
    """

    probe = EmailMessage()
    try:
        probe["From"] = value
        header = probe["From"]
        # Touch both lazily computed attributes inside the guard.
        header.addresses  # noqa: B018 -- evaluated for its exceptions only
        header.defects  # noqa: B018 -- evaluated for its exceptions only
    except Exception:  # noqa: BLE001 -- parser failures of any type mean "no address"
        return None
    return header


def envelope_sender(raw: str | None) -> str | None:
    """The envelope sender (``MAIL FROM:<...>``) for the sender ``raw``.

    Without an explicit envelope sender ``smtplib.SMTP.send_message`` takes
    the ``From`` header and reads it with ``email.utils.getaddresses`` --
    since Python 3.12 a strict parser that returns ``('', '')`` for a domain
    in brackets. ``pw@[192.0.2.10]`` (a valid address literal, accepted by
    :func:`mail_from_problem`) then went out as ``MAIL FROM:<>``: the null
    sender of a bounce, which servers treat differently and which can never
    receive a reply. The envelope is therefore derived here, from the same
    parse the ``From`` header gets (an :class:`~email.message.EmailMessage`,
    default policy), and passed to ``send_message`` explicitly.

    :param raw: the configured sender, with or without a display name
        (``pw@example.org``, ``ProcWorks <pw@example.org>``,
        ``"Müller, Max" <max@firma.example>``, ``pw@[IPv6:2001:db8::1]``).
    :returns: the ``addr-spec`` of the one address in ``raw`` -- for every
        ordinary address the same text ``smtplib`` derived itself before;
        for an address literal the literal in brackets. ``None`` when no
        envelope can be named: nothing set, a header the parser refuses or
        fails on (line break; an address literal without its closing
        bracket, :func:`_read_from_header`), no or several addresses, or an
        address without local
        part or domain (``procworks``, ``pw@``, ``Name <<pw@x.example>>``).
        Parser defects alone do not give ``None``: the address that the
        ``From`` header carries is what the envelope carries too (the form
        itself is :func:`mail_from_problem`'s business, which warns for every
        value this function rejects). ``None`` does not mean "not sent":
        :meth:`SmtpMailSender.send` then lets ``smtplib`` derive the envelope
        as it always did (``procworks`` goes out as ``MAIL FROM:<procworks>``,
        some internal relays accept it) and refuses only where that would be
        the null sender (:func:`_smtplib_envelope`).
    """

    value = (raw or "").strip()
    if not value:
        return None
    header = _read_from_header(value)
    if header is None:
        return None
    addresses = header.addresses
    if len(addresses) != 1 or not addresses[0].username or not addresses[0].domain:
        return None
    # ``Address`` is untyped in the stub; ``addr_spec`` is always a ``str``.
    return str(addresses[0].addr_spec)


def _smtplib_envelope(email: EmailMessage) -> str:
    """The envelope sender ``smtplib.SMTP.send_message`` derives for ``email``.

    Exactly ``send_message``'s own rule for ``from_addr=None``: the first
    ``addr-spec`` that ``email.utils.getaddresses`` reads from the ``Sender``
    header, or from ``From`` when there is no ``Sender`` (a ``Resent-`` block
    never occurs, :meth:`SmtpMailSender.build_email` sets none). Used only to
    predict the null sender before connecting, never to change the envelope.

    :param email: the message as :meth:`SmtpMailSender.build_email` built it.
    :returns: the address ``smtplib`` would send in ``MAIL FROM``; ``""``
        means it would send ``MAIL FROM:<>`` (also for an address literal or
        a missing ``From``).
    """

    header = email["Sender"] if "Sender" in email else email["From"]
    if header is None:
        return ""
    found = getaddresses([str(header)])
    return found[0][1] if found else ""


#: Why a mail is not sent when no envelope sender can be named and ``smtplib``
#: would send the null sender. Never contains the value (it can name a
#: person); ends up as the outbox error.
NO_ENVELOPE_SENDER = (
    "PROCWORKS_MAIL_FROM contains no single e-mail address of the form "
    "local-part@domain; the mail is not sent with an empty envelope sender "
    "(MAIL FROM:<>)"
)

#: Why a mail is not built when the header parser fails on the configured
#: sender (an address literal without its closing bracket, ``pw@[192.0.2.10``):
#: no ``From:`` can be written, and ``smtplib`` would read no address from it
#: either. Never contains the value; ends up as the outbox error.
UNREADABLE_SENDER = (
    "PROCWORKS_MAIL_FROM cannot be read as an e-mail address (no single "
    "e-mail address of the form local-part@domain); the mail is not sent"
)

#: Why a mail is not built when the header parser fails on a recipient
#: address. Never contains the address; ends up as the outbox error.
UNREADABLE_RECIPIENT = (
    "a recipient address cannot be read as an e-mail address; the mail is not sent"
)


def _set_address_header(email: EmailMessage, name: str, value: str, reason: str) -> None:
    """Set the address header ``name`` of ``email`` to ``value``.

    The header parser of the standard library raises ``ValueError`` for a
    value it refuses (a line break) but fails inside itself with other types
    for some broken input (``AttributeError`` for an address literal without
    its closing bracket). A send attempt must end in a reason the outbox can
    show, not in the parser's internal error text, so those are turned into
    ``ValueError(reason)``.

    :param email: the message being built.
    :param name: the header name (``From``, ``To``).
    :param value: the header value as configured or modelled.
    :param reason: the message of the ``ValueError`` for a parser failure;
        must not contain the value (it can name a person).
    :raises ValueError: the parser's own ``ValueError`` unchanged (its text
        names no value, e.g. "Header values may not contain linefeed or
        carriage return characters" -- as before), or ``ValueError(reason)``
        for any other exception of the parser.
    """

    try:
        email[name] = value
        header = email[name]
        header.addresses  # noqa: B018 -- evaluated for its exceptions only
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 -- any other parser failure means "unreadable"
        raise ValueError(reason) from None


def address_literal(address: str) -> str:
    """The RFC 5321 address literal for the local end of a connection.

    :param address: the host part of ``socket.getsockname()`` -- IPv4, IPv6
        (a zone such as ``%en0`` is dropped, it means nothing to the server)
        or an IPv4 address mapped into IPv6 (written as plain IPv4).
    :returns: ``[192.0.2.10]`` or ``[IPv6:2001:db8::1]``; for something that
        is no IP address at all (cannot come from an IP socket) the
        loopback literal ``[127.0.0.1]``, the same fallback ``smtplib`` uses.
    """

    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "[127.0.0.1]"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return f"[{ip.ipv4_mapped}]"
        return f"[IPv6:{ip.compressed}]"
    return f"[{ip}]"


class _DeadlineSMTP(smtplib.SMTP):
    """``smtplib.SMTP`` whose sockets are cut off by a :class:`Deadline`.

    The connection is opened in :meth:`_get_socket` and replaced by STARTTLS;
    both sockets are registered, so the greeting (read during connecting) and
    the encrypted part are covered alike.

    The name in ``EHLO``/``HELO`` is never looked up: without an explicit
    ``local_hostname`` ``smtplib`` calls ``socket.getfqdn()`` while the object
    is built -- a blocking name lookup no socket timeout and no
    :class:`Deadline` can cut off (measured: 30 s on a host with a slow
    resolver, before the first byte to the server), and it puts the internal
    host name into the greeting. Instead ``ehlo_name`` is used, or, without
    one, the address literal of the local end of the connection
    (:func:`address_literal`), which RFC 5321 provides for a client without a
    meaningful name.

    :param deadline: the session's total time limit.
    :param timeout: the socket timeout of each step.
    :param data_reply_timeout: socket timeout for the reply after the message
        (``None``: the step timeout as for every other reply).
    :param ehlo_name: name for the greeting, already checked by
        :func:`parse_ehlo_name`; ``None`` for the address literal.
    """

    def __init__(
        self,
        deadline: Deadline,
        *,
        timeout: float,
        data_reply_timeout: float | None = None,
        ehlo_name: str | None = None,
    ) -> None:
        self._deadline = deadline
        #: Socket timeout for the reply after the message (``None``: the step
        #: timeout as for every other reply).
        self._data_reply_timeout = data_reply_timeout
        self._awaiting_data_reply = False
        self._ehlo_name = ehlo_name
        # Any explicit value keeps smtplib from calling ``socket.getfqdn()``;
        # without a configured name it is replaced in ``_get_socket`` by the
        # literal of the real local address, before the greeting is sent.
        super().__init__(timeout=timeout, local_hostname=ehlo_name or "[127.0.0.1]")

    def send(self, s: Buffer | str) -> None:
        """Send raw data; remember when it was the end of the message.

        :param s: what ``smtplib`` sends -- a command line, or the whole
            dot-quoted message ending with ``CRLF . CRLF``.
        """

        super().send(s)
        payload = s.encode("ascii", "replace") if isinstance(s, str) else bytes(s)
        self._awaiting_data_reply = payload.endswith(_DATA_TERMINATOR)

    def getreply(self) -> tuple[int, bytes]:
        """Read a reply; the one after the message waits up to ``data_reply_timeout``.

        The socket timeout is raised for this one read and restored after it;
        the session's total deadline cuts it off regardless.

        :returns: ``(code, text)`` as :meth:`smtplib.SMTP.getreply`.
        """

        if not (self._awaiting_data_reply and self._data_reply_timeout and self.sock):
            return super().getreply()
        self._awaiting_data_reply = False
        previous = self.sock.gettimeout()
        self.sock.settimeout(self._data_reply_timeout)
        try:
            return super().getreply()
        finally:
            if self.sock is not None:
                self.sock.settimeout(previous)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        # smtplib's own hook for opening the connection (``SMTP_SSL`` and
        # ``LMTP`` override it the same way); not in the type stubs, hence
        # taken from the class dictionary. The greeting is read right after,
        # already under the deadline.
        opened: socket.socket = vars(smtplib.SMTP)["_get_socket"](self, host, port, timeout)
        self._deadline.watch(opened)
        if self._ehlo_name is None:
            self.local_hostname = address_literal(str(opened.getsockname()[0]))
        return opened

    def starttls(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
        reply = super().starttls(*args, **kwargs)
        if self.sock is not None:
            self._deadline.watch(self.sock)  # the TLS wrapper took over the socket
        return reply

#: ``last_error`` of an entry whose final send was abandoned (its dispatcher
#: never recorded an outcome before the lease ran out). Whether the mail went
#: out is unknown; it is not sent again.
ABANDONED_SEND = "Versand abgebrochen (Reservierung abgelaufen)"


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
    #: Emitted -- as a digest, see :func:`smtp_message_id` -- as the SMTP
    #: ``Message-ID`` so a receiver can de-duplicate an at-least-once
    #: redelivery. Empty for the legacy best-effort path.
    message_id: str = ""


class MailSender(Protocol):
    """Pluggable transport for a rendered notification (analogous to the
    webhook ``Transport``). ``send`` raises on failure; the notifier catches it."""

    def send(self, message: MailMessage) -> None: ...


class NullMailSender:
    """Default sender when no SMTP server is configured: logs and drops.

    Lets the whole feature (modelling, validation, recipient resolution) work
    and be tested without a mail server, and makes an unconfigured deployment a
    no-op rather than an error. ``delivers = False`` tells the outbox that a
    "successful" call delivered nothing: the entry becomes ``DROPPED``, not
    ``SENT`` (and no ``MAIL_SENT`` audit event is written).
    """

    #: This sender never reaches a mail server (see :meth:`MailOutboxDispatcher._deliver`).
    delivers = False

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

    ``ehlo_name`` is the name in the SMTP greeting (checked by
    :func:`parse_ehlo_name`, a wrong value raises ``ValueError`` here
    already); ``None`` greets with the address literal of the local end of
    the connection. Either way no name lookup runs (see :class:`_DeadlineSMTP`).
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
        total_timeout: float = SMTP_TOTAL_SECONDS,
        data_reply_timeout: float = SMTP_DATA_REPLY_SECONDS,
        ehlo_name: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        # Derived once, never raised here: an unusable sender must not abort
        # the start (the warning comes from ``mail_from_problem``).
        # ``None`` leaves the envelope to ``smtplib`` as before (see ``send``).
        self._envelope_sender = envelope_sender(sender)
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._timeout = timeout
        self._total_timeout = total_timeout
        self._data_reply_timeout = data_reply_timeout
        self._ehlo_name = None if ehlo_name is None else parse_ehlo_name(ehlo_name)

    def build_email(self, message: MailMessage) -> EmailMessage:
        """The e-mail for ``message`` exactly as :meth:`send` transmits it.

        ``EmailMessage`` parses every header it is given and shortens a value
        that does not fit the header's grammar instead of refusing it. Each
        header is therefore built in a form the parser keeps whole:

        * ``To``: CR/LF become blanks (no header injection, even though N1
          already rejects malformed addresses at modelling time).
        * ``Subject``: unstructured, keeps ``:``, ``|``, quotes and umlauts
          (encoded word on the wire); every control character becomes a blank
          (:func:`_subject_text`). The receiver shows it literally: a text
          with ``=?`` -- it could be read as an encoded word, by
          ``EmailMessage`` already when the header is set -- goes out as
          :class:`_LiteralSubject`, fully encoded by ProcWorks itself.
        * ``Message-ID``: :func:`smtp_message_id` of the outbox key -- stable
          for a redelivery of the same activation, distinct for a new one
          (the receiver can de-duplicate the at-least-once delivery). No
          header without a key.

        * ``From``/``To``: set through :func:`_set_address_header` -- a
          value the header parser fails on (an address literal without its
          closing bracket) raises ``ValueError`` with
          :data:`UNREADABLE_SENDER` or :data:`UNREADABLE_RECIPIENT`, never
          the parser's internal error.

        :param message: the rendered notification.
        :returns: the message, ready for ``smtplib.SMTP.send_message``.
        :raises ValueError: only ``ValueError``, always with a reason that
            names no address: a line break in the sender (the parser's own
            message, as before) or an unreadable sender/recipient. The outbox
            records it as a failed attempt.
        """

        email = EmailMessage()
        _set_address_header(email, "From", self._sender, UNREADABLE_SENDER)
        _set_address_header(
            email, "To", ", ".join(_no_header_break(addr) for addr in message.to),
            UNREADABLE_RECIPIENT,
        )
        subject = _subject_text(message.subject)
        email["Subject"] = _LiteralSubject(subject) if "=?" in subject else subject
        if message.message_id:
            email["Message-ID"] = smtp_message_id(message.message_id, self._sender)
        email.set_content(message.body or message.subject)
        return email

    def send(self, message: MailMessage) -> None:
        """Build and transmit the e-mail. Raises on any SMTP/transport error.

        ``timeout`` limits each SMTP step, ``data_reply_timeout``
        (:data:`SMTP_DATA_REPLY_SECONDS`) the server's answer after the
        message (content checks take longer than a step), ``total_timeout``
        (:data:`SMTP_TOTAL_SECONDS`) the whole session; a session cut off by
        it raises ``TimeoutError`` naming the total limit and counts as a
        failed send (retried with the same ``Message-ID``).

        The envelope sender (``MAIL FROM``) is the ``addr-spec`` of the
        configured sender (:func:`envelope_sender`), passed explicitly --
        ``send_message``'s own reading sends an address literal as
        ``MAIL FROM:<>``. When the sender holds no single address
        (``procworks``, ``ProcWorks <pw>``, two addresses) nothing changes
        against before: ``smtplib`` derives the envelope itself
        (``<procworks>``, ``<pw>``, the first address) and the server decides;
        the start check has warned about the value.

        :raises ValueError: :data:`NO_ENVELOPE_SENDER` only when neither
            :func:`envelope_sender` nor ``smtplib`` (:func:`_smtplib_envelope`)
            finds an address, i.e. ``smtplib`` would send ``MAIL FROM:<>``
            (``pw@``, ``pw@example.org.``) -- raised before any connection, so
            no mail goes out with the null sender; the outbox records a failed
            attempt with this reason. A value the header itself refuses (line
            break) raises from :meth:`build_email` as before; a sender the
            header parser fails on (``pw@[192.0.2.10``, no closing bracket)
            raises :data:`UNREADABLE_SENDER` from there, also before any
            connection (``smtplib`` would read no address from it either).
        """

        email = self.build_email(message)
        from_addr = self._envelope_sender
        if from_addr is None and not _smtplib_envelope(email):
            raise ValueError(NO_ENVELOPE_SENDER)
        with Deadline(self._total_timeout) as deadline:
            try:
                with _DeadlineSMTP(
                    deadline,
                    timeout=self._timeout,
                    data_reply_timeout=self._data_reply_timeout,
                    ehlo_name=self._ehlo_name,
                ) as smtp:
                    smtp.connect(self._host, self._port)
                    if self._use_tls:
                        smtp.starttls()
                    if self._username:
                        smtp.login(self._username, self._password or "")
                    # ``None``: smtplib derives the envelope itself, as before.
                    smtp.send_message(email, from_addr=from_addr)
            except (OSError, smtplib.SMTPException) as exc:
                deadline.raise_if_expired(exc)
                raise


#: Environment variable with the sender address of every notification.
MAIL_FROM_ENV = "PROCWORKS_MAIL_FROM"


def mail_from_problem(raw: str | None) -> str | None:
    """Why the configured sender address is not usable, or ``None``.

    The sender goes into ``From:`` and ``MAIL FROM:<...>``; a value such as
    ``procworks`` (no domain) or ``pw@procworks`` (no fully qualified domain)
    is no valid address there, and many mail servers refuse or mark such a
    mail. The check is advisory: :func:`create_mail_sender` only logs the
    reason, the start goes on (a refusing server shows up per mail in the
    outbox anyway).

    The value is read the way the sender uses it: as the ``From`` header of
    an :class:`~email.message.EmailMessage` (default policy), whose parsed
    address is what reaches ``From:`` and the envelope. Reading it by hand
    instead judged edge forms wrongly -- ``Name <<x@firma.example>>`` sends
    only ``Name`` and passed, a stray ``>`` was blamed on the domain, and a
    trailing comment ``x@firma.example (Notiz)``, a valid form, warned.

    Two reasons, checked in this order:

    1. *no single e-mail address* -- the header parser reports a defect, the
       value holds no or several addresses, or the address lacks a local
       part or domain. Every value for which :func:`envelope_sender` finds no
       envelope (sent only as ``smtplib`` reads it, or not at all) lands
       here. This is also the
       answer **whenever the form is in doubt** (stray or doubled angle
       brackets, a blank in the domain, a trailing dot, a line break): the
       operator has to correct the form, and naming the domain there would
       mislead.
    2. *domain not fully qualified* -- the form is a clean single address,
       but its domain is neither a fully qualified name nor an address
       literal (:func:`parse_ehlo_name`), e.g. ``pw@procworks``.

    :param raw: the configured value; ``None`` or blank counts as not set.
        Accepted forms: ``local@domain``, ``Name <local@domain>``,
        ``"Name, Vorname" <local@domain>`` and ``local@domain (comment)``,
        the domain a fully qualified name or an address literal
        (``pw@[192.0.2.10]`` too).
    :returns: ``None`` for a usable address, else a short English reason.
        **The reason never contains the value** -- the address can name a
        person, and the log keeps only what the operator needs to act.
    """

    value = (raw or "").strip()
    if not value:
        return f"{MAIL_FROM_ENV} is not set, no notification is sent"
    no_single = (f"{MAIL_FROM_ENV} contains no single e-mail address of the form "
                 "local-part@domain")
    header = _read_from_header(value)
    if header is None:
        # A line break (header injection) or input the parser fails on (an
        # address literal without its closing bracket): never a single
        # address. The check is advisory and must not abort the start, so
        # these are answered, not raised.
        return no_single
    addresses, defects = header.addresses, header.defects
    # A value without an envelope sender goes out only as ``smtplib`` reads it
    # (or, where that is the null sender, not at all); the start must warn
    # about every such value, whatever else the parser reports.
    if defects or len(addresses) != 1 or envelope_sender(value) is None:
        return no_single
    local, domain = addresses[0].username, addresses[0].domain
    if (
        not local
        or not domain
        or any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in local)
    ):
        return no_single
    try:
        parse_ehlo_name(domain)
    except ValueError:
        return (f"the domain in {MAIL_FROM_ENV} is not fully qualified (e.g. "
                "example.org) and no address literal")
    return None


def create_mail_sender() -> MailSender:
    """Build the process-wide mail sender from the environment.

    Returns an :class:`SmtpMailSender` when ``PROCWORKS_SMTP_HOST`` and
    ``PROCWORKS_MAIL_FROM`` are set, otherwise a :class:`NullMailSender` (the
    feature stays fully modellable and validated, mails are just not sent).

    ``PROCWORKS_SMTP_EHLO_NAME`` (optional) sets the name in the greeting;
    unset or empty greets with the address literal of the connection.

    When ``PROCWORKS_SMTP_HOST`` is set, the sender address is checked
    (:func:`mail_from_problem`): a missing or unusable address logs **one**
    warning with the reason (never the value) -- the API builds the sender
    once per process at import, so this is once per start. It never aborts
    the start; without ``PROCWORKS_SMTP_HOST`` nothing is checked or logged.

    :raises ValueError: when ``PROCWORKS_SMTP_EHLO_NAME`` is set but no valid
        greeting name (:func:`parse_ehlo_name`) -- the start aborts instead of
        every mail failing later. Only checked when SMTP is configured.
    """

    host = os.getenv("PROCWORKS_SMTP_HOST")
    sender = os.getenv(MAIL_FROM_ENV)
    if host:
        problem = mail_from_problem(sender)
        if problem is not None:
            logger.warning("Mail sender address: %s.", problem)
    if not host or not sender:
        return NullMailSender()
    return SmtpMailSender(
        host=host,
        # ``or``: Compose reicht nicht gesetzte Variablen als "" durch.
        port=int(os.getenv("PROCWORKS_SMTP_PORT") or "587"),
        sender=sender,
        username=os.getenv("PROCWORKS_SMTP_USER") or None,
        password=os.getenv("PROCWORKS_SMTP_PASSWORD") or None,
        use_tls=os.getenv("PROCWORKS_SMTP_TLS", "1") not in ("0", "false", "False"),
        ehlo_name=(os.getenv(SMTP_EHLO_NAME_ENV) or "").strip() or None,
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


def completion_baseline(
    before_states: dict[str, NodeState], completed_node_id: str
) -> dict[str, NodeState]:
    """The "before" marking a ready-diff must use after completing a step.

    Every boundary observer of an advance (activation stamp, mail notification)
    finds newly ready tasks by diffing ``before_states`` against the instance
    after the advance. That diff is blind to one transition: a loop body step
    that is completed straight from ACTIVATED and re-offered by the loop reset
    in the same advance is ACTIVATED before *and* after -- the second, third
    ... round would never be notified, and its activation clock would keep the
    stamp of round one (overdue right away). The step has, however, passed
    through COMPLETED in between; recording exactly that in the baseline makes
    the diff see the fresh activation. A step completed from RUNNING already
    differed and behaves the same as before.

    :param before_states: node marking taken *before* the engine advance; it is
        not mutated.
    :param completed_node_id: the step the advance completed.
    :returns: a copy of ``before_states`` in which ``completed_node_id`` is
        COMPLETED. Outside a loop the completed step is never ACTIVATED
        afterwards, so the baseline changes nothing there (exactly one
        notification per activation, as before).
    """

    baseline = dict(before_states)
    baseline[completed_node_id] = NodeState.COMPLETED
    return baseline


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
            unit_of_work.mark_irreversible()  # a sent mail cannot be taken back
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
        ``FAILED`` with a back-off, exhausted budget -> ``DEAD`` (dead-letter),
        a sender without mail server -> ``DROPPED`` (final).
        The ``sender`` is passed per call so callers can supply the current
        process-wide sender (and tests can inject a collector) without the
        dispatcher holding a stale reference. Never raises -- a delivery problem
        is recorded on the entry, never propagated into the triggering step.

        Dispatches run after every commit and overlap, so each due entry is
        first **reserved** (:meth:`MailOutboxStore.claim_entry`, lease
        :data:`_SEND_LEASE_SECONDS`): only the dispatcher whose reservation
        was stored sends it, the others skip it, and its outcome is stored
        only while it still holds the lease. A send abandoned by a dying
        process is due again after the lease -- sent once more (same
        ``Message-ID``) or, if it was the last attempt, ``DEAD`` with
        :data:`ABANDONED_SEND` and reported as ``dead``.

        :returns: one result per entry this call reserved.
        """

        now = self._now()
        results: list[MailDispatchResult] = []
        for entry in self._store.list_entries():
            if entry.state not in DUE_MAIL_OUTBOX_STATES or entry.next_attempt_at > now:
                continue
            at = max(now, self._now())  # per entry: sends run one after the other
            lease_until = at + _SEND_LEASE_SECONDS
            claimed = self._store.claim_entry(entry.id, now=at, lease_until=lease_until)
            if claimed is None:
                continue  # another dispatcher is sending it, or it is done
            if claimed.attempts > claimed.max_attempts:
                result = self._bury_abandoned(claimed, at, lease_until)
                if result is not None:
                    results.append(result)
                continue
            results.append(self._deliver(claimed, sender, at, lease_until))
        return results

    def _bury_abandoned(
        self, entry: MailOutboxEntry, now: float, lease_until: float
    ) -> MailDispatchResult | None:
        """Close an entry whose last send was abandoned, without sending it.

        :param entry: the reserved entry (``attempts`` one past the budget).
        :param now: the current time.
        :param lease_until: the reservation just made.
        :returns: a ``dead`` result (so the boundary audits the failure), or
            ``None`` if the reservation was lost meanwhile.
        """

        entry.attempts = entry.max_attempts
        entry.state = MailOutboxState.DEAD
        entry.next_attempt_at = now
        entry.last_error = ABANDONED_SEND
        if not self._store.finish_entry(entry, lease_until=lease_until):
            return None
        logger.error(
            "mail notification dead-lettered, its last send was abandoned "
            "(instance=%s node=%s)",
            entry.instance_id,
            entry.node_id,
        )
        return MailDispatchResult(entry=entry, delivered=False, dead=True)

    def _deliver(
        self, entry: MailOutboxEntry, sender: MailSender, now: float, lease_until: float
    ) -> MailDispatchResult:
        """Make the reserved send attempt for ``entry`` and store the new state.

        :param entry: the entry, reserved by this dispatcher -- its
            ``attempts`` already counts this attempt. Updated in place.
        :param sender: the mail sender.
        :param now: the attempt time.
        :param lease_until: the reservation; the outcome is stored only while
            it is still held (:meth:`MailOutboxStore.finish_entry`), otherwise
            a warning is logged and the newer outcome stays.
        :returns: the outcome of this attempt.
        """

        message = MailMessage(
            to=list(entry.recipients),
            subject=entry.subject,
            body=entry.body,
            instance_id=entry.instance_id,
            node_id=entry.node_id,
            schema_id=entry.schema_id,
            message_id=entry.dedup_key,
        )
        attempt = entry.attempts
        error: str | None = None
        try:
            unit_of_work.mark_irreversible()  # a sent mail cannot be taken back
            sender.send(message)
            ok = True
        except Exception as exc:  # noqa: BLE001 -- transport failures are recorded, not raised
            ok = False
            error = str(exc)

        entry.attempts = attempt
        entry.last_error = error
        # A sender without a mail server (NullMailSender) "succeeds" without
        # delivering anything: final DROPPED, never SENT, no retry.
        dropped = ok and getattr(sender, "delivers", True) is False
        delivered = ok and not dropped
        dead = False
        if dropped:
            entry.state = MailOutboxState.DROPPED
            entry.last_error = "kein Mailserver konfiguriert"
            entry.next_attempt_at = now
        elif ok:
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
        if not self._store.finish_entry(entry, lease_until=lease_until):
            logger.warning(
                "mail outbox entry %s: outcome of attempt %d not recorded, its "
                "reservation had run out and was taken over",
                entry.id,
                attempt,
            )
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
