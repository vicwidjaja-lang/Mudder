"""
TriggerManager - regex-based auto-response triggers.

Each trigger watches incoming game text for a regex pattern.  When matched,
it schedules one or more commands to be sent to the server.

Capture groups ($1, $2, ...) from the regex can be referenced in the command
string, allowing dynamic responses like:
  pattern:  r"(\w+) tells you '(.+)'"
  command:  "tell $1 Got it!"

Triggers support:
  - enabled / disabled flag
  - per-trigger cooldown (seconds) to avoid rapid-fire loops
  - optional delay before the command fires
  - gag: if True, suppress the matched line from display
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class Trigger:
    name: str
    pattern: str
    command: str
    enabled: bool = True
    cooldown: float = 0.0          # seconds between fires of this trigger
    delay: float = 0.0             # seconds to wait before sending command
    gag: bool = False              # suppress matched line from display
    description: str = ""
    action: str = "send"           # "send" (default) or "reroll" (stat roller)
    threshold: int = 0             # used by action="reroll": target stat total
    _last_fired: float = field(default=0.0, repr=False, compare=False)
    _compiled: Optional[re.Pattern] = field(default=None, repr=False, compare=False)

    def compile(self) -> None:
        try:
            self._compiled = re.compile(self.pattern, re.IGNORECASE)
        except re.error as exc:
            logger.error("Bad regex for trigger %r: %s", self.name, exc)
            self._compiled = None

    def match(self, line: str) -> Optional[re.Match]:
        if not self.enabled or self._compiled is None:
            return None
        now = time.monotonic()
        if self.cooldown > 0 and (now - self._last_fired) < self.cooldown:
            return None
        return self._compiled.search(line)

    def fire(self, m: re.Match) -> Optional[str]:
        """Return the command to send, with $N group substitutions applied."""
        if not self.command:
            return None
        cmd = self.command
        for i, group in enumerate(m.groups(), start=1):
            if group is not None:
                cmd = cmd.replace(f"${i}", group)
        self._last_fired = time.monotonic()
        return cmd


_DEFAULT_TRIGGERS = [
    {
        "name": "flee_critical",
        "pattern": r"You wish that your wounds would stop BLEEDING",
        "command": "flee",
        "enabled": False,
        "cooldown": 15.0,
        "description": "Auto-flee when critically wounded (DISABLED by default — enable carefully)",
    },
    {
        "name": "wake_sleep",
        "pattern": r"You feel less tired",
        "command": "wake",
        "enabled": True,
        "cooldown": 5.0,
        "description": "Auto-wake when rested",
    },
    {
        "name": "stand_after_rest",
        "pattern": r"You stop resting",
        "command": "",
        "enabled": False,
        "cooldown": 2.0,
        "description": "Example: do something after you stop resting",
    },
    {
        "name": "log_death",
        "pattern": r"is DEAD!",
        "command": "",
        "enabled": False,
        "cooldown": 1.0,
        "description": "Fires when something dies — add a command if desired",
    },
]


class TriggerManager:
    def __init__(self, config_dir: str):
        self._path = os.path.join(config_dir, "triggers.json")
        self._triggers: Dict[str, Trigger] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        raw = []
        if os.path.exists(self._path):
            try:
                with open(self._path) as fh:
                    raw = json.load(fh).get("triggers", [])
                logger.debug("Loaded %d triggers.", len(raw))
            except Exception as exc:
                logger.warning("Could not load triggers (%s), using defaults.", exc)
                raw = _DEFAULT_TRIGGERS
        else:
            raw = _DEFAULT_TRIGGERS

        self._triggers = {}
        for td in raw:
            t = Trigger(
                name=td["name"],
                pattern=td["pattern"],
                command=td.get("command", ""),
                enabled=td.get("enabled", True),
                cooldown=td.get("cooldown", 0.0),
                delay=td.get("delay", 0.0),
                gag=td.get("gag", False),
                description=td.get("description", ""),
                action=td.get("action", "send"),
                threshold=td.get("threshold", 0),
            )
            t.compile()
            self._triggers[t.name] = t

        if not os.path.exists(self._path):
            self.save()

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data = []
        for t in self._triggers.values():
            data.append({
                "name": t.name,
                "pattern": t.pattern,
                "command": t.command,
                "enabled": t.enabled,
                "cooldown": t.cooldown,
                "delay": t.delay,
                "gag": t.gag,
                "description": t.description,
                "action": t.action,
                "threshold": t.threshold,
            })
        try:
            with open(self._path, "w") as fh:
                json.dump({"triggers": data}, fh, indent=2)
        except Exception as exc:
            logger.error("Could not save triggers: %s", exc)

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def process(self, text: str) -> List[Tuple[str, float, str, int]]:
        """
        Scan text line-by-line against all triggers.
        Returns list of (command, delay, action, threshold) tuples.
        """
        results: List[Tuple[str, float, str, int]] = []
        for line in text.splitlines():
            for trigger in self._triggers.values():
                m = trigger.match(line)
                if m:
                    cmd = trigger.fire(m)
                    if cmd:
                        results.append((cmd, trigger.delay, trigger.action, trigger.threshold))
        return results

    # ------------------------------------------------------------------
    # Management
    # ------------------------------------------------------------------

    def add(self, name: str, pattern: str, command: str, **kwargs) -> Trigger:
        t = Trigger(name=name, pattern=pattern, command=command, **kwargs)
        t.compile()
        self._triggers[name] = t
        self.save()
        return t

    def remove(self, name: str) -> bool:
        if name in self._triggers:
            del self._triggers[name]
            self.save()
            return True
        return False

    def set_enabled(self, name: str, enabled: bool) -> bool:
        if name in self._triggers:
            self._triggers[name].enabled = enabled
            self.save()
            return True
        return False

    def list_triggers(self) -> List[Trigger]:
        return list(self._triggers.values())

    def get(self, name: str) -> Optional[Trigger]:
        return self._triggers.get(name)
