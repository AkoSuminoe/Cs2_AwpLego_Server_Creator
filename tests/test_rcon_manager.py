"""Offline tests for RCON command rendering and Source RCON wire framing."""
from __future__ import annotations

import asyncio
import struct
from typing import Any, Optional

import pytest

from core.rcon_manager import (
    RCONAuthError,
    RCONClient,
    RCONConnectionError,
    _pack_packet,
    _parse_payload,
)
from models.schemas import RCONCommand


# Source RCON packet types (mirror of core/rcon_manager.py private constants)
_TYPE_EXECCOMMAND = 2
_TYPE_RESPONSE_VALUE = 0


# ---------------------------------------------------------------------------
# RCONCommand.render — plain string assembly
# ---------------------------------------------------------------------------

def test_rcon_command_render_joins_command_and_args() -> None:
    """render() must join the command and its args with a single space."""
    cmd = RCONCommand(command="changelevel", args=["de_dust2", "casual"])
    assert cmd.render() == "changelevel de_dust2 casual"


def test_rcon_command_render_bare_command_has_no_trailing_whitespace() -> None:
    """A command with no arguments must render as itself, no trailing space."""
    assert RCONCommand(command="status").render() == "status"


# ---------------------------------------------------------------------------
# _pack_packet — Source RCON wire format producer
# ---------------------------------------------------------------------------

def test_pack_packet_returns_bytes_object() -> None:
    """_pack_packet must yield a bytes object ready for socket.send()."""
    packet = _pack_packet(packet_id=1, packet_type=_TYPE_EXECCOMMAND, body="status")
    assert isinstance(packet, bytes)


def test_pack_packet_ends_with_double_null_terminator() -> None:
    """
    Source RCON spec: the payload must end with two null bytes — one for
    the C-string terminator, one for the mandatory empty-string tail.
    """
    packet = _pack_packet(1, _TYPE_EXECCOMMAND, "status")
    assert packet[-2:] == b"\x00\x00"


def test_pack_packet_size_field_equals_body_length_plus_ten() -> None:
    """
    size = 4 (id) + 4 (type) + len(body) + 2 (double null) = len(body) + 10.
    Crucially, the size field must NOT count itself (per the Source spec).
    """
    body = "status"
    packet = _pack_packet(packet_id=1, packet_type=_TYPE_EXECCOMMAND, body=body)
    size_field = struct.unpack("<i", packet[:4])[0]
    assert size_field == len(body) + 10


def test_pack_packet_writes_little_endian_header() -> None:
    """
    The first 12 bytes must decode as three little-endian int32 values
    in the order: size, id, type.
    """
    packet = _pack_packet(packet_id=42, packet_type=_TYPE_EXECCOMMAND, body="ping")
    size, packet_id, packet_type = struct.unpack("<iii", packet[:12])
    assert size == len("ping") + 10
    assert packet_id == 42
    assert packet_type == _TYPE_EXECCOMMAND


def test_pack_packet_encodes_body_as_utf8_after_header() -> None:
    """The body must sit immediately after the 12-byte header as UTF-8 bytes."""
    body = "kick alice"
    packet = _pack_packet(packet_id=7, packet_type=_TYPE_EXECCOMMAND, body=body)
    body_slice = packet[12 : 12 + len(body)]
    assert body_slice == body.encode("utf-8")


def test_pack_packet_total_length_matches_size_field() -> None:
    """
    Total wire length must be size_field + 4 (the size prefix itself).
    Any mismatch breaks length-prefixed framing on the receiving end.
    """
    packet = _pack_packet(packet_id=99, packet_type=_TYPE_EXECCOMMAND, body="ping")
    size_field = struct.unpack("<i", packet[:4])[0]
    assert len(packet) == size_field + 4


# ---------------------------------------------------------------------------
# _parse_payload — Source RCON wire format consumer
# ---------------------------------------------------------------------------

