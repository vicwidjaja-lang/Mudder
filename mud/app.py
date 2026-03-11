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
  levelling start <area>       - load scripts config and start levellingscript area
  go <landmark>|go list        - run character/default travel route to a landmark
  #char <name>                 - set active character (loads char-specific aliases)
  #char                        - show active character
  #alias <name> <cmd>          - add or update a global alias
  #alias <name>                - remove a global alias
  #aliases                     - list global aliases
  #charalias <name> <cmd>      - set alias for active character only
  #charalias <name>            - remove char alias
  #charaliases                 - list char-specific aliases
  #setheal <dirs>|status|clear - set current character heal alias path
  t <target>|status|clear      - set/show PK chase target (works with or without #)
  ch <1|2|3> <cmd>|status|clear - set/show chase action command for k/l/m
  ch1/ch2/ch3 <cmd>|status|clear - set/show chase action command for k/l/m
  chs [target]|status          - enable auto chase scan (room/flee) using chase action 1
  chstp                        - disable auto chase scan
  k|l|m [target]               - fire chase action 1/2/3 at target; arg retargets
  <dir>k|<dir>l|<dir>m [target] - move then fire chase action (examples: nk, nek, nwl)
  kkkk [target]                - send 9x chase action 1 at target, then one `where`
  #trigger <name> <pat> <cmd>  - add trigger (name/pattern/command)
  #triggers                    - list triggers
  #trigger <name> on|off       - enable / disable trigger
  #trigdel <name>              - delete trigger
  #skills                      - list skills
  #skill <name> on|off         - enable / disable skill
  #autoskill on|off            - toggle auto-skill loop
  #spellmaint on|off|status    - toggle/view spell maintainer
  #spellmaint list             - list spell maintainer entries
  #spellmaint <name> on|off    - enable / disable maintained spell
  #pvp on|off|status           - high-risk mode; disables/restores maintainer triggers
  #session [fighter] on|off|status - toggle fighter session spell-maint bundle
  #setsanc <spell cmd>         - set sanc command for current character
  #sethaste <spell cmd>        - set haste command for current character
  #setfly <spell cmd>          - set fly command for current character
  #autowhoami on|off|status    - auto-run whoami after login to set active character
  #setalign <good|evil|neutral> [spell cmd] - set protection alignment/command
  #setprotect <spell cmd>      - set protection spell for current alignment
  #setspellup                  - capture practiced buff entries from `spells` + `songs` (>=50%)
  #setspellup list             - list saved spellup spells for active character
  #setspellup drop <row>       - remove a spellup entry by row number
  #setspellup reference <name> - add a buff reference spell/song for future captures
  #setspellup reference list   - list buff reference names used for capture
  #spellup                     - cast saved spellup list in order (`sing` for songs)
  #needs on|off|status         - auto-manage hunger/thirst from server notices
  #needs hunger|thirst <cmd>   - set current-char (or default) need command
  #needs cooldown <secs>       - set minimum delay between need retries
  #telldraft status|list        - show tell-draft policy and pending drafts
  #telldraft approve <id>       - approve and send drafted reply
  #telldraft reject <id>        - reject drafted reply
  #telldraft nonimm on|off      - allow/disable non-imm draft generation
  #telldraft autosend on|off    - send drafts automatically (default OFF)
  #inputdebug on|off|status    - trace input pipeline (keys -> queue -> send)
  #state                       - show parsed game state
  #log start                   - begin logging entered commands
  #log stop [purpose]          - stop logging and save summary
  #log purpose <label>         - classify the last saved log
  #log status                  - show logging status
  #dmgmask on|off|status       - compact damage lines to per-round totals
  #dmgmap on|off|status|report|clear - map severe hit words to damage ranges
  #save                        - save all configs
  #restart                     - save configs and restart the client
  #quit                        - quit client

Script commands (prefix #script):
  #script load [file]          - load script config (default: configs/scripts.json)
  #script start <route|area>   - start route directly or a levellingscript area
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
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .aliases import AliasManager
from .affects import AffectTracker
from .client import TelnetClient
from .damage_map import DamageMap
from .damage_mask import DamageMask
from .game_state import GameState
from .room_parser import parse_rooms
from .script_engine import ScriptEngine
from .skills import SkillsManager
from .spell_maintainer import SpellMaintainer
from .triggers import TriggerManager

if TYPE_CHECKING:
    from .split_ui import SplitUI

logger = logging.getLogger(__name__)

# ANSI reset for client messages so they stand out
_CLIENT = "\033[1;36m[CLIENT]\033[0m "
_WARN   = "\033[1;33m[WARN]\033[0m "
_ERR    = "\033[1;31m[ERR]\033[0m "
_YOU    = "\033[1;34m[YOU]\033[0m "
_TRIGGER = "\033[1;35m[TRIGGER]\033[0m "
_INPUTDBG = "\033[0;37m[INPUTDBG]\033[0m "
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CSI_PARTIAL_TAIL_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*)?$")
# Some servers/chunks occasionally drop ESC and emit bare SGR like "[1;33m".
_BARE_SGR_RE = re.compile(r"(?<!\x1b)(\[(?:\d{1,3}(?:;\d{1,3})*)m)")
# OSC (Operating System Command), e.g. terminal title updates.
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
# ESC two-byte controls outside CSI.
_ESC_SINGLE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|[78])")
# Remove control chars but keep ESC (0x1b) so ANSI SGR sequences survive.
_RENDER_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]")
_MAX_CAPTURE_LINES = 500
_DIRECTION_CMDS = {
    "n", "s", "e", "w", "u", "d", "ne", "nw", "se", "sw",
    "north", "south", "east", "west", "up", "down",
}
_FOR_DAMAGE_RE = re.compile(r"\bfor\s+(\d+)\s+damage\b", re.IGNORECASE)
# DSL bracket-format channels: [Channel] Name: 'message'
_CHANNEL_TAGS = frozenset((
    "newbie", "gossip", "auction", "clan", "guild",
    "group", "question", "music", "broadcast",
    "immtalk", "claninfo", "kingdom", "ooc",
))
_CHANNEL_BRACKET_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*(?P<rest>.+)$")
_CHANNEL_BRACKET_MSG_RE = re.compile(r"^(?P<name>[^:]+):\s*'(?P<msg>.*)'\s*$")
_CHANNEL_PAREN_RE = re.compile(
    r"^(?P<name>[A-Za-z][\w'-]*)\s*\((?P<tag>[A-Za-z]+)\)\s*'(?P<msg>.*)'\s*$",
    re.IGNORECASE,
)
_CHANNEL_COLON_RE = re.compile(
    r"^(?P<name>[A-Za-z][\w'-]*)\s+(?P<tag>[A-Za-z]+)\s*:\s*'(?P<msg>.*)'\s*$",
    re.IGNORECASE,
)
_CHANNEL_GOSSIPS_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<tag>[A-Za-z]+)\s+gossips?\s*'(?P<msg>.*)'\s*$",
    re.IGNORECASE,
)
_CHANNEL_OOC_RE = re.compile(
    r"^(?P<name>[A-Za-z][\w'-]*)\s+OOC(?:\s+(?P<scope>[A-Za-z]+))?\s*:\s*'(?P<msg>.*)'\s*$",
    re.IGNORECASE,
)
_CHANNEL_IMM_OOC_RE = re.compile(
    r"^\((?:An\s+)?Imm\)\s*(?P<name>[A-Za-z][\w'-]*)\s+OOC(?:\s+(?P<scope>[A-Za-z]+))?\s*:\s*'(?P<msg>.*)'\s*$",
    re.IGNORECASE,
)
_CHANNEL_TELL_RE = re.compile(
    r"^(?P<name>\w+)\s+tell(?:s)?\s+(?:the\s+)?(?P<tag>[A-Za-z]+)\s*'(?P<msg>.*)'\s*$",
    re.IGNORECASE,
)
_CHANNEL_SHORT_SELF_RE = re.compile(
    r"^(?P<name>You)\s+(?P<tag>[A-Za-z]+)\s*'(?P<msg>.*)'\s*$",
    re.IGNORECASE,
)
_DIRECT_TELL_RE = re.compile(
    r"^(?P<name>\w+)\s+tells?\s+you\s*'(?P<msg>.*)'\s*$",
    re.IGNORECASE,
)
_IMM_DIRECT_TELL_RE = re.compile(
    r"^\((?:An\s+)?Imm\)\s*(?P<name>[A-Za-z][\w'-]*)\s+tells?\s+you\s*'(?P<msg>.*)'\s*$",
    re.IGNORECASE,
)
_WHOIS_IMM_HINTS = (
    " an imm",
    "(an imm)",
    "immortal",
    "implementor",
    "deity",
)
_WHOIS_NONIMM_HINTS = (
    "no player by that name",
    "not found",
    "does not exist",
)
_WHOIS_TIMEOUT_SECS = 6.0
_MAX_TELL_DRAFTS = 50
_LOGIN_MENU_MARKERS = (
    "dark and shattered lands: main login menu",
    "dark and shattered lands: master login menu",
    "what is your master account's name",
    "do you agree? (yes, no, show)",
    "do you want color? (y/n)",
    "please answer (y/n)?",
    "your selection? ->",
    "password:",
    "old password:",
)
_WORLD_PROMPT_RE = re.compile(
    r"HP:\s*\d+/\d+\s+Mana:\s*\d+/\d+\s+MV:\s*\d+/\d+\s*>",
    re.IGNORECASE,
)
_WHOAMI_PATTERNS = (
    re.compile(r"\bYou are logged in as:\s*(?P<name>[A-Za-z][A-Za-z0-9_-]{1,19})\b", re.IGNORECASE),
    re.compile(r"\bYou are known as\s+(?P<name>[A-Za-z][A-Za-z0-9_-]{1,19})\b", re.IGNORECASE),
    re.compile(r"\bYour name is\s+(?P<name>[A-Za-z][A-Za-z0-9_-]{1,19})\b", re.IGNORECASE),
    re.compile(r"^Name:\s*(?P<name>[A-Za-z][A-Za-z0-9_-]{1,19})$", re.IGNORECASE),
    re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9_-]{1,19})$"),
)
_WHOAMI_STOPWORDS = {
    "you",
    "your",
    "the",
    "hp",
    "mana",
    "mv",
    "help",
}
_WHOAMI_TIMEOUT_SECS = 6.0
_WHOAMI_RETRY_SECS = 8.0
_WHOAMI_EXPLICIT_MARKERS = (
    "you are logged in as:",
    "you are known as",
    "your name is",
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
_SPELLS_PAGER_RE = re.compile(r"\(\s*C\s*\)\s*ontinue", re.IGNORECASE)
_SPELL_ENTRY_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z' -]*?)\s+(?P<mana>\d+)\s+mana,\s*(?P<pct>\d+)%",
    re.IGNORECASE,
)
_PROTECTION_SPELL_NAMES = frozenset((
    "protection good",
    "protection evil",
    "protection neutral",
))
# Default capture allow-list for `setspellup`. Users can extend this with
# `setspellup reference <spell name>` when class/gear/meta changes.
_DEFAULT_SPELLUP_REFERENCES = [
    "armor",
    "detect evil",
    "detect good",
    "detect hidden",
    "detect invis",
    "detect magic",
    "detect poison",
    "fly",
    "giant strength",
    "haste",
    "infravision",
    "invisibility",
    "light foot",
    "mass invis",
    "pass door",
    "protection evil",
    "protection good",
    "protection neutral",
    "rehearse",
    "refresh",
    "sanctuary",
    "self projection",
    "shield",
    "shield of wards",
    "song of war",
    "stone skin",
    "water breathing",
    "we come",
]
_SPELLUP_EXCLUDED_NAMES = frozenset((
    "faerie fire",
    "fireproof",
))
_SPELLUP_EXCLUDED_REASONS = {
    "faerie fire": "debuff spell (not a self-buff)",
    "fireproof": "not a castable spellup buff",
}
_SPELLUP_SING_NAMES = frozenset((
    "song of war",
    "we come",
    "shield of wards",
))
_SPELLUP_DIRECT_CMDS = {
    "rehearse": "rehearse",
}
_SPELLUP_SONG_DELAY_SECS = 4.0
_SPELLUP_CAPTURE_COMMANDS = ("spells", "songs")
_PK_CHASE_ACTION_KEYS = ("k", "l", "m")
_DEFAULT_PK_CHASE_ACTIONS = {
    "k": "k",
    "l": "l",
    "m": "m",
}
_PK_ROOM_PRESENCE_RE = re.compile(
    r"^(?P<name>.+?)\s+is here(?:,.*)?\.?\s*$",
    re.IGNORECASE,
)
_PK_ARTICLE_RE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)
_PK_VOID_RE = re.compile(
    r"\b(?:you disappear into the void|you have entered the void)\b",
    re.IGNORECASE,
)
_PK_FLEE_SUCCESS_RE = re.compile(r"\b(?:You flee|escape the fray)\b", re.IGNORECASE)
_PK_CHASE_FLEE_SCAN_WINDOW_SECS = 8.0


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
        self.spell_maintainer = SpellMaintainer(config_dir)
        self.state    = GameState()
        self.config_dir = config_dir
        self.bridge_mode = bridge_mode
        self.ui_mode = ui_mode
        self._running = False
        self._send_queue: Optional[asyncio.Queue] = None
        self._config_dir = config_dir
        self.script = ScriptEngine(
            enqueue=lambda cmd: self._enqueue(cmd, source="script"),
            get_state=lambda: self.state,
            print_fn=self._print,
            get_char=lambda: self._active_char,
            get_active_affects=lambda: self.affects.active_affects(),
            get_affects_snapshot_version=lambda: self.affects.snapshot_version(),
        )
        self._last_room: Optional[str] = None
        self._last_exits: list[str] = []
        self._active_cmd_log: Optional[dict] = None
        self._last_saved_log_id: Optional[str] = None
        self._path_log_file = Path(config_dir) / "path_logs.jsonl"
        self._last_user_input: str = ""
        self.damage_mask = DamageMask(enabled=True)
        self.damage_map = DamageMap(Path(config_dir) / "damage_map.json")
        self.affects = AffectTracker(Path(config_dir) / "affect_windows.jsonl")
        self._active_char: str = ""
        self.damage_map_enabled: bool = True
        self._pending_severe_labels: list[str] = []
        self._pending_hp_before: Optional[int] = None
        self._capture_feed_lines: list[str] = []
        self._split_ui: Optional["SplitUI"] = None
        self._render_ansi_carry: str = ""
        self._restart_requested: bool = False
        self._fighter_session: bool = False
        self._high_risk_mode: bool = False
        self._high_risk_disabled_maintainers: set[str] = set()
        self._input_debug: bool = False
        self._settings_path = Path(config_dir) / "client_settings.json"
        self._auto_whoami: bool = True
        # Tell-response policy defaults:
        # - non-imm drafting disabled
        # - approval required (auto-send disabled)
        self._tell_nonimm_enabled: bool = False
        self._tell_auto_send: bool = False
        self._tell_drafts: list[dict] = []
        self._next_tell_draft_id: int = 1
        self._recent_tell_fingerprints: deque[str] = deque(maxlen=256)
        self._whois_role_cache: dict[str, bool] = {}
        self._pending_whois_name: str = ""
        self._pending_whois_at: float = 0.0
        self._pending_tell_by_sender: dict[str, str] = {}
        self._login_menu_seen: bool = False
        self._awaiting_whoami_response: bool = False
        self._whoami_requested_at: float = 0.0
        self._last_whoami_attempt: float = 0.0
        self._needs_enabled: bool = True
        self._needs_cooldown_secs: float = 20.0
        self._needs_hunger_default: str = ""
        self._needs_thirst_default: str = ""
        self._needs_hunger_by_char: dict[str, str] = {}
        self._needs_thirst_by_char: dict[str, str] = {}
        self._needs_last_sent_at: dict[str, float] = {"hunger": 0.0, "thirst": 0.0}
        self._spellup_references: list[str] = list(_DEFAULT_SPELLUP_REFERENCES)
        self._spellup_by_char: dict[str, list[str]] = {}
        self._spellup_capture_active: bool = False
        self._spellup_capture_char: str = ""
        self._spellup_capture_lines: list[str] = []
        self._spellup_capture_pending_cmds: deque[str] = deque()
        self._pk_target_by_char: dict[str, str] = {}
        self._pk_chase_actions_by_char: dict[str, dict[str, str]] = {}
        self._pk_chase_scan_enabled_by_char: dict[str, bool] = {}
        self._pk_spam_target: str = ""
        self._pk_spam_until: float = 0.0
        self._pk_spam_echo_suppress: set[str] = set()
        self._pk_flee_scan_until_by_char: dict[str, float] = {}
        self._pk_last_scan_fire_room_by_char: dict[str, str] = {}
        self._pk_last_scan_fire_at_by_char: dict[str, float] = {}
        self._song_cast_block_until: float = 0.0
        self._load_client_settings()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        if self.ui_mode == "split":
            try:
                from .split_ui import SplitUI
                self._split_ui = SplitUI()
                self._split_ui.set_tick_source(self.state.tick_countdown)
                self._split_ui.set_input_debug_hook(self._on_input_debug_event)
                await self._split_ui.start()
                self._refresh_capture_panes()
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
        self._send_queue = asyncio.Queue()

        # Register output callback
        self.client.on_data(self._on_server_data)

        # Launch background tasks
        read_task   = asyncio.create_task(self.client.read_loop(),   name="read_loop")
        send_task   = asyncio.create_task(self._send_loop(),          name="send_loop")
        skills_task = asyncio.create_task(self._skills_loop(),        name="skills_loop")
        spell_task  = asyncio.create_task(self._spell_maintainer_loop(), name="spell_maintainer_loop")
        script_task = asyncio.create_task(self.script.run(),          name="script_loop")

        # Input loop (runs in thread pool so it doesn't block the event loop)
        try:
            await self._input_loop()
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            self._running = False
            await self.client.disconnect()
            for task in (read_task, send_task, skills_task, spell_task, script_task):
                task.cancel()
            await asyncio.gather(
                read_task,
                send_task,
                skills_task,
                spell_task,
                script_task,
                return_exceptions=True,
            )
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
                self._log_input_debug("queue.pop", f"line={line!r}")
                if line is None:
                    break
                await self._handle_input(line)
            return

        loop = asyncio.get_event_loop()

        # Try to use prompt_toolkit for a nicer experience when running in a TTY.
        if sys.stdin.isatty() and sys.stdout.isatty():
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
                            await self._handle_input(line)
                        except (EOFError, KeyboardInterrupt):
                            break
                return
            except ImportError:
                pass
            except Exception as exc:
                self._print(
                    f"{_WARN}Interactive prompt unavailable ({exc}); falling back to plain input."
                )

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
        raw_line = line
        line = line.strip()
        self._log_input_debug("handle.input", f"raw={raw_line!r} normalized={line!r}")
        if not line:
            self._echo_input("")
            self._log_user_command("")
            sent = await self.client.send("")
            self._log_input_debug("send.user", f"cmd=<ENTER> ok={sent}")
            return
        self._last_user_input = line
        self._echo_input(line)

        if await self._handle_levelling_alias(line):
            return

        local_cmd = line.split(maxsplit=1)[0].lower()
        # Keep these available without `#` for fast in-combat/field usage.
        if local_cmd in ("setspellup", "spellup"):
            await self._handle_client_cmd(line)
            return

        if line.startswith("#"):
            await self._handle_client_cmd(line[1:])
        else:
            for part in line.split(";"):
                raw_part = part.strip()
                if not raw_part:
                    continue
                if self._handle_pk_control_input(raw_part):
                    continue
                if await self._handle_pk_chase_input(raw_part):
                    continue
                if await self._handle_go_alias(raw_part, enqueue_source="travel"):
                    continue
                # Keep `heal` aligned with per-character script routes.
                if raw_part.lower() == "heal" and self._active_char:
                    self._sync_heal_alias_from_script(self._active_char)
                expanded = self.aliases.expand(raw_part)
                # Alias values may themselves be multi-command (semicolon-separated).
                for cmd in expanded.split(";"):
                    cmd = cmd.strip()
                    if cmd:
                        if self._handle_pk_control_input(cmd):
                            continue
                        if await self._handle_pk_chase_input(cmd):
                            continue
                        if await self._handle_go_alias(cmd, enqueue_source="travel"):
                            continue
                        await self._send_user_command(cmd)

    def _echo_input(self, line: str) -> None:
        shown = line if line else "<ENTER>"
        self._print(f"{_YOU}{shown}")

    async def _send_user_command(self, cmd: str) -> None:
        await self._wait_for_song_cast_window()
        self._record_outgoing_command(cmd)
        self._log_user_command(cmd)
        sent = await self.client.send(cmd)
        self._start_song_cast_window_if_needed(cmd)
        self._log_input_debug("send.user", f"cmd={cmd!r} ok={sent}")

    async def _wait_for_song_cast_window(self) -> None:
        remaining = self._song_cast_block_until - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

    def _start_song_cast_window_if_needed(self, cmd: str) -> None:
        if self._is_song_like_command(cmd):
            self._song_cast_block_until = max(
                self._song_cast_block_until,
                time.monotonic() + _SPELLUP_SONG_DELAY_SECS,
            )

    @staticmethod
    def _is_song_like_command(cmd: str) -> bool:
        low = cmd.strip().lower()
        if not low:
            return False
        if low == "rehearse" or low.startswith("rehearse "):
            return True
        return low.startswith("sing ")

    def _pk_profile_key(self) -> str:
        return self._active_character_key() or "default"

    def _pk_actions(self, profile_key: Optional[str] = None) -> dict[str, str]:
        key = (profile_key or self._pk_profile_key()).strip().lower() or "default"
        actions = self._pk_chase_actions_by_char.get(key)
        if not isinstance(actions, dict):
            actions = dict(_DEFAULT_PK_CHASE_ACTIONS)
            self._pk_chase_actions_by_char[key] = actions
        for action_key, default_cmd in _DEFAULT_PK_CHASE_ACTIONS.items():
            if not str(actions.get(action_key, "")).strip():
                actions[action_key] = default_cmd
        return actions

    def _pk_target(self, profile_key: Optional[str] = None) -> str:
        key = (profile_key or self._pk_profile_key()).strip().lower() or "default"
        return self._pk_target_by_char.get(key, "").strip()

    def _pk_chase_scan_enabled(self, profile_key: Optional[str] = None) -> bool:
        key = (profile_key or self._pk_profile_key()).strip().lower() or "default"
        return bool(self._pk_chase_scan_enabled_by_char.get(key, False))

    def _pk_set_chase_scan_enabled(
        self,
        enabled: bool,
        profile_key: Optional[str] = None,
        *,
        reason: str = "manual",
    ) -> None:
        key = (profile_key or self._pk_profile_key()).strip().lower() or "default"
        if enabled:
            self._pk_chase_scan_enabled_by_char[key] = True
            self._pk_last_scan_fire_room_by_char.pop(key, None)
            self._pk_last_scan_fire_at_by_char.pop(key, None)
        else:
            self._pk_chase_scan_enabled_by_char.pop(key, None)
            self._pk_flee_scan_until_by_char.pop(key, None)
            self._pk_last_scan_fire_room_by_char.pop(key, None)
            self._pk_last_scan_fire_at_by_char.pop(key, None)
        self._save_client_settings()
        if reason == "void":
            self._print(f"{_CLIENT}[chs] OFF (void detected).")
        elif reason == "manual":
            self._print(f"{_CLIENT}[{key}] chs {'ON' if enabled else 'OFF'}.")

    @staticmethod
    def _normalize_pk_target(target: str) -> str:
        cleaned = target.strip()
        if len(cleaned) >= 2 and cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = cleaned[1:-1].strip()
        return cleaned.lower()

    def _pk_set_target(self, target: str, profile_key: Optional[str] = None) -> None:
        key = (profile_key or self._pk_profile_key()).strip().lower() or "default"
        cleaned = self._normalize_pk_target(target)
        if cleaned:
            self._pk_target_by_char[key] = cleaned
        else:
            self._pk_target_by_char.pop(key, None)
        self._save_client_settings()

    @staticmethod
    def _pk_norm_name(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

    @staticmethod
    def _pk_tokens_prefix_subsequence(needle: list[str], haystack: list[str]) -> bool:
        if not needle or len(needle) > len(haystack):
            return False
        pos = 0
        for token in needle:
            matched = False
            while pos < len(haystack):
                if haystack[pos].startswith(token):
                    matched = True
                    pos += 1
                    break
                pos += 1
            if not matched:
                return False
        return True

    def _pk_target_matches_candidate(self, target: str, candidate: str) -> bool:
        t_norm = self._pk_norm_name(target)
        c_norm = self._pk_norm_name(candidate)
        if not t_norm or not c_norm:
            return False
        if t_norm == c_norm:
            return True
        if c_norm.startswith(t_norm):
            return True
        return self._pk_tokens_prefix_subsequence(t_norm.split(), c_norm.split())

    def _pk_presence_candidates(self, clean_text: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw_line in clean_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            m = _PK_ROOM_PRESENCE_RE.match(line)
            if not m:
                continue
            name = _PK_ARTICLE_RE.sub("", m.group("name").strip())
            if not name:
                continue
            key = self._pk_norm_name(name)
            if key and key not in seen:
                seen.add(key)
                out.append(name)
        return out

    def _pk_refine_target_from_candidates(
        self,
        target: str,
        candidates: list[str],
    ) -> tuple[Optional[str], bool, list[str]]:
        if not target:
            return None, False, []
        exact: list[str] = []
        prefix_matches: list[str] = []
        t_norm = self._pk_norm_name(target)
        for name in candidates:
            n_norm = self._pk_norm_name(name)
            if not n_norm:
                continue
            if n_norm == t_norm:
                exact.append(name)
            elif self._pk_target_matches_candidate(target, name):
                prefix_matches.append(name)
        if exact:
            return exact[0], False, []
        if len(prefix_matches) == 1:
            return prefix_matches[0], True, []
        if len(prefix_matches) > 1:
            return None, False, prefix_matches
        return None, False, []

    async def _process_pk_chase_scan(self, text: str, prev_room: Optional[str]) -> None:
        profile_key = self._pk_profile_key()
        if not self._pk_chase_scan_enabled(profile_key):
            return

        clean = _ANSI_RE.sub("", text)
        if not clean:
            return

        if _PK_VOID_RE.search(clean):
            self._pk_set_chase_scan_enabled(False, profile_key, reason="void")
            return

        now = time.monotonic()
        if _PK_FLEE_SUCCESS_RE.search(clean):
            self._pk_flee_scan_until_by_char[profile_key] = max(
                self._pk_flee_scan_until_by_char.get(profile_key, 0.0),
                now + _PK_CHASE_FLEE_SCAN_WINDOW_SECS,
            )

        room_changed = (
            bool(prev_room)
            and bool(self.state.current_room)
            and prev_room != self.state.current_room
        )
        flee_window_active = now < self._pk_flee_scan_until_by_char.get(profile_key, 0.0)
        if not room_changed and not flee_window_active:
            return

        target = self._pk_target(profile_key)
        if not target:
            return

        candidates = self._pk_presence_candidates(clean)
        if not candidates:
            return

        matched, refined, ambiguous = self._pk_refine_target_from_candidates(target, candidates)
        if ambiguous:
            self._print(
                f"{_WARN}[chs] ambiguous target match for {target!r}: "
                + ", ".join(ambiguous)
            )
            return
        if not matched:
            return

        if refined:
            self._pk_set_target(matched, profile_key)
            self._print(f"{_CLIENT}[chs] target refined: {target!r} -> {matched!r}")
            target = self._pk_target(profile_key)

        room_key = (self.state.current_room or "").strip().lower()
        if room_key:
            last_room = self._pk_last_scan_fire_room_by_char.get(profile_key, "")
            last_at = self._pk_last_scan_fire_at_by_char.get(profile_key, 0.0)
            if room_key == last_room and now - last_at < 1.5:
                return

        chase_cmd = self._pk_render_chase_command("k", target, profile_key)
        self._print(f"{_CLIENT}[chs] ~{chase_cmd}")
        await self._enqueue(chase_cmd, source="pk-chs")
        if room_key:
            self._pk_last_scan_fire_room_by_char[profile_key] = room_key
        self._pk_last_scan_fire_at_by_char[profile_key] = now
        if flee_window_active:
            self._pk_flee_scan_until_by_char[profile_key] = 0.0

    def _handle_pk_control_input(self, raw: str) -> bool:
        parts = raw.strip().split(maxsplit=1)
        if not parts:
            return False
        cmd = parts[0].lower()
        if cmd not in ("t", "target", "ch", "ch1", "ch2", "ch3", "chs", "chstp"):
            return False

        arg = parts[1].strip() if len(parts) >= 2 else ""
        profile_key = self._pk_profile_key()
        actions = self._pk_actions(profile_key)

        if cmd == "chstp":
            self._pk_set_chase_scan_enabled(False, profile_key, reason="manual")
            return True

        if cmd == "chs":
            if arg.lower() in ("status", "show"):
                status = "ON" if self._pk_chase_scan_enabled(profile_key) else "OFF"
                target = self._pk_target(profile_key) or "<unset>"
                self._print(
                    f"{_CLIENT}[{profile_key}] chs: {status} | target={target} "
                    f"| ch1={actions['k']!r}"
                )
                return True
            if arg:
                self._pk_set_target(arg, profile_key)
            target = self._pk_target(profile_key)
            if not target:
                self._print(f"{_WARN}No chase target set. Use t <name> before chs.")
                return True
            self._pk_set_chase_scan_enabled(True, profile_key, reason="manual")
            self._print(
                f"{_CLIENT}[{profile_key}] chs armed: target={target!r} "
                f"ch1={actions['k']!r}"
            )
            return True

        if cmd in ("t", "target"):
            if not arg or arg.lower() in ("status", "show"):
                target = self._pk_target(profile_key) or "<unset>"
                self._print(
                    f"{_CLIENT}[{profile_key}] chase target: {target} | "
                    f"ch1={actions['k']!r} ch2={actions['l']!r} ch3={actions['m']!r}"
                )
                return True
            if arg.lower() in ("clear", "off", "none", "reset"):
                self._pk_set_target("", profile_key)
                self._print(f"{_CLIENT}[{profile_key}] chase target cleared.")
                return True
            self._pk_set_target(arg, profile_key)
            self._print(
                f"{_CLIENT}[{profile_key}] chase target -> {self._pk_target(profile_key)!r}"
            )
            return True

        action_slot = cmd[-1] if cmd in ("ch1", "ch2", "ch3") else ""
        if cmd == "ch":
            if not arg or arg.lower() in ("status", "show", "list"):
                target = self._pk_target(profile_key) or "<unset>"
                self._print(
                    f"{_CLIENT}[{profile_key}] chase actions: "
                    f"ch1={actions['k']!r} ch2={actions['l']!r} ch3={actions['m']!r} "
                    f"| target={target}"
                )
                return True
            subparts = arg.split(maxsplit=1)
            slot_token = subparts[0].strip().lower()
            slot_lookup = {
                "1": "1",
                "2": "2",
                "3": "3",
                "ch1": "1",
                "ch2": "2",
                "ch3": "3",
                "k": "1",
                "l": "2",
                "m": "3",
            }
            action_slot = slot_lookup.get(slot_token, "")
            if not action_slot:
                self._print(
                    f"{_CLIENT}Usage: ch <1|2|3> <cmd>|status|clear "
                    f"(target uses: t <name>|status|clear)"
                )
                return True
            arg = subparts[1].strip() if len(subparts) >= 2 else "status"

        action_key = {"1": "k", "2": "l", "3": "m"}[action_slot]
        if not arg or arg.lower() in ("status", "show"):
            self._print(f"{_CLIENT}[{profile_key}] ch{action_slot} -> {actions[action_key]!r}")
            return True
        if arg.lower() in ("clear", "off", "reset", "default"):
            actions[action_key] = _DEFAULT_PK_CHASE_ACTIONS[action_key]
            self._save_client_settings()
            self._print(
                f"{_CLIENT}[{profile_key}] ch{action_slot} reset -> {actions[action_key]!r}"
            )
            return True
        actions[action_key] = arg
        self._save_client_settings()
        self._print(f"{_CLIENT}[{profile_key}] ch{action_slot} -> {arg!r}")
        return True

    @staticmethod
    def _split_directional_chase_token(token: str) -> Optional[tuple[str, str]]:
        low = token.strip().lower()
        for action_key in _PK_CHASE_ACTION_KEYS:
            if not low.endswith(action_key):
                continue
            direction = low[: -len(action_key)]
            if direction in _DIRECTION_CMDS:
                return direction, action_key
        return None

    def _pk_render_chase_command(
        self,
        action_key: str,
        target: str,
        profile_key: Optional[str] = None,
    ) -> str:
        actions = self._pk_actions(profile_key)
        template = str(actions.get(action_key, action_key)).strip() or action_key
        if "{target}" in template:
            return template.replace("{target}", target)
        return f"{template} {target}".strip()

    def _apply_pk_spam_output_filter(self, text: str) -> str:
        if not text:
            return text
        if self._pk_spam_until <= 0.0:
            return text
        if time.monotonic() > self._pk_spam_until:
            self._pk_spam_until = 0.0
            self._pk_spam_target = ""
            self._pk_spam_echo_suppress.clear()
            return text

        lines = text.splitlines()
        if not lines:
            return text

        out: list[str] = []
        misses = 0
        suppress = {cmd.strip().lower() for cmd in self._pk_spam_echo_suppress if cmd.strip()}

        for raw_line in lines:
            plain = _ANSI_RE.sub("", raw_line).strip()
            prompt_only = ""
            payload = plain
            m_prompt = _WORLD_PROMPT_RE.search(plain)
            if m_prompt:
                prompt_only = m_prompt.group(0)
                if ">" in plain:
                    payload = plain.rsplit(">", 1)[-1].strip()
                else:
                    payload = ""
            low = payload.lower()
            if low == "they aren't here.":
                misses += 1
                if prompt_only:
                    out.append(prompt_only)
                continue
            if low in suppress:
                if prompt_only:
                    out.append(prompt_only)
                continue
            out.append(raw_line)

        if misses > 0:
            target = self._pk_spam_target or "target"
            out.append(f"{_CLIENT}(spamming x {target}) They aren't here. x{misses}")

        if not out:
            return ""
        rendered = "\n".join(out)
        if text.endswith("\n"):
            rendered += "\n"
        return rendered

    async def _handle_pk_chase_input(self, raw: str) -> bool:
        parts = raw.strip().split(maxsplit=1)
        if not parts:
            return False
        token = parts[0].lower()
        arg = self._normalize_pk_target(parts[1]) if len(parts) >= 2 else ""

        profile_key = self._pk_profile_key()

        if token == "kkkk":
            if arg:
                self._pk_set_target(arg, profile_key)
            target = arg or self._pk_target(profile_key)
            if not target:
                self._print(f"{_WARN}No chase target set. Use t <name> or kkkk <name>.")
                return True
            chase_cmd = self._pk_render_chase_command("k", target, profile_key)
            self._pk_spam_target = target
            self._pk_spam_until = time.monotonic() + 8.0
            self._pk_spam_echo_suppress = {chase_cmd.lower(), "where"}
            self._print(f"{_CLIENT}(spamming x {target})")
            for _ in range(9):
                await self._send_user_command(chase_cmd)
            await self._send_user_command("where")
            return True

        move_cmd = ""
        action_key = ""
        if token in _PK_CHASE_ACTION_KEYS:
            action_key = token
        else:
            parsed = self._split_directional_chase_token(token)
            if parsed is None:
                return False
            move_cmd, action_key = parsed

        if arg:
            self._pk_set_target(arg, profile_key)
        target = arg or self._pk_target(profile_key)
        if not target:
            self._print(
                f"{_WARN}No chase target set. Use t <name> or {action_key} <name>."
            )
            return True

        if move_cmd:
            await self._send_user_command(move_cmd)
        chase_cmd = self._pk_render_chase_command(action_key, target, profile_key)
        await self._send_user_command(chase_cmd)
        return True

    async def _handle_levelling_alias(self, line: str) -> bool:
        """Support shorthand command: levelling start <area>."""
        parts = line.strip().split(maxsplit=2)
        if not parts:
            return False
        if parts[0].lower() not in ("levelling", "leveling"):
            return False

        if len(parts) < 2 or parts[1].lower() != "start":
            self._print(f"{_CLIENT}Usage: levelling start <levellingscriptname>")
            return True
        if len(parts) < 3 or not parts[2].strip():
            self._print(f"{_CLIENT}Usage: levelling start <levellingscriptname>")
            return True

        route_name = parts[2].strip()
        path = Path(self._config_dir) / "scripts.json"
        err = self.script.load(path)
        if err:
            self._print(f"{_ERR}{err}")
            return True

        err = self.script.start(route_name)
        if err:
            self._print(f"{_ERR}{err}")
            return True

        for pre_cmd in self.script.startup_commands():
            await self._enqueue(pre_cmd, source="script-prep")
            self._print(f"{_CLIENT}[script-prep] {pre_cmd}")
        return True

    async def _handle_go_alias(self, part: str, enqueue_source: str = "travel") -> bool:
        tokens = part.strip().split(maxsplit=1)
        if not tokens or tokens[0].lower() != "go":
            return False

        target = tokens[1].strip() if len(tokens) > 1 else ""
        if not target:
            self._print(f"{_CLIENT}Usage: go <landmark>|list")
            return True

        if target.lower() in ("list", "ls", "help", "?"):
            lines, err = self._landmark_listing()
            if err:
                self._print(f"{_WARN}{err}")
                return True
            self._print(f"{_CLIENT}Landmarks:\n" + "\n".join(lines))
            return True

        plan, err = self._resolve_go_plan(target)
        if err:
            self._print(f"{_WARN}{err}")
            return True
        if not plan:
            self._print(f"{_WARN}No route resolved for {target!r}.")
            return True

        label = str(plan.get("destination_label", plan.get("destination_key", target))).strip()
        profile = str(plan.get("profile", "default")).strip() or "default"
        route_name = str(plan.get("route_name", "inline")).strip() or "inline"
        mode = str(plan.get("mode", "direct")).strip() or "direct"
        cmds = plan.get("commands", [])
        if not isinstance(cmds, list):
            self._print(f"{_WARN}Resolved travel commands are invalid for {label!r}.")
            return True

        self._print(
            f"{_CLIENT}[travel] {label} via {route_name} "
            f"(profile={profile}, mode={mode}, steps={len(cmds)})"
        )
        for cmd in cmds:
            if not isinstance(cmd, str):
                continue
            rendered = cmd.strip()
            if not rendered:
                continue
            await self._enqueue(rendered, source=enqueue_source)
        return True

    # ------------------------------------------------------------------
    # Server output
    # ------------------------------------------------------------------

    def _on_server_data(self, text: str) -> None:
        """Sync callback called from read_loop."""
        self._update_last_room(text)
        masked = self.damage_mask.process(text)
        normalized = self._normalize_terminal_output(masked)
        normalized = self._apply_pk_spam_output_filter(normalized)
        if self._split_ui is not None:
            self._split_ui.append_main(normalized)
            capture_lines = self._extract_capture_lines(normalized)
            if capture_lines:
                self._capture_feed_lines.extend(capture_lines)
                if len(self._capture_feed_lines) > _MAX_CAPTURE_LINES:
                    self._capture_feed_lines = self._capture_feed_lines[-_MAX_CAPTURE_LINES:]
                self._refresh_capture_panes()
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
        prev_game_minutes = self.state.game_time_minutes
        prev_room = self.state.current_room
        prev_exits = list(self.state.current_exits)
        prev_mv = self.state.mv
        self._track_severe_incoming_hits(text, prev_hp)
        self.state.parse(text)
        self._reconcile_active_log(
            text,
            prev_room=prev_room,
            prev_exits=prev_exits,
            prev_mv=prev_mv,
        )
        await self._process_tell_policy(text)
        await self._maybe_sync_character_from_whoami(text)
        await self._process_pk_chase_scan(text, prev_room=prev_room)
        await self._process_spellup_capture(text)
        await self._process_needs_policy(text)
        if self.state.in_combat and self.script.is_running():
            self.script.hold_for_combat("Combat output detected")
        tick_delta = self._tick_delta(prev_game_minutes, self.state.game_time_minutes)
        affects_changed = False
        if tick_delta > 0:
            affects_changed |= self.affects.on_tick(tick_delta)
        affects_changed |= self.affects.process_output(text)
        self.script.on_output(text)
        notice = self.spell_maintainer.process_output(text)
        if notice:
            self._print(f"{_WARN}{notice}")
        self._emit_damage_map_if_ready()
        if affects_changed:
            self._refresh_capture_panes()

        if (not self.bridge_mode) or self.triggers.allow_in_bridge:
            # Bridge handles login/menu automation; avoid double-firing menu
            # triggers from the split client while in bridge mode.
            if self.bridge_mode and self._looks_like_login_menu_text(text):
                return
            for cmd, delay, action, threshold in self.triggers.process(text):
                if delay > 0:
                    await asyncio.sleep(delay)
                if action == "reroll":
                    await self._handle_reroll(cmd, threshold)
                else:
                    for part in cmd.split(";"):
                        part = part.strip()
                        if not part:
                            continue
                        if part.upper() in ("<ENTER>", "<RETURN>"):
                            self._print(f"{_TRIGGER}<ENTER>")
                            await self._enqueue("", source="trigger")
                            continue
                        if part.startswith("#"):
                            await self._handle_client_cmd(part[1:])
                        else:
                            self._print(f"{_TRIGGER}{part}")
                            # If a trigger engages a target, freeze route stepping immediately
                            # so movement cannot outrun combat state parsing.
                            if part.lower().startswith("kill "):
                                self.script.hold_for_combat("Kill trigger fired")
                            await self._enqueue(part, source="trigger")

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
            await self._enqueue("N", source="reroll")
        sys.stdout.flush()

    async def _enqueue(self, cmd: str, source: str = "generic") -> None:
        self._record_outgoing_command(cmd)
        if self._send_queue is None:
            self._send_queue = asyncio.Queue()
        await self._send_queue.put((cmd, source))
        self._log_input_debug(
            "queue.push",
            f"source={source} cmd={cmd!r} depth={self._send_queue.qsize()}",
        )

    @staticmethod
    def _is_direction_command(cmd: str) -> bool:
        token = cmd.strip().split()[0].lower() if cmd.strip() else ""
        return token in _DIRECTION_CMDS

    @staticmethod
    def _looks_like_login_menu_text(text: str) -> bool:
        clean = _ANSI_RE.sub("", text).lower()
        return any(marker in clean for marker in _LOGIN_MENU_MARKERS)

    async def _send_loop(self) -> None:
        """Drain the send queue, respecting the client rate limit."""
        if self._send_queue is None:
            self._send_queue = asyncio.Queue()
        while True:
            try:
                item = await self._send_queue.get()
                if isinstance(item, tuple) and len(item) == 2:
                    cmd, source = item
                else:
                    cmd, source = str(item), "generic"
                self._log_input_debug(
                    "queue.pop.send",
                    f"source={source} cmd={cmd!r} depth={self._send_queue.qsize()}",
                )

                # Preserve route position: defer (don't drop) script movement while combat is active.
                if source == "script" and self._is_direction_command(cmd):
                    while self.script.is_running() and (
                        self.state.in_combat or self.script.combat_waiting()
                    ):
                        await asyncio.sleep(0.15)

                    # Script was stopped while waiting: discard stale queued route movement.
                    if not self.script.is_running():
                        self._send_queue.task_done()
                        continue

                # Allow queued script/trigger commands to invoke local `go <landmark>`
                # expansion instead of sending literal text to the MUD server.
                if source != "travel" and await self._handle_go_alias(cmd, enqueue_source=source):
                    self._send_queue.task_done()
                    continue

                await self._wait_for_song_cast_window()
                sent = await self.client.send(cmd)
                self._start_song_cast_window_if_needed(cmd)
                self._log_input_debug(
                    "send.queue",
                    f"source={source} cmd={cmd!r} ok={sent}",
                )
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
                    await self._enqueue(cmd, source="auto-skill")
            except asyncio.CancelledError:
                break

    async def _spell_maintainer_loop(self) -> None:
        """Periodically maintain configured affects (combat vs idle policies)."""
        while True:
            try:
                await asyncio.sleep(self.spell_maintainer.cycle_interval)
                if not self._fighter_session:
                    continue
                cmd = self.spell_maintainer.next_command(
                    in_combat=self.state.in_combat,
                    active_affects=self.affects.active_affects(),
                )
                if cmd:
                    self._print(f"{_CLIENT}[spell-maint] {cmd}")
                    await self._enqueue(cmd, source="spell-maint")
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

        # ---- character profile ----
        elif cmd == "char":
            if len(parts) >= 2:
                name = parts[1].lower()
                self._activate_character(name)
                char_als = self.aliases.list_char_aliases()
                self._print(
                    f"{_CLIENT}Active character: {name!r}  "
                    f"({len(char_als)} char-specific aliases)"
                )
            else:
                char = self.aliases.active_character()
                self._print(
                    f"{_CLIENT}Active character: {char!r}" if char else f"{_CLIENT}No active character set."
                )

        elif cmd == "charalias":
            if len(parts) >= 3:
                name, expansion = parts[1], " ".join(parts[2:])
                try:
                    self.aliases.set_char_alias(name, expansion)
                    char = self.aliases.active_character()
                    self._print(f"{_CLIENT}[{char}] alias set: {name!r} -> {expansion!r}")
                except ValueError as exc:
                    self._print(f"{_ERR}{exc}")
            elif len(parts) == 2:
                removed = self.aliases.remove_char_alias(parts[1])
                if removed:
                    self._print(f"{_CLIENT}Char alias {parts[1]!r} removed.")
                else:
                    self._print(f"{_CLIENT}No char alias named {parts[1]!r}.")
            else:
                self._print(f"{_CLIENT}Usage: #charalias <name> <expansion>  or  #charalias <name>  to remove")

        elif cmd == "charaliases":
            char = self.aliases.active_character()
            als = self.aliases.list_char_aliases()
            if als:
                lines = [f"Char aliases [{char}]:"] + [f"  {k:<12} -> {v}" for k, v in sorted(als.items())]
                self._print("\n".join(lines))
            else:
                self._print(f"{_CLIENT}No char-specific aliases for {char!r}." if char else f"{_CLIENT}No active character set.")

        elif cmd == "setheal":
            active = self.aliases.active_character()
            if not active:
                self._print(f"{_ERR}No active character set. Use #char <name> first.")
            else:
                arg = raw.strip()[len(parts[0]):].strip() if parts else ""
                if not arg or arg.lower() == "status":
                    current = self.aliases.list_char_aliases().get("heal", "")
                    if current:
                        self._print(f"{_CLIENT}[{active}] heal -> {current}")
                    else:
                        self._print(f"{_CLIENT}[{active}] heal alias is not set.")
                elif arg.lower() in ("clear", "off", "reset"):
                    removed = self.aliases.remove_char_alias("heal")
                    if removed:
                        self._print(f"{_CLIENT}[{active}] heal alias cleared.")
                    else:
                        self._print(f"{_CLIENT}[{active}] heal alias was not set.")
                else:
                    alias_value = self._compose_heal_alias(arg)
                    try:
                        self.aliases.set_char_alias("heal", alias_value)
                        self._print(f"{_CLIENT}[{active}] heal -> {alias_value}")
                    except ValueError as exc:
                        self._print(f"{_ERR}{exc}")

        elif cmd in ("t", "target", "ch", "ch1", "ch2", "ch3", "chs", "chstp"):
            self._handle_pk_control_input(raw)

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

        elif cmd == "spellmaint":
            raw_tokens = raw.strip().split()
            if len(parts) == 1 or parts[1].lower() in ("status", "list"):
                auto = "ON" if self.spell_maintainer.enabled else "OFF"
                fighter = "ON" if self._fighter_session else "OFF"
                lines = [
                    f"Fighter session: {fighter}",
                    f"Spell maintainer: {auto}  (interval {self.spell_maintainer.cycle_interval}s)",
                    "Spells:",
                ]
                for sp in self.spell_maintainer.list_spells():
                    flag = "ON " if sp.enabled else "OFF"
                    contexts = []
                    if sp.maintain_in_combat:
                        contexts.append("combat")
                    if sp.maintain_out_of_combat:
                        contexts.append("idle")
                    if not contexts:
                        contexts.append("none")
                    refresh = (
                        f"<= {sp.refresh_at_or_below}"
                        if sp.refresh_at_or_below is not None
                        else "missing-only"
                    )
                    lines.append(
                        f"  [{flag}] pri={sp.priority} {sp.name:<18} cmd={sp.command!r} "
                        f"ctx={','.join(contexts)} refresh={refresh} cd={sp.cooldown}s"
                    )
                self._print("\n".join(lines))
            elif len(parts) >= 2 and parts[1].lower() in ("on", "off"):
                enabled = parts[1].lower() == "on"
                self.spell_maintainer.set_enabled(enabled)
                self._print(f"{_CLIENT}Spell maintainer {'enabled' if enabled else 'disabled'}.")
                if enabled and not self._fighter_session:
                    self._print(
                        f"{_WARN}Fighter session is OFF. Use #session fighter on to run the maintainer loop."
                    )
            elif len(raw_tokens) >= 3 and raw_tokens[-1].lower() in ("on", "off"):
                enabled = raw_tokens[-1].lower() == "on"
                spell_name = " ".join(raw_tokens[1:-1]).strip()
                ok = self.spell_maintainer.set_spell_enabled(spell_name, enabled)
                if ok:
                    self._print(
                        f"{_CLIENT}Maintained spell {spell_name!r} "
                        f"{'enabled' if enabled else 'disabled'}."
                    )
                else:
                    self._print(f"{_ERR}Unknown maintained spell: {spell_name!r}")
            else:
                self._print(
                    f"{_CLIENT}Usage: #spellmaint on|off|status|list or #spellmaint <name> on|off"
                )

        elif cmd == "session":
            mode = parts[1].lower() if len(parts) >= 2 else "status"
            action = parts[2].lower() if len(parts) >= 3 else "status"

            # Shorthand: #session on|off
            if mode in ("on", "off"):
                action = mode
                mode = "fighter"

            if mode == "status":
                fighter = "ON" if self._fighter_session else "OFF"
                auto = "ON" if self.spell_maintainer.enabled else "OFF"
                spells = self.spell_maintainer.list_spells()
                enabled_count = sum(1 for s in spells if s.enabled)
                self._print(
                    f"{_CLIENT}Fighter session: {fighter}. "
                    f"Spell maintainer: {auto}. "
                    f"Enabled maintained spells: {enabled_count}/{len(spells)}."
                )
            elif mode == "fighter" and action in ("on", "off"):
                enabled = action == "on"
                self._fighter_session = enabled
                self.spell_maintainer.set_enabled(enabled)
                count = self.spell_maintainer.set_all_spells_enabled(enabled)
                self._print(
                    f"{_CLIENT}Fighter session {'enabled' if enabled else 'disabled'}: "
                    f"spell maintainer {'ON' if enabled else 'OFF'} "
                    f"and {count} maintained spell(s) {'enabled' if enabled else 'disabled'}."
                )
            elif mode == "fighter" and action == "status":
                fighter = "ON" if self._fighter_session else "OFF"
                self._print(f"{_CLIENT}Fighter session: {fighter}.")
            else:
                self._print(
                    f"{_CLIENT}Usage: #session status | #session fighter on|off|status "
                    f"(shorthand: #session on|off)"
                )

        elif cmd in ("pvp", "risk"):
            action = parts[1].lower() if len(parts) >= 2 else "status"
            maintainers = self._maintainer_triggers()
            if action == "status":
                total = len(maintainers)
                enabled = sum(1 for t in maintainers if t.enabled)
                mode = "ON" if self._high_risk_mode else "OFF"
                self._print(
                    f"{_CLIENT}High-risk mode: {mode}. "
                    f"Maintainer triggers enabled: {enabled}/{total}."
                )
            elif action == "on":
                if self._high_risk_mode:
                    self._print(f"{_CLIENT}High-risk mode is already ON.")
                else:
                    self._high_risk_disabled_maintainers = {
                        t.name for t in maintainers if t.enabled
                    }
                    changed = self.triggers.set_group_enabled("maintainer", False)
                    self._high_risk_mode = True
                    self._print(
                        f"{_CLIENT}High-risk mode ON. "
                        f"Disabled {changed} maintainer trigger(s)."
                    )
            elif action == "off":
                if not self._high_risk_mode:
                    self._print(f"{_CLIENT}High-risk mode is already OFF.")
                else:
                    restored = 0
                    for name in sorted(self._high_risk_disabled_maintainers):
                        if self.triggers.set_enabled(name, True):
                            restored += 1
                    self._high_risk_disabled_maintainers.clear()
                    self._high_risk_mode = False
                    self._print(
                        f"{_CLIENT}High-risk mode OFF. "
                        f"Restored {restored} maintainer trigger(s)."
                    )
            else:
                self._print(f"{_CLIENT}Usage: #pvp on|off|status")

        elif cmd == "setsanc":
            if len(parts) < 2:
                self._print(f"{_CLIENT}Usage: #setsanc <spell cmd>")
            else:
                spell_cmd = " ".join(raw.strip().split()[1:]).strip()
                self._print(f"{_CLIENT}{self.script.set_sanc_spell(spell_cmd)}")

        elif cmd == "sethaste":
            if len(parts) < 2:
                self._print(f"{_CLIENT}Usage: #sethaste <spell cmd>")
            else:
                spell_cmd = " ".join(raw.strip().split()[1:]).strip()
                self._print(f"{_CLIENT}{self.script.set_haste_spell(spell_cmd)}")

        elif cmd == "setfly":
            if len(parts) < 2:
                self._print(f"{_CLIENT}Usage: #setfly <spell cmd>")
            else:
                spell_cmd = " ".join(raw.strip().split()[1:]).strip()
                self._print(f"{_CLIENT}{self.script.set_fly_spell(spell_cmd)}")

        elif cmd == "autowhoami":
            mode = parts[1].lower() if len(parts) >= 2 else "status"
            if mode in ("on", "off"):
                self._auto_whoami = mode == "on"
                self._save_client_settings()
                if not self._auto_whoami:
                    self._awaiting_whoami_response = False
                status = "enabled" if self._auto_whoami else "disabled"
                self._print(f"{_CLIENT}Auto-whoami {status}.")
            elif mode == "status":
                status = "ON" if self._auto_whoami else "OFF"
                self._print(f"{_CLIENT}Auto-whoami: {status}.")
            else:
                self._print(f"{_CLIENT}Usage: #autowhoami on|off|status")

        elif cmd == "inputdebug":
            mode = parts[1].lower() if len(parts) >= 2 else "status"
            if mode in ("on", "off"):
                self._input_debug = mode == "on"
                status = "ON" if self._input_debug else "OFF"
                self._print(f"{_CLIENT}Input debug: {status}.")
                if self._input_debug:
                    self._log_input_debug(
                        "status",
                        f"ui={self.ui_mode} bridge={self.bridge_mode} connected={self.client.connected}",
                    )
            elif mode == "status":
                status = "ON" if self._input_debug else "OFF"
                self._print(f"{_CLIENT}Input debug: {status}.")
            else:
                self._print(f"{_CLIENT}Usage: #inputdebug on|off|status")

        elif cmd == "setalign":
            tokens = raw.strip().split(maxsplit=2)
            if len(tokens) < 2:
                self._print(
                    f"{_CLIENT}Usage: #setalign <good|evil|neutral> [spell cmd]"
                )
            else:
                alignment = tokens[1]
                spell_cmd = tokens[2].strip() if len(tokens) >= 3 else ""
                self._print(
                    f"{_CLIENT}{self.script.set_protection_alignment(alignment, spell_cmd)}"
                )

        elif cmd in ("setprotect", "setprot"):
            if len(parts) < 2:
                self._print(f"{_CLIENT}Usage: #setprotect <spell cmd>")
            else:
                spell_cmd = " ".join(raw.strip().split()[1:]).strip()
                self._print(f"{_CLIENT}{self.script.set_protection_spell(spell_cmd)}")

        elif cmd == "setspellup":
            await self._handle_setspellup_cmd(raw, parts)

        elif cmd == "spellup":
            await self._handle_spellup_cmd()

        elif cmd == "telldraft":
            await self._handle_telldraft_cmd(parts)

        elif cmd == "needs":
            await self._handle_needs_cmd(raw, parts)

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
            self.spell_maintainer.save()
            self._save_client_settings()
            err = self.script.save()
            if err:
                self._print(f"{_WARN}{err}")
            self._print(f"{_CLIENT}Configs saved.")

        # ---- restart ----
        elif cmd == "restart":
            self.aliases.save()
            self.triggers.save()
            self.skills.save()
            self.spell_maintainer.save()
            self._save_client_settings()
            err = self.script.save()
            if err:
                self._print(f"{_WARN}{err}")
            self._print(f"{_CLIENT}Restarting client...")
            self._restart_requested = True
            self._running = False
            await self.client.disconnect()

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
                    self._print(f"{_CLIENT}Usage: #script start <route|area>")
                else:
                    route_name = parts[2]
                    err = self.script.start(route_name)
                    if err:
                        self._print(f"{_ERR}{err}")
                    else:
                        for pre_cmd in self.script.startup_commands():
                            await self._enqueue(pre_cmd, source="script-prep")
                            self._print(f"{_CLIENT}[script-prep] {pre_cmd}")

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

    def _load_client_settings(self) -> None:
        self._auto_whoami = True
        self._tell_nonimm_enabled = False
        self._tell_auto_send = False
        self._needs_enabled = True
        self._needs_cooldown_secs = 20.0
        self._needs_hunger_default = ""
        self._needs_thirst_default = ""
        # Safe baseline for current active character workflow.
        self._needs_hunger_by_char = {"noitamin": "eat sausage"}
        self._needs_thirst_by_char = {"noitamin": "drink decanter"}
        self._needs_last_sent_at = {"hunger": 0.0, "thirst": 0.0}
        self._spellup_references = list(_DEFAULT_SPELLUP_REFERENCES)
        self._spellup_by_char = {}
        self._spellup_capture_active = False
        self._spellup_capture_char = ""
        self._spellup_capture_lines = []
        self._spellup_capture_pending_cmds = deque()
        self._pk_target_by_char = {}
        self._pk_chase_actions_by_char = {}
        self._pk_chase_scan_enabled_by_char = {}
        self._pk_flee_scan_until_by_char = {}
        self._pk_last_scan_fire_room_by_char = {}
        self._pk_last_scan_fire_at_by_char = {}
        if not self._settings_path.exists():
            return
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data, dict):
            self._auto_whoami = bool(data.get("auto_whoami", True))
            self._tell_nonimm_enabled = bool(data.get("tell_nonimm_enabled", False))
            self._tell_auto_send = bool(data.get("tell_auto_send", False))
            self._needs_enabled = bool(data.get("needs_enabled", self._needs_enabled))
            try:
                cooldown = float(data.get("needs_cooldown_secs", self._needs_cooldown_secs))
            except (TypeError, ValueError):
                cooldown = self._needs_cooldown_secs
            self._needs_cooldown_secs = max(0.0, cooldown)
            self._needs_hunger_default = str(
                data.get("needs_hunger_default", self._needs_hunger_default)
            ).strip()
            self._needs_thirst_default = str(
                data.get("needs_thirst_default", self._needs_thirst_default)
            ).strip()
            loaded_hunger = self._clean_cmd_map(data.get("needs_hunger_by_char", {}))
            loaded_thirst = self._clean_cmd_map(data.get("needs_thirst_by_char", {}))
            if loaded_hunger:
                self._needs_hunger_by_char = loaded_hunger
            if loaded_thirst:
                self._needs_thirst_by_char = loaded_thirst
            loaded_refs = self._clean_spell_name_list(data.get("spellup_references", []))
            loaded_spellup = self._clean_spellup_map(data.get("spellup_by_char", {}))
            baseline_refs = sorted(
                {
                    self._normalize_spell_name(x)
                    for x in _DEFAULT_SPELLUP_REFERENCES
                    if self._spellup_name_allowed(x)
                }
            )
            if loaded_refs:
                self._spellup_references = sorted(set(loaded_refs) | set(baseline_refs))
            else:
                self._spellup_references = baseline_refs
            if loaded_spellup:
                self._spellup_by_char = loaded_spellup
            loaded_pk_targets = self._clean_cmd_map(data.get("pk_target_by_char", {}))
            loaded_pk_actions = self._clean_pk_chase_actions_map(
                data.get("pk_chase_actions_by_char", {})
            )
            loaded_pk_scan = self._clean_bool_map(data.get("pk_chase_scan_enabled_by_char", {}))
            if loaded_pk_targets:
                self._pk_target_by_char = loaded_pk_targets
            if loaded_pk_actions:
                self._pk_chase_actions_by_char = loaded_pk_actions
            if loaded_pk_scan:
                self._pk_chase_scan_enabled_by_char = loaded_pk_scan

    def _save_client_settings(self) -> None:
        data = {
            "auto_whoami": self._auto_whoami,
            "tell_nonimm_enabled": self._tell_nonimm_enabled,
            "tell_auto_send": self._tell_auto_send,
            "needs_enabled": self._needs_enabled,
            "needs_cooldown_secs": self._needs_cooldown_secs,
            "needs_hunger_default": self._needs_hunger_default,
            "needs_thirst_default": self._needs_thirst_default,
            "needs_hunger_by_char": self._needs_hunger_by_char,
            "needs_thirst_by_char": self._needs_thirst_by_char,
            "spellup_references": self._spellup_references,
            "spellup_by_char": self._spellup_by_char,
            "pk_target_by_char": self._pk_target_by_char,
            "pk_chase_actions_by_char": self._pk_chase_actions_by_char,
            "pk_chase_scan_enabled_by_char": self._pk_chase_scan_enabled_by_char,
        }
        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Could not save client settings: %s", exc)

    @staticmethod
    def _clean_cmd_map(raw: object) -> dict[str, str]:
        out: dict[str, str] = {}
        if not isinstance(raw, dict):
            return out
        for key, value in raw.items():
            k = str(key).strip().lower()
            cmd = str(value).strip()
            if k and cmd:
                out[k] = cmd
        return out

    @staticmethod
    def _clean_bool_map(raw: object) -> dict[str, bool]:
        out: dict[str, bool] = {}
        if not isinstance(raw, dict):
            return out
        for key, value in raw.items():
            k = str(key).strip().lower()
            if not k:
                continue
            out[k] = bool(value)
        return out

    @staticmethod
    def _clean_spell_name_list(raw: object) -> list[str]:
        if not isinstance(raw, list):
            return []
        cleaned = {
            MudApp._normalize_spell_name(str(item))
            for item in raw
            if MudApp._spellup_name_allowed(str(item))
        }
        return sorted(cleaned)

    @staticmethod
    def _clean_spellup_map(raw: object) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        if not isinstance(raw, dict):
            return out
        for key, value in raw.items():
            char_key = str(key).strip().lower()
            if not char_key or not isinstance(value, list):
                continue
            spells = MudApp._clean_spell_name_list(value)
            if spells:
                out[char_key] = spells
        return out

    @staticmethod
    def _clean_pk_action_map(raw: object) -> dict[str, str]:
        out = dict(_DEFAULT_PK_CHASE_ACTIONS)
        if not isinstance(raw, dict):
            return out
        for key, value in raw.items():
            action_key = str(key).strip().lower()
            if action_key not in _PK_CHASE_ACTION_KEYS:
                continue
            cmd = str(value).strip()
            if cmd:
                out[action_key] = cmd
        return out

    @staticmethod
    def _clean_pk_chase_actions_map(raw: object) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        if not isinstance(raw, dict):
            return out
        for key, value in raw.items():
            char_key = str(key).strip().lower()
            if not char_key:
                continue
            out[char_key] = MudApp._clean_pk_action_map(value)
        return out

    def _script_heal_route_steps(self, char: str) -> tuple[str, list[str]]:
        path = Path(self._config_dir) / "scripts.json"
        if not path.exists():
            return "", []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return "", []
        if not isinstance(data, dict):
            return "", []
        routes = data.get("routes", {})
        if not isinstance(routes, dict):
            return "", []

        base = str(data.get("heal_route", "to_healing")).strip() or "to_healing"
        char_key = char.strip().lower()
        candidates: list[str] = []
        if char_key:
            candidates.append(f"{base}_{char_key}")
        candidates.append(base)

        for route_name in candidates:
            raw_steps = routes.get(route_name)
            if not isinstance(raw_steps, list):
                continue
            cmds: list[str] = []
            for step in raw_steps:
                if not isinstance(step, dict):
                    continue
                cmd = str(step.get("cmd", "")).strip()
                if cmd:
                    cmds.append(cmd)
            if cmds:
                return route_name, cmds
        return "", []

    def _sync_heal_alias_from_script(self, char: str, announce: bool = False) -> bool:
        char_key = char.strip().lower()
        if not char_key:
            return False
        route_name, cmds = self._script_heal_route_steps(char_key)
        if not cmds:
            return False

        alias_value = "; ".join(cmds)
        current = self.aliases.list_char_aliases().get("heal", "").strip()
        if current != alias_value:
            try:
                self.aliases.set_char_alias("heal", alias_value)
            except ValueError:
                return False

        if announce:
            self._print(f"{_CLIENT}[{char_key}] heal synced from {route_name}: {alias_value}")
        return True

    @staticmethod
    def _normalize_landmark_token(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.strip().lower())

    @staticmethod
    def _extract_commands_from_steps(raw_steps: object) -> list[str]:
        commands: list[str] = []
        if isinstance(raw_steps, str):
            for part in raw_steps.split(";"):
                cmd = part.strip()
                if cmd:
                    commands.append(cmd)
            return commands
        if not isinstance(raw_steps, list):
            return commands
        for step in raw_steps:
            if isinstance(step, str):
                cmd = step.strip()
            elif isinstance(step, dict):
                cmd = str(step.get("cmd", "")).strip()
            else:
                cmd = ""
            if cmd:
                commands.append(cmd)
        return commands

    def _read_scripts_config_data(self) -> tuple[dict, str]:
        path = Path(self._config_dir) / "scripts.json"
        if not path.exists():
            return {}, f"scripts config not found: {path}"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {}, f"could not read scripts config: {exc}"
        if not isinstance(data, dict):
            return {}, "scripts config is not a JSON object."
        return data, ""

    def _parse_landmarks(self, data: dict) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
        raw = data.get("landmarks", {})
        if not isinstance(raw, dict):
            return {}, {}
        landmarks: dict[str, dict[str, object]] = {}
        alias_to_key: dict[str, str] = {}

        for raw_key, raw_meta in raw.items():
            key = str(raw_key).strip()
            if not key:
                continue
            label = key
            room = ""
            aliases: list[str] = []
            if isinstance(raw_meta, str):
                label = raw_meta.strip() or key
            elif isinstance(raw_meta, dict):
                label = str(
                    raw_meta.get("label")
                    or raw_meta.get("name")
                    or raw_meta.get("title")
                    or key
                ).strip() or key
                room = str(raw_meta.get("room", "")).strip()
                raw_aliases = raw_meta.get("aliases", [])
                if isinstance(raw_aliases, str):
                    raw_aliases = [raw_aliases]
                if isinstance(raw_aliases, list):
                    aliases = [str(v).strip() for v in raw_aliases if str(v).strip()]
            landmarks[key] = {"label": label, "room": room, "aliases": aliases}

            candidates = [key, label, room, *aliases]
            for candidate in candidates:
                tok = self._normalize_landmark_token(candidate)
                if tok and tok not in alias_to_key:
                    alias_to_key[tok] = key
        return landmarks, alias_to_key

    def _travel_profile_destinations(self, profile: object) -> dict[str, object]:
        if not isinstance(profile, dict):
            return {}
        raw = profile.get("destinations")
        if isinstance(raw, dict):
            return raw
        return {
            str(k): v
            for k, v in profile.items()
            if str(k) not in ("from_landmark", "notes", "comment")
        }

    @staticmethod
    def _travel_profile_from_landmark(profile: object) -> dict[str, object]:
        if not isinstance(profile, dict):
            return {}
        raw = profile.get("from_landmark", {})
        return raw if isinstance(raw, dict) else {}

    def _lookup_landmark_mapping_value(
        self,
        mapping: dict[str, object],
        wanted_key: str,
        alias_to_key: dict[str, str],
    ) -> object:
        if wanted_key in mapping:
            return mapping[wanted_key]
        wanted_norm = self._normalize_landmark_token(wanted_key)
        for raw_key, val in mapping.items():
            key_text = str(raw_key).strip()
            if not key_text:
                continue
            key_norm = self._normalize_landmark_token(key_text)
            canonical = alias_to_key.get(key_norm, key_text)
            if canonical == wanted_key or key_norm == wanted_norm:
                return val
        return None

    def _resolve_travel_spec_commands(
        self,
        spec: object,
        routes: dict[str, object],
    ) -> tuple[list[str], str]:
        if isinstance(spec, dict):
            if "route" in spec:
                return self._resolve_travel_spec_commands(spec.get("route"), routes)
            if "commands" in spec:
                return self._resolve_travel_spec_commands(spec.get("commands"), routes)
            return [], ""

        if isinstance(spec, str):
            route_name = spec.strip()
            if not route_name:
                return [], ""
            route_steps = routes.get(route_name)
            if route_steps is not None:
                return self._extract_commands_from_steps(route_steps), route_name
            inline = self._extract_commands_from_steps(route_name)
            return inline, "inline"

        if isinstance(spec, list):
            return self._extract_commands_from_steps(spec), "inline"

        return [], ""

    def _current_landmark_key(
        self,
        alias_to_key: dict[str, str],
    ) -> str:
        room = (self._last_room or "").strip()
        if not room:
            return ""
        return alias_to_key.get(self._normalize_landmark_token(room), "")

    def _resolve_go_plan(self, target: str) -> tuple[Optional[dict], str]:
        data, err = self._read_scripts_config_data()
        if err:
            return None, err

        landmarks, alias_to_key = self._parse_landmarks(data)
        if not landmarks:
            return None, "No landmarks configured in scripts.json (missing or empty 'landmarks')."

        wanted = target.strip()
        wanted_key = alias_to_key.get(self._normalize_landmark_token(wanted), "")
        if not wanted_key and wanted in landmarks:
            wanted_key = wanted
        if not wanted_key:
            known = ", ".join(sorted(landmarks.keys()))
            return None, f"Unknown landmark {wanted!r}. Known keys: {known}"

        routes = data.get("routes", {})
        routes_map = routes if isinstance(routes, dict) else {}
        travel = data.get("travel", {})
        if not isinstance(travel, dict):
            travel = {}

        char_key = (self._active_char or self.aliases.active_character() or "").strip().lower()
        profiles: list[tuple[str, object]] = []
        if char_key and char_key in travel:
            profiles.append((char_key, travel[char_key]))
        if "default" in travel:
            profiles.append(("default", travel["default"]))
        if not profiles and travel:
            profiles.append(("default", travel))

        origin_key = self._current_landmark_key(alias_to_key)
        attempts = 0

        for profile_name, profile in profiles:
            if origin_key:
                origin_map = self._travel_profile_from_landmark(profile)
                origin_spec = self._lookup_landmark_mapping_value(origin_map, origin_key, alias_to_key)
                if isinstance(origin_spec, dict):
                    spec = self._lookup_landmark_mapping_value(origin_spec, wanted_key, alias_to_key)
                    if spec is not None:
                        cmds, route_name = self._resolve_travel_spec_commands(spec, routes_map)
                        attempts += 1
                        if cmds:
                            return {
                                "destination_key": wanted_key,
                                "destination_label": landmarks[wanted_key].get("label", wanted_key),
                                "profile": profile_name,
                                "mode": f"from:{origin_key}",
                                "route_name": route_name,
                                "commands": cmds,
                            }, ""

            dest_map = self._travel_profile_destinations(profile)
            spec = self._lookup_landmark_mapping_value(dest_map, wanted_key, alias_to_key)
            if spec is None:
                continue
            cmds, route_name = self._resolve_travel_spec_commands(spec, routes_map)
            attempts += 1
            if cmds:
                return {
                    "destination_key": wanted_key,
                    "destination_label": landmarks[wanted_key].get("label", wanted_key),
                    "profile": profile_name,
                    "mode": "direct",
                    "route_name": route_name,
                    "commands": cmds,
                }, ""

        label = str(landmarks[wanted_key].get("label", wanted_key))
        if attempts:
            return None, f"Travel for {label!r} is configured but has no usable commands."
        if profiles:
            checked = ", ".join(name for name, _ in profiles)
            return None, f"No travel route configured for {label!r} in profiles: {checked}."
        return None, f"No travel profiles configured for {label!r}. Add a 'travel' section in scripts.json."

    def _landmark_listing(self) -> tuple[list[str], str]:
        data, err = self._read_scripts_config_data()
        if err:
            return [], err
        landmarks, alias_to_key = self._parse_landmarks(data)
        if not landmarks:
            return [], "No landmarks configured in scripts.json (missing or empty 'landmarks')."

        routes = data.get("routes", {})
        routes_map = routes if isinstance(routes, dict) else {}
        travel = data.get("travel", {})
        if not isinstance(travel, dict):
            travel = {}
        char_key = (self._active_char or self.aliases.active_character() or "").strip().lower()
        profiles: list[tuple[str, object]] = []
        if char_key and char_key in travel:
            profiles.append((char_key, travel[char_key]))
        if "default" in travel:
            profiles.append(("default", travel["default"]))
        if not profiles and travel:
            profiles.append(("default", travel))

        lines: list[str] = []
        for key in sorted(landmarks):
            label = str(landmarks[key].get("label", key)).strip() or key
            room = str(landmarks[key].get("room", "")).strip()
            status = "unset"
            for _name, profile in profiles:
                spec = self._lookup_landmark_mapping_value(
                    self._travel_profile_destinations(profile),
                    key,
                    alias_to_key,
                )
                if spec is None:
                    continue
                cmds, _route_name = self._resolve_travel_spec_commands(spec, routes_map)
                if cmds:
                    status = "ready"
                    break
                status = "configured-empty"
            room_text = f" room={room}" if room else ""
            lines.append(f"- {key}: {label} [{status}]{room_text}")
        return lines, ""

    def _activate_character(self, name: str, source: str = "manual") -> bool:
        char = name.strip().lower()
        if not char:
            return False
        changed = char != self._active_char
        self._active_char = char
        self.aliases.set_character(char)
        self._sync_heal_alias_from_script(char)
        if source == "whoami":
            if changed:
                char_als = self.aliases.list_char_aliases()
                self._print(
                    f"{_CLIENT}Active character auto-set from whoami: {char!r} "
                    f"({len(char_als)} char-specific aliases)"
                )
        return True

    @staticmethod
    def _looks_like_world_output(clean: str, state: GameState) -> bool:
        low = clean.lower()
        if "your selection? ->" in low:
            return False
        if _WORLD_PROMPT_RE.search(clean):
            return True
        if "[exits:" in low or "obvious exits" in low:
            return True
        if state.max_hp > 0 and state.max_mana > 0:
            return True
        return False

    @staticmethod
    def _extract_whoami_character(clean: str) -> Optional[str]:
        for raw_line in clean.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if _WORLD_PROMPT_RE.search(line):
                if ">" in line:
                    line = line.rsplit(">", 1)[-1].strip()
                else:
                    continue
                if not line:
                    continue
            for pat in _WHOAMI_PATTERNS:
                m = pat.search(line)
                if not m:
                    continue
                candidate = m.group("name").strip()
                low = candidate.lower()
                if low in _WHOAMI_STOPWORDS:
                    continue
                if len(low) < 2:
                    continue
                return low
        return None

    async def _maybe_sync_character_from_whoami(self, text: str) -> None:
        clean = _ANSI_RE.sub("", text)
        if self._looks_like_login_menu_text(clean):
            self._login_menu_seen = True

        now = time.monotonic()
        found = self._extract_whoami_character(clean)
        low = clean.lower()
        explicit_whoami = any(marker in low for marker in _WHOAMI_EXPLICIT_MARKERS)
        if found and (
            explicit_whoami
            or self._awaiting_whoami_response
            or not self._active_char
            or self._login_menu_seen
        ):
            self._awaiting_whoami_response = False
            self._activate_character(found, source="whoami")
            return

        if self._awaiting_whoami_response:
            if now - self._whoami_requested_at >= _WHOAMI_TIMEOUT_SECS:
                self._awaiting_whoami_response = False
            return

        if not self._auto_whoami:
            return
        if self._last_whoami_attempt > 0.0 and now - self._last_whoami_attempt < _WHOAMI_RETRY_SECS:
            return
        if not self._looks_like_world_output(clean, self.state):
            return
        if self._active_char and not self._login_menu_seen:
            return

        self._login_menu_seen = False
        self._awaiting_whoami_response = True
        self._whoami_requested_at = now
        self._last_whoami_attempt = now
        await self._enqueue("whoami", source="system")

    def _maintainer_triggers(self) -> list:
        return [
            t
            for t in self.triggers.list_triggers()
            if t.group.strip().lower() == "maintainer"
        ]

    @staticmethod
    def _compose_heal_alias(raw_steps: str) -> str:
        text = raw_steps.strip()
        if not text:
            return "recall"

        if ";" in text:
            steps = [s.strip() for s in text.split(";") if s.strip()]
        else:
            steps = [s.strip() for s in re.split(r"[\s,]+", text) if s.strip()]

        normalized: list[str] = []
        for step in steps:
            low = step.lower()
            if low in ("r", "rec", "recall"):
                normalized.append("recall")
            else:
                normalized.append(low)

        if not normalized:
            return "recall"
        if normalized[0] != "recall":
            normalized.insert(0, "recall")
        return "; ".join(normalized)

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
            current_room = self.state.current_room or self._last_room or "unknown"
            current_exits = list(self.state.current_exits or self._last_exits)
            current_mv = self.state.mv if self.state.mv > 0 else None
            self._active_cmd_log = {
                "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "start_room": current_room,
                "start_exits": current_exits,
                "start_mv": current_mv,
                "commands": [],
                "pending_direction_indices": [],
                "reconciled_transitions": 0,
                "last_reconcile_room": current_room,
                "last_reconcile_exits": current_exits,
                "last_reconcile_mv": current_mv,
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
            self._finalize_log_pending(self._active_cmd_log)
            purpose = " ".join(parts[2:]).strip() if len(parts) > 2 else "unclassified"
            entry = dict(self._active_cmd_log)
            pending = entry.pop("pending_direction_indices", [])
            reconciled = int(entry.pop("reconciled_transitions", 0))
            entry.pop("last_reconcile_room", None)
            entry.pop("last_reconcile_exits", None)
            entry.pop("last_reconcile_mv", None)
            entry["reconciler"] = {
                "pending_unresolved": len(pending) if isinstance(pending, list) else 0,
                "unexpected_relocations": reconciled,
            }
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
                f"(directions={entry['summary']['directions']}, "
                f"attempted={entry['summary']['directions_attempted']}, "
                f"other={entry['summary']['other']}, "
                f"reconciled={entry['summary']['reconciled']})."
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
        rooms = parse_rooms(clean)
        if not rooms:
            return
        latest = rooms[-1]
        self._last_room = latest.name
        self._last_exits = list(latest.exits)

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

    async def _handle_telldraft_cmd(self, parts: list[str]) -> None:
        sub = parts[1].lower() if len(parts) >= 2 else "status"

        if sub in ("status", "list"):
            pending = [d for d in self._tell_drafts if d.get("status") == "pending"]
            self._print(
                f"{_CLIENT}Tell drafts: pending={len(pending)} total={len(self._tell_drafts)} "
                f"non-imm={'ON' if self._tell_nonimm_enabled else 'OFF'} "
                f"autosend={'ON' if self._tell_auto_send else 'OFF'}"
            )
            if sub == "list" and pending:
                for d in pending[-10:]:
                    kind = "IMM" if d.get("is_imm") else "PLAYER"
                    self._print(
                        f"{_CLIENT}[#{d['id']}] {kind} from {d['sender']}: {d['message']} "
                        f"-> {d['reply']}"
                    )
            return

        if sub == "approve":
            if len(parts) < 3:
                self._print(f"{_CLIENT}Usage: #telldraft approve <id>")
                return
            try:
                draft_id = int(parts[2])
            except ValueError:
                self._print(f"{_ERR}Invalid draft id: {parts[2]!r}")
                return
            draft = next((d for d in self._tell_drafts if d["id"] == draft_id), None)
            if draft is None:
                self._print(f"{_ERR}Unknown tell draft id: {draft_id}")
                return
            if draft.get("status") != "pending":
                self._print(f"{_WARN}Tell draft #{draft_id} is already {draft.get('status')}.")
                return
            await self._enqueue(draft["reply"], source="tell-draft")
            draft["status"] = "approved"
            self._print(f"{_CLIENT}Approved tell draft #{draft_id}: {draft['reply']}")
            return

        if sub == "reject":
            if len(parts) < 3:
                self._print(f"{_CLIENT}Usage: #telldraft reject <id>")
                return
            try:
                draft_id = int(parts[2])
            except ValueError:
                self._print(f"{_ERR}Invalid draft id: {parts[2]!r}")
                return
            draft = next((d for d in self._tell_drafts if d["id"] == draft_id), None)
            if draft is None:
                self._print(f"{_ERR}Unknown tell draft id: {draft_id}")
                return
            draft["status"] = "rejected"
            self._print(f"{_CLIENT}Rejected tell draft #{draft_id}.")
            return

        if sub == "nonimm":
            if len(parts) < 3 or parts[2].lower() not in ("on", "off"):
                self._print(f"{_CLIENT}Usage: #telldraft nonimm on|off")
                return
            self._tell_nonimm_enabled = parts[2].lower() == "on"
            self._save_client_settings()
            state = "ON" if self._tell_nonimm_enabled else "OFF"
            self._print(f"{_CLIENT}Tell drafting for non-imm messages: {state}.")
            return

        if sub == "autosend":
            if len(parts) < 3 or parts[2].lower() not in ("on", "off"):
                self._print(f"{_CLIENT}Usage: #telldraft autosend on|off")
                return
            self._tell_auto_send = parts[2].lower() == "on"
            self._save_client_settings()
            state = "ON" if self._tell_auto_send else "OFF"
            self._print(f"{_CLIENT}Tell draft autosend: {state}.")
            return

        self._print(
            f"{_CLIENT}Usage: #telldraft status|list|approve <id>|reject <id>|"
            f"nonimm on|off|autosend on|off"
        )

    def _active_character_key(self) -> str:
        return (self._active_char or self.aliases.active_character() or "").strip().lower()

    def _spellup_char_key(self) -> str:
        key = self._active_character_key()
        return key if key else "default"

    @staticmethod
    def _normalize_spell_name(name: str) -> str:
        return re.sub(r"\s+", " ", name.strip().lower())

    @staticmethod
    def _strip_wrapping_quotes(text: str) -> str:
        t = text.strip()
        if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
            return t[1:-1].strip()
        return t

    @staticmethod
    def _spellup_exclusion_reason(name: str) -> str:
        clean = MudApp._normalize_spell_name(name)
        return _SPELLUP_EXCLUDED_REASONS.get(clean, "excluded from spellup")

    @staticmethod
    def _spellup_name_allowed(name: str) -> bool:
        clean = MudApp._normalize_spell_name(name)
        return bool(clean) and clean not in _SPELLUP_EXCLUDED_NAMES

    def _spellup_reference_set(self) -> set[str]:
        refs: set[str] = set()
        for item in self._spellup_references:
            clean = self._normalize_spell_name(item)
            if not clean or not self._spellup_name_allowed(clean):
                continue
            refs.add(clean)
        return refs

    async def _handle_setspellup_cmd(self, raw: str, parts: list[str]) -> None:
        action = parts[1].lower() if len(parts) >= 2 else ""
        char_key = self._spellup_char_key()

        if not action:
            await self._start_spellup_capture(char_key)
            return

        if action in ("status", "list"):
            spells = list(self._spellup_by_char.get(char_key, []))
            if not spells:
                self._print(f"{_CLIENT}Spellup list [{char_key}] is empty. Run #setspellup first.")
                return
            lines = [f"Spellup list [{char_key}] ({len(spells)} spell(s)):"]
            for idx, name in enumerate(spells, start=1):
                lines.append(f"  {idx:>2}. {name}")
            self._print("\n".join(lines))
            return

        if action == "drop":
            if len(parts) < 3:
                self._print(f"{_CLIENT}Usage: #setspellup drop <row>")
                return
            try:
                row = int(parts[2])
            except ValueError:
                self._print(f"{_ERR}Invalid row: {parts[2]!r}")
                return
            spells = list(self._spellup_by_char.get(char_key, []))
            if row < 1 or row > len(spells):
                self._print(
                    f"{_ERR}Row out of range for [{char_key}]: {row}. "
                    f"Use #setspellup list."
                )
                return
            removed = spells.pop(row - 1)
            if spells:
                self._spellup_by_char[char_key] = spells
            else:
                self._spellup_by_char.pop(char_key, None)
            self._save_client_settings()
            self._print(f"{_CLIENT}Removed spellup row {row} for [{char_key}]: {removed}")
            return

        if action == "reference":
            match = re.match(r"^\s*setspellup\s+reference\b(.*)$", raw, re.IGNORECASE)
            tail = match.group(1).strip() if match else ""
            if not tail:
                self._print(
                    f"{_CLIENT}Usage: #setspellup reference <spell name> "
                    f"| #setspellup reference list "
                    f"(example: #setspellup reference 'self projection')"
                )
                return
            if tail.lower() in ("list", "status"):
                refs = list(self._spellup_references)
                lines = [f"Spellup references ({len(refs)}):"]
                for idx, name in enumerate(refs, start=1):
                    lines.append(f"  {idx:>2}. {name}")
                self._print("\n".join(lines))
                return
            spell_name = self._normalize_spell_name(self._strip_wrapping_quotes(tail))
            if not spell_name:
                self._print(f"{_ERR}Reference spell name cannot be empty.")
                return
            if not self._spellup_name_allowed(spell_name):
                self._print(
                    f"{_WARN}Spellup reference ignored: {spell_name} "
                    f"({_SPELLUP_EXCLUDED_REASONS.get(spell_name, 'excluded')})."
                )
                return
            refs = self._spellup_reference_set()
            if spell_name in refs:
                self._print(f"{_CLIENT}Spellup reference already exists: {spell_name}")
                return
            self._spellup_references.append(spell_name)
            self._spellup_references = sorted(
                {self._normalize_spell_name(x) for x in self._spellup_references if x.strip()}
            )
            self._save_client_settings()
            self._print(
                f"{_CLIENT}Spellup reference added: {spell_name}. "
                f"Run #setspellup to refresh captured list."
            )
            return

        self._print(
            f"{_CLIENT}Usage: #setspellup | #setspellup list | #setspellup drop <row> | "
            f"#setspellup reference <spell name>|list"
        )

    async def _start_spellup_capture(self, char_key: str) -> None:
        if self._spellup_capture_active:
            self._print(
                f"{_WARN}Spellup capture already in progress for [{self._spellup_capture_char}]."
            )
            return
        self._spellup_capture_active = True
        self._spellup_capture_char = char_key
        self._spellup_capture_lines = []
        self._spellup_capture_pending_cmds = deque(_SPELLUP_CAPTURE_COMMANDS)
        self._print(
            f"{_CLIENT}Capturing spell list for [{char_key}] "
            f"(references={len(self._spellup_references)}, threshold=50%)."
        )
        await self._spellup_capture_advance_or_finish()

    async def _spellup_capture_advance_or_finish(self) -> None:
        while self._spellup_capture_pending_cmds:
            cmd = str(self._spellup_capture_pending_cmds.popleft()).strip()
            if not cmd:
                continue
            await self._enqueue(cmd, source="spellup-set")
            return
        self._finish_spellup_capture()

    @staticmethod
    def _extract_spells_from_line(line: str) -> list[tuple[str, int]]:
        # DSL can print two spells on one line; this regex is intentionally
        # finditer-based so both entries are captured.
        working = re.sub(r"^\s*Level\s+\d+\s*:\s*", "", line, flags=re.IGNORECASE)
        entries: list[tuple[str, int]] = []
        for match in _SPELL_ENTRY_RE.finditer(working):
            name = MudApp._normalize_spell_name(match.group("name"))
            if not name:
                continue
            try:
                practiced = int(match.group("pct"))
            except (TypeError, ValueError):
                continue
            entries.append((name, practiced))
        return entries

    def _extract_spellup_spells(self, lines: list[str]) -> list[str]:
        refs = self._spellup_reference_set()
        found: list[str] = []
        seen: set[str] = set()
        for line in lines:
            for name, practiced in self._extract_spells_from_line(line):
                if practiced < 50:
                    continue
                if not self._spellup_name_allowed(name):
                    continue
                if name not in refs:
                    continue
                if name in seen:
                    continue
                seen.add(name)
                found.append(name)
        return found

    async def _process_spellup_capture(self, text: str) -> None:
        if not self._spellup_capture_active:
            return
        clean = _ANSI_RE.sub("", text)
        if not clean:
            return
        for raw_line in clean.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if _SPELLS_PAGER_RE.search(line):
                # Keep capture flowing through paged `spells` output.
                await self._enqueue("", source="spellup-set")
                continue
            self._spellup_capture_lines.append(line)
            if len(self._spellup_capture_lines) > 2000:
                self._spellup_capture_lines = self._spellup_capture_lines[-2000:]
            low = line.lower()
            if (
                "you have not learned any spells" in low
                or "you have not learned any songs" in low
            ):
                await self._spellup_capture_advance_or_finish()
                return
            if _WORLD_PROMPT_RE.search(line):
                # DSL prompt marks the end of the command output block.
                await self._spellup_capture_advance_or_finish()
                return

    def _finish_spellup_capture(self) -> None:
        char_key = self._spellup_capture_char or self._spellup_char_key()
        spells = self._extract_spellup_spells(self._spellup_capture_lines)
        if spells:
            self._spellup_by_char[char_key] = spells
        else:
            self._spellup_by_char.pop(char_key, None)
        self._save_client_settings()

        self._spellup_capture_active = False
        self._spellup_capture_char = ""
        self._spellup_capture_lines = []
        self._spellup_capture_pending_cmds = deque()

        if not spells:
            self._print(
                f"{_WARN}No practiced buff spells (>=50%) matched references for [{char_key}]. "
                f"Use #setspellup reference <spell name> to expand matching."
            )
            return
        lines = [f"Spellup saved [{char_key}] ({len(spells)} spell(s)):"]
        for idx, name in enumerate(spells, start=1):
            lines.append(f"  {idx:>2}. {name}")
        self._print("\n".join(lines))

    async def _handle_spellup_cmd(self) -> None:
        char_key = self._spellup_char_key()
        spells = list(self._spellup_by_char.get(char_key, []))
        if not spells:
            self._print(
                f"{_WARN}No spellup list saved for [{char_key}]. Run #setspellup first."
            )
            return
        queued = 0
        skipped: list[str] = []
        for name in spells:
            if not self._spellup_name_allowed(name):
                skipped.append(self._normalize_spell_name(name))
                continue
            cmd = self._spellup_cast_command(name)
            if not cmd:
                continue
            await self._enqueue(cmd, source="spellup")
            queued += 1
        if skipped:
            unique = sorted({s for s in skipped if s})
            self._print(
                f"{_WARN}Skipped excluded spellup entries: {', '.join(unique)}."
            )
        self._print(f"{_CLIENT}Queued spellup for [{char_key}]: {queued} spell(s).")

    def _spellup_cast_command(self, spell_name: str) -> str:
        name = self._normalize_spell_name(spell_name)
        if not name:
            return ""
        if not self._spellup_name_allowed(name):
            return ""
        if name in _SPELLUP_DIRECT_CMDS:
            return _SPELLUP_DIRECT_CMDS[name]
        if name in _SPELLUP_SING_NAMES:
            if "'" in name:
                return f'sing "{name}"'
            return f"sing '{name}'"
        if name in _PROTECTION_SPELL_NAMES:
            # Protection alignment spells do not stack; always route through
            # the currently configured setprotect command.
            return self.script.current_protection_spell()
        if "'" in name:
            return f'c "{name}"'
        return f"c '{name}'"

    def _needs_command_for(self, need: str) -> str:
        char = self._active_character_key()
        if need == "hunger":
            if char:
                cmd = self._needs_hunger_by_char.get(char, "").strip()
                if cmd:
                    return cmd
            return self._needs_hunger_default.strip()
        if need == "thirst":
            if char:
                cmd = self._needs_thirst_by_char.get(char, "").strip()
                if cmd:
                    return cmd
            return self._needs_thirst_default.strip()
        return ""

    async def _process_needs_policy(self, text: str) -> None:
        if not self._needs_enabled:
            return
        clean = _ANSI_RE.sub("", text)
        if self._looks_like_login_menu_text(clean):
            return

        detected: list[str] = []
        for raw_line in clean.splitlines():
            low = raw_line.strip().lower().rstrip(".! ")
            if low == "you are thirsty":
                detected.append("thirst")
            elif low == "you are hungry":
                detected.append("hunger")

        if not detected:
            return

        now = time.monotonic()
        for need in detected:
            last = self._needs_last_sent_at.get(need, 0.0)
            if last > 0.0 and now - last < self._needs_cooldown_secs:
                continue
            cmd = self._needs_command_for(need)
            if not cmd:
                continue
            self._needs_last_sent_at[need] = now
            self._print(f"{_CLIENT}[needs] {need} -> {cmd}")
            await self._enqueue(cmd, source="needs")

    async def _handle_needs_cmd(self, raw: str, parts: list[str]) -> None:
        tokens = raw.strip().split(maxsplit=2)
        sub = tokens[1].lower() if len(tokens) >= 2 else "status"

        if sub in ("status", "list"):
            status = "ON" if self._needs_enabled else "OFF"
            char = self._active_character_key() or "default"
            hunger = self._needs_command_for("hunger") or "<unset>"
            thirst = self._needs_command_for("thirst") or "<unset>"
            self._print(
                f"{_CLIENT}Needs: {status}. cooldown={self._needs_cooldown_secs:.1f}s."
            )
            self._print(
                f"{_CLIENT}Needs commands ({char}): hunger={hunger!r} thirst={thirst!r}"
            )
            return

        if sub in ("on", "off"):
            self._needs_enabled = sub == "on"
            self._save_client_settings()
            self._print(f"{_CLIENT}Needs maintainer {'enabled' if self._needs_enabled else 'disabled'}.")
            return

        if sub == "cooldown":
            if len(tokens) < 3:
                self._print(f"{_CLIENT}Usage: #needs cooldown <seconds>")
                return
            try:
                value = float(tokens[2].strip())
            except ValueError:
                self._print(f"{_ERR}Invalid cooldown: {tokens[2]!r}")
                return
            self._needs_cooldown_secs = max(0.0, value)
            self._save_client_settings()
            self._print(f"{_CLIENT}Needs cooldown set to {self._needs_cooldown_secs:.1f}s.")
            return

        if sub in ("hunger", "thirst"):
            if len(tokens) < 3 or not tokens[2].strip():
                self._print(f"{_CLIENT}Usage: #needs {sub} <command>")
                return
            cmd_text = tokens[2].strip()
            char = self._active_character_key()
            if sub == "hunger":
                if char:
                    self._needs_hunger_by_char[char] = cmd_text
                else:
                    self._needs_hunger_default = cmd_text
            else:
                if char:
                    self._needs_thirst_by_char[char] = cmd_text
                else:
                    self._needs_thirst_default = cmd_text
            self._save_client_settings()
            scope = char if char else "default"
            self._print(f"{_CLIENT}Needs {sub} command set for {scope}: {cmd_text!r}")
            return

        self._print(
            f"{_CLIENT}Usage: #needs on|off|status | "
            f"#needs hunger|thirst <cmd> | #needs cooldown <seconds>"
        )

    def _extract_tell_events(self, clean: str) -> list[dict]:
        events: list[dict] = []
        for raw_line in clean.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            imm_match = _IMM_DIRECT_TELL_RE.match(line)
            if imm_match:
                events.append(
                    {
                        "sender": imm_match.group("name").strip(),
                        "message": imm_match.group("msg").strip(),
                        "is_imm": True,
                        "reason": "(An Imm) pattern",
                    }
                )
                continue

            tell_match = _DIRECT_TELL_RE.match(line)
            if tell_match:
                events.append(
                    {
                        "sender": tell_match.group("name").strip(),
                        "message": tell_match.group("msg").strip(),
                        "is_imm": False,
                        "reason": "direct tell",
                    }
                )
        return events

    def _tell_fingerprint(self, sender: str, message: str) -> str:
        return f"{sender.lower()}|{message.strip().lower()}"

    def _find_pending_tell_draft(self, sender: str, message: str) -> Optional[dict]:
        s = sender.strip().lower()
        m = message.strip()
        for draft in reversed(self._tell_drafts):
            if draft.get("status") != "pending":
                continue
            if draft.get("sender", "").strip().lower() != s:
                continue
            if draft.get("message", "").strip() != m:
                continue
            return draft
        return None

    async def _upgrade_draft_to_imm(self, draft: dict, reason: str) -> None:
        if draft.get("is_imm"):
            return
        draft["is_imm"] = True
        draft["reason"] = reason
        draft["reply"] = self._draft_tell_reply(draft["sender"], is_imm=True)
        self._print(
            f"{_CLIENT}[tell-draft #{draft['id']}] upgraded to IMM after verification."
        )
        self._print(f"{_CLIENT}[tell-draft #{draft['id']}] proposed: {draft['reply']}")
        if self._tell_auto_send and draft.get("status") == "pending":
            await self._enqueue(draft["reply"], source="tell-draft-auto")
            draft["status"] = "approved"

    def _draft_tell_reply(self, sender: str, is_imm: bool) -> str:
        if is_imm:
            return f"tell {sender} Thanks for the message. I am on it."
        return f"tell {sender} Thanks for reaching out."

    def _queue_tell_draft(self, sender: str, message: str, is_imm: bool, reason: str) -> Optional[dict]:
        if not is_imm and not self._tell_nonimm_enabled:
            return None
        reply = self._draft_tell_reply(sender, is_imm=is_imm)
        draft = {
            "id": self._next_tell_draft_id,
            "sender": sender,
            "message": message,
            "is_imm": is_imm,
            "reason": reason,
            "reply": reply,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._next_tell_draft_id += 1
        self._tell_drafts.append(draft)
        if len(self._tell_drafts) > _MAX_TELL_DRAFTS:
            self._tell_drafts = self._tell_drafts[-_MAX_TELL_DRAFTS:]
        kind = "IMM" if is_imm else "PLAYER"
        self._print(f"{_CLIENT}[tell-draft #{draft['id']}] {kind} {sender}: {message}")
        self._print(f"{_CLIENT}[tell-draft #{draft['id']}] proposed: {reply}")
        self._print(f"{_CLIENT}[tell-draft #{draft['id']}] approval required: #telldraft approve {draft['id']}")
        return draft

    def _process_whois_feedback(self, clean: str) -> Optional[tuple[str, bool]]:
        pending = self._pending_whois_name.strip().lower()
        if not pending:
            return None
        lines = [ln.strip().lower() for ln in clean.splitlines() if ln.strip()]
        if not lines:
            return None

        for line in lines:
            if pending not in line:
                continue
            if any(hint in line for hint in _WHOIS_IMM_HINTS):
                self._whois_role_cache[pending] = True
                self._pending_whois_name = ""
                self._pending_whois_at = 0.0
                return pending, True
            if any(hint in line for hint in _WHOIS_NONIMM_HINTS):
                self._whois_role_cache[pending] = False
                self._pending_whois_name = ""
                self._pending_whois_at = 0.0
                return pending, False

        if (
            self._pending_whois_at > 0.0
            and time.monotonic() - self._pending_whois_at >= _WHOIS_TIMEOUT_SECS
        ):
            self._whois_role_cache[pending] = False
            self._pending_whois_name = ""
            self._pending_whois_at = 0.0
            return pending, False
        return None

    async def _process_tell_policy(self, text: str) -> None:
        clean = _ANSI_RE.sub("", text)

        whois_result = self._process_whois_feedback(clean)
        if whois_result is not None:
            name_key, is_imm = whois_result
            if is_imm:
                msg = self._pending_tell_by_sender.get(name_key, "")
                if msg:
                    existing = self._find_pending_tell_draft(name_key, msg)
                    if existing is not None:
                        await self._upgrade_draft_to_imm(existing, reason="whois verification")
                    else:
                        draft = self._queue_tell_draft(
                            sender=name_key.title(),
                            message=msg,
                            is_imm=True,
                            reason="whois verification",
                        )
                        if draft is not None and self._tell_auto_send:
                            await self._enqueue(draft["reply"], source="tell-draft-auto")
                            draft["status"] = "approved"
            self._pending_tell_by_sender.pop(name_key, None)

        for ev in self._extract_tell_events(clean):
            sender = ev["sender"].strip()
            message = ev["message"].strip()
            is_imm = bool(ev["is_imm"])
            if not sender or not message:
                continue

            existing = self._find_pending_tell_draft(sender, message)
            if existing is not None and is_imm and not existing.get("is_imm"):
                await self._upgrade_draft_to_imm(existing, reason=ev["reason"])
                continue

            fp = self._tell_fingerprint(sender, message)
            if fp in self._recent_tell_fingerprints:
                continue
            self._recent_tell_fingerprints.append(fp)

            sender_key = sender.lower()
            if is_imm or self._whois_role_cache.get(sender_key, False):
                draft = self._queue_tell_draft(sender, message, is_imm=True, reason=ev["reason"])
                if draft is not None and self._tell_auto_send:
                    await self._enqueue(draft["reply"], source="tell-draft-auto")
                    draft["status"] = "approved"
                continue

            # Unknown tell sender: verify via whois and only draft if verified IMM.
            self._pending_tell_by_sender[sender_key] = message
            if sender_key not in self._whois_role_cache and self._pending_whois_name != sender_key:
                self._pending_whois_name = sender_key
                self._pending_whois_at = time.monotonic()
                await self._enqueue(f"whois {sender}", source="tell-verify")

            if self._tell_nonimm_enabled:
                draft = self._queue_tell_draft(sender, message, is_imm=False, reason=ev["reason"])
                if draft is not None and self._tell_auto_send:
                    await self._enqueue(draft["reply"], source="tell-draft-auto")
                    draft["status"] = "approved"

    def _extract_capture_lines(self, text: str) -> list[str]:
        clean = _ANSI_RE.sub("", text)
        out: list[str] = []
        for line in clean.splitlines():
            line = line.strip()
            if _WORLD_PROMPT_RE.search(line) and ">" in line:
                line = line.rsplit(">", 1)[-1].strip()
            low = line.lower()
            if not low:
                continue
            # DSL bracket-format: [Help] Name: 'msg', [Gossip] Name: 'msg', etc.
            if line.startswith("["):
                m = _CHANNEL_BRACKET_RE.match(line.strip())
                if m:
                    tag = m.group("tag").strip().lower()
                    if tag in _CHANNEL_TAGS:
                        rest = m.group("rest").strip()
                        mm = _CHANNEL_BRACKET_MSG_RE.match(rest)
                        if mm:
                            name = mm.group("name").strip()
                            msg = mm.group("msg")
                            out.append(f"{name} ({tag}) '{msg}'")
                        else:
                            out.append(line.strip())
                        continue
            # Normalized channel line: Name (channel) 'message'
            m = _CHANNEL_PAREN_RE.match(line.strip())
            if m:
                tag = m.group("tag").strip().lower()
                if tag in _CHANNEL_TAGS:
                    name = m.group("name").strip()
                    msg = m.group("msg")
                    out.append(f"{name} ({tag}) '{msg}'")
                    continue
            # OOC channel variants:
            #   (Imm) Name OOC: 'msg'
            #   (An Imm) Name OOC KINGDOM: 'msg'
            #   Name OOC: 'msg'
            #   Name OOC KINGDOM: 'msg'
            #   Name OOC CLAN: 'msg'
            m = _CHANNEL_IMM_OOC_RE.match(line.strip())
            if m:
                name = m.group("name").strip()
                scope = (m.group("scope") or "").strip().lower()
                msg = m.group("msg")
                tag = "imm ooc" if not scope else f"imm ooc {scope}"
                out.append(f"{name} ({tag}) '{msg}'")
                continue
            m = _CHANNEL_OOC_RE.match(line.strip())
            if m:
                name = m.group("name").strip()
                scope = (m.group("scope") or "").strip().lower()
                msg = m.group("msg")
                tag = "ooc" if not scope else f"ooc {scope}"
                out.append(f"{name} ({tag}) '{msg}'")
                continue
            # Colon-format channels:
            #   Name KINGDOM: 'msg'
            #   Name CLAN: 'msg'
            m = _CHANNEL_COLON_RE.match(line.strip())
            if m:
                tag = m.group("tag").strip().lower()
                if tag in _CHANNEL_TAGS:
                    name = m.group("name").strip()
                    msg = m.group("msg")
                    out.append(f"{name} ({tag}) '{msg}'")
                    continue
            # Gossip format channels:
            #   Name clan gossips 'msg'
            m = _CHANNEL_GOSSIPS_RE.match(line.strip())
            if m:
                raw_tag = m.group("tag").strip().lower()
                if raw_tag in _CHANNEL_TAGS:
                    tag = "gossip" if raw_tag == "gossip" else f"{raw_tag} gossip"
                    name = m.group("name").strip()
                    msg = m.group("msg")
                    out.append(f"{name} ({tag}) '{msg}'")
                    continue
            # Channel tell variant: You tell the group 'msg'
            m = _CHANNEL_TELL_RE.match(line.strip())
            if m:
                tag = m.group("tag").strip().lower()
                if tag in _CHANNEL_TAGS:
                    raw_name = m.group("name").strip()
                    name = "You" if raw_name.lower() == "you" else raw_name
                    msg = m.group("msg")
                    out.append(f"{name} ({tag}) '{msg}'")
                    continue
            # Short self-channel variant: You clan 'msg'
            m = _CHANNEL_SHORT_SELF_RE.match(line.strip())
            if m:
                tag = m.group("tag").strip().lower()
                if tag in _CHANNEL_TAGS:
                    name = "You"
                    msg = m.group("msg")
                    out.append(f"{name} ({tag}) '{msg}'")
                    continue
            # Direct tell:
            #   (Imm) Bob tells you 'msg'
            #   Bob tells you 'msg'
            m = _IMM_DIRECT_TELL_RE.match(line.strip())
            if m:
                name = m.group("name").strip()
                msg = m.group("msg")
                out.append(f"{name} (imm tell) '{msg}'")
                continue
            m = _DIRECT_TELL_RE.match(line.strip())
            if m:
                name = m.group("name").strip()
                msg = m.group("msg")
                out.append(f"{name} (tell) '{msg}'")
                continue
        return out

    def _record_outgoing_command(self, cmd: str) -> None:
        if not cmd.strip():
            return
        changed = self.affects.observe_outgoing_command(cmd)
        if changed:
            self._refresh_capture_panes()

    def _refresh_capture_panes(self) -> None:
        if self._split_ui is None:
            return
        capture = ""
        if self._capture_feed_lines:
            capture = "\n".join(self._capture_feed_lines) + "\n"
        self._split_ui.set_capture(capture)
        self._split_ui.set_affects(self.affects.render_window())

    @staticmethod
    def _tick_delta(old_minutes: int, new_minutes: int) -> int:
        if old_minutes < 0 or new_minutes < 0:
            return 0
        if new_minutes == old_minutes:
            return 0
        # Game prompt time may jump by many in-game minutes per tick
        # (for example 7:30 -> 8:00), but this is still a single tick event.
        return 1

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
        current_room = self.state.current_room or self._last_room
        current_exits = list(self.state.current_exits or self._last_exits)
        current_mv = self.state.mv if self.state.mv > 0 else None

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "command": rendered,
            "kind": kind,
        }
        if kind == "direction":
            entry["from_room"] = current_room
            entry["from_exits"] = current_exits
            if current_mv is not None:
                entry["mv_before"] = current_mv
            entry["moved"] = False
            entry["counted_direction"] = False
        self._active_cmd_log["commands"].append(entry)
        if kind == "direction":
            pending = self._active_cmd_log.get("pending_direction_indices")
            if not isinstance(pending, list):
                pending = []
                self._active_cmd_log["pending_direction_indices"] = pending
            pending.append(len(self._active_cmd_log["commands"]) - 1)

    @staticmethod
    def _summarize_commands(commands: list[dict]) -> dict:
        attempted_directions = sum(1 for c in commands if c.get("kind") == "direction")
        directions = sum(
            1 for c in commands if c.get("kind") == "direction" and bool(c.get("counted_direction"))
        )
        reconciled = sum(1 for c in commands if c.get("kind") == "reconcile")
        total = len(commands)
        other = max(total - attempted_directions - reconciled, 0)
        return {
            "total": total,
            "directions": directions,
            "directions_attempted": attempted_directions,
            "reconciled": reconciled,
            "other": other,
        }

    def _finalize_log_pending(self, log_entry: dict) -> None:
        pending = log_entry.get("pending_direction_indices")
        commands = log_entry.get("commands")
        if not isinstance(pending, list) or not isinstance(commands, list):
            return
        for idx in pending:
            if not isinstance(idx, int) or idx < 0 or idx >= len(commands):
                continue
            row = commands[idx]
            if not isinstance(row, dict):
                continue
            if row.get("kind") != "direction":
                continue
            if bool(row.get("moved")):
                continue
            row["moved"] = False
            row["move_reason"] = "unconfirmed"
            row["counted_direction"] = False
        pending.clear()

    @staticmethod
    def _confirm_direction_row(
        row: dict,
        *,
        reason: str,
        to_room: Optional[str],
        to_exits: list[str],
        mv_after: Optional[int],
    ) -> None:
        row["moved"] = True
        row["move_reason"] = reason
        row["to_room"] = to_room
        row["to_exits"] = list(to_exits)
        mv_before = row.get("mv_before")
        if isinstance(mv_before, int) and mv_before > 0 and isinstance(mv_after, int) and mv_after >= 0:
            row["mv_after"] = mv_after
            row["mv_delta"] = mv_after - mv_before
            row["counted_direction"] = mv_after < mv_before
        else:
            if isinstance(mv_after, int) and mv_after >= 0:
                row["mv_after"] = mv_after
            row["counted_direction"] = False

    def _reconcile_active_log(
        self,
        text: str,
        *,
        prev_room: Optional[str],
        prev_exits: list[str],
        prev_mv: int,
    ) -> None:
        if self._active_cmd_log is None:
            return

        log_entry = self._active_cmd_log
        commands = log_entry.get("commands")
        if not isinstance(commands, list):
            return

        pending = log_entry.get("pending_direction_indices")
        if not isinstance(pending, list):
            pending = []
            log_entry["pending_direction_indices"] = pending

        clean = _ANSI_RE.sub("", text)
        rooms = parse_rooms(clean)

        anchor_room = prev_room or log_entry.get("last_reconcile_room")
        raw_anchor_exits = prev_exits or log_entry.get("last_reconcile_exits", [])
        anchor_exits = list(raw_anchor_exits) if isinstance(raw_anchor_exits, list) else []

        mv_after = self.state.mv if self.state.mv > 0 else None

        for snap in rooms:
            changed = snap.name != anchor_room or list(snap.exits) != anchor_exits
            if not changed:
                continue

            if pending:
                idx = pending.pop(0)
                if isinstance(idx, int) and 0 <= idx < len(commands):
                    row = commands[idx]
                    if isinstance(row, dict) and row.get("kind") == "direction":
                        self._confirm_direction_row(
                            row,
                            reason="room_change",
                            to_room=snap.name,
                            to_exits=snap.exits,
                            mv_after=mv_after,
                        )
            else:
                commands.append(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "command": "<auto-reconcile>",
                        "kind": "reconcile",
                        "from_room": anchor_room or "unknown",
                        "to_room": snap.name,
                        "from_exits": anchor_exits,
                        "to_exits": list(snap.exits),
                        "note": "unexpected relocation detected (no pending direction)",
                    }
                )
                log_entry["reconciled_transitions"] = int(log_entry.get("reconciled_transitions", 0)) + 1

            anchor_room = snap.name
            anchor_exits = list(snap.exits)

        mv_drop = 0
        if prev_mv > 0 and isinstance(mv_after, int):
            mv_drop = max(prev_mv - mv_after, 0)
        if mv_drop > 0 and pending:
            confirmations = min(mv_drop, len(pending))
            for _ in range(confirmations):
                idx = pending.pop(0)
                if isinstance(idx, int) and 0 <= idx < len(commands):
                    row = commands[idx]
                    if isinstance(row, dict) and row.get("kind") == "direction":
                        to_room = self.state.current_room or anchor_room
                        to_exits = list(self.state.current_exits or anchor_exits)
                        self._confirm_direction_row(
                            row,
                            reason="mv_drop",
                            to_room=to_room,
                            to_exits=to_exits,
                            mv_after=mv_after,
                        )

        final_room = self.state.current_room or anchor_room
        final_exits = list(self.state.current_exits or anchor_exits)
        log_entry["last_reconcile_room"] = final_room
        log_entry["last_reconcile_exits"] = final_exits
        if isinstance(mv_after, int):
            log_entry["last_reconcile_mv"] = mv_after

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

    def _normalize_terminal_output(self, text: str) -> str:
        """
        Normalize telnet text for local rendering in classic/split clients.
        - Convert CR variants to LF to avoid prompt overwrite.
        - Keep only SGR ANSI codes; drop cursor/terminal control escapes.
        - Strip non-printing control bytes except tab/newline.
        - Force-reset style at chunk boundaries to prevent color bleed.
        """
        text = text.replace("\r\n", "\n")
        text = text.replace("\r", "\n")
        text = self._render_ansi_carry + text

        # Keep partial CSI at the end for the next chunk so we do not emit
        # broken escape fragments into the terminal.
        self._render_ansi_carry = ""
        last_esc = text.rfind("\x1b")
        if last_esc != -1:
            tail = text[last_esc:]
            if _CSI_PARTIAL_TAIL_RE.fullmatch(tail):
                text = text[:last_esc]
                self._render_ansi_carry = tail

        # Recover orphaned SGR markers (e.g. "[1;33m") so prompt_toolkit/classic
        # renderers interpret them as ANSI instead of literal text.
        text = _BARE_SGR_RE.sub(lambda m: f"\x1b{m.group(1)}", text)
        text = _OSC_RE.sub("", text)
        text = _ANSI_RE.sub(lambda m: m.group(0) if m.group(0).endswith("m") else "", text)
        text = _ESC_SINGLE_RE.sub("", text)
        text = re.sub(r"\x1b(?!\[)", "", text)
        text = _RENDER_CTRL_RE.sub("", text)
        if "\x1b[" in text and not text.endswith("\x1b[0m"):
            text += "\x1b[0m"
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

    def _on_input_debug_event(self, stage: str, detail: str) -> None:
        self._log_input_debug(stage, detail)

    def _log_input_debug(self, stage: str, detail: str) -> None:
        if not self._input_debug:
            return
        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._print(f"{_INPUTDBG}{now} {stage}: {detail}")

    def _print(self, msg: str) -> None:
        if self._split_ui is not None:
            self._split_ui.append_main(msg + "\n")
        else:
            print(msg, flush=True)
