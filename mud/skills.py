"""
SkillsManager - priority-based auto-skill system.

Skills are evaluated periodically while connected.  Each skill has:
  - priority  (lower number = higher priority)
  - conditions (in_combat, hp_percent range, mana_percent range)
  - cooldown  (seconds before the same skill can fire again)
  - enabled   flag

Only ONE skill fires per evaluation cycle; the highest-priority applicable
skill that is not on cooldown is chosen.

Auto-skill is DISABLED by default.  The player must explicitly turn it on
with:  #autoskill on

NOTE: The cycle_interval (default 2 s) is deliberately conservative so we
don't flood the server and risk an IP ban.  You can tune this in skills.json.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .game_state import GameState

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    name: str
    command: str
    priority: int = 10              # lower = fires first
    enabled: bool = False
    cooldown: float = 4.0           # seconds between uses
    # Conditions that must ALL be true for the skill to fire
    require_combat: bool = True
    hp_min_pct: float = 0.0         # fire when HP% >= this
    hp_max_pct: float = 100.0       # fire when HP% <= this
    mana_min_pct: float = 0.0
    mana_max_pct: float = 100.0
    description: str = ""
    _last_used: float = field(default=0.0, repr=False, compare=False)

    def is_ready(self, state: GameState) -> bool:
        """Return True if this skill can fire right now."""
        if not self.enabled:
            return False
        if self.require_combat and not state.in_combat:
            return False
        hp_pct = state.hp_percent()
        if not (self.hp_min_pct <= hp_pct <= self.hp_max_pct):
            return False
        mana_pct = state.mana_percent()
        if not (self.mana_min_pct <= mana_pct <= self.mana_max_pct):
            return False
        if time.monotonic() - self._last_used < self.cooldown:
            return False
        return True

    def use(self) -> str:
        """Mark as used and return the command string."""
        self._last_used = time.monotonic()
        return self.command


_DEFAULT_SKILLS = [
    {
        "name": "bash",
        "command": "bash",
        "priority": 1,
        "enabled": False,
        "cooldown": 4.0,
        "require_combat": True,
        "hp_min_pct": 0,
        "hp_max_pct": 100,
        "description": "Bash the current opponent (warrior skill)",
    },
    {
        "name": "kick",
        "command": "kick",
        "priority": 2,
        "enabled": False,
        "cooldown": 3.0,
        "require_combat": True,
        "hp_min_pct": 0,
        "hp_max_pct": 100,
        "description": "Kick the current opponent",
    },
    {
        "name": "backstab",
        "command": "backstab",
        "priority": 1,
        "enabled": False,
        "cooldown": 6.0,
        "require_combat": False,
        "hp_min_pct": 0,
        "hp_max_pct": 100,
        "description": "Rogue opening move (use before combat starts)",
    },
    {
        "name": "flee_emergency",
        "command": "flee",
        "priority": 0,           # highest — overrides everything
        "enabled": False,
        "cooldown": 10.0,
        "require_combat": True,
        "hp_min_pct": 0,
        "hp_max_pct": 20,        # only when HP drops below 20 %
        "description": "Emergency flee when HP < 20% (DISABLED — enable with care)",
    },
]


class SkillsManager:
    def __init__(self, config_dir: str):
        self._path = os.path.join(config_dir, "skills.json")
        self.auto_skill: bool = False
        self.cycle_interval: float = 2.0   # seconds between auto-skill checks
        self._skills: Dict[str, Skill] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        raw_skills = []
        if os.path.exists(self._path):
            try:
                with open(self._path) as fh:
                    data = json.load(fh)
                self.auto_skill = data.get("auto_skill", False)
                self.cycle_interval = data.get("cycle_interval", 2.0)
                raw_skills = data.get("skills", [])
                logger.debug("Loaded %d skills.", len(raw_skills))
            except Exception as exc:
                logger.warning("Could not load skills (%s), using defaults.", exc)
                raw_skills = _DEFAULT_SKILLS
        else:
            raw_skills = _DEFAULT_SKILLS

        self._skills = {}
        for sd in raw_skills:
            s = Skill(
                name=sd["name"],
                command=sd["command"],
                priority=sd.get("priority", 10),
                enabled=sd.get("enabled", False),
                cooldown=sd.get("cooldown", 4.0),
                require_combat=sd.get("require_combat", True),
                hp_min_pct=sd.get("hp_min_pct", 0.0),
                hp_max_pct=sd.get("hp_max_pct", 100.0),
                mana_min_pct=sd.get("mana_min_pct", 0.0),
                mana_max_pct=sd.get("mana_max_pct", 100.0),
                description=sd.get("description", ""),
            )
            self._skills[s.name] = s

        if not os.path.exists(self._path):
            self.save()

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data: dict = {
            "auto_skill": self.auto_skill,
            "cycle_interval": self.cycle_interval,
            "skills": [],
        }
        for s in self._skills.values():
            data["skills"].append({
                "name": s.name,
                "command": s.command,
                "priority": s.priority,
                "enabled": s.enabled,
                "cooldown": s.cooldown,
                "require_combat": s.require_combat,
                "hp_min_pct": s.hp_min_pct,
                "hp_max_pct": s.hp_max_pct,
                "mana_min_pct": s.mana_min_pct,
                "mana_max_pct": s.mana_max_pct,
                "description": s.description,
            })
        try:
            with open(self._path, "w") as fh:
                json.dump(data, fh, indent=2)
        except Exception as exc:
            logger.error("Could not save skills: %s", exc)

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------

    def next_skill(self, state: GameState) -> Optional[str]:
        """
        Return the command for the highest-priority ready skill, or None.
        Calling this marks the chosen skill as used (cooldown starts).
        """
        if not self.auto_skill:
            return None
        candidates = sorted(
            (s for s in self._skills.values() if s.is_ready(state)),
            key=lambda s: s.priority,
        )
        if candidates:
            return candidates[0].use()
        return None

    # ------------------------------------------------------------------
    # Management
    # ------------------------------------------------------------------

    def set_enabled(self, name: str, enabled: bool) -> bool:
        if name in self._skills:
            self._skills[name].enabled = enabled
            self.save()
            return True
        return False

    def list_skills(self) -> List[Skill]:
        return sorted(self._skills.values(), key=lambda s: s.priority)

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def add(self, name: str, command: str, **kwargs) -> Skill:
        s = Skill(name=name, command=command, **kwargs)
        self._skills[name] = s
        self.save()
        return s

    def remove(self, name: str) -> bool:
        if name in self._skills:
            del self._skills[name]
            self.save()
            return True
        return False
