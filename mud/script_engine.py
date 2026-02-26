"""
ScriptEngine - automated leveling/routing with combat and healing management.

State machine:
  IDLE         - not running
  WALKING      - stepping through the leveling route
  COMBAT_WAIT  - route paused; auto-skills fight while we wait for combat to end
  BUFFING      - recasting one or more expired maintenance spells
  HEALING      - navigating to healing room
  RETURNING    - navigating back from healing room

App commands (prefix #):
  #script load [file]      - load script config (default: configs/scripts.json)
  #script start <route>    - start running a named route
  #script pause            - pause execution (combat/healing still handled)
  #script resume           - resume after manual pause
  #script stop             - stop and return to IDLE
  #script status           - show current state
  #script routes           - list available routes

Config file format (JSON):
  {
    "loop": true,
    "heal_threshold_pct": 50,
    "full_health_pct": 90,
    "heal_route": "to_healing",
    "return_route": "from_healing",
    "routes": {
      "leveling": [
        {"cmd": "n", "delay": 1.2},
        {"cmd": "kill goblin", "delay": 0.5}
      ],
      "to_healing": [
        {"cmd": "recall", "delay": 2.0},
        {"cmd": "n", "delay": 1.0}
      ],
      "from_healing": [
        {"cmd": "s", "delay": 1.0}
      ]
    },
    "buffs": [
      {
        "spell": "cast armor",
        "expiry_pattern": "Your armor spell has worn off",
        "active": true
      }
    ]
  }
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Coroutine, Optional

from .game_state import GameState

logger = logging.getLogger(__name__)

_SCRIPT = "\033[1;35m[SCRIPT]\033[0m "


class ScriptStatus(Enum):
    IDLE        = "idle"
    WALKING     = "walking"
    COMBAT_WAIT = "combat_wait"
    BUFFING     = "buffing"
    HEALING     = "healing"
    RETURNING   = "returning"


@dataclass
class RouteStep:
    cmd: str
    delay: float = 1.0


@dataclass
class BuffSpec:
    spell: str           # Command to cast the buff
    expiry_pattern: str  # Regex that fires when the buff wears off
    active: bool = True  # Start assuming active (True) or cast immediately (False)

    def __post_init__(self) -> None:
        self._re = re.compile(self.expiry_pattern, re.IGNORECASE)

    def scan(self, text: str) -> bool:
        """Return True and mark expired if the expiry pattern is found in text."""
        if self.active and self._re.search(text):
            self.active = False
            logger.info("[script] Buff expired: %s", self.spell)
            return True
        return False


class ScriptEngine:
    """
    Tick-based state machine that drives automated leveling.

    External callers must:
      - call on_output(text) for every chunk of server text
      - await run() as an asyncio background task
    """

    def __init__(
        self,
        enqueue: Callable[[str], Coroutine],  # app._enqueue(cmd)
        get_state: Callable[[], GameState],   # lambda: app.state
        print_fn: Callable[[str], None],      # app._print
    ) -> None:
        self._enqueue   = enqueue
        self._get_state = get_state
        self._print     = print_fn

        # Config
        self._routes: dict[str, list[RouteStep]] = {}
        self._buffs: list[BuffSpec] = []
        self._heal_route: str = ""
        self._return_route: str = ""
        self._heal_threshold: float = 50.0
        self._full_health: float = 90.0
        self._loop_route: bool = True

        # Runtime
        self._status: ScriptStatus = ScriptStatus.IDLE
        self._active_route: str = ""
        self._step_idx: int = 0          # Current position in leveling route
        self._heal_step_idx: int = 0     # Current position in heal route
        self._return_step_idx: int = 0   # Current position in return route

        self._next_step_time: float = 0.0  # monotonic time for next route step
        self._buff_queue: list[BuffSpec] = []
        self._buff_next_time: float = 0.0  # monotonic time for next buff cast
        self._after_buff: ScriptStatus = ScriptStatus.WALKING

        self._manual_pause = asyncio.Event()
        self._manual_pause.set()  # set = not paused

        self._config_path: Optional[Path] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, path: Path) -> str:
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            return f"Failed to load script: {exc}"

        self._routes = {
            name: [RouteStep(**s) for s in steps]
            for name, steps in data.get("routes", {}).items()
        }
        self._buffs         = [BuffSpec(**b) for b in data.get("buffs", [])]
        self._heal_route    = data.get("heal_route", "")
        self._return_route  = data.get("return_route", "")
        self._heal_threshold = data.get("heal_threshold_pct", 50.0)
        self._full_health   = data.get("full_health_pct", 90.0)
        self._loop_route    = data.get("loop", True)
        self._config_path   = path

        self._print(
            f"{_SCRIPT}Loaded {len(self._routes)} route(s), {len(self._buffs)} buff(s) from {path}"
        )
        return ""

    def start(self, route_name: str) -> str:
        if not self._routes:
            return "No script loaded. Use  #script load  first."
        if route_name not in self._routes:
            return f"Unknown route '{route_name}'. Available: {', '.join(self._routes)}"
        self._active_route  = route_name
        self._step_idx      = 0
        self._next_step_time = 0.0
        self._status        = ScriptStatus.WALKING
        self._manual_pause.set()
        self._print(f"{_SCRIPT}Started route: {route_name}")
        return ""

    def pause(self) -> None:
        self._manual_pause.clear()
        self._print(f"{_SCRIPT}Paused.")

    def resume(self) -> None:
        self._manual_pause.set()
        self._next_step_time = 0.0  # Allow next step immediately
        self._print(f"{_SCRIPT}Resumed.")

    def stop(self) -> None:
        self._status = ScriptStatus.IDLE
        self._manual_pause.set()
        self._print(f"{_SCRIPT}Stopped.")

    def on_output(self, text: str) -> None:
        """Check every line of server output for buff expiry patterns."""
        for buff in self._buffs:
            buff.scan(text)

    def status_str(self) -> str:
        state = self._get_state()
        steps = self._routes.get(self._active_route, [])
        buff_info = (
            "  ".join(f"{b.spell}={'OK' if b.active else 'EXPIRED'}" for b in self._buffs)
            if self._buffs else "none"
        )
        return (
            f"Status : {self._status.value}\n"
            f"Route  : {self._active_route or 'none'}  "
            f"step {self._step_idx}/{len(steps)}"
            f"{'  (loop)' if self._loop_route else ''}\n"
            f"HP     : {state.hp}/{state.max_hp} ({state.hp_percent():.0f}%)\n"
            f"Buffs  : {buff_info}"
        )

    def routes_str(self) -> str:
        if not self._routes:
            return "No routes loaded."
        lines = ["Routes:"]
        for name, steps in self._routes.items():
            marker = ""
            if name == self._heal_route:
                marker = " [heal]"
            elif name == self._return_route:
                marker = " [return]"
            lines.append(f"  {name}{marker}  ({len(steps)} steps)")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Background task
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop — run as an asyncio background task."""
        while True:
            try:
                await asyncio.sleep(0.2)

                if self._status == ScriptStatus.IDLE:
                    continue

                await self._manual_pause.wait()

                state = self._get_state()
                await self._tick(state)

            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # State machine tick
    # ------------------------------------------------------------------

    async def _tick(self, state: GameState) -> None:
        if self._status == ScriptStatus.WALKING:
            await self._tick_walking(state)
        elif self._status == ScriptStatus.COMBAT_WAIT:
            self._tick_combat_wait(state)
        elif self._status == ScriptStatus.BUFFING:
            await self._tick_buffing(state)
        elif self._status == ScriptStatus.HEALING:
            await self._tick_healing(state)
        elif self._status == ScriptStatus.RETURNING:
            await self._tick_returning(state)

    async def _tick_walking(self, state: GameState) -> None:
        # --- Priority 1: interrupt for combat ---
        if state.in_combat:
            self._print(f"{_SCRIPT}Combat detected — pausing route at step {self._step_idx}")
            self._status = ScriptStatus.COMBAT_WAIT
            return

        # --- Priority 2: interrupt for low HP ---
        if self._needs_healing(state):
            self._print(
                f"{_SCRIPT}HP low ({state.hp_percent():.0f}%) — heading to healing room"
            )
            self._heal_step_idx = 0
            self._next_step_time = 0.0
            self._status = ScriptStatus.HEALING
            return

        # --- Priority 3: recast expired buffs ---
        expired = [b for b in self._buffs if not b.active]
        if expired:
            self._print(
                f"{_SCRIPT}Buffs expired: {', '.join(b.spell for b in expired)} — recasting"
            )
            self._buff_queue = list(expired)
            self._buff_next_time = 0.0
            self._after_buff = ScriptStatus.WALKING
            self._status = ScriptStatus.BUFFING
            return

        # --- Execute next route step when timer fires ---
        now = time.monotonic()
        if now < self._next_step_time:
            return

        steps = self._routes.get(self._active_route, [])
        if not steps:
            self._status = ScriptStatus.IDLE
            return

        step = steps[self._step_idx]
        logger.debug("[script] walking step %d: %s", self._step_idx, step.cmd)
        await self._enqueue(step.cmd)
        self._next_step_time = now + step.delay

        self._step_idx += 1
        if self._step_idx >= len(steps):
            if self._loop_route:
                self._step_idx = 0
            else:
                self._print(f"{_SCRIPT}Route complete.")
                self._status = ScriptStatus.IDLE

    def _tick_combat_wait(self, state: GameState) -> None:
        if state.in_combat:
            return  # Still fighting — auto-skills handle this

        # Combat ended
        self._print(f"{_SCRIPT}Combat ended")

        if self._needs_healing(state):
            self._print(f"{_SCRIPT}Post-combat HP low — heading to healing room")
            self._heal_step_idx = 0
            self._next_step_time = 0.0
            self._status = ScriptStatus.HEALING
            return

        expired = [b for b in self._buffs if not b.active]
        if expired:
            self._buff_queue = list(expired)
            self._buff_next_time = 0.0
            self._after_buff = ScriptStatus.WALKING
            self._status = ScriptStatus.BUFFING
            return

        self._print(f"{_SCRIPT}Resuming route at step {self._step_idx}")
        self._next_step_time = 0.0
        self._status = ScriptStatus.WALKING

    async def _tick_buffing(self, state: GameState) -> None:
        if not self._buff_queue:
            self._print(f"{_SCRIPT}Buffs refreshed — returning to {self._after_buff.value}")
            self._next_step_time = 0.0
            self._status = self._after_buff
            return

        now = time.monotonic()
        if now < self._buff_next_time:
            return

        buff = self._buff_queue.pop(0)
        self._print(f"{_SCRIPT}Casting buff: {buff.spell}")
        await self._enqueue(buff.spell)
        buff.active = True
        self._buff_next_time = now + 2.5  # Wait for cast + server message

    async def _tick_healing(self, state: GameState) -> None:
        heal_steps = self._routes.get(self._heal_route, [])

        # Still have heal route steps to walk
        if self._heal_step_idx < len(heal_steps):
            now = time.monotonic()
            if now < self._next_step_time:
                return
            step = heal_steps[self._heal_step_idx]
            logger.debug("[script] heal route step %d: %s", self._heal_step_idx, step.cmd)
            await self._enqueue(step.cmd)
            self._next_step_time = now + step.delay
            self._heal_step_idx += 1
            return

        # Heal route done — wait until fully healed
        if state.hp_percent() >= self._full_health:
            self._print(
                f"{_SCRIPT}Fully healed ({state.hp_percent():.0f}%) — heading back"
            )
            self._return_step_idx = 0
            self._next_step_time = 0.0
            self._status = ScriptStatus.RETURNING

    async def _tick_returning(self, state: GameState) -> None:
        return_steps = self._routes.get(self._return_route, [])

        if self._return_step_idx < len(return_steps):
            now = time.monotonic()
            if now < self._next_step_time:
                return
            step = return_steps[self._return_step_idx]
            logger.debug("[script] return route step %d: %s", self._return_step_idx, step.cmd)
            await self._enqueue(step.cmd)
            self._next_step_time = now + step.delay
            self._return_step_idx += 1
            return

        # Return route done — reapply buffs then resume leveling
        expired = [b for b in self._buffs if not b.active]
        if expired:
            self._buff_queue = list(expired)
            self._buff_next_time = 0.0
            self._after_buff = ScriptStatus.WALKING
            self._status = ScriptStatus.BUFFING
        else:
            self._print(f"{_SCRIPT}Back at leveling area — resuming route at step {self._step_idx}")
            self._next_step_time = 0.0
            self._status = ScriptStatus.WALKING

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _needs_healing(self, state: GameState) -> bool:
        if not self._heal_route:
            return False
        return state.hp_percent() < self._heal_threshold
