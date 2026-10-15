# SPDX-License-Identifier: BUSL-1.1
"""Every delivery ends within its total time limit -- also against a receiver
that answers drop by drop.

A delivery is reserved for its time limit plus a margin; after that another
API process may take the entry over and deliver it once more. The limit used
to apply to each socket operation only: a receiver that sent its answer one
byte every few seconds kept every single read short, and the delivery as a
whole ran for minutes -- past the reservation, so the same entry went out a
second time while the first delivery was still reading, and the request that
triggered it waited all along.

Now each exchange has a total deadline (:class:`procworks.deadline.Deadline`)
that cuts the connection when it is reached: the HTTP transport of webhooks
and pushes after the delivery's time limit, the SMTP sender after
:data:`procworks.mail_runtime.SMTP_TOTAL_SECONDS` -- both below their
reservation. The attempt then counts as failed (``TimeoutError`` naming the
total limit) and follows the normal retry rules.

Only local sockets are used (no name resolution, no network).
"""

from __future__ import annotations

import smtplib
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from procworks import mail_runtime, outbox
from procworks.deadline import Deadline
from procworks.mail_runtime import MailMessage, SmtpMailSender
from procworks.outbox import PinnedTarget, UrllibTransport
from procworks.store import InMemoryWebhookStore


@contextmanager
def _server(chunks: list[bytes], pause: float, *, read_first: bool = True) -> Iterator[int]:
    """A one-connection TCP server that answers ``chunks`` with ``pause`` between them.

    :param read_first: read the client's request before answering (HTTP);
        ``False`` answers at once (an SMTP greeting).
    :yields: the port.
    """

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                if read_first:
                    conn.recv(65536)
                for chunk in chunks:
                    if stop.is_set():
                        return
                    conn.sendall(chunk)
                    time.sleep(pause)
                stop.wait(5)
            except OSError:
                return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        listener.close()
        thread.join(5)


def _target(port: int) -> PinnedTarget:
    return PinnedTarget(scheme="http", host="127.0.0.1", port=port, path="/hook", ip="127.0.0.1")


def _trickle(text: bytes) -> list[bytes]:
    return [text[i : i + 1] for i in range(len(text))]


# --- the HTTP transport (webhooks and pushes) --------------------------------------------


def test_a_receiver_answering_drop_by_drop_is_cut_off_at_the_time_limit() -> None:
    body = b"x" * 50
    answer = b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n" + body
    with _server([answer[:40], *_trickle(answer[40:])], pause=0.1) as port:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="total time limit"):
            UrllibTransport().post_pinned(_target(port), b"{}", {}, timeout=0.5)
        elapsed = time.monotonic() - started

    assert elapsed < 1.5, f"the delivery ran {elapsed:.1f} s past its limit of 0.5 s"


def test_headers_sent_drop_by_drop_are_cut_off_as_well() -> None:
    answer = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    with _server(_trickle(answer), pause=0.1) as port:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="total time limit"):
            UrllibTransport().post_pinned(_target(port), b"{}", {}, timeout=0.5)

    assert time.monotonic() - started < 1.5


def test_a_prompt_receiver_is_delivered_as_before() -> None:
    answer = b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
    with _server([answer], pause=0) as port:
        assert UrllibTransport().post_pinned(_target(port), b"{}", {}, timeout=2) == 204


def test_the_deadline_stays_below_the_reservation() -> None:
    """The lease is the time limit plus a margin; the deadline is the time limit."""

    dispatcher = outbox.OutboxDispatcher(InMemoryWebhookStore())
    for timeout in (None, 0.5, 5.0, 30.0):
        effective = timeout if timeout else dispatcher._timeout_s
        assert dispatcher._lease_seconds(timeout) - effective >= 30


# --- the SMTP sender -----------------------------------------------------------------------


def test_an_smtp_server_answering_drop_by_drop_is_cut_off() -> None:
    greeting = b"220 langsam.example ESMTP\r\n"
    with _server(_trickle(greeting), pause=0.1, read_first=False) as port:
        sender = SmtpMailSender(host="127.0.0.1", port=port, sender="pw@example.org",
                                use_tls=False, timeout=5, total_timeout=0.5)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="total time limit"):
            sender.send(MailMessage(to=["a@example.org"], subject="s", body="b",
                                    instance_id="i1", node_id="n1", schema_id="s1"))

    assert time.monotonic() - started < 1.5


