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
                logger.debug("Loaded %d aliases from %s", len(self._aliases), self._path)
                return
            except Exception as exc:
                logger.warning("Could not load aliases (%s), using defaults.", exc)
        self._aliases = dict(_DEFAULT_ALIASES)
        self.save()

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        try:
            with open(self._path, "w") as fh:
                json.dump({"aliases": self._aliases}, fh, indent=2)
        except Exception as exc:
            logger.error("Could not save aliases: %s", exc)

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def expand(self, line: str) -> str:
        """
        Expand the first word if it matches an alias.
        Returns the (possibly unchanged) command string.
        """
        parts = line.split(maxsplit=1)
        if not parts:
            return line
        word = parts[0]
        if word in self._aliases:
            expansion = self._aliases[word]
            rest = (" " + parts[1]) if len(parts) > 1 else ""
            return expansion + rest
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
