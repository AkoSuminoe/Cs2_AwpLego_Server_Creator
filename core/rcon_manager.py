from __future__ import annotations

import asyncio
import struct
from typing import Optional

# Source RCON packet types
_TYPE_AUTH = 3
_TYPE_EXECCOMMAND = 2
_TYPE_RESPONSE_VALUE = 0
_TYPE_AUTH_RESPONSE = 2


class RCONAuthError(Exception):
    pass


class RCONConnectionError(Exception):
    pass


class RCONClient:
    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._next_id = 1

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise RCONConnectionError(
                f"Cannot connect to {self._host}:{self._port} — {exc}"
            ) from exc

        packet_id = self._next_id
        self._next_id += 1
        await self._send(_pack_packet(packet_id, _TYPE_AUTH, self._password))

        resp_id, _, _ = await self._recv_packet()
        if resp_id == -1:
            raise RCONAuthError("RCON authentication failed — wrong password.")

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
            self._reader = None

    async def execute(self, command: str) -> str:
        if not self._writer or not self._reader:
            raise RCONConnectionError("Not connected. Call connect() first.")

        packet_id = self._next_id
        self._next_id += 1
        await self._send(_pack_packet(packet_id, _TYPE_EXECCOMMAND, command))
        _, _, body = await self._recv_packet()
        return body

    async def add_admin(self, steam_id: str, permission: str = "@css/root") -> str:
        return await self.execute(f"css_addadmin {steam_id} {permission}")

    async def change_map(self, map_name: str) -> str:
        return await self.execute(f"changelevel {map_name}")

    async def kick_player(self, target: str, reason: str = "") -> str:
        cmd = f"css_kick {target}" if not reason else f"css_kick {target} {reason}"
        return await self.execute(cmd)

    async def ban_player(self, steam_id: str, duration_minutes: int = 0) -> str:
        return await self.execute(f"css_ban {steam_id} {duration_minutes}")

    async def broadcast(self, message: str) -> str:
        return await self.execute(f"say {message}")

    async def _send(self, packet: bytes) -> None:
        if self._writer is None:
            raise RCONConnectionError("Not connected. Call connect() first.")
        try:
            self._writer.write(packet)
            await asyncio.wait_for(self._writer.drain(), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise RCONConnectionError("RCON send timed out.") from exc
        except (OSError, ConnectionError) as exc:
            raise RCONConnectionError(f"RCON send failed: {exc}") from exc

    async def _recv_packet(self) -> tuple[int, int, str]:
        if self._reader is None:
            raise RCONConnectionError("Not connected. Call connect() first.")
        try:
            size_data = await asyncio.wait_for(
                self._reader.readexactly(4), timeout=self._timeout
            )
            size = struct.unpack("<i", size_data)[0]
            if size < 10 or size > 4_194_304:  # 4 MiB safety ceiling
                raise RCONConnectionError(f"RCON returned malformed size: {size}")
            payload = await asyncio.wait_for(
                self._reader.readexactly(size), timeout=self._timeout
            )
        except asyncio.TimeoutError as exc:
            raise RCONConnectionError("RCON receive timed out.") from exc
        except asyncio.IncompleteReadError as exc:
            raise RCONConnectionError("RCON connection closed unexpectedly.") from exc
        except struct.error as exc:
            raise RCONConnectionError(f"RCON header malformed: {exc}") from exc
        except (OSError, ConnectionError) as exc:
            raise RCONConnectionError(f"RCON receive failed: {exc}") from exc

        try:
            return _parse_payload(payload)
        except struct.error as exc:
            raise RCONConnectionError(f"RCON payload malformed: {exc}") from exc

    async def __aenter__(self) -> RCONClient:
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


def _pack_packet(packet_id: int, packet_type: int, body: str) -> bytes:
    body_bytes = body.encode("utf-8") + b"\x00\x00"
    size = 4 + 4 + len(body_bytes)
    return struct.pack("<iii", size, packet_id, packet_type) + body_bytes


def _parse_payload(payload: bytes) -> tuple[int, int, str]:
    """
    Inverse of _pack_packet on the bytes AFTER the 4-byte size prefix.

    Layout: [id:int32 LE][type:int32 LE][body utf-8][\\x00][\\x00]
    """
    if len(payload) < 8:
        raise struct.error(f"RCON payload too short: {len(payload)} bytes")
    packet_id = struct.unpack("<i", payload[:4])[0]
    packet_type = struct.unpack("<i", payload[4:8])[0]
    body = payload[8:].rstrip(b"\x00").decode("utf-8", errors="replace")
    return packet_id, packet_type, body
