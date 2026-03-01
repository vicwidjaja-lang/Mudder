"""
MudBridgeServer - holds one shared MUD connection and fans output to all
local bridge clients (main.py --bridge, mud_mcp_server with use_bridge).

Architecture:
    MUD Server (dsl-mud.org:4000)
           |
     TelnetClient (IAC handling, rate limiting)
           |
     MudBridgeServer
     - TriggerManager (auto-login fires HERE only)
     - GameState
     - TCP listener localhost:4001
         |               |
   main.py --bridge    mud_mcp_server.py
     BridgeClient       BridgeClient (use_bridge=True)
"""

import asyncio
import logging
import re
import sys
from collections import deque
from pathlib import Path
from typing import List, Optional

from .client import TelnetClient
from .game_state import GameState
from .triggers import TriggerManager

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mKHJABCDsuhl]")

logger = logging.getLogger(__name__)


class MudBridgeServer:
    def __init__(
        self,
        mud_host: str,
        mud_port: int,
        bridge_host: str = "127.0.0.1",
        bridge_port: int = 4001,
        config_dir: str = "configs",
        rate_limit: float = 0.5,
        client_idle_timeout: float = 1800.0,
        quiet: bool = False,
    ):
        self.mud_host = mud_host
        self.mud_port = mud_port
        self.bridge_host = bridge_host
        self.bridge_port = bridge_port
        self.config_dir = config_dir
        self.client_idle_timeout = client_idle_timeout

        self.client = TelnetClient(mud_host, mud_port, rate_limit)
        self.client.on_data(self._on_mud_data)
        self.triggers = TriggerManager(config_dir)
        self.state = GameState()
        self.quiet = quiet

        self._clients: List[asyncio.StreamWriter] = []
        self._send_queue: Optional[asyncio.Queue[str]] = None

        # Rolling log: ~10 pages of ANSI-stripped output for LLM consumption
        self._log_buffer: deque[str] = deque(maxlen=4500)
        self._log_path = Path(config_dir) / "bridge_output.log"
        self._log_dirty = False

    async def start(self) -> None:
        """Start local bridge server and keep upstream MUD link healthy."""
        # Bind queue to the active loop used by asyncio.run().
        self._send_queue = asyncio.Queue()

        server = await asyncio.start_server(
            self._handle_client_connection,
            self.bridge_host,
            self.bridge_port,
        )
        print(f"[BRIDGE] Bridge listening on {self.bridge_host}:{self.bridge_port}")

        mud_task = asyncio.create_task(self._mud_loop(), name="bridge_mud_loop")
        send_task = asyncio.create_task(self._send_loop(), name="bridge_send_loop")
        log_task  = asyncio.create_task(self._log_flush_loop(), name="bridge_log_flush")

        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            self._flush_log()  # final write
            await self.client.disconnect()
            for t in (mud_task, send_task, log_task):
                t.cancel()
            await asyncio.gather(mud_task, send_task, log_task, return_exceptions=True)

    async def _mud_loop(self) -> None:
        """
        Keep the upstream telnet session alive.
        If the MUD drops, reconnect with bounded backoff.
        """
        backoff = 1.0
        while True:
            try:
                logger.info("Connecting to MUD %s:%s ...", self.mud_host, self.mud_port)
                await self.client.connect()
                print(f"[BRIDGE] Connected to MUD {self.mud_host}:{self.mud_port}")
                backoff = 1.0
                await self.client.read_loop()
                logger.warning("Upstream connection ended; reconnecting.")
            except asyncio.CancelledError:
                break
            except ConnectionError as exc:
                logger.warning("Upstream connect failed: %s", exc)
            except Exception as exc:
                logger.exception("Unexpected upstream error: %s", exc)
            finally:
                await self.client.disconnect()

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

    def _on_mud_data(self, text: str) -> None:
        """Sync callback from TelnetClient.read_loop — fan out to all clients."""
        encoded = text.encode("utf-8", errors="replace")
        loop = asyncio.get_running_loop()

        dead = []
        for writer in self._clients:
            try:
                writer.write(encoded)
                # Schedule drain; exceptions are caught inside _safe_drain so
                # a dead/slow client never propagates to the event loop handler.
                loop.create_task(self._safe_drain(writer))
            except Exception as exc:
                logger.warning("Dead bridge client, removing: %s", exc)
                dead.append(writer)

        for w in dead:
            self._clients.remove(w)

        # Print MUD output to bridge terminal for monitoring (suppressed in quiet mode)
        if not self.quiet:
            sys.stdout.write(text)
            sys.stdout.flush()

        # Buffer ANSI-stripped output for LLM log file
        clean = _ANSI_RE.sub("", text)
        for line in clean.splitlines(keepends=True):
            self._log_buffer.append(line)
        self._log_dirty = True

        # Parse state and fire triggers on the bridge side
        self.state.parse(text)
        asyncio.get_event_loop().create_task(self._process_triggers(text))

    async def _safe_drain(self, writer: asyncio.StreamWriter) -> None:
        """Drain a client writer; silently remove it on any error."""
        try:
            await asyncio.wait_for(writer.drain(), timeout=10.0)
        except Exception as exc:
            logger.warning("Bridge client drain failed, removing: %s", exc)
            if writer in self._clients:
                self._clients.remove(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def _process_triggers(self, text: str) -> None:
        """Async trigger processing — fires auto-login and other triggers."""
        if self._send_queue is None:
            return
        for cmd, delay, action, threshold in self.triggers.process(text):
            if delay > 0:
                await asyncio.sleep(delay)
            if action == "send":
                for part in cmd.split(";"):
                    part = part.strip()
                    if part:
                        await self._send_queue.put(part)

    async def _handle_client_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a new bridge client connecting on :4001."""
        peer = writer.get_extra_info("peername", "unknown")
        logger.info("Bridge client connected: %s", peer)
        self._clients.append(writer)

        try:
            while True:
                if self.client_idle_timeout and self.client_idle_timeout > 0:
                    line = await asyncio.wait_for(
                        reader.readline(), timeout=self.client_idle_timeout
                    )
                else:
                    line = await reader.readline()
                if not line:
                    break
                cmd = line.decode("utf-8", errors="replace").rstrip("\r\n")
                # Forward blank lines too: DSL menus/pagers often require Enter.
                if self._send_queue is not None:
                    await self._send_queue.put(cmd)
        except asyncio.TimeoutError:
            logger.info(
                "Bridge client idle timeout (%ss): %s",
                self.client_idle_timeout,
                peer,
            )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Bridge client error (%s): %s", peer, exc)
        finally:
            if writer in self._clients:
                self._clients.remove(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("Bridge client disconnected: %s", peer)

    def _flush_log(self) -> None:
        """Write the log buffer to disk."""
        if not self._log_dirty:
            return
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_path.write_text("".join(self._log_buffer), encoding="utf-8")
            self._log_dirty = False
        except Exception as exc:
            logger.warning("Log flush failed: %s", exc)

    async def _log_flush_loop(self) -> None:
        """Flush the log buffer to disk every 5 seconds."""
        while True:
            try:
                await asyncio.sleep(5)
                self._flush_log()
            except asyncio.CancelledError:
                break

    async def _send_loop(self) -> None:
        """Drain the send queue through TelnetClient.send() (rate-limited)."""
        if self._send_queue is None:
            return
        while True:
            try:
                cmd = await self._send_queue.get()
                await self.client.send(cmd)
                self._send_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Send loop error: %s", exc)
