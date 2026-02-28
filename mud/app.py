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
  #log start                   - begin logging entered commands
  #log stop [purpose]          - stop logging and save summary
  #log purpose <label>         - classify the last saved log
  #log status                  - show logging status
  #dmgmask on|off|status       - compact damage lines to per-round totals
  #dmgmap on|off|status|report|clear - map severe hit words to damage ranges
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
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .aliases import AliasManager
from .client import TelnetClient
from .damage_map import DamageMap
from .damage_mask import DamageMask
from .game_state import GameState
from .script_engine import ScriptEngine
from .skills import SkillsManager
from .split_ui import SplitUI
from .triggers import TriggerManager

logger = logging.getLogger(__name__)

# ANSI reset for client messages so they stand out
_CLIENT = "\033[1;36m[CLIENT]\033[0m "
_WARN   = "\033[1;33m[WARN]\033[0m "
_ERR    = "\033[1;31m[ERR]\033[0m "
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mKHJABCDsuhl]")
_DIRECTION_CMDS = {
    "n", "s", "e", "w", "u", "d", "ne", "nw", "se", "sw",
    "north", "south", "east", "west", "up", "down",
}
_FOR_DAMAGE_RE = re.compile(r"\bfor\s+(\d+)\s+damage\b", re.IGNORECASE)
_CAPTURE_PATTERNS = (
    "tells you",
    "you tell",
    " says ",
    "gossip",
    "auction",
    "shout",
    "newbie",
    "broadcast",
    "group",
    "clan",
    "guild",
)
_SEVERE_DAMAGE_WORDS = (
    "mutilate",
    "disembowel",
    "dismember",
    "massacre",
    "mangle",
    "demolish",
    "devastate",
    "obliterate",
    "annihilate",
    "eradicate",
    "ghastly",
    "horrid",
    "dreadful",
    "hideous",
    "indescribable",
    "unspeakable",
)


