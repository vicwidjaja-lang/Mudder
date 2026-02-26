"""
GameState - parses MUD output to track character stats and combat status.

Supports common Diku/Circle/ROM prompt formats like:
  <100hp 50m 200mv>
  HP: 100/200  Mana: 50/100  Move: 200/300 >
  [100/200H 50/100M 200/300V]
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patterns for various MUD prompt formats
# ---------------------------------------------------------------------------

# Format: <100hp 50m 200mv>  or  <100/200hp 50/100m 200/300mv>
_PROMPT_ANGLE = re.compile(
    r"<\s*(\d+)(?:/(\d+))?hp\s+(\d+)(?:/(\d+))?m\w*\s+(\d+)(?:/(\d+))?mv?\s*>",
    re.IGNORECASE,
)

# Format: HP: 100(200)  Mana: 50(100)  Move: 200(300)
_PROMPT_PAREN = re.compile(
    r"HP:\s*(\d+)\((\d+)\)\s+(?:Mana|Mn):\s*(\d+)\((\d+)\)\s+(?:Move|Mv|MV):\s*(\d+)\((\d+)\)",
    re.IGNORECASE,
)

# Format: HP: 100/200  Mana: 50/100  Move: 200/300
_PROMPT_SLASH = re.compile(
    r"HP:\s*(\d+)/(\d+)\s+(?:Mana|Mn):\s*(\d+)/(\d+)\s+(?:Move|Mv|MV):\s*(\d+)/(\d+)",
    re.IGNORECASE,
)

# Combat indicators
_COMBAT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"You (?:hit|miss|slash|bash|kick|pierce|crush|smash|pound|strike) ",
        r"(?:hits|misses|slashes|bashes|kicks|pierces|crushes|smashes) you",
        r"You are fighting",
        r"You're fighting",
        r"Combat round",
        r"(?:attempts to|tries to) flee",
        r"You attempt to flee",
    ]
]

_END_COMBAT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"is DEAD",
        r"You stop fighting",
        r"You flee",
        r"You couldn't escape",
        r"No longer fighting",
    ]
]

# Critical health strings (Diku/Circle)
_HP_CRITICAL = [
    "You wish that your wounds would stop BLEEDING so much!",
    "You are severely wounded",
    "You are in awful condition",
]
_HP_LOW = [
    "You are hurt and bleeding",
    "You look pretty hurt",
]


@dataclass
class GameState:
    """Mutable snapshot of the character's known state."""

    hp: int = 0
    max_hp: int = 0
    mana: int = 0
    max_mana: int = 0
    mv: int = 0
    max_mv: int = 0
    in_combat: bool = False
    combat_target: Optional[str] = None
    hp_critical: bool = False  # e.g. "You wish that your wounds..."
    hp_low: bool = False

    # Skill cooldown tracking lives in SkillsManager, not here.

    def hp_percent(self) -> float:
        if self.max_hp == 0:
            return 100.0
        return (self.hp / self.max_hp) * 100.0

    def mana_percent(self) -> float:
        if self.max_mana == 0:
            return 100.0
        return (self.mana / self.max_mana) * 100.0

    def parse(self, text: str) -> None:
        """Update state by scanning a chunk of MUD output."""
        self._parse_prompt(text)
        self._parse_combat(text)
        self._parse_condition(text)

    # ------------------------------------------------------------------

    def _parse_prompt(self, text: str) -> None:
        m = _PROMPT_ANGLE.search(text)
        if m:
            self.hp = int(m.group(1))
            self.max_hp = int(m.group(2)) if m.group(2) else self.hp
            self.mana = int(m.group(3))
            self.max_mana = int(m.group(4)) if m.group(4) else self.mana
            self.mv = int(m.group(5))
            self.max_mv = int(m.group(6)) if m.group(6) else self.mv
            return

        m = _PROMPT_PAREN.search(text)
        if m:
            self.hp, self.max_hp = int(m.group(1)), int(m.group(2))
            self.mana, self.max_mana = int(m.group(3)), int(m.group(4))
            self.mv, self.max_mv = int(m.group(5)), int(m.group(6))
            return

        m = _PROMPT_SLASH.search(text)
        if m:
            self.hp, self.max_hp = int(m.group(1)), int(m.group(2))
            self.mana, self.max_mana = int(m.group(3)), int(m.group(4))
            self.mv, self.max_mv = int(m.group(5)), int(m.group(6))

    def _parse_combat(self, text: str) -> None:
        for p in _END_COMBAT_PATTERNS:
            if p.search(text):
                self.in_combat = False
                self.combat_target = None
                return
        for p in _COMBAT_PATTERNS:
            if p.search(text):
                self.in_combat = True
                return

    def _parse_condition(self, text: str) -> None:
        self.hp_critical = any(s in text for s in _HP_CRITICAL)
        self.hp_low = self.hp_critical or any(s in text for s in _HP_LOW)
