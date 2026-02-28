"""
BridgeClient - drop-in replacement for TelnetClient that connects to the
local MudBridgeServer on TCP :4001 instead of directly to the MUD.

Same public API as TelnetClient:
  connect(), disconnect(), send(), on_data(), read_loop(), connected

No IAC processing needed — the bridge already strips telnet codes.
No rate limiting — the bridge handles that too.
"""

import asyncio
import logging
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


class BridgeClient:
    """
    Connects to the local bridge server (localhost:4001 by default).
    Forwards all received text to registered callbacks, just like TelnetClient.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 4001):
        self.host = host
        self.port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self.connected: bool = False
        self._output_callbacks: List[Callable[[str], None]] = []

    def on_data(self, callback: Callable[[str], None]) -> None:
        """Register a callback invoked with decoded text from the bridge."""
        self._output_callbacks.append(callback)

    async def connect(self) -> None:
        """Open TCP connection to the bridge server."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=5,
            )
            self.connected = True
            logger.info("BridgeClient connected to %s:%s", self.host, self.port)
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"Timed out connecting to bridge at {self.host}:{self.port}. "
                "Is bridge.py running?"
            )
        except OSError as exc:
            raise ConnectionError(
                f"Could not connect to bridge at {self.host}:{self.port}: {exc}. "
                "Is bridge.py running?"
            ) from exc

    async def disconnect(self) -> None:
        self.connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        logger.info("BridgeClient disconnected.")

    async def send(self, text: str) -> bool:
        """
        Send a command to the bridge (newline-terminated).
        No rate limiting — the bridge handles pacing to the MUD.
        Returns True on success, False if not connected.
        """
        if not self.connected or self._writer is None:
            return False
        try:
            self._writer.write((text + "\n").encode("utf-8", errors="replace"))
            await self._writer.drain()
            return True
        except Exception as exc:
            logger.error("BridgeClient send error: %s", exc)
            self.connected = False
            return False

    async def read_loop(self) -> None:
        """Read data from the bridge in 4096-byte chunks, calling callbacks."""
        while self.connected and self._reader is not None:
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=120)
                if not chunk:
                    logger.info("Bridge closed connection.")
                    break
                text = chunk.decode("utf-8", errors="replace")
                for cb in self._output_callbacks:
                    try:
                        cb(text)
                    except Exception as exc:
                        logger.error("BridgeClient callback error: %s", exc)
            except asyncio.TimeoutError:
                # 2-minute silence — send a keepalive blank line
                if self.connected:
                    await self.send("")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("BridgeClient read loop error: %s", exc)
                break
        self.connected = False
