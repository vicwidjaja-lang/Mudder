#!/usr/bin/env python3
"""
MUD MCP Server
==============
Exposes the MUD client as MCP tools so Claude can play through chat.

STDIO transport — stdout is used for JSON-RPC, never for debug output.
All logging goes to stderr.

── Claude Desktop setup ───────────────────────────────────────────────────────
Add to ~/Library/Application Support/Claude/claude_desktop_config.json
(macOS) or %APPDATA%\\Claude\\claude_desktop_config.json (Windows):

{
  "mcpServers": {
    "mud-player": {
      "command": "python",
      "args": ["/ABSOLUTE/PATH/TO/Mudder/mud_mcp_server.py"]
    }
  }
}

── Tools exposed ──────────────────────────────────────────────────────────────
  mud_connect      Connect to the MUD server
  mud_login        Send stored login credentials
  mud_send         Send any command to the MUD
  mud_read         Read buffered game output
  mud_wait         Wait up to N seconds for new output to arrive
  mud_status       Connection state + parsed HP/mana/combat info
  mud_aliases      List configured aliases
  mud_set_alias    Add / update an alias
  mud_skills       List skills + auto-skill status
  mud_skill_toggle Enable or disable a skill
  mud_autoskill    Turn the auto-skill loop on / off
  mud_disconnect   Disconnect cleanly
"""

import asyncio
import json
import logging
import os
import re
import sys
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

# ── Logging to stderr only (stdout is JSON-RPC) ───────────────────────────────
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [MUD-MCP] %(levelname)s %(message)s",
)
log = logging.getLogger("mud_mcp")

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(_HERE))
CONFIG_DIR = str(_HERE / "configs")

from mud.client import TelnetClient
from mud.bridge_client import BridgeClient
from mud.game_state import GameState
from mud.aliases import AliasManager
from mud.skills import SkillsManager
from mud.triggers import TriggerManager

# ── ANSI stripper ─────────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mKHJABCDsuhl]")

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ── Output ring buffer ────────────────────────────────────────────────────────
# Lines are stored as plain text (ANSI stripped).
# We track an absolute "total added" counter so the cursor works even
# as old lines fall off the deque.

_output: deque[str]          # initialised in lifespan
_total_added: int = 0        # absolute count of lines ever added
_read_cursor: int = 0        # Claude has read up to this absolute index
_new_output_ev: asyncio.Event


def _buffer_text(raw: str) -> None:
    """Append decoded MUD text to the output buffer (called from asyncio)."""
    global _total_added
    clean = _strip_ansi(raw)
    for line in clean.splitlines(keepends=True):
        _output.append(line)
        _total_added += 1
    if clean.strip():
        _new_output_ev.set()


def _get_unread_lines(max_lines: int = 200, advance: bool = True) -> list[str]:
    global _read_cursor
    oldest_abs = _total_added - len(_output)          # absolute idx of _output[0]
    rel = max(0, _read_cursor - oldest_abs)           # offset inside deque
    unread = list(_output)[rel:]
    if max_lines:
        unread = unread[:max_lines]
    if advance:
        _read_cursor = oldest_abs + rel + len(unread)
    return unread


def _all_recent_lines(max_lines: int = 200) -> list[str]:
    lines = list(_output)
    return lines[-max_lines:] if len(lines) > max_lines else lines


# ── Bridge mode flag ──────────────────────────────────────────────────────────
_use_bridge: bool = False

# ── Connection state ──────────────────────────────────────────────────────────
_client: Optional[TelnetClient] = None
_state: GameState
_aliases: AliasManager
_skills: SkillsManager
_triggers: TriggerManager
_read_task: Optional[asyncio.Task] = None
_skills_task: Optional[asyncio.Task] = None


def _on_server_data(text: str) -> None:
    """Sync callback from TelnetClient/BridgeClient.read_loop (runs inside event loop)."""
    _buffer_text(text)
    _state.parse(text)
    # Trigger auto-responses — only fire on direct connections; bridge handles triggers itself
    if not _use_bridge:
        for cmd, delay, action, threshold in _triggers.process(text):
            if action == "send":
                asyncio.get_event_loop().create_task(_delayed_send(cmd, delay))


async def _delayed_send(cmd: str, delay: float) -> None:
    if delay > 0:
        await asyncio.sleep(delay)
    if _client and _client.connected:
        for part in cmd.split(";"):
            await _client.send(part.strip())
            log.info("[trigger] sent: %r", part.strip())


async def _skills_loop() -> None:
    """Background task: fire highest-priority ready skill every cycle."""
    while _client and _client.connected:
        await asyncio.sleep(_skills.cycle_interval)
        cmd = _skills.next_skill(_state)
        if cmd and _client and _client.connected:
            log.info("[auto-skill] %r", cmd)
            await _client.send(cmd)


