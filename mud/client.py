"""
TelnetClient - handles low-level TCP/telnet connection to the MUD server.
Includes rate limiting to avoid flooding / IP bans.
"""

import asyncio
import logging
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# Telnet protocol constants
IAC  = 255
WILL = 251
WONT = 252
DO   = 253
DONT = 254
SB   = 250
SE   = 240
GA   = 249
EOR  = 239
ECHO = 1
NAWS = 31
TTYPE = 24


class TelnetClient:
    """
    Async telnet client with:
    - IAC negotiation handling
    - Rate limiting (min delay between sends)
    - Pluggable output callbacks
    """

    def __init__(self, host: str, port: int, rate_limit: float = 0.5):
        self.host = host
        self.port = port
        # Minimum seconds between outgoing commands to avoid flooding
        self.rate_limit = rate_limit
        self._last_send: float = 0.0
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self.connected: bool = False
        self._output_callbacks: List[Callable[[str], None]] = []

    def on_data(self, callback: Callable[[str], None]) -> None:
        """Register a callback invoked with decoded text from the server."""
        self._output_callbacks.append(callback)

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=15,
            )
            self.connected = True
            logger.info("Connected to %s:%s", self.host, self.port)
        except asyncio.TimeoutError:
            raise ConnectionError(f"Timed out connecting to {self.host}:{self.port}")
        except OSError as exc:
            raise ConnectionError(f"Could not connect: {exc}") from exc

    async def disconnect(self) -> None:
        self.connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        logger.info("Disconnected.")

    async def send(self, text: str) -> bool:
        """
        Send a line to the server, respecting the rate limit.
        Returns True on success, False if not connected.
        """
        if not self.connected or self._writer is None:
            return False

        now = time.monotonic()
        wait = self.rate_limit - (now - self._last_send)
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            self._writer.write((text + "\r\n").encode("utf-8", errors="replace"))
            await self._writer.drain()
            self._last_send = time.monotonic()
            return True
        except Exception as exc:
            logger.error("Send error: %s", exc)
            self.connected = False
            return False

    async def read_loop(self) -> None:
        """Read incoming data forever, stripping telnet codes and calling callbacks."""
        buf = b""
        while self.connected and self._reader is not None:
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=120)
                if not chunk:
                    logger.info("Server closed connection.")
                    break
                buf += chunk
                text, buf = self._process_telnet(buf)
                if text:
                    for cb in self._output_callbacks:
                        try:
                            cb(text)
                        except Exception as exc:
                            logger.error("Output callback error: %s", exc)
            except asyncio.TimeoutError:
                # 2-minute silence — send a blank line as keepalive
                if self.connected:
                    await self.send("")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Read loop error: %s", exc)
                break
        self.connected = False

    # ------------------------------------------------------------------
    # Internal telnet processing
    # ------------------------------------------------------------------

    def _process_telnet(self, data: bytes) -> tuple:
        """
        Strip IAC sequences from raw bytes.
        Returns (decoded_text, leftover_bytes_needing_more_data).
        """
        result = bytearray()
        i = 0
        while i < len(data):
            b = data[i]

            if b != IAC:
                result.append(b)
                i += 1
                continue

            # Need at least one more byte after IAC
            if i + 1 >= len(data):
                return result.decode("utf-8", errors="replace"), data[i:]

            cmd = data[i + 1]

            if cmd == IAC:
                # Escaped literal 0xFF
                result.append(IAC)
                i += 2

            elif cmd in (WILL, WONT, DO, DONT):
                if i + 2 >= len(data):
                    return result.decode("utf-8", errors="replace"), data[i:]
                option = data[i + 2]
                self._negotiate(cmd, option)
                i += 3

            elif cmd == SB:
                # Sub-negotiation — skip until IAC SE
                end = data.find(bytes([IAC, SE]), i + 2)
                if end == -1:
                    return result.decode("utf-8", errors="replace"), data[i:]
                i = end + 2

            elif cmd in (GA, EOR):
                # Go-ahead / end-of-record — treat as whitespace hint
                i += 2

            else:
                # Unknown two-byte sequence
                i += 2

        try:
            return result.decode("utf-8", errors="replace"), b""
        except Exception:
            return result.decode("latin-1", errors="replace"), b""

    def _negotiate(self, cmd: int, option: int) -> None:
        """Respond to WILL/WONT/DO/DONT with sensible defaults."""
        if self._writer is None:
            return
        if cmd == DO:
            # Server requests we do something — decline everything
            self._writer.write(bytes([IAC, WONT, option]))
        elif cmd == WILL:
            # Server offers to do something
            if option == ECHO:
                # Accept server echo (standard)
                self._writer.write(bytes([IAC, DO, option]))
            else:
                self._writer.write(bytes([IAC, DONT, option]))
        # WONT / DONT — no reply needed