def test_parse_payload_extracts_id_type_and_body() -> None:
    """
    Given a hand-crafted Source RCON payload (id + type + body + \\x00\\x00),
    _parse_payload must return the correct (id, type, body) tuple.
    """
    payload = (
        struct.pack("<i", 99)
        + struct.pack("<i", _TYPE_RESPONSE_VALUE)
        + b"OK - map changed"
        + b"\x00\x00"
    )

    packet_id, packet_type, body = _parse_payload(payload)

    assert packet_id == 99
    assert packet_type == _TYPE_RESPONSE_VALUE
    assert body == "OK - map changed"


def test_parse_payload_handles_empty_body() -> None:
    """A payload with no body (just the two null bytes) must return an empty string."""
    payload = struct.pack("<i", 1) + struct.pack("<i", _TYPE_RESPONSE_VALUE) + b"\x00\x00"
    _, _, body = _parse_payload(payload)
    assert body == ""


def test_parse_payload_raises_on_truncated_input() -> None:
    """
    A payload shorter than the 8-byte header must raise struct.error so
    the async caller can convert it into RCONConnectionError.
    """
    with pytest.raises(struct.error):
        _parse_payload(b"\x00\x00\x00")


# ---------------------------------------------------------------------------
# Round-trip — _pack_packet → _parse_payload symmetry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("packet_id", "packet_type", "body"),
    [
        (1, _TYPE_EXECCOMMAND, "status"),
        (42, _TYPE_EXECCOMMAND, "sv_cheats 1"),
        (99, _TYPE_RESPONSE_VALUE, ""),
        (7, _TYPE_EXECCOMMAND, "say Hello, World!"),
    ],
    ids=["status", "with_number_arg", "empty_body", "with_punctuation"],
)
def test_pack_and_parse_roundtrip_preserves_fields(
    packet_id: int,
    packet_type: int,
    body: str,
) -> None:
    """
    Packing then parsing must yield the exact input values. This is the
    single most important guarantee of the wire codec — every command
    the client sends must be reconstructible on the server side.
    """
    packet = _pack_packet(packet_id, packet_type, body)
    # Strip the 4-byte size prefix; the rest is what _recv_packet reads from the socket
    payload = packet[4:]

    parsed_id, parsed_type, parsed_body = _parse_payload(payload)

    assert parsed_id == packet_id
    assert parsed_type == packet_type
    assert parsed_body == body


# ---------------------------------------------------------------------------
# RCONClient — end-to-end auth + execute with a faked TCP transport
# ---------------------------------------------------------------------------

class _FakeStreamReader:
    """asyncio.StreamReader-compatible replacement backed by a byte buffer."""

    def __init__(self, data: bytes) -> None:
        self._buf = data
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        if self._pos + n > len(self._buf):
            partial = self._buf[self._pos:]
            self._pos = len(self._buf)
            raise asyncio.IncompleteReadError(partial, expected=n)
        chunk = self._buf[self._pos:self._pos + n]
        self._pos += n
        return chunk


class _FakeStreamWriter:
    """asyncio.StreamWriter-compatible replacement that records outbound bytes."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _install_fake_transport(
    monkeypatch: pytest.MonkeyPatch,
    reader: _FakeStreamReader,
    writer: _FakeStreamWriter,
) -> None:
    """Patch asyncio.open_connection so RCONClient.connect() gets our fakes."""
    async def _fake_open_connection(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return reader, writer

    monkeypatch.setattr("asyncio.open_connection", _fake_open_connection)


def test_rcon_client_connect_succeeds_on_valid_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A server response echoing the auth packet id (not -1) must let
    connect() complete without raising, and an AUTH packet (type 3)
    must have been sent on the wire.
    """
    reader = _FakeStreamReader(_pack_packet(1, 2, ""))
    writer = _FakeStreamWriter()
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> None:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()
        await client.close()

    asyncio.run(_run())

    # The first packet written must be an AUTH packet (type field at offset 8)
    auth_type = struct.unpack("<i", writer.written[8:12])[0]
    assert auth_type == 3, "Client did not send an AUTH packet on connect()"