async def _stop_bg_tasks() -> None:
    global _read_task, _skills_task
    for task in (_read_task, _skills_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _read_task = None
    _skills_task = None


# ── FastMCP setup with lifespan ───────────────────────────────────────────────
from mcp.server.fastmcp import FastMCP


@asynccontextmanager
async def lifespan(server: FastMCP):
    global _output, _new_output_ev, _state, _aliases, _skills, _triggers
    _output        = deque(maxlen=1000)
    _new_output_ev = asyncio.Event()
    _state         = GameState()
    _aliases       = AliasManager(CONFIG_DIR)
    _skills        = SkillsManager(CONFIG_DIR)
    _triggers      = TriggerManager(CONFIG_DIR)
    log.info("MUD MCP Server initialised (config: %s)", CONFIG_DIR)
    yield
    # Cleanup on shutdown
    if _client and _client.connected:
        await _client.disconnect()
    await _stop_bg_tasks()
    log.info("MUD MCP Server shut down.")


mcp = FastMCP("MUD Player", lifespan=lifespan)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def mud_connect(
    host: str = "dsl-mud.org",
    port: int = 4000,
    use_bridge: bool = True,
    bridge_host: str = "127.0.0.1",
    bridge_port: int = 4001,
) -> str:
    """
    Connect to the MUD server.
    Call this first. After connecting, call mud_read() to see the login prompt,
    then mud_login() or mud_send() to authenticate.

    Set use_bridge=True to connect via the local bridge server (bridge.py) instead
    of opening a direct connection. This lets Claude share a session with the human
    player. bridge_host/bridge_port default to 127.0.0.1:4001.
    """
    global _client, _read_task, _skills_task, _total_added, _read_cursor, _use_bridge
    if _client and _client.connected:
        return "Already connected. Call mud_status() to check state."

    _use_bridge = use_bridge
    if use_bridge:
        _client = BridgeClient(bridge_host, bridge_port)
        log.info("Using bridge at %s:%s", bridge_host, bridge_port)
    else:
        _client = TelnetClient(host, port, rate_limit=0.6)
        log.info("Connecting directly to %s:%s", host, port)

    _client.on_data(_on_server_data)
    _output.clear()
    _total_added = 0
    _read_cursor = 0

    try:
        await _client.connect()
    except ConnectionError as exc:
        _client = None
        return f"Connection failed: {exc}"

    await _stop_bg_tasks()
    _read_task   = asyncio.create_task(_client.read_loop(), name="mud_read_loop")
    _skills_task = asyncio.create_task(_skills_loop(),      name="mud_skills_loop")

    if use_bridge:
        log.info("Connected to bridge %s:%s", bridge_host, bridge_port)
        await asyncio.sleep(0.5)
        greeting = "".join(_get_unread_lines(100, advance=False))
        _read_cursor = _total_added
        return (
            f"Connected to bridge at {bridge_host}:{bridge_port}.\n\n"
            f"--- Recent bridge output ---\n{greeting}"
        )
    else:
        log.info("Connected to %s:%s", host, port)
        # Give the server a moment to send its greeting
        await asyncio.sleep(1.5)
        greeting = "".join(_get_unread_lines(100, advance=False))
        _read_cursor = _total_added  # mark all as read so next mud_read() shows new content
        return f"Connected to {host}:{port}.\n\n--- Server greeting ---\n{greeting}"


@mcp.tool()
async def mud_login(name: str = "", password: str = "") -> str:
    """
    Send the login credentials to the MUD.
    If name/password are empty, reads them from configs/credentials.json.
    Call mud_read() after this to see the result.
    """
    if not (_client and _client.connected):
        return "Not connected. Call mud_connect() first."

    cred_path = Path(CONFIG_DIR) / "credentials.json"
    if not name and cred_path.exists():
        creds = json.loads(cred_path.read_text())
        name     = creds.get("name", "")
        password = creds.get("password", "")

    if not name or not password:
        return "No credentials. Pass name/password or create configs/credentials.json."

    # Send name, wait, send password
    await _client.send(name)
    await asyncio.sleep(1.0)
    await _client.send(password)
    await asyncio.sleep(2.0)
    return f"Sent login for '{name}'. Call mud_read() to see the result."


@mcp.tool()
async def mud_cmd(command: str, wait: float = 2.0) -> str:
    """
    Send a command and return the response in one step.
    Prefer this over mud_send + mud_wait + mud_read — it's 3x faster.
    Use semicolons to send multiple commands: 'get sword;wear sword'
    """
    if not (_client and _client.connected):
        return "Not connected. Call mud_connect() first."
    _new_output_ev.clear()
    for part in command.split(";"):
        expanded = _aliases.expand(part.strip())
        ok = await _client.send(expanded)
        if not ok:
            return "Send failed — connection may have dropped."
    try:
        await asyncio.wait_for(_new_output_ev.wait(), timeout=wait)
    except asyncio.TimeoutError:
        pass
    return "".join(_get_unread_lines(200)) or "(no response)"


@mcp.tool()
async def mud_send(command: str) -> str:
    """
    Send a command to the MUD (with alias expansion).
    Examples: 'look', 'n', 'kill goblin', 'score', 'inventory'
    """
    if not (_client and _client.connected):
        return "Not connected. Call mud_connect() first."
    results = []
    for part in command.split(";"):
        expanded = _aliases.expand(part.strip())
        ok = await _client.send(expanded)
        if not ok:
            return "Send failed — connection may have dropped."
        log.info("Sent: %r (expanded from %r)", expanded, part.strip())
        results.append(expanded)
    return "Sent: " + " ; ".join(f"{r!r}" for r in results)


@mcp.tool()
async def mud_read(max_lines: int = 80, include_all: bool = False) -> str:
    """
    Read output from the MUD.

    By default returns only lines not yet read by Claude (like a stream).
    Set include_all=True to re-read all buffered output (last 200 lines).
    Call mud_wait() first if you expect more text to arrive.
    """
    if not (_client and _client.connected):
        if _total_added > 0:
            pass  # allow reading buffer even after disconnect
        else:
            return "Not connected and no buffered output."

    if include_all:
        lines = _all_recent_lines(max_lines)
    else:
        lines = _get_unread_lines(max_lines)

    if not lines:
        return "(no new output — try mud_wait() then mud_read() again)"

    return "".join(lines)


@mcp.tool()
async def mud_wait(seconds: float = 3.0) -> str:
    """
    Wait up to `seconds` for new MUD output to arrive, then return.
    Use before mud_read() when you expect the server to respond to a command.
    """
    _new_output_ev.clear()
    try:
        await asyncio.wait_for(_new_output_ev.wait(), timeout=seconds)
        return "New output is available. Call mud_read() now."
    except asyncio.TimeoutError:
        return f"No new output arrived within {seconds}s."


@mcp.tool()
async def mud_status() -> str:
    """
    Return connection status and the latest parsed game state
    (HP, mana, movement, combat flag).
    """
    connected = bool(_client and _client.connected)
    s = _state
    lines = [
        f"Connected : {'YES' if connected else 'NO'}",
        f"HP        : {s.hp}/{s.max_hp}  ({s.hp_percent():.0f}%)",
        f"Mana      : {s.mana}/{s.max_mana}  ({s.mana_percent():.0f}%)",
        f"Move      : {s.mv}/{s.max_mv}",
        f"In combat : {'YES' if s.in_combat else 'no'}",
        f"HP crit   : {s.hp_critical}",
        f"Buffered  : {len(_output)} lines  ({_total_added} total received)",
    ]
    return "\n".join(lines)


@mcp.tool()
async def mud_aliases() -> str:
    """List all configured command aliases."""
    als = _aliases.list_aliases()
    if not als:
        return "No aliases defined."
    rows = [f"  {k:<14} -> {v}" for k, v in sorted(als.items())]
    return "Aliases:\n" + "\n".join(rows)


@mcp.tool()
async def mud_set_alias(name: str, command: str) -> str:
    """
    Add or update a command alias.
    Example: name='bs', command='backstab' means typing 'bs goblin' sends 'backstab goblin'.
    """
    _aliases.add(name, command)
    return f"Alias set: {name!r} -> {command!r}"


@mcp.tool()
async def mud_skills() -> str:
    """List all configured skills and current auto-skill status."""
    auto = "ON" if _skills.auto_skill else "OFF"
    lines = [f"Auto-skill: {auto}  (every {_skills.cycle_interval}s)", "Skills:"]
    for s in _skills.list_skills():
        flag = "ON " if s.enabled else "OFF"
        lines.append(
            f"  [{flag}] pri={s.priority}  {s.name:<18} cmd={s.command!r}"
            f"  cd={s.cooldown}s"
        )
    return "\n".join(lines)


@mcp.tool()
async def mud_skill_toggle(name: str, enabled: bool) -> str:
    """Enable or disable a skill by name."""
    ok = _skills.set_enabled(name, enabled)
    if not ok:
        names = [s.name for s in _skills.list_skills()]
        return f"Unknown skill {name!r}. Available: {names}"
    status = "enabled" if enabled else "disabled"
    return f"Skill {name!r} {status}."


@mcp.tool()
async def mud_autoskill(enabled: bool) -> str:
    """
    Turn the automatic skill-use loop on or off.
    When on, the highest-priority ready skill fires every cycle.
    Make sure individual skills are also enabled with mud_skill_toggle().
    """
    _skills.auto_skill = enabled
    _skills.save()
    return f"Auto-skill {'enabled' if enabled else 'disabled'}."


@mcp.tool()
async def mud_disconnect() -> str:
    """Disconnect from the MUD server cleanly."""
    if not (_client and _client.connected):
        return "Not connected."
    await _stop_bg_tasks()
    await _client.disconnect()
    return "Disconnected."


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
