"""
Affect tracking for split UI capture.

Tracks active affects, inferred casts, full `affects` snapshots, and tick-based
duration decay.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mKHJABCDsuhl]")
_AFFECTS_HEADER_RE = re.compile(
    r"you are affected by the following spells:",
    re.IGNORECASE,
)
_SPELL_LINE_RE = re.compile(
    r"\bSpell:\s*(?P<name>[^:]+?)\s*:\s*(?P<body>.+)$",
    re.IGNORECASE,
)
_CYCLES_RE = re.compile(r"\bfor\s+(-?\d+)\s+cycles?\b", re.IGNORECASE)
_PERMANENT_RE = re.compile(r"\bpermanent(?:ly)?\b", re.IGNORECASE)
_CAST_PREFIX_RE = re.compile(r"^\s*cast\s+(.+)$", re.IGNORECASE)
_CAST_QUOTED_RE = re.compile(r"""^\s*["']([^"']+)["'](?:\s+.*)?$""")
_QUAFF_RE = re.compile(r"^\s*(?:quaff|drink)\s+(.+?)\s*$", re.IGNORECASE)
_RECITE_RE = re.compile(r"^\s*recite\s+(.+?)\s*$", re.IGNORECASE)
_WORN_OFF_PATTERNS = (
    re.compile(r"^Your\s+(.+?)\s+spell has worn off\.?$", re.IGNORECASE),
    re.compile(r"^The effects of\s+(.+?)\s+have worn off\.?$", re.IGNORECASE),
)
_KNOWN_DROP_PATTERNS = (
    (re.compile(r"^You feel yourself slow down\.?$", re.IGNORECASE), "haste"),
    (re.compile(r"^The white aura around your body fades\.?$", re.IGNORECASE), "sanctuary"),
)
_KNOWN_GAIN_PATTERNS = (
    (re.compile(r"^You are surrounded by a white aura\.?$", re.IGNORECASE), "sanctuary"),
)
_LEADING_TIME_PREFIX_RE = re.compile(
    r"^(?:\s*(?:\[[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?\]|[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)\s+)+"
)


@dataclass
class AffectEntry:
    name: str
    duration_cycles: Optional[int] = None
    permanent: bool = False
    source: str = "unknown"
    updated_at: float = field(default_factory=time.monotonic)


@dataclass
class PendingSource:
    kind: str
    label: str
    key: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)


class AffectTracker:
    """Tracks active affects and emits concise window text + history logs."""

    def __init__(self, log_path: Optional[Path] = None) -> None:
        self._affects: dict[str, AffectEntry] = {}
        self._events: deque[str] = deque(maxlen=160)
        self._pending_sources: deque[PendingSource] = deque(maxlen=40)
        self._log_path = log_path
        self._collecting_snapshot = False
        self._snapshot_entries: dict[str, AffectEntry] = {}
        self._snapshot_started_at: float = 0.0

    def observe_outgoing_command(self, cmd: str) -> bool:
        """Record cast/quaff hints. Returns True when visible state changed."""
        raw = (cmd or "").strip()
        if not raw:
            return False
        self._prune_pending_sources()

        changed = False
        spell_name = self._extract_cast_spell(raw)
        if spell_name:
            norm = self._norm(spell_name)
            self._pending_sources.append(
                PendingSource(kind="cast", label=f"cast:{raw}", key=norm)
            )
            if norm not in self._affects:
                self._affects[norm] = AffectEntry(
                    name=spell_name,
                    duration_cycles=None,
                    permanent=False,
                    source=f"cast:{raw}",
                )
                self._event("add", f"+ {spell_name} duration=? (from cast)")
                changed = True
            return changed

        m = _QUAFF_RE.match(raw)
        if m:
            self._pending_sources.append(
                PendingSource(kind="potion", label=f"quaff:{m.group(1).strip()}")
            )
            self._event("source", f"pending source: quaff {m.group(1).strip()}")
            return False

        m = _RECITE_RE.match(raw)
        if m:
            self._pending_sources.append(
                PendingSource(kind="scroll", label=f"recite:{m.group(1).strip()}")
            )
            self._event("source", f"pending source: recite {m.group(1).strip()}")
            return False

        return False

    def on_tick(self, ticks: int = 1) -> bool:
        """Decrement known finite affect durations by tick count."""
        if ticks <= 0:
            return False
        changed = False
        for key in list(self._affects):
            aff = self._affects[key]
            if aff.permanent or aff.duration_cycles is None:
                continue
            aff.duration_cycles -= ticks
            aff.updated_at = time.monotonic()
            if aff.duration_cycles <= -1:
                self._event("expire", f"- {aff.name} removed (duration hit {aff.duration_cycles})")
                del self._affects[key]
                changed = True
            else:
                self._event("tick", f"{aff.name} -> {aff.duration_cycles}")
                changed = True
        if changed:
            self._window_log("tick")
        return changed

    def process_output(self, text: str) -> bool:
        """Parse output chunk for affects snapshots and wear-off lines."""
        if not text:
            return False
        self._prune_pending_sources()
        clean = _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
        changed = False
        for raw_line in clean.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if _AFFECTS_HEADER_RE.search(line):
                if self._collecting_snapshot:
                    changed |= self._finalize_snapshot("header-reset")
                self._collecting_snapshot = True
                self._snapshot_entries = {}
                self._snapshot_started_at = time.monotonic()
                self._event("snapshot", "affects snapshot started")
                continue

            if self._collecting_snapshot:
                parsed = self._parse_spell_line(line)
                if parsed is not None:
                    self._snapshot_entries[self._norm(parsed.name)] = parsed
                    continue
                changed |= self._finalize_snapshot("line-break")
                # Continue handling this same line as non-snapshot content.

            changed |= self._apply_wearoff_line(line)
            changed |= self._apply_gain_line(line)

        # Snapshot chunks can straddle network frames; keep collecting.
        # If the server stops mid-snapshot for too long, flush on next chunk.
        if self._collecting_snapshot and (time.monotonic() - self._snapshot_started_at) > 5.0:
            changed |= self._finalize_snapshot("timeout")
        return changed

    def render_window(self, max_affects: int = 40, max_events: int = 14) -> str:
        """
        Render the on-screen affects pane.

        The pane intentionally shows only current spell status. Event history is
        still logged via JSONL, but not displayed live to avoid per-tick echo
        noise in the UI.
        """
        _ = max_events  # kept for backward compatibility with existing callers
        lines: list[str] = []
        lines.append("Affects")
        lines.append("-------")
        rows = self._render_affect_rows(self._display_affects())[:max_affects]
        if rows:
            lines.extend(rows)
        else:
            lines.append("(none)")
        return "\n".join(lines) + "\n"

    def active_affects(self) -> list[dict]:
        """Structured snapshot used by tests and diagnostics."""
        return [
            {
                "name": aff.name,
                "duration_cycles": aff.duration_cycles,
                "permanent": aff.permanent,
                "source": aff.source,
            }
            for aff in self._display_affects()
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _norm(name: str) -> str:
        return " ".join(name.lower().split())

    @staticmethod
    def _extract_cast_spell(cmd: str) -> Optional[str]:
        m = _CAST_PREFIX_RE.match(cmd)
        if not m:
            return None
        rest = m.group(1).strip()
        qm = _CAST_QUOTED_RE.match(rest)
        if qm:
            spell = qm.group(1).strip()
            return spell or None

        words = rest.split()
        if not words:
            return None
        # Unquoted DSL-style cast syntax uses a single spell token
        # (possibly abbreviated), e.g. "cast stone self".
        spell = words[0]
        return spell or None

    def _parse_spell_line(self, line: str) -> Optional[AffectEntry]:
        m = _SPELL_LINE_RE.search(line)
        if not m:
            return None
        name = self._clean_display_name(m.group("name"))
        if not name:
            return None
        body = m.group("body")
        cycles: Optional[int] = None
        permanent = bool(_PERMANENT_RE.search(body))
        mm = _CYCLES_RE.search(body)
        if mm:
            try:
                cycles = int(mm.group(1))
            except ValueError:
                cycles = None
        return AffectEntry(
            name=name,
            duration_cycles=cycles,
            permanent=permanent,
            source="snapshot",
        )

    def _finalize_snapshot(self, reason: str) -> bool:
        self._collecting_snapshot = False
        snapshot = self._snapshot_entries
        self._snapshot_entries = {}

        changed = False
        old_keys = set(self._affects)
        new_keys = set(snapshot)

        removed = old_keys - new_keys
        for key in sorted(removed):
            name = self._affects[key].name
            del self._affects[key]
            self._event("drop", f"- {name} removed (missing from affects)")
            changed = True

        added = sorted(new_keys - old_keys)
        for key in added:
            snap = snapshot[key]
            source = self._source_for_new_affect(key, added_count=len(added))
            snap.source = source
            self._affects[key] = snap
            dur = "perm" if snap.permanent else ("?" if snap.duration_cycles is None else str(snap.duration_cycles))
            self._event("add", f"+ {snap.name} duration={dur} ({source})")
            changed = True

        for key in sorted(new_keys & old_keys):
            snap = snapshot[key]
            cur = self._affects[key]
            contradiction = (
                cur.duration_cycles != snap.duration_cycles
                or cur.permanent != snap.permanent
            )
            cur.name = snap.name
            cur.updated_at = time.monotonic()
            if contradiction:
                before = "perm" if cur.permanent else ("?" if cur.duration_cycles is None else str(cur.duration_cycles))
                after = "perm" if snap.permanent else ("?" if snap.duration_cycles is None else str(snap.duration_cycles))
                cur.duration_cycles = snap.duration_cycles
                cur.permanent = snap.permanent
                cur.source = "snapshot-refresh"
                self._event("contradiction", f"~ {cur.name} duration {before} -> {after} (snapshot)")
                changed = True

        self._event("snapshot", f"affects snapshot applied ({reason}) [{len(new_keys)} spell(s)]")
        self._window_log(f"snapshot:{reason}")
        return changed

    def _source_for_new_affect(self, key: str, added_count: int) -> str:
        now = time.monotonic()
        cast_match: Optional[PendingSource] = None
        cast_prefix_matches: list[PendingSource] = []
        potion_candidates: list[PendingSource] = []
        for src in self._pending_sources:
            if now - src.created_at > 45.0:
                continue
            if src.kind == "cast" and src.key == key:
                cast_match = src
                break
            if src.kind == "cast" and src.key and key.startswith(src.key):
                cast_prefix_matches.append(src)
            if src.kind == "potion":
                potion_candidates.append(src)
        if cast_match is not None:
            try:
                self._pending_sources.remove(cast_match)
            except ValueError:
                pass
            return cast_match.label
        if len(cast_prefix_matches) == 1:
            match = cast_prefix_matches[0]
            try:
                self._pending_sources.remove(match)
            except ValueError:
                pass
            return f"{match.label}:prefix"
        if len(potion_candidates) == 1 and added_count == 1:
            return potion_candidates[0].label
        return "snapshot"

    def _apply_wearoff_line(self, line: str) -> bool:
        drop_name: Optional[str] = None
        for pattern in _WORN_OFF_PATTERNS:
            m = pattern.match(line)
            if m:
                drop_name = m.group(1).strip()
                break
        if drop_name is None:
            lower = line.lower()
            for pattern, effect in _KNOWN_DROP_PATTERNS:
                if pattern.match(line):
                    drop_name = effect
                    break
                if "slow down" in lower and "feel yourself" in lower:
                    drop_name = effect
                    break
        if not drop_name:
            return False

        key = self._norm(drop_name)
        if key in self._affects:
            removed = self._affects.pop(key)
            self._event("drop", f"- {removed.name} removed (wear-off line)")
            self._window_log("wear-off")
            return True
        return False

    def _apply_gain_line(self, line: str) -> bool:
        add_name: Optional[str] = None
        for pattern, effect in _KNOWN_GAIN_PATTERNS:
            if pattern.match(line):
                add_name = effect
                break
        if not add_name:
            return False

        key = self._norm(add_name)
        if key in self._affects:
            self._affects[key].updated_at = time.monotonic()
            return False

        self._affects[key] = AffectEntry(
            name=add_name,
            duration_cycles=None,
            permanent=False,
            source="inferred:line",
        )
        self._event("add", f"+ {add_name} duration=? (inferred:line)")
        self._window_log("gain-line")
        return True

    def _render_affect_rows(self, affects: list[AffectEntry]) -> list[str]:
        def sort_key(item: AffectEntry) -> tuple[int, int, str]:
            if item.permanent:
                return (2, 10_000, item.name.lower())
            if item.duration_cycles is None:
                return (1, 9_999, item.name.lower())
            return (0, item.duration_cycles, item.name.lower())

        rows = []
        for aff in sorted(affects, key=sort_key):
            if aff.permanent:
                duration = "perm"
            elif aff.duration_cycles is None:
                duration = "?"
            else:
                duration = str(aff.duration_cycles)
            rows.append(f"{aff.name:<22} {duration:>4}")
        return rows

    def _display_affects(self) -> list[AffectEntry]:
        """
        Return deduplicated affects for UI + maintainer consumers.

        Duplicates can happen when short-form casts and snapshot names differ or
        when stale prefixed text slips into a name field; collapse those to one
        canonical row so the pane remains stable.
        """
        merged: dict[str, AffectEntry] = {}
        for aff in self._affects.values():
            clean_name = self._clean_display_name(aff.name)
            if not clean_name:
                continue
            key = self._norm(clean_name)
            candidate = AffectEntry(
                name=clean_name,
                duration_cycles=aff.duration_cycles,
                permanent=aff.permanent,
                source=aff.source,
                updated_at=aff.updated_at,
            )
            current = merged.get(key)
            if current is None or self._is_better_display_entry(candidate, current):
                merged[key] = candidate
        return sorted(merged.values(), key=lambda a: a.name.lower())

    @staticmethod
    def _is_better_display_entry(candidate: AffectEntry, current: AffectEntry) -> bool:
        def rank(item: AffectEntry) -> tuple[int, int, float]:
            if item.permanent:
                return (3, 10_000, item.updated_at)
            if item.duration_cycles is None:
                return (1, -1, item.updated_at)
            return (2, item.duration_cycles, item.updated_at)

        return rank(candidate) > rank(current)

    def _clean_display_name(self, raw_name: str) -> str:
        name = " ".join((raw_name or "").split())
        if not name:
            return ""
        # Defensive scrub: if a timestamp is prefixed into a spell label,
        # keep only the spell name for on-screen display and matching.
        name = _LEADING_TIME_PREFIX_RE.sub("", name).strip()
        return " ".join(name.split())

    def _event(self, kind: str, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"{stamp} {message}"
        self._events.append(line)
        self._append_json_log({"kind": kind, "message": message})

    def _window_log(self, reason: str) -> None:
        self._append_json_log(
            {
                "kind": "window",
                "reason": reason,
                "affects": [
                    {
                        "name": aff.name,
                        "duration_cycles": aff.duration_cycles,
                        "permanent": aff.permanent,
                        "source": aff.source,
                    }
                    for aff in sorted(self._affects.values(), key=lambda a: a.name.lower())
                ],
            }
        )

    def _append_json_log(self, payload: dict) -> None:
        if self._log_path is None:
            return
        entry = {"ts": datetime.now(timezone.utc).isoformat(), **payload}
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=True) + "\n")
        except Exception:
            return

    def _prune_pending_sources(self) -> None:
        now = time.monotonic()
        while self._pending_sources and now - self._pending_sources[0].created_at > 120.0:
            self._pending_sources.popleft()
