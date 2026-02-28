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
import sys
from typing import List, Optional

from .client import TelnetClient
from .game_state import GameState
from .triggers import TriggerManager

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
        quiet: bool = False,
    ):
        self.mud_host = mud_host
        self.mud_port = mud_port
        self.bridge_host = bridge_host
        self.bridge_port = bridge_port
        self.config_dir = config_dir

        self.client = TelnetClient(mud_host, mud_port, rate_limit)
        self.triggers = TriggerManager(config_dir)
        self.state = GameState()
        self.quiet = quiet

        self._clients: List[asyncio.StreamWriter] = []
        self._send_queue: Optional[asyncio.Queue[str]] = None

    async def start(self) -> None:
        """Connect to MUD, start bridge TCP server, run until cancelled."""
        # Bind queue to the active loop used by asyncio.run().
        self._send_queue = asyncio.Queue()
        logger.info("Connecting to MUD %s:%s ...", self.mud_host, self.mud_port)
        try:
            await self.client.connect()
        except ConnectionError as exc:
            print(f"[BRIDGE] Connection failed: {exc}")
            return

        print(f"[BRIDGE] Connected to MUD {self.mud_host}:{self.mud_port}")

        self.client.on_data(self._on_mud_data)

        server = await asyncio.start_server(
            self._handle_client_connection,
            self.bridge_host,
            self.bridge_port,
        )
        print(f"[BRIDGE] Bridge listening on {self.bridge_host}:{self.bridge_port}")

        read_task = asyncio.create_task(self.client.read_loop(), name="bridge_mud_read")
        send_task = asyncio.create_task(self._send_loop(), name="bridge_send_loop")

        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            await self.client.disconnect()
            read_task.cancel()
            send_task.cancel()
            await asyncio.gather(read_task, send_task, return_exceptions=True)

    def _on_mud_data(self, text: str) -> None:
        """Sync callback from TelnetClient.read_loop — fan out to all clients."""
        encoded = text.encode("utf-8", errors="replace")

        dead = []
        for writer in self._clients:
            try:
                writer.write(encoded)
                # schedule drain without blocking the callback
                asyncio.get_event_loop().create_task(writer.drain())
            except Exception as exc:
                logger.warning("Dead bridge client, removing: %s", exc)
                dead.append(writer)

        for w in dead:
            self._clients.remove(w)

        # Print MUD output to bridge terminal for monitoring (suppressed in quiet mode)
        if not self.quiet:
            sys.stdout.write(text)
            sys.stdout.flush()

        # Parse state and fire triggers on the bridge side
        self.state.parse(text)
        asyncio.get_event_loop().create_task(self._process_triggers(text))

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
                line = await asyncio.wait_for(reader.readline(), timeout=300)
                if not line:
                    break
                cmd = line.decode("utf-8", errors="replace").rstrip("\r\n")
                # Forward blank lines too: DSL menus/pagers often require Enter.
                if self._send_queue is not None:
                    await self._send_queue.put(cmd)
        except asyncio.TimeoutError:
            logger.info("Bridge client idle timeout: %s", peer)
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
