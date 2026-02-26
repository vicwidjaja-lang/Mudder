"""
MudApp - orchestrates the full MUD client session.

  - Connects via TelnetClient
  - Displays game output (ANSI colours pass-through)
  - Processes input: expands aliases, handles #commands, sends to server
  - Runs trigger processing on each block of received text
  - Runs skills auto-use loop (disabled by default)
  - Runs script engine for automated leveling/routing (disabled by default)

Built-in client commands (prefix #):
  #help                        - show this list
  #alias <name> <cmd>          - add or update an alias
  #alias <name>                - remove an alias
  #aliases                     - list aliases
  #trigger <name> <pat> <cmd>  - add trigger (name/pattern/command)
  #triggers                    - list triggers
  #trigger <name> on|off       - enable / disable trigger
  #trigdel <name>              - delete trigger
  #skills                      - list skills
  #skill <name> on|off         - enable / disable skill
  #autoskill on|off            - toggle auto-skill loop
  #state                       - show parsed game state
  #save                        - save all configs
  #quit                        - quit client

Script commands (prefix #script):
  #script load [file]          - load script config (default: configs/scripts.json)
  #script start <route>        - start running a named route
  #script pause                - pause execution
  #script resume               - resume after pause
  #script stop                 - stop and return to idle
  #script status               - show current script state
  #script routes               - list available routes in loaded config
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

from .aliases import AliasManager
from .client import TelnetClient
from .game_state import GameState
from .script_engine import ScriptEngine
from .skills import SkillsManager
from .triggers import TriggerManager

logger = logging.getLogger(__name__)

# ANSI reset for client messages so they stand out
_CLIENT = "\033[1;36m[CLIENT]\033[0m "
_WARN   = "\033[1;33m[WARN]\033[0m "
_ERR    = "\033[1;31m[ERR]\033[0m "


class MudApp:
    def __init__(
        self,
        host: str,
        port: int,
        config_dir: str,
        rate_limit: float = 0.5,
    ):
        self.client   = TelnetClient(host, port, rate_limit)
        self.aliases  = AliasManager(config_dir)
        self.triggers = TriggerManager(config_dir)
        self.skills   = SkillsManager(config_dir)
        self.state    = GameState()
        self._running = False
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._config_dir = config_dir
        self.script = ScriptEngine(
            enqueue=self._enqueue,
            get_state=lambda: self.state,
            print_fn=self._print,
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._print(f"Connecting to {self.client.host}:{self.client.port} ...")

        try:
            await self.client.connect()
        except ConnectionError as exc:
            self._print(f"{_ERR}{exc}")
            return

        self._print(f"{_CLIENT}Connected!  Type  #help  for client commands.\n")
        self._running = True

        # Register output callback
        self.client.on_data(self._on_server_data)

        # Launch background tasks
        read_task   = asyncio.create_task(self.client.read_loop(),   name="read_loop")
        send_task   = asyncio.create_task(self._send_loop(),          name="send_loop")
        skills_task = asyncio.create_task(self._skills_loop(),        name="skills_loop")
        script_task = asyncio.create_task(self.script.run(),          name="script_loop")

        # Input loop (runs in thread pool so it doesn't block the event loop)
        try:
            await self._input_loop()
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            self._running = False
            await self.client.disconnect()
            for task in (read_task, send_task, skills_task, script_task):
                task.cancel()
            await asyncio.gather(read_task, send_task, skills_task, script_task, return_exceptions=True)
            self._print(f"\n{_CLIENT}Goodbye.")

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    async def _input_loop(self) -> None:
        """Read lines from stdin (non-blocking via executor)."""
        loop = asyncio.get_event_loop()

        # Try to use prompt_toolkit for a nicer experience; fall back to plain
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
            from prompt_toolkit.patch_stdout import patch_stdout

            session = PromptSession(
                history=FileHistory(".mud_history"),
                auto_suggest=AutoSuggestFromHistory(),
            )
            with patch_stdout():
                while self._running and self.client.connected:
                    try:
                        line = await session.prompt_async("> ")
                        await self._handle_input(line)
                    except (EOFError, KeyboardInterrupt):
                        break
        except ImportError:
            # Plain readline fallback
            while self._running and self.client.connected:
                try:
                    line = await loop.run_in_executor(None, self._readline_prompt)
                    if line is None:
                        break
                    await self._handle_input(line)
                except (EOFError, KeyboardInterrupt):
                    break

    @staticmethod
    def _readline_prompt() -> Optional[str]:
        try:
            return input("> ")
        except EOFError:
            return None

    async def _handle_input(self, line: str) -> None:
        line = line.strip()
        if not line:
            await self.client.send("")
            return

        if line.startswith("#"):
            await self._handle_client_cmd(line[1:])
        else:
            expanded = self.aliases.expand(line)
            await self.client.send(expanded)

    # ------------------------------------------------------------------
    # Server output
    # ------------------------------------------------------------------

    def _on_server_data(self, text: str) -> None:
        """Sync callback called from read_loop."""
        # Print immediately (prompt_toolkit patch_stdout handles ordering)
        sys.stdout.write(text)
        sys.stdout.flush()

        # Schedule async processing
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._process_output(text))
        except RuntimeError:
            pass  # event loop gone

    async def _process_output(self, text: str) -> None:
        """Update state and fire triggers based on received text."""
        self.state.parse(text)
        self.script.on_output(text)

        for cmd, delay in self.triggers.process(text):
            if delay > 0:
                await asyncio.sleep(delay)
            await self._enqueue(cmd)

    # ------------------------------------------------------------------
    # Send queue (serialises outgoing commands)
    # ------------------------------------------------------------------

    async def _enqueue(self, cmd: str) -> None:
        await self._send_queue.put(cmd)

    async def _send_loop(self) -> None:
        """Drain the send queue, respecting the client rate limit."""
        while True:
            try:
                cmd = await self._send_queue.get()
                await self.client.send(cmd)
                self._send_queue.task_done()
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Skills loop
    # ------------------------------------------------------------------

    async def _skills_loop(self) -> None:
        """Periodically fire the highest-priority ready skill."""
        while True:
            try:
                await asyncio.sleep(self.skills.cycle_interval)
                cmd = self.skills.next_skill(self.state)
                if cmd:
                    self._print(f"{_CLIENT}[auto-skill] {cmd}")
                    await self._enqueue(cmd)
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Built-in client commands
    # ------------------------------------------------------------------

    async def _handle_client_cmd(self, raw: str) -> None:
        parts = raw.strip().split(maxsplit=3)
        if not parts:
            return
        cmd = parts[0].lower()

        # ---- help ----
        if cmd == "help":
            self._print(__doc__ or "")

        # ---- alias management ----
        elif cmd == "alias":
            if len(parts) >= 3:
                name, expansion = parts[1], " ".join(parts[2:])
                self.aliases.add(name, expansion)
                self._print(f"{_CLIENT}Alias set: {name!r} -> {expansion!r}")
            elif len(parts) == 2:
                removed = self.aliases.remove(parts[1])
                if removed:
                    self._print(f"{_CLIENT}Alias {parts[1]!r} removed.")
                else:
                    self._print(f"{_CLIENT}No alias named {parts[1]!r}.")
            else:
                self._print(f"{_CLIENT}Usage: #alias <name> <expansion>  or  #alias <name>  to remove")

        elif cmd == "aliases":
            als = self.aliases.list_aliases()
            if als:
                lines = ["Aliases:"] + [f"  {k:<12} -> {v}" for k, v in sorted(als.items())]
                self._print("\n".join(lines))
            else:
                self._print(f"{_CLIENT}No aliases defined.")

        # ---- trigger management ----
        elif cmd == "trigger":
            if len(parts) >= 4:
                name, pattern, command = parts[1], parts[2], parts[3]
                self.triggers.add(name, pattern, command)
                self._print(f"{_CLIENT}Trigger {name!r} added.  Pattern: {pattern!r}")
            elif len(parts) == 3 and parts[2].lower() in ("on", "off"):
                enabled = parts[2].lower() == "on"
                ok = self.triggers.set_enabled(parts[1], enabled)
                status = "enabled" if enabled else "disabled"
                self._print(
                    f"{_CLIENT}Trigger {parts[1]!r} {status}."
                    if ok else f"{_ERR}Unknown trigger: {parts[1]!r}"
                )
            else:
                self._print(f"{_CLIENT}Usage: #trigger <name> <pattern> <cmd>  or  #trigger <name> on|off")

        elif cmd == "triggers":
            ts = self.triggers.list_triggers()
            if ts:
                lines = ["Triggers:"]
                for t in ts:
                    flag = "ON " if t.enabled else "OFF"
                    lines.append(f"  [{flag}] {t.name:<20} pat={t.pattern!r}  cmd={t.command!r}")
                self._print("\n".join(lines))
            else:
                self._print(f"{_CLIENT}No triggers defined.")

        elif cmd == "trigdel":
            if len(parts) >= 2:
                ok = self.triggers.remove(parts[1])
                self._print(
                    f"{_CLIENT}Trigger {parts[1]!r} deleted."
                    if ok else f"{_ERR}Unknown trigger: {parts[1]!r}"
                )

        # ---- skill management ----
        elif cmd == "skills":
            sl = self.skills.list_skills()
            auto = "ON" if self.skills.auto_skill else "OFF"
            lines = [f"Auto-skill: {auto}  (interval {self.skills.cycle_interval}s)", "Skills:"]
            for s in sl:
                flag = "ON " if s.enabled else "OFF"
                lines.append(
                    f"  [{flag}] pri={s.priority}  {s.name:<16} cmd={s.command!r}"
                    f"  cd={s.cooldown}s"
                )
            self._print("\n".join(lines))

        elif cmd == "skill":
            if len(parts) == 3 and parts[2].lower() in ("on", "off"):
                enabled = parts[2].lower() == "on"
                ok = self.skills.set_enabled(parts[1], enabled)
                status = "enabled" if enabled else "disabled"
                self._print(
                    f"{_CLIENT}Skill {parts[1]!r} {status}."
                    if ok else f"{_ERR}Unknown skill: {parts[1]!r}"
                )
            else:
                self._print(f"{_CLIENT}Usage: #skill <name> on|off")

        elif cmd == "autoskill":
            if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
                self.skills.auto_skill = parts[1].lower() == "on"
                self.skills.save()
                status = "enabled" if self.skills.auto_skill else "disabled"
                self._print(f"{_CLIENT}Auto-skill {status}.")
            else:
                self._print(f"{_CLIENT}Usage: #autoskill on|off")

        # ---- game state ----
        elif cmd == "state":
            s = self.state
            self._print(
                f"HP: {s.hp}/{s.max_hp} ({s.hp_percent():.0f}%)  "
                f"Mana: {s.mana}/{s.max_mana} ({s.mana_percent():.0f}%)  "
                f"MV: {s.mv}/{s.max_mv}  "
                f"Combat: {'YES' if s.in_combat else 'no'}  "
                f"HP-crit: {s.hp_critical}"
            )

        # ---- save ----
        elif cmd == "save":
            self.aliases.save()
            self.triggers.save()
            self.skills.save()
            self._print(f"{_CLIENT}Configs saved.")

        # ---- quit ----
        elif cmd in ("quit", "exit", "q"):
            self._running = False
            await self.client.disconnect()

        # ---- script engine ----
        elif cmd == "script":
            sub = parts[1].lower() if len(parts) >= 2 else ""

            if sub == "load":
                path = Path(parts[2]) if len(parts) >= 3 else Path(self._config_dir) / "scripts.json"
                err = self.script.load(path)
                if err:
                    self._print(f"{_ERR}{err}")

            elif sub == "start":
                if len(parts) < 3:
                    self._print(f"{_CLIENT}Usage: #script start <route>")
                else:
                    err = self.script.start(parts[2])
                    if err:
                        self._print(f"{_ERR}{err}")

            elif sub == "pause":
                self.script.pause()

            elif sub == "resume":
                self.script.resume()

            elif sub == "stop":
                self.script.stop()

            elif sub == "status":
                self._print(self.script.status_str())

            elif sub == "routes":
                self._print(self.script.routes_str())

            else:
                self._print(
                    f"{_CLIENT}Script subcommands: load, start, pause, resume, stop, status, routes"
                )

        else:
            self._print(f"{_ERR}Unknown client command: #{cmd}  (try #help)")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _print(msg: str) -> None:
        print(msg, flush=True)
