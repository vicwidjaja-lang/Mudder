"""
AliasManager - simple command alias expansion.

Aliases are stored as a JSON dict mapping short names to full commands.
The first word of user input is checked; if it matches an alias the whole
input is rewritten before sending.

Multi-word alias expansions are supported.  The remainder of the original
line (args) is appended after the expansion.

Example:
  alias "kk" -> "kill"
  user types:  "kk goblin"
  sent:        "kill goblin"
"""

import json
import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

_DEFAULT_ALIASES: Dict[str, str] = {
    # Combat shortcuts
    "kk": "kill",
    # Navigation (these are already short but listed for discoverability)
    # Stats / info
    "sc":  "score",
    "inv": "inventory",
    "eq":  "equipment",
    "af":  "affected",
    "wh":  "where",
    # Common actions
    "rec": "recall",
    "rs":  "rest",
    "slp": "sleep",
    "wk":  "wake",
    "fl":  "flee",
    "gt":  "get all",
}


class AliasManager:
    def __init__(self, config_dir: str):
        self._path = os.path.join(config_dir, "aliases.json")
        self._aliases: Dict[str, str] = {}
        self._char_aliases: Dict[str, str] = {}
        self._active_char: str = ""
        self._all_chars: Dict[str, Dict[str, str]] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as fh:
                    data = json.load(fh)
                self._aliases = data.get("aliases", {})
                self._all_chars = data.get("characters", {})
                logger.debug("Loaded %d aliases from %s", len(self._aliases), self._path)
                # Re-apply active character if already set.
                if self._active_char:
                    self._char_aliases = dict(self._all_chars.get(self._active_char, {}))
                return
            except Exception as exc:
                logger.warning("Could not load aliases (%s), using defaults.", exc)
        self._aliases = dict(_DEFAULT_ALIASES)
        self._all_chars = {}
        self.save()

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        try:
            with open(self._path, "w") as fh:
                json.dump(
                    {"aliases": self._aliases, "characters": self._all_chars},
                    fh,
                    indent=2,
                )
        except Exception as exc:
            logger.error("Could not save aliases: %s", exc)

    # ------------------------------------------------------------------
    # Character profiles
    # ------------------------------------------------------------------

    def set_character(self, name: str) -> None:
        """Activate character-specific alias overrides."""
        self._active_char = name.lower()
        self._char_aliases = dict(self._all_chars.get(self._active_char, {}))
        logger.debug("Active character: %s (%d char aliases)", self._active_char, len(self._char_aliases))

    def active_character(self) -> str:
        return self._active_char

    def set_char_alias(self, name: str, command: str) -> None:
        """Set a character-specific alias for the active character."""
        if not self._active_char:
            raise ValueError("No active character set. Use #char <name> first.")
        self._char_aliases[name] = command
        self._all_chars.setdefault(self._active_char, {})[name] = command
        self.save()

    def remove_char_alias(self, name: str) -> bool:
        if not self._active_char:
            return False
        char_map = self._all_chars.get(self._active_char, {})
        if name in char_map:
            del char_map[name]
            self._char_aliases.pop(name, None)
            self.save()
            return True
        return False

    def list_char_aliases(self) -> Dict[str, str]:
        return dict(self._char_aliases)

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def expand(self, line: str) -> str:
        """
        Expand the first word if it matches an alias.
        Character-specific aliases take priority over global ones.
        Returns the (possibly unchanged) command string.
        """
        parts = line.split(maxsplit=1)
        if not parts:
            return line
        word = parts[0]
        rest = (" " + parts[1]) if len(parts) > 1 else ""
        # Character-specific alias takes priority.
        for table in (self._char_aliases, self._aliases):
            if word in table:
                return table[word] + rest
        return line

    def add(self, name: str, command: str) -> None:
        self._aliases[name] = command
        self.save()

    def remove(self, name: str) -> bool:
        if name in self._aliases:
            del self._aliases[name]
            self.save()
            return True
        return False

    def list_aliases(self) -> Dict[str, str]:
        return dict(self._aliases)
