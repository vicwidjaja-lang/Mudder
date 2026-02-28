#!/usr/bin/env python3
"""Mudder profile memory helper.

Commands:
  record-learning <text>
  record-function <text>
  write-summary <text>
  build-prompt
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".codex-mud"
SYSTEM_PROMPT_FILE = ROOT / "configs" / "system_prompt_dsl_operator.txt"
LEARNINGS_LOG = STATE_DIR / "learnings.jsonl"
COUNTS_FILE = STATE_DIR / "learning_counts.json"
PROFILE_CONTEXT = STATE_DIR / "profile_context.md"
LAST_SUMMARY = STATE_DIR / "last_session_summary.md"
FUNCTION_IDEAS = STATE_DIR / "function_ideas.md"

PROMOTION_THRESHOLD = 3
MAX_CONTEXT_ITEMS = 12
MAX_SUMMARY_CHARS = 2500


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_counts() -> dict:
    if not COUNTS_FILE.exists():
        return {}
    try:
        return json.loads(COUNTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_counts(counts: dict) -> None:
    COUNTS_FILE.write_text(json.dumps(counts, indent=2, sort_keys=True), encoding="utf-8")


def regenerate_profile_context(counts: dict) -> None:
    promoted = []
    for key, row in counts.items():
        try:
            count = int(row.get("count", 0))
        except Exception:
            count = 0
        if count >= PROMOTION_THRESHOLD:
            promoted.append((count, row.get("canonical", key)))

    promoted.sort(key=lambda x: (-x[0], x[1].lower()))
    promoted = promoted[:MAX_CONTEXT_ITEMS]

    lines = [
        "# Mud Player Learned Context",
        "",
        f"Auto-promoted learnings (threshold: {PROMOTION_THRESHOLD}).",
        "",
    ]
    if not promoted:
        lines.append("- None yet.")
    else:
        for count, text in promoted:
            lines.append(f"- ({count}x) {text}")

    PROFILE_CONTEXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def record_learning(text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("learning text is empty")

    ensure_state_dir()
    key = normalize(text)
    counts = load_counts()

    row = counts.get(key, {})
    count = int(row.get("count", 0)) + 1
    canonical = row.get("canonical") or text

    counts[key] = {
        "canonical": canonical,
        "count": count,
        "first_seen": row.get("first_seen") or utc_now(),
        "last_seen": utc_now(),
    }

    save_counts(counts)
    regenerate_profile_context(counts)

    LEARNINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with LEARNINGS_LOG.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "timestamp": utc_now(),
                    "learning": text,
                    "normalized_key": key,
                    "count": count,
                }
            )
            + "\n"
        )

    return count


def write_summary(text: str) -> None:
    text = text.strip()
    ensure_state_dir()
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS].rstrip() + "\n\n[truncated]"

    payload = (
        "# Last Mud Session Summary\n\n"
        f"Updated: {utc_now()}\n\n"
        f"{text}\n"
    )
    LAST_SUMMARY.write_text(payload, encoding="utf-8")


def record_function(text: str) -> None:
    text = text.strip()
    if not text:
        raise ValueError("function text is empty")

    ensure_state_dir()
    lines = []
    if FUNCTION_IDEAS.exists():
        lines = FUNCTION_IDEAS.read_text(encoding="utf-8").splitlines()
    if not lines:
        lines = [
            "# Mud Player Function Ideas",
            "",
            "Potential functions/scripts to improve autonomous gameplay and mapping.",
            "",
        ]

    lines.append(f"- [{utc_now()}] {text}")
    FUNCTION_IDEAS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def safe_read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def build_prompt() -> str:
    ensure_state_dir()
    system_prompt = safe_read(SYSTEM_PROMPT_FILE)
    context_block = safe_read(PROFILE_CONTEXT)
    summary_block = safe_read(LAST_SUMMARY)
    function_ideas_block = safe_read(FUNCTION_IDEAS)

    sections = []

    if system_prompt:
        sections.append(system_prompt)
        sections.append("")

    sections += [
        "Session startup:",
        "1) Launch the shared GUI client: `./shareplay-solo` (starts bridge silently and opens split UI).",
        "2) Operate and troubleshoot the live DSL session directly.",
        "3) Play autonomously; minimize asks unless blocked or risky.",
        "",
        "Memory protocol:",
        "- When context reaches 90%+, extract key learning(s) and record each with:",
        "  `python3 scripts/mud_profile_memory.py record-learning \"<learning>\"`",
        "- Repeated learnings are auto-promoted to profile context at 3 occurrences.",
        "- If gameplay/mapping friction suggests automation, raise a function idea with:",
        "  `python3 scripts/mud_profile_memory.py record-function \"<function idea with trigger/benefit>\"`",
        "- Before ending or compaction, overwrite last summary with:",
        "  `python3 scripts/mud_profile_memory.py write-summary \"<session summary>\"`",
    ]

    if context_block:
        sections.append("")
        sections.append("Promoted learned context:")
        sections.append(context_block)

    if summary_block:
        sections.append("")
        sections.append("Previous session summary:")
        sections.append(summary_block)

    if function_ideas_block:
        sections.append("")
        sections.append("Open function ideas backlog:")
        sections.append(function_ideas_block)

    return "\n".join(sections).strip() + "\n"


def usage() -> int:
    print("Usage: mud_profile_memory.py [record-learning|record-function|write-summary|build-prompt] [text]")
    return 2


def main() -> int:
    if len(sys.argv) < 2:
        return usage()

    cmd = sys.argv[1].strip().lower()
    arg = " ".join(sys.argv[2:]).strip()

    if cmd == "record-learning":
        if not arg:
            return usage()
        count = record_learning(arg)
        print(f"learning_count={count}")
        return 0

    if cmd == "write-summary":
        if not arg:
            return usage()
        write_summary(arg)
        print("summary_written=1")
        return 0

    if cmd == "record-function":
        if not arg:
            return usage()
        record_function(arg)
        print("function_recorded=1")
        return 0

    if cmd == "build-prompt":
        print(build_prompt(), end="")
        return 0

    return usage()


if __name__ == "__main__":
    raise SystemExit(main())
