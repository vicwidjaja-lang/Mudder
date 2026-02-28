"""
Damage map persistence for severe-hit labels.
Stores rolling low/high/avg ranges for dealt and taken samples.
"""

import json
from pathlib import Path
from typing import Dict


class DamageMap:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Dict[str, Dict[str, float]]] = {"dealt": {}, "taken": {}}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data["dealt"] = raw.get("dealt", {}) or {}
                self.data["taken"] = raw.get("taken", {}) or {}
        except Exception:
            # Keep empty defaults on malformed file.
            self.data = {"dealt": {}, "taken": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")

    def add(self, direction: str, label: str, amount: int) -> None:
        if direction not in ("dealt", "taken"):
            return
        if amount <= 0:
            return
        d = self.data[direction]
        row = d.get(label)
        if not row:
            d[label] = {"count": 1, "min": amount, "max": amount, "sum": amount}
        else:
            row["count"] = int(row.get("count", 0)) + 1
            row["min"] = min(int(row.get("min", amount)), amount)
            row["max"] = max(int(row.get("max", amount)), amount)
            row["sum"] = int(row.get("sum", 0)) + amount
        self.save()

    def clear(self) -> None:
        self.data = {"dealt": {}, "taken": {}}
        self.save()

    def report_lines(self) -> list[str]:
        lines: list[str] = []
        for direction in ("dealt", "taken"):
            lines.append(f"{direction.upper()}:")
            rows = self.data.get(direction, {})
            if not rows:
                lines.append("  (no samples)")
                continue
            for label in sorted(rows.keys()):
                row = rows[label]
                count = int(row.get("count", 0))
                low = int(row.get("min", 0))
                high = int(row.get("max", 0))
                total = int(row.get("sum", 0))
                avg = (total / count) if count else 0.0
                lines.append(f"  {label:<14} n={count:<4} range={low}-{high} avg={avg:.1f}")
        return lines

    def get_combined_range(self, label: str):
        rows = []
        for direction in ("dealt", "taken"):
            row = self.data.get(direction, {}).get(label)
            if row:
                rows.append(row)
        if not rows:
            return None
        low = min(int(r.get("min", 0)) for r in rows)
        high = max(int(r.get("max", 0)) for r in rows)
        count = sum(int(r.get("count", 0)) for r in rows)
        return low, high, count
