"""
DamageMask - collapses verbose combat damage lines into per-round summaries.
Works with DSL descriptor-based combat (no numeric damage values).
"""

import re
from collections import Counter


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mKHJABCDsuhl]")
_ROUND_HINT_RE = re.compile(
    r"(combat round|^\s*<\d+(?:/\d+)?hp\b|^\s*hp:\s*\d+[/\(]|^\s*\[\d+/\d+[hmv])",
    re.IGNORECASE,
)
_END_COMBAT_RE = re.compile(
    r"(is DEAD!|you stop fighting|you flee|no longer fighting)",
    re.IGNORECASE,
)

# "Your <weapon> [*** WORD ***|=== WORD ===|WORD] <target>[.!]"
_DEALT_HIT_RE = re.compile(
    r"^Your\s+\S+\s+(?:\*+\s+(\w+)\s+\*+|=+\s+(\w+)\s+=+|(\w+))\s+\S",
    re.IGNORECASE,
)

# "<mob>'s <weapon> [*** WORD ***|=== WORD ===|WORD] you[.!]"
_TAKEN_HIT_RE = re.compile(
    r"^.+?'s\s+\S+\s+(?:\*+\s+(\w+)\s+\*+|=+\s+(\w+)\s+=+|(\w+))\s+you[.!]",
    re.IGNORECASE,
)

# "<mob> dodges/parries your attack." — dealt miss from mob's perspective
_DEALT_MISS_RE = re.compile(r"^.+?\s+(?:dodges|parries)\s+your\s+attack\.", re.IGNORECASE)

# "You dodge/parry <mob>'s attack." — taken avoidance
_YOU_AVOID_RE = re.compile(r"^You\s+(?:dodge|parry)\s+", re.IGNORECASE)

# Lifetap/passive — pure noise, one line per hit
_LIFETAP_RE = re.compile(r"\bdraws\s+life\s+from\b", re.IGNORECASE)


def _descriptor(m: re.Match) -> str:
    """Return normalized descriptor string from a dealt/taken regex match."""
    g1, g2, g3 = m.group(1), m.group(2), m.group(3)
    if g1:
        return f"***{g1.upper()}***"
    if g2:
        return f"==={g2.upper()}==="
    return g3.upper() if g3 else "UNKNOWN"


class DamageMask:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._dealt: Counter = Counter()
        self._taken: Counter = Counter()

    def process(self, text: str) -> str:
        if not self.enabled:
            return text

        out: list[str] = []
        for raw_line in text.splitlines(keepends=True):
            clean = _ANSI_RE.sub("", raw_line).strip()

            # Flush summary at round boundaries or combat end.
            if clean and (_ROUND_HINT_RE.search(clean) or _END_COMBAT_RE.search(clean)):
                summary = self._flush_summary()
                if summary:
                    out.append(summary)

            if not clean:
                out.append(raw_line)
                continue

            # Lifetap — one line per hit, no useful info.
            if _LIFETAP_RE.search(clean):
                continue

            # Dealt hit/miss: "Your slash DESCRIPTOR target!"
            m = _DEALT_HIT_RE.match(clean)
            if m:
                desc = _descriptor(m)
                # Normalize "MISSES" verb to "MISS" for consistent display.
                if desc == "MISSES":
                    desc = "MISS"
                self._dealt[desc] += 1
                continue

            # Dealt miss from mob's perspective: "mob dodges your attack."
            if _DEALT_MISS_RE.match(clean):
                self._dealt["MISS"] += 1
                continue

            # Taken avoidance: "You dodge/parry mob's attack."
            if _YOU_AVOID_RE.match(clean):
                self._taken["DODGE"] += 1
                continue

            # Taken hit: "mob's weapon DESCRIPTOR you!"
            m = _TAKEN_HIT_RE.match(clean)
            if m:
                self._taken[_descriptor(m)] += 1
                continue

            out.append(raw_line)

        return "".join(out)

    def status_text(self) -> str:
        state = "ON" if self.enabled else "OFF"
        dealt_total = sum(self._dealt.values())
        taken_total = sum(self._taken.values())
        return (
            f"Damage mask: {state}. "
            f"Current round -> dealt={dealt_total} hits, taken={taken_total} hits"
        )

    def _flush_summary(self) -> str:
        if not self._dealt and not self._taken:
            return ""

        def fmt(c: Counter) -> str:
            if not c:
                return "0"
            total = sum(c.values())
            parts = [f"{d}×{n}" if n > 1 else d for d, n in c.most_common()]
            return f"{total}h [{' '.join(parts)}]"

        line = (
            f"\033[1;35m[DMG]\033[0m "
            f"Dealt: {fmt(self._dealt)}  Taken: {fmt(self._taken)}\n"
        )
        self._dealt.clear()
        self._taken.clear()
        return line