def test_the_smtp_total_limit_stays_below_the_reservation() -> None:
    assert mail_runtime.SMTP_TOTAL_SECONDS + 60 <= mail_runtime._SEND_LEASE_SECONDS
    assert SmtpMailSender(host="h", port=25, sender="s@example.org")._total_timeout == (
        mail_runtime.SMTP_TOTAL_SECONDS
    )


# --- the deadline itself ---------------------------------------------------------------------


def test_a_deadline_that_is_not_reached_cuts_nothing() -> None:
    left, right = socket.socketpair()
    with left, right, Deadline(5) as deadline:
        deadline.watch(left)
        right.sendall(b"ok")
        assert left.recv(2) == b"ok"
    assert deadline.expired is False


def test_a_socket_watched_after_the_deadline_is_cut_at_once() -> None:
    left, right = socket.socketpair()
    with left, right, Deadline(0.05) as deadline:
        time.sleep(0.15)
        deadline.watch(left)
        assert deadline.expired is True
        assert left.recv(1) == b""  # shut down: no more data


# --- the SMTP answer after the message (own, longer time limit) ----------------------------


@contextmanager
def _smtp_server(answer_delay: float) -> Iterator[int]:
    """A minimal SMTP server that accepts one mail and answers it after ``answer_delay``.

    Every other reply comes at once; only the confirmation after the message
    (the reply to the terminating ``.``) is delayed -- like a server that
    scans the content before it accepts.

    :yields: the port.
    """

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn, conn.makefile("rb") as reader:
            conn.settimeout(10)
            try:
                conn.sendall(b"220 pruefend.example ESMTP\r\n")
                in_data = False
                for raw in reader:
                    line = raw.rstrip(b"\r\n")
                    if in_data:
                        if line == b".":
                            in_data = False
                            if stop.wait(answer_delay):
                                return
                            conn.sendall(b"250 2.0.0 accepted\r\n")
                        continue
                    command = line[:4].upper()
                    if command in (b"EHLO", b"HELO"):
                        conn.sendall(b"250 pruefend.example\r\n")
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
        stop.set()
        listener.close()
        thread.join(5)


def _mail() -> MailMessage:
    return MailMessage(to=["a@example.org"], subject="s", body="b",
                       instance_id="i1", node_id="n1", schema_id="s1")


def test_a_slow_confirmation_after_the_message_gets_its_own_longer_limit() -> None:
    """The server checks the content for 0.8 s; each other step may take 0.3 s."""

    with _smtp_server(answer_delay=0.8) as port:
        sender = SmtpMailSender(host="127.0.0.1", port=port, sender="pw@example.org",
                                use_tls=False, timeout=0.3, total_timeout=5,
                                data_reply_timeout=2.0)
        sender.send(_mail())  # no exception: accepted


def test_a_confirmation_later_than_its_limit_is_a_failed_send() -> None:
    """Negativ: the reason is the time limit of the reply, not anything else."""

    with _smtp_server(answer_delay=1.5) as port:
        sender = SmtpMailSender(host="127.0.0.1", port=port, sender="pw@example.org",
                                use_tls=False, timeout=0.3, total_timeout=5,
                                data_reply_timeout=0.6)
        started = time.monotonic()
        with pytest.raises((TimeoutError, smtplib.SMTPServerDisconnected)) as exc:
            sender.send(_mail())
    elapsed = time.monotonic() - started
    assert 0.5 < elapsed < 1.4, elapsed
    assert "timed out" in str(exc.value) or isinstance(exc.value, TimeoutError)


def test_the_total_limit_still_caps_the_confirmation() -> None:
    """Grenzfall: a reply limit beyond the total limit ends at the total limit."""

    with _smtp_server(answer_delay=3.0) as port:
        sender = SmtpMailSender(host="127.0.0.1", port=port, sender="pw@example.org",
                                use_tls=False, timeout=0.3, total_timeout=0.8,
                                data_reply_timeout=10.0)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="total time limit"):
            sender.send(_mail())
    assert time.monotonic() - started < 2.0


def test_the_reply_limit_is_longer_than_a_step_and_inside_the_session() -> None:
    assert mail_runtime.SMTP_DATA_REPLY_SECONDS == 60.0
    assert 10.0 < mail_runtime.SMTP_DATA_REPLY_SECONDS < mail_runtime.SMTP_TOTAL_SECONDS
    assert SmtpMailSender(host="h", port=25, sender="s@example.org")._data_reply_timeout == (
        mail_runtime.SMTP_DATA_REPLY_SECONDS
    )