class MudApp:
    def __init__(
        self,
        host: str,
        port: int,
        config_dir: str,
        rate_limit: float = 0.5,
        bridge_mode: bool = False,
        ui_mode: str = "classic",
    ):
        self.client   = TelnetClient(host, port, rate_limit)
        self.aliases  = AliasManager(config_dir)
        self.triggers = TriggerManager(config_dir)
        self.skills   = SkillsManager(config_dir)
        self.state    = GameState()
        self.config_dir = config_dir
        self.bridge_mode = bridge_mode
        self.ui_mode = ui_mode
        self._running = False
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._config_dir = config_dir
        self.script = ScriptEngine(
            enqueue=self._enqueue,
            get_state=lambda: self.state,
            print_fn=self._print,
        )
        self._last_room: Optional[str] = None
        self._active_cmd_log: Optional[dict] = None
        self._last_saved_log_id: Optional[str] = None
        self._path_log_file = Path(config_dir) / "path_logs.jsonl"
        self._last_user_input: str = ""
        self.damage_mask = DamageMask(enabled=True)
        self.damage_map = DamageMap(Path(config_dir) / "damage_map.json")
        self.damage_map_enabled: bool = True
        self._pending_severe_labels: list[str] = []
        self._pending_hp_before: Optional[int] = None
        self._split_ui: Optional[SplitUI] = None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        if self.ui_mode == "split":
            try:
                self._split_ui = SplitUI()
                await self._split_ui.start()
            except Exception as exc:
                self._split_ui = None
                print(f"{_WARN}Split UI unavailable ({exc}); falling back to classic mode.", flush=True)

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
            if self._split_ui is not None:
                await self._split_ui.stop()

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    async def _input_loop(self) -> None:
        """Read lines from stdin (non-blocking via executor)."""
        if self._split_ui is not None:
            while self._running and self.client.connected:
                line = await self._split_ui.read_line()
                if line is None:
                    break
                await self._handle_input(line)
            return

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
                        default = self._last_user_input
                        line = await session.prompt_async(
                            "> ",
                            default=default,
                            pre_run=self._select_all_input if default else None,
                        )
                        # If user just hits Enter on untouched prefill, treat it as no-action.
                        if default and line == default:
                            line = ""
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
            self._log_user_command("")
            await self.client.send("")
            return
        self._last_user_input = line

        if line.startswith("#"):
            await self._handle_client_cmd(line[1:])
        else:
            for part in line.split(";"):
                expanded = self.aliases.expand(part.strip())
                self._log_user_command(expanded)
                await self.client.send(expanded)

    # ------------------------------------------------------------------
    # Server output
    # ------------------------------------------------------------------

    def _on_server_data(self, text: str) -> None:
        """Sync callback called from read_loop."""
        self._update_last_room(text)
        masked = self.damage_mask.process(text)
        normalized = self._normalize_terminal_output(masked)
        if self._split_ui is not None:
            self._split_ui.append_main(normalized)
            capture = self._extract_capture_lines(normalized)
            if capture:
                self._split_ui.append_capture(capture)
        else:
            sys.stdout.write(normalized)
            sys.stdout.flush()


        # Schedule async processing
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._process_output(text))
        except RuntimeError:
            pass  # event loop gone

    async def _process_output(self, text: str) -> None:
        """Update state and fire triggers based on received text."""
        prev_hp = self.state.hp
        self._track_severe_incoming_hits(text, prev_hp)
        self.state.parse(text)
        self.script.on_output(text)
        self._emit_damage_map_if_ready()

        if not self.bridge_mode:
            for cmd, delay, action, threshold in self.triggers.process(text):
                if delay > 0:
                    await asyncio.sleep(delay)
                if action == "reroll":
                    await self._handle_reroll(cmd, threshold)
                else:
                    for part in cmd.split(";"):
                        await self._enqueue(part.strip())

    # ------------------------------------------------------------------
    # Send queue (serialises outgoing commands)
    # ------------------------------------------------------------------

    async def _handle_reroll(self, stats_str: str, threshold: int) -> None:
        """Echo stat total; auto-send N if below threshold."""
        try:
            total = sum(int(x) for x in stats_str.split())
        except ValueError:
            return
        if total >= threshold:
            sys.stdout.write(f"\033[1;32m>>> ROLL TOTAL: {total} — KEEP IT! (type Y) <<<\033[0m\n")
        else:
            sys.stdout.write(f"\033[1;31m>>> ROLL TOTAL: {total}/{threshold} — rerolling... <<<\033[0m\n")
            await self._enqueue("N")
        sys.stdout.flush()

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
                f"HP-crit: {s.hp_critical}  "
                f"Room: {self._last_room or 'unknown'}"
            )

        # ---- command logging ----
        elif cmd in ("log", "pathlog", "route"):
            await self._handle_log_cmd(parts)

        # ---- client-side damage masking ----
        elif cmd == "dmgmask":
            if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
                self.damage_mask.enabled = parts[1].lower() == "on"
                status = "enabled" if self.damage_mask.enabled else "disabled"
                self._print(f"{_CLIENT}Damage mask {status}.")
            else:
                self._print(f"{_CLIENT}{self.damage_mask.status_text()}")

        elif cmd == "dmgmap":
            sub = parts[1].lower() if len(parts) >= 2 else "status"
            if sub in ("on", "off"):
                self.damage_map_enabled = sub == "on"
                status = "enabled" if self.damage_map_enabled else "disabled"
                self._print(f"{_CLIENT}Damage mapping trigger {status}.")
                if not self.damage_map_enabled:
                    self._pending_severe_labels = []
                    self._pending_hp_before = None
            elif sub == "report":
                lines = self.damage_map.report_lines()
                self._print(f"{_CLIENT}Damage map samples:\n" + "\n".join(lines))
            elif sub == "clear":
                self.damage_map.clear()
                self._pending_severe_labels = []
                self._pending_hp_before = None
                self._print(f"{_CLIENT}Damage map samples cleared.")
            else:
                status = "ON" if self.damage_map_enabled else "OFF"
                pending = ", ".join(self._pending_severe_labels) if self._pending_severe_labels else "none"
                self._print(f"{_CLIENT}Damage mapping trigger: {status}. Pending severe hits: {pending}.")

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

    async def _handle_log_cmd(self, parts: list[str]) -> None:
        if len(parts) < 2:
            self._print(
                f"{_CLIENT}Usage: #log start | #log stop [purpose] | #log purpose <label> | #log status"
            )
            return

        sub = parts[1].lower()
        if sub == "start":
            if self._active_cmd_log is not None:
                self._print(f"{_WARN}Command logging already active. Use #log stop first.")
                return
            self._active_cmd_log = {
                "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "start_room": self._last_room or "unknown",
                "commands": [],
            }
            self._print(
                f"{_CLIENT}Command logging started. Start room: {self._active_cmd_log['start_room']!r}"
            )
            return

        if sub == "status":
            if self._active_cmd_log is None:
                self._print(f"{_CLIENT}Command logging is OFF.")
            else:
                count = len(self._active_cmd_log["commands"])
                self._print(
                    f"{_CLIENT}Command logging is ON. "
                    f"Commands captured: {count}. Start room: {self._active_cmd_log['start_room']!r}"
                )
            return

        if sub == "stop":
            if self._active_cmd_log is None:
                self._print(f"{_WARN}No active command log. Use #log start first.")
                return
            purpose = " ".join(parts[2:]).strip() if len(parts) > 2 else "unclassified"
            entry = dict(self._active_cmd_log)
            entry["ended_at"] = datetime.now(timezone.utc).isoformat()
            entry["end_room"] = self._last_room or "unknown"
            entry["purpose"] = purpose
            entry["summary"] = self._summarize_commands(entry["commands"])
            self._append_path_log(entry)
            self._last_saved_log_id = entry["id"]
            self._active_cmd_log = None
            self._print(
                f"{_CLIENT}Command logging stopped. "
                f"Saved {entry['summary']['total']} commands "
                f"(directions={entry['summary']['directions']}, other={entry['summary']['other']})."
            )
            if purpose == "unclassified":
                self._print(
                    f"{_CLIENT}Set purpose with: #log purpose directions | #log purpose discard | "
                    f"#log purpose levelling path"
                )
            return

        if sub == "purpose":
            label = " ".join(parts[2:]).strip() if len(parts) > 2 else ""
            if not label:
                self._print(f"{_CLIENT}Usage: #log purpose <directions|discard|levelling path|...>")
                return
            if not self._last_saved_log_id:
                self._print(f"{_WARN}No saved log to classify yet. Use #log start then #log stop.")
                return
            updated = self._update_saved_log_purpose(self._last_saved_log_id, label)
            if updated:
                self._print(f"{_CLIENT}Updated log {self._last_saved_log_id} purpose -> {label!r}.")
            else:
                self._print(f"{_ERR}Could not update purpose for log {self._last_saved_log_id}.")
            return

        self._print(
            f"{_CLIENT}Usage: #log start | #log stop [purpose] | #log purpose <label> | #log status"
        )

    def _update_last_room(self, text: str) -> None:
        clean = _ANSI_RE.sub("", text)
        lines = [ln.strip() for ln in clean.splitlines()]
        for idx, line in enumerate(lines):
            if not line:
                continue
            low = line.lower()
            if "exits:" not in low and not low.startswith("obvious exits"):
                continue
            room = self._previous_room_candidate(lines, idx)
            if room:
                self._last_room = room

    def _track_severe_incoming_hits(self, text: str, hp_before: int) -> None:
        if not self.damage_map_enabled:
            return
        clean = _ANSI_RE.sub("", text)
        for line in clean.splitlines():
            low = line.strip().lower()
            if not low:
                continue
            label = self._severe_label_from_line(low)
            if not label:
                continue

            amount = self._extract_damage_amount(low)

            # We hit someone else: exact amount is usually available.
            if low.startswith("you ") or low.startswith("your "):
                if amount is not None:
                    self.damage_map.add("dealt", label, amount)
                continue

            # Someone hit us.
            if " you" in f" {low} ":
                if amount is not None:
                    self.damage_map.add("taken", label, amount)
                    self._print(f"{_CLIENT}[dmgmap] Severe hit ({label}) -> taken: {amount}")
                else:
                    self._pending_severe_labels.append(label)
                    if self._pending_hp_before is None:
                        self._pending_hp_before = hp_before
                continue

            # Other people taking severe hits: print inferred range from learned samples.
            inferred = self.damage_map.get_combined_range(label)
            if inferred:
                low_amt, high_amt, count = inferred
                self._print(
                    f"{_CLIENT}[dmgmap] {label} observed on others -> estimated range {low_amt}-{high_amt} "
                    f"(n={count})"
                )

    def _emit_damage_map_if_ready(self) -> None:
        if not self.damage_map_enabled or not self._pending_severe_labels:
            return
        if self._pending_hp_before is None:
            self._pending_severe_labels = []
            return
        hp_after = self.state.hp
        if hp_after <= 0 and self.state.max_hp <= 0:
            return
        delta = self._pending_hp_before - hp_after
        labels = ", ".join(self._pending_severe_labels)
        if delta > 0:
            self._print(f"{_CLIENT}[dmgmap] Severe hit ({labels}) -> estimated taken: {delta}")
            if len(self._pending_severe_labels) == 1:
                self.damage_map.add("taken", self._pending_severe_labels[0], delta)
            self._pending_severe_labels = []
            self._pending_hp_before = None
        elif not self.state.in_combat:
            # Drop stale pending severe markers when combat ends without a measurable HP drop.
            self._pending_severe_labels = []
            self._pending_hp_before = None

    @staticmethod
    def _severe_label_from_line(low: str) -> Optional[str]:
        for word in _SEVERE_DAMAGE_WORDS:
            if word in low:
                return word.upper()
        return None

    @staticmethod
    def _extract_damage_amount(low: str) -> Optional[int]:
        m = _FOR_DAMAGE_RE.search(low)
        if not m:
            return None
        try:
            return int(m.group(1))
        except ValueError:
            return None

    def _extract_capture_lines(self, text: str) -> str:
        clean = _ANSI_RE.sub("", text)
        out: list[str] = []
        for line in clean.splitlines():
            low = line.strip().lower()
            if not low:
                continue
            if low.startswith("[broadcast"):
                out.append(line.strip())
                continue
            if low.startswith("[") and "]" in low and any(tag in low for tag in ("newbie", "gossip", "auction", "clan", "guild", "group")):
                out.append(line.strip())
                continue
            if any(pat in low for pat in _CAPTURE_PATTERNS):
                out.append(line.strip())
        if not out:
            return ""
        return "\n".join(out) + "\n"

    @staticmethod
    def _previous_room_candidate(lines: list[str], idx: int) -> Optional[str]:
        for j in range(idx - 1, -1, -1):
            cand = lines[j].strip()
            if not cand:
                continue
            low = cand.lower()
            if len(cand) > 80:
                continue
            if low.startswith("[dsl]") or low.startswith("your selection"):
                continue
            if "main login menu" in low or "visible mortals" in low:
                continue
            if low.startswith("hp:") or low.startswith("<"):
                continue
            if cand.startswith("(") and cand.endswith(")"):
                continue
            return cand
        return None

    def _log_user_command(self, cmd: str) -> None:
        if self._active_cmd_log is None:
            return
        rendered = cmd.strip()
        if not rendered:
            rendered = "<ENTER>"
        token = rendered.split()[0].lower() if rendered else ""
        kind = "direction" if token in _DIRECTION_CMDS else "other"
        self._active_cmd_log["commands"].append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "command": rendered,
                "kind": kind,
            }
        )

    @staticmethod
    def _summarize_commands(commands: list[dict]) -> dict:
        directions = sum(1 for c in commands if c.get("kind") == "direction")
        total = len(commands)
        return {
            "total": total,
            "directions": directions,
            "other": total - directions,
        }

    def _append_path_log(self, entry: dict) -> None:
        self._path_log_file.parent.mkdir(parents=True, exist_ok=True)
        with self._path_log_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=True) + "\n")

    def _update_saved_log_purpose(self, log_id: str, purpose: str) -> bool:
        if not self._path_log_file.exists():
            return False
        lines = self._path_log_file.read_text(encoding="utf-8").splitlines()
        updated = False
        out = []
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                out.append(line)
                continue
            if obj.get("id") == log_id:
                obj["purpose"] = purpose
                updated = True
            out.append(json.dumps(obj, ensure_ascii=True))
        if updated:
            self._path_log_file.write_text("\n".join(out) + "\n", encoding="utf-8")
        return updated

    @staticmethod
    def _normalize_terminal_output(text: str) -> str:
        """
        Normalize telnet line endings for local terminal rendering.
        Raw carriage returns can move the cursor and overwrite the current
        input line while typing; convert them to line breaks.
        """
        text = text.replace("\r\n", "\n")
        text = text.replace("\r", "\n")
        return text

    @staticmethod
    def _select_all_input() -> None:
        """Select all prompt text so typing replaces last command immediately."""
        try:
            from prompt_toolkit.application.current import get_app
            from prompt_toolkit.selection import SelectionType

            buf = get_app().current_buffer
            if not buf.text:
                return
            buf.cursor_position = 0
            buf.start_selection(selection_type=SelectionType.CHARACTERS)
            buf.cursor_position = len(buf.text)
        except Exception:
            # Keep prompt usable even if prompt_toolkit internals differ.
            return

    def _print(self, msg: str) -> None:
        if self._split_ui is not None:
            self._split_ui.append_main(msg + "\n")
        else:
            print(msg, flush=True)