def test_rcon_client_connect_raises_auth_error_on_negative_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Source RCON signals bad password by returning packet id -1 in the
    auth response. RCONClient must convert that into RCONAuthError.
    """
    reader = _FakeStreamReader(_pack_packet(-1, 2, ""))
    writer = _FakeStreamWriter()
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> None:
        client = RCONClient("host", 27015, "wrong-password", timeout=1.0)
        await client.connect()

    with pytest.raises(RCONAuthError):
        asyncio.run(_run())


def test_rcon_client_connect_raises_connection_error_when_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused OS-level connection must surface as RCONConnectionError."""
    async def _refuse(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        raise ConnectionRefusedError("nothing listening")

    monkeypatch.setattr("asyncio.open_connection", _refuse)

    async def _run() -> None:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()

    with pytest.raises(RCONConnectionError):
        asyncio.run(_run())


def test_rcon_client_execute_returns_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    After a successful auth, execute() must return the body of the
    server's response verbatim.
    """
    auth_resp = _pack_packet(1, 2, "")
    cmd_resp = _pack_packet(2, 0, "map de_dust2")
    reader = _FakeStreamReader(auth_resp + cmd_resp)
    writer = _FakeStreamWriter()
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> str:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()
        try:
            return await client.execute("status")
        finally:
            await client.close()

    assert asyncio.run(_run()) == "map de_dust2"


def test_rcon_client_execute_before_connect_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling execute() without a prior connect() must raise RCONConnectionError."""
    async def _run() -> None:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.execute("status")

    with pytest.raises(RCONConnectionError):
        asyncio.run(_run())


def test_rcon_client_context_manager_closes_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    `async with RCONClient(...)` must close the writer on exit — the
    contract that operators rely on for clean disconnects.
    """
    reader = _FakeStreamReader(_pack_packet(1, 2, ""))
    writer = _FakeStreamWriter()
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> bool:
        async with RCONClient("host", 27015, "password", timeout=1.0):
            pass
        return writer.closed

    assert asyncio.run(_run()) is True


def test_rcon_client_add_admin_sends_css_addadmin_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    add_admin() is a thin wrapper — verify the wire body contains the
    expected `css_addadmin <steam_id> <permission>` string.
    """
    auth_resp = _pack_packet(1, 2, "")
    cmd_resp = _pack_packet(2, 0, "OK")
    reader = _FakeStreamReader(auth_resp + cmd_resp)
    writer = _FakeStreamWriter()
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> None:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()
        try:
            await client.add_admin("76561198000000000", "@css/root")
        finally:
            await client.close()

    asyncio.run(_run())

    # Skip auth packet (14 bytes for empty-body auth) and the header of the
    # command packet (12 bytes) to reach the body of the second packet.
    auth_len = len(_pack_packet(1, 3, "password"))
    cmd_body_start = auth_len + 12
    body_bytes = writer.written[cmd_body_start:].rstrip(b"\x00")
    assert body_bytes.decode("utf-8") == "css_addadmin 76561198000000000 @css/root"


# ---------------------------------------------------------------------------
# Error-injecting stream fakes (for timeout / OS-error paths)
# ---------------------------------------------------------------------------

class _RaisingStreamReader:
    """StreamReader stand-in whose readexactly always raises a preset exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def readexactly(self, n: int) -> bytes:
        raise self._exc


class _RaisingStreamWriter:
    """
    StreamWriter stand-in that can raise on drain() or close() to exercise the
    send-failure and close-failure branches.
    """

    def __init__(
        self,
        drain_exc: Optional[BaseException] = None,
        close_exc: Optional[BaseException] = None,
    ) -> None:
        self.written = bytearray()
        self.closed = False
        self._drain_exc = drain_exc
        self._close_exc = close_exc

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        if self._drain_exc is not None:
            raise self._drain_exc

    def close(self) -> None:
        if self._close_exc is not None:
            raise self._close_exc
        self.closed = True

    async def wait_closed(self) -> None:
        return None


# ---------------------------------------------------------------------------
# _recv_packet — framing errors surfaced through connect()
# ---------------------------------------------------------------------------

def test_rcon_connect_raises_on_malformed_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A size prefix below the 10-byte floor must raise RCONConnectionError."""
    reader = _FakeStreamReader(struct.pack("<i", 5))
    writer = _FakeStreamWriter()
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> None:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()

    with pytest.raises(RCONConnectionError):
        asyncio.run(_run())


def test_rcon_connect_raises_when_stream_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty stream (server hung up) must surface as RCONConnectionError."""
    reader = _FakeStreamReader(b"")
    writer = _FakeStreamWriter()
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> None:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()

    with pytest.raises(RCONConnectionError):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Timeout / OS-error arms across connect, send, and receive
# ---------------------------------------------------------------------------

def test_rcon_connect_raises_on_open_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out socket open must be wrapped in RCONConnectionError."""
    async def _timeout(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        raise asyncio.TimeoutError

    monkeypatch.setattr("asyncio.open_connection", _timeout)

    async def _run() -> None:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()

    with pytest.raises(RCONConnectionError):
        asyncio.run(_run())


def test_rcon_send_raises_on_drain_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain() timeout during the auth send must raise RCONConnectionError."""
    reader = _FakeStreamReader(_pack_packet(1, 2, ""))
    writer = _RaisingStreamWriter(drain_exc=asyncio.TimeoutError())
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> None:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()

    with pytest.raises(RCONConnectionError):
        asyncio.run(_run())


def test_rcon_send_raises_on_os_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken pipe during send must be wrapped in RCONConnectionError."""
    reader = _FakeStreamReader(_pack_packet(1, 2, ""))
    writer = _RaisingStreamWriter(drain_exc=ConnectionResetError("broken pipe"))
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> None:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()

    with pytest.raises(RCONConnectionError):
        asyncio.run(_run())


def test_rcon_recv_raises_on_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read timeout while awaiting the auth response must raise RCONConnectionError."""
    reader = _RaisingStreamReader(asyncio.TimeoutError())
    writer = _FakeStreamWriter()
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> None:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()

    with pytest.raises(RCONConnectionError):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Lifecycle — close() robustness
# ---------------------------------------------------------------------------

def test_rcon_close_is_noop_when_not_connected() -> None:
    """close() on a client that never connected must be a silent no-op."""
    async def _run() -> Optional[Any]:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.close()
        return client._writer

    assert asyncio.run(_run()) is None


def test_rcon_close_swallows_writer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer that errors on close() must not propagate; state must still reset."""
    reader = _FakeStreamReader(_pack_packet(1, 2, ""))
    writer = _RaisingStreamWriter(close_exc=OSError("already closed"))
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> Optional[Any]:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()
        await client.close()
        return client._writer

    assert asyncio.run(_run()) is None


# ---------------------------------------------------------------------------
# Convenience wrappers — verify the exact command body reaches the wire
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("method", "args", "expected_body"),
    [
        ("change_map", ("de_nuke",), "changelevel de_nuke"),
        ("kick_player", ("alice",), "css_kick alice"),
        ("kick_player", ("alice", "afk"), "css_kick alice afk"),
        ("ban_player", ("76561198000000000", 30), "css_ban 76561198000000000 30"),
        ("ban_player", ("76561198000000000",), "css_ban 76561198000000000 0"),
        ("broadcast", ("gg wp",), "say gg wp"),
    ],
    ids=[
        "change_map",
        "kick",
        "kick_with_reason",
        "ban_with_duration",
        "ban_default",
        "broadcast",
    ],
)
def test_rcon_convenience_commands_send_expected_bodies(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    args: tuple[Any, ...],
    expected_body: str,
) -> None:
    """Each helper must place the correct command string on the wire verbatim."""
    auth_resp = _pack_packet(1, 2, "")
    cmd_resp = _pack_packet(2, 0, "OK")
    reader = _FakeStreamReader(auth_resp + cmd_resp)
    writer = _FakeStreamWriter()
    _install_fake_transport(monkeypatch, reader, writer)

    async def _run() -> None:
        client = RCONClient("host", 27015, "password", timeout=1.0)
        await client.connect()
        try:
            await getattr(client, method)(*args)
        finally:
            await client.close()

    asyncio.run(_run())

    # Skip the auth packet, then the 12-byte header of the command packet.
    auth_len = len(_pack_packet(1, 3, "password"))
    cmd_body_start = auth_len + 12
    body_bytes = writer.written[cmd_body_start:].rstrip(b"\x00")
    assert body_bytes.decode("utf-8") == expected_body
