#!/usr/bin/env python3
"""Mudder profile memory helper.

Commands:
  record-learning <text>
  record-evidence <learning_text> <support|contradict> <source> [detail]
  merge-learnings <from_text> <to_text> [note]
  set-learning-status <active|conditional|stale> <text>
  supersede-learning <old_text> <new_text> [note]
  run-auto-stale
  list-learnings [active|conditional|stale]
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
from typing import Optional

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
MAX_EVIDENCE_ITEMS = 20
MIN_PROMOTION_CONFIDENCE = 0.75
AUTO_CONDITIONAL_AGE_DAYS = 21
AUTO_STALE_AGE_DAYS = 60
AUTO_CONDITIONAL_CONTRADICTIONS = 1
AUTO_STALE_CONTRADICTIONS = 2

STATUS_ACTIVE = "active"
STATUS_CONDITIONAL = "conditional"
STATUS_STALE = "stale"
VALID_STATUSES = {
    STATUS_ACTIVE,
    STATUS_CONDITIONAL,
    STATUS_STALE,
}

STATUS_ORIGIN_AUTO = "auto"
STATUS_ORIGIN_MANUAL = "manual"

EVIDENCE_SUPPORT = "support"
EVIDENCE_CONTRADICT = "contradict"
VALID_EVIDENCE_KINDS = {
    EVIDENCE_SUPPORT,
    EVIDENCE_CONTRADICT,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def parse_utc_timestamp(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_timestamp(value: object) -> str:
    dt = parse_utc_timestamp(value)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def min_timestamp(left: object, right: object) -> str:
    a = parse_utc_timestamp(left)
    b = parse_utc_timestamp(right)
    if a is None and b is None:
        return ""
    if a is None:
        return normalize_timestamp(b)
    if b is None:
        return normalize_timestamp(a)
    return normalize_timestamp(min(a, b))


def max_timestamp(left: object, right: object) -> str:
    a = parse_utc_timestamp(left)
    b = parse_utc_timestamp(right)
    if a is None and b is None:
        return ""
    if a is None:
        return normalize_timestamp(b)
    if b is None:
        return normalize_timestamp(a)
    return normalize_timestamp(max(a, b))


def parse_count(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def normalize_status(value: object) -> str:
    status = str(value or STATUS_ACTIVE).strip().lower()
    if status in VALID_STATUSES:
        return status
    return STATUS_ACTIVE


def normalize_status_origin(value: object) -> str:
    origin = str(value or STATUS_ORIGIN_AUTO).strip().lower()
    if origin in {STATUS_ORIGIN_AUTO, STATUS_ORIGIN_MANUAL}:
        return origin
    return STATUS_ORIGIN_AUTO


def require_status(value: str) -> str:
    status = str(value).strip().lower()
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status: {value!r}. expected one of: {', '.join(sorted(VALID_STATUSES))}"
        )
    return status


def require_evidence_kind(value: str) -> str:
    kind = str(value).strip().lower()
    if kind not in VALID_EVIDENCE_KINDS:
        raise ValueError(
            f"invalid evidence kind: {value!r}. expected one of: {', '.join(sorted(VALID_EVIDENCE_KINDS))}"
        )
    return kind


def normalize_evidence_items(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []

    cleaned: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or EVIDENCE_SUPPORT).strip().lower()
        if kind not in VALID_EVIDENCE_KINDS:
            continue
        source = str(item.get("source") or "unspecified").strip() or "unspecified"
        detail = str(item.get("detail") or "").strip()
        ts = normalize_timestamp(item.get("timestamp"))
        if not ts:
            ts = utc_now()
        cleaned.append(
            {
                "timestamp": ts,
                "kind": kind,
                "source": source,
                "detail": detail,
            }
        )

    cleaned.sort(
        key=lambda ev: (
            ev.get("timestamp", ""),
            ev.get("kind", ""),
            ev.get("source", "").lower(),
            ev.get("detail", "").lower(),
        )
    )

    deduped: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for ev in cleaned:
        fp = (
            str(ev.get("kind", "")),
            str(ev.get("source", "")).lower(),
            str(ev.get("detail", "")).lower(),
        )
        if fp in seen:
            continue
        seen.add(fp)
        deduped.append(ev)

    if len(deduped) > MAX_EVIDENCE_ITEMS:
        deduped = deduped[-MAX_EVIDENCE_ITEMS:]
    return deduped


def evidence_signal_counts(row: dict) -> tuple[int, int]:
    evidence = normalize_evidence_items(row.get("evidence", []))
    support = 0
    contradict = 0
    for ev in evidence:
        kind = str(ev.get("kind", ""))
        if kind == EVIDENCE_SUPPORT:
            support += 1
        elif kind == EVIDENCE_CONTRADICT:
            contradict += 1
    contradict = max(contradict, parse_count(row.get("contradictions", 0)))
    return support, contradict


def learning_confidence(row: dict) -> float:
    count = parse_count(row.get("count", 0))
    support, contradict = evidence_signal_counts(row)
    numerator = count + support
    denominator = numerator + (2 * contradict)
    if denominator <= 0:
        return 0.0
    score = numerator / denominator
    return max(0.0, min(1.0, score))


def normalize_row(key: str, row: object) -> dict:
    if not isinstance(row, dict):
        row = {}

    normalized = {
        "canonical": str(row.get("canonical") or key),
        "count": parse_count(row.get("count", 0)),
        "first_seen": normalize_timestamp(row.get("first_seen")),
        "last_seen": normalize_timestamp(row.get("last_seen")),
        "status": normalize_status(row.get("status")),
        "status_origin": normalize_status_origin(row.get("status_origin")),
        "contradictions": parse_count(row.get("contradictions", 0)),
    }

    evidence = normalize_evidence_items(row.get("evidence", []))
    if evidence:
        normalized["evidence"] = evidence
    merged_from = row.get("merged_from")
    if isinstance(merged_from, list):
        cleaned = [str(x).strip() for x in merged_from if str(x).strip()]
        if cleaned:
            normalized["merged_from"] = cleaned

    if row.get("status_note"):
        normalized["status_note"] = str(row.get("status_note"))
    if row.get("status_updated"):
        normalized["status_updated"] = normalize_timestamp(row.get("status_updated"))
    if normalized["status"] == STATUS_STALE and row.get("superseded_by"):
        normalized["superseded_by"] = str(row.get("superseded_by"))

    return normalized


def normalize_counts(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}

    normalized: dict[str, dict] = {}
    for key, row in payload.items():
        if not isinstance(key, str):
            continue
        normalized[key] = normalize_row(key, row)
    return normalized


def load_counts() -> dict:
    if not COUNTS_FILE.exists():
        return {}
    try:
        payload = json.loads(COUNTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return normalize_counts(payload)


def save_counts(counts: dict) -> None:
    COUNTS_FILE.write_text(json.dumps(counts, indent=2, sort_keys=True), encoding="utf-8")


def _auto_status_target(row: dict, *, now: datetime) -> tuple[str, str]:
    _, contradict = evidence_signal_counts(row)
    confidence = learning_confidence(row)
    last_seen_dt = parse_utc_timestamp(row.get("last_seen"))
    age_days = None
    if last_seen_dt is not None:
        age_days = max((now - last_seen_dt).days, 0)

    if row.get("superseded_by"):
        return STATUS_STALE, "auto: superseded by newer learning"
    if contradict >= AUTO_STALE_CONTRADICTIONS:
        return STATUS_STALE, "auto: repeated contradictory evidence"
    if contradict >= AUTO_CONDITIONAL_CONTRADICTIONS:
        return STATUS_CONDITIONAL, "auto: contradictory evidence observed"
    if age_days is not None and age_days >= AUTO_STALE_AGE_DAYS and confidence < 0.95:
        return STATUS_STALE, "auto: stale due age"
    if age_days is not None and age_days >= AUTO_CONDITIONAL_AGE_DAYS:
        return STATUS_CONDITIONAL, "auto: old evidence; needs reconfirmation"
    if parse_count(row.get("count", 0)) >= PROMOTION_THRESHOLD and confidence < MIN_PROMOTION_CONFIDENCE:
        return STATUS_CONDITIONAL, "auto: low confidence"
    return STATUS_ACTIVE, ""


def apply_auto_stale_policy(counts: dict, now: Optional[datetime] = None) -> int:
    if now is None:
        now = datetime.now(timezone.utc)
    changed = 0
    now_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    for key in list(counts.keys()):
        row = normalize_row(key, counts.get(key, {}))
        origin = normalize_status_origin(row.get("status_origin"))
        row["status_origin"] = origin
        if origin == STATUS_ORIGIN_MANUAL:
            counts[key] = row
            continue

        old_status = normalize_status(row.get("status"))
        old_note = str(row.get("status_note") or "")
        target_status, target_note = _auto_status_target(row, now=now)
        if old_status != target_status or old_note != target_note:
            row["status"] = target_status
            row["status_updated"] = now_ts
            if target_note:
                row["status_note"] = target_note
            else:
                row.pop("status_note", None)
            changed += 1
        counts[key] = row

    return changed


def regenerate_profile_context(counts: dict) -> None:
    promoted_active = []
    promoted_conditional = []
    stale_total = 0
    low_conf_total = 0

    for key, row in counts.items():
        count = parse_count(row.get("count", 0))
        status = normalize_status(row.get("status"))
        text = str(row.get("canonical") or key)
        conf = learning_confidence(row)
        if status == STATUS_STALE:
            stale_total += 1
        if count < PROMOTION_THRESHOLD:
            continue
        if conf < MIN_PROMOTION_CONFIDENCE:
            low_conf_total += 1
            continue
        item = (count, text, conf)
        if status == STATUS_CONDITIONAL:
            promoted_conditional.append(item)
        elif status == STATUS_STALE:
            continue
        else:
            promoted_active.append(item)

    promoted_active.sort(key=lambda x: (-x[0], x[1].lower()))
    promoted_conditional.sort(key=lambda x: (-x[0], x[1].lower()))

    promoted_active = promoted_active[:MAX_CONTEXT_ITEMS]
    remaining = max(MAX_CONTEXT_ITEMS - len(promoted_active), 0)
    promoted_conditional = promoted_conditional[:remaining]

    lines = [
        "# Mud Player Learned Context",
        "",
        (
            f"Auto-promoted learnings (threshold: {PROMOTION_THRESHOLD}, "
            f"high-confidence >= {MIN_PROMOTION_CONFIDENCE:.2f})."
        ),
        "",
    ]

    if not promoted_active and not promoted_conditional:
        lines.append("- None yet.")
    if promoted_active:
        lines.append("Active learnings:")
        for count, text, conf in promoted_active:
            lines.append(f"- ({count}x, conf={conf:.2f}) {text}")
    if promoted_conditional:
        if promoted_active:
            lines.append("")
        lines.append("Conditional learnings:")
        for count, text, conf in promoted_conditional:
            lines.append(f"- ({count}x, conf={conf:.2f}) {text}")
    if low_conf_total > 0:
        lines.append("")
        lines.append(
            f"Low-confidence promoted candidates hidden: {low_conf_total}."
        )
    if stale_total > 0:
        lines.append("")
        lines.append(
            f"Stale/superseded learnings hidden from policy context: {stale_total}."
        )

    PROFILE_CONTEXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def persist_counts(counts: dict, *, apply_policy: bool = True) -> int:
    normalized = normalize_counts(counts)
    changed = 0
    if apply_policy:
        changed = apply_auto_stale_policy(normalized)
    save_counts(normalized)
    regenerate_profile_context(normalized)
    return changed


def append_learning_log(payload: dict) -> None:
    LEARNINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with LEARNINGS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def record_learning(text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("learning text is empty")

    ensure_state_dir()
    key = normalize(text)
    counts = load_counts()

    now = utc_now()
    existing = counts.get(key)
    row = normalize_row(key, existing if existing is not None else {})
    count = parse_count(row.get("count", 0)) + 1
    if existing is None:
        canonical = text
        row["status_origin"] = STATUS_ORIGIN_AUTO
    else:
        canonical = str(row.get("canonical") or text)

    row["canonical"] = canonical
    row["count"] = count
    row["first_seen"] = row.get("first_seen") or now
    row["last_seen"] = now
    row["status"] = normalize_status(row.get("status"))
    counts[key] = row

    auto_changed = persist_counts(counts)

    append_learning_log(
        {
            "timestamp": now,
            "event": "record_learning",
            "learning": text,
            "normalized_key": key,
            "count": count,
            "auto_status_changed": auto_changed,
        }
    )

    return count


def record_evidence(learning_text: str, kind: str, source: str, detail: str = "") -> int:
    learning_text = learning_text.strip()
    if not learning_text:
        raise ValueError("learning text is empty")
    source = source.strip()
    if not source:
        raise ValueError("evidence source is empty")
    normalized_kind = require_evidence_kind(kind)

    counts = load_counts()
    key = normalize(learning_text)
    if key not in counts:
        raise KeyError(f"unknown learning: {learning_text!r}")

    now = utc_now()
    row = normalize_row(key, counts.get(key, {}))
    evidence = normalize_evidence_items(row.get("evidence", []))
    evidence.append(
        {
            "timestamp": now,
            "kind": normalized_kind,
            "source": source,
            "detail": detail.strip(),
        }
    )
    row["evidence"] = normalize_evidence_items(evidence)
    if normalized_kind == EVIDENCE_CONTRADICT:
        row["contradictions"] = parse_count(row.get("contradictions", 0)) + 1
    row["last_seen"] = now
    counts[key] = row

    auto_changed = persist_counts(counts)

    append_learning_log(
        {
            "timestamp": now,
            "event": "record_evidence",
            "normalized_key": key,
            "kind": normalized_kind,
            "source": source,
            "detail": detail.strip(),
            "evidence_count": len(row.get("evidence", [])),
            "auto_status_changed": auto_changed,
        }
    )

    return len(row.get("evidence", []))


def merge_learnings(from_text: str, to_text: str, note: str = "") -> tuple[int, int]:
    from_text = from_text.strip()
    to_text = to_text.strip()
    if not from_text or not to_text:
        raise ValueError("from_text and to_text are required")

    from_key = normalize(from_text)
    to_key = normalize(to_text)
    if from_key == to_key:
        raise ValueError("from_text and to_text must be different")

    counts = load_counts()
    if from_key not in counts:
        raise KeyError(f"unknown learning: {from_text!r}")

    now = utc_now()
    from_row = normalize_row(from_key, counts.get(from_key, {}))
    to_existing = counts.get(to_key)
    to_row = normalize_row(to_key, to_existing if to_existing is not None else {})

    from_count = parse_count(from_row.get("count", 0))
    to_count = parse_count(to_row.get("count", 0))
    merged_count = from_count + to_count

    to_row["canonical"] = str(to_row.get("canonical") or to_text)
    to_row["count"] = merged_count
    to_row["first_seen"] = min_timestamp(to_row.get("first_seen"), from_row.get("first_seen")) or now
    to_row["last_seen"] = max_timestamp(to_row.get("last_seen"), from_row.get("last_seen")) or now
    to_row["contradictions"] = (
        parse_count(to_row.get("contradictions", 0))
        + parse_count(from_row.get("contradictions", 0))
    )
    to_row["evidence"] = normalize_evidence_items(
        list(to_row.get("evidence", [])) + list(from_row.get("evidence", []))
    )

    merged_from: list[str] = []
    for value in list(to_row.get("merged_from", [])) + list(from_row.get("merged_from", [])) + [from_key]:
        value = str(value).strip()
        if value and value != to_key and value not in merged_from:
            merged_from.append(value)
    if merged_from:
        to_row["merged_from"] = merged_from

    to_origin = normalize_status_origin(to_row.get("status_origin"))
    from_origin = normalize_status_origin(from_row.get("status_origin"))
    if from_origin == STATUS_ORIGIN_MANUAL and to_origin != STATUS_ORIGIN_MANUAL:
        to_row["status"] = normalize_status(from_row.get("status"))
        to_row["status_origin"] = STATUS_ORIGIN_MANUAL
        if from_row.get("status_note"):
            to_row["status_note"] = str(from_row.get("status_note"))
        to_row["status_updated"] = max_timestamp(from_row.get("status_updated"), now) or now
    elif to_origin == STATUS_ORIGIN_MANUAL:
        to_row["status"] = normalize_status(to_row.get("status"))
        to_row["status_origin"] = STATUS_ORIGIN_MANUAL
    else:
        to_row["status"] = STATUS_ACTIVE
        to_row["status_origin"] = STATUS_ORIGIN_AUTO
        to_row.pop("superseded_by", None)
        to_row.pop("status_note", None)

    counts[to_key] = to_row
    del counts[from_key]

    for key in list(counts.keys()):
        row = normalize_row(key, counts.get(key, {}))
        if row.get("superseded_by") == from_key:
            row["superseded_by"] = to_key
            counts[key] = row

    auto_changed = persist_counts(counts)

    append_learning_log(
        {
            "timestamp": now,
            "event": "merge_learnings",
            "from_key": from_key,
            "to_key": to_key,
            "from_count": from_count,
            "to_count_before": to_count,
            "to_count_after": merged_count,
            "note": note.strip(),
            "auto_status_changed": auto_changed,
        }
    )

    return from_count, merged_count


def set_learning_status(text: str, status: str, note: str = "") -> str:
    text = text.strip()
    if not text:
        raise ValueError("learning text is empty")

    normalized_status = require_status(status)
    counts = load_counts()
    key = normalize(text)
    if key not in counts:
        raise KeyError(f"unknown learning: {text!r}")

    now = utc_now()
    row = normalize_row(key, counts.get(key, {}))
    row["status"] = normalized_status
    row["status_origin"] = STATUS_ORIGIN_MANUAL
    row["status_updated"] = now
    if note.strip():
        row["status_note"] = note.strip()
    else:
        row.pop("status_note", None)
    if normalized_status != STATUS_STALE:
        row.pop("superseded_by", None)

    counts[key] = row
    persist_counts(counts)

    append_learning_log(
        {
            "timestamp": now,
            "event": "set_learning_status",
            "normalized_key": key,
            "status": normalized_status,
            "note": note.strip(),
        }
    )
    return normalized_status


def supersede_learning(old_text: str, new_text: str, note: str = "") -> tuple[int, int]:
    old_text = old_text.strip()
    new_text = new_text.strip()
    if not old_text or not new_text:
        raise ValueError("old_text and new_text are required")

    old_key = normalize(old_text)
    new_key = normalize(new_text)
    if old_key == new_key:
        raise ValueError("old_text and new_text must be different")

    counts = load_counts()
    if old_key not in counts:
        raise KeyError(f"unknown learning: {old_text!r}")

    now = utc_now()
    old_row = normalize_row(old_key, counts.get(old_key, {}))
    old_count = parse_count(old_row.get("count", 0))
    old_was_promoted = old_count >= PROMOTION_THRESHOLD

    new_existing = counts.get(new_key)
    new_row = normalize_row(new_key, new_existing if new_existing is not None else {})
    if new_existing is None:
        new_row["canonical"] = new_text
    else:
        new_row["canonical"] = str(new_row.get("canonical") or new_text)
    new_count = parse_count(new_row.get("count", 0)) + 1
    if old_was_promoted and new_count < PROMOTION_THRESHOLD:
        new_count = PROMOTION_THRESHOLD
    new_row["count"] = new_count
    new_row["first_seen"] = new_row.get("first_seen") or now
    new_row["last_seen"] = now
    new_row["status"] = STATUS_ACTIVE
    new_row["status_origin"] = STATUS_ORIGIN_MANUAL
    new_row["status_updated"] = now
    new_row.pop("superseded_by", None)
    if note.strip():
        new_row["status_note"] = (
            f"Promoted by superseding: {old_row.get('canonical', old_text)}"
        )
    counts[new_key] = new_row

    old_row["status"] = STATUS_STALE
    old_row["status_origin"] = STATUS_ORIGIN_MANUAL
    old_row["status_updated"] = now
    old_row["superseded_by"] = new_key
    old_row["last_seen"] = now
    old_row["status_note"] = note.strip() or (
        f"Superseded by: {new_row.get('canonical', new_text)}"
    )
    counts[old_key] = old_row

    persist_counts(counts)

    append_learning_log(
        {
            "timestamp": now,
            "event": "supersede_learning",
            "old_key": old_key,
            "new_key": new_key,
            "old_count": old_count,
            "new_count": new_count,
            "note": note.strip(),
        }
    )

    return old_count, new_count


def run_auto_stale_policy() -> int:
    counts = load_counts()
    changed = persist_counts(counts, apply_policy=True)
    append_learning_log(
        {
            "timestamp": utc_now(),
            "event": "run_auto_stale",
            "changed_rows": changed,
        }
    )
    return changed


def list_learnings(status_filter: str = "") -> str:
    normalized_filter = ""
    if status_filter.strip():
        normalized_filter = require_status(status_filter.strip())

    counts = load_counts()
    rows: list[tuple[str, int, float, str, str]] = []
    for key, row in counts.items():
        item_status = normalize_status(row.get("status"))
        if normalized_filter and item_status != normalized_filter:
            continue
        text = str(row.get("canonical") or key)
        count = parse_count(row.get("count", 0))
        conf = learning_confidence(row)
        rows.append((item_status, count, conf, text, str(row.get("superseded_by") or "")))

    rows.sort(key=lambda item: (item[0], -item[1], -item[2], item[3].lower()))

    lines = [f"learning_rows={len(rows)}"]
    for status, count, conf, text, superseded_by in rows:
        suffix = ""
        if superseded_by and superseded_by in counts:
            replacement = str(counts[superseded_by].get("canonical") or superseded_by)
            suffix = f" -> {replacement}"
        lines.append(f"[{status}] ({count}x, conf={conf:.2f}) {text}{suffix}")

    return "\n".join(lines)


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
    counts = load_counts()
    if counts:
        persist_counts(counts, apply_policy=True)

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
        "- Attach evidence to learnings (support/contradict) for confidence tracking:",
        "  `python3 scripts/mud_profile_memory.py record-evidence \"<learning>\" support \"<source>\" \"<detail>\"`",
        "- Merge duplicate learnings into one canonical entry:",
        "  `python3 scripts/mud_profile_memory.py merge-learnings \"<from>\" \"<to>\"`",
        "- Repeated learnings are auto-promoted to profile context at 3 occurrences.",
        "- Auto-stale policy reclassifies old/contradicted learnings during updates.",
        "- When old guidance is contradicted, mark it stale/conditional manually:",
        "  `python3 scripts/mud_profile_memory.py set-learning-status <status> \"<learning>\"`",
        "- To replace old policy with new policy in one step:",
        "  `python3 scripts/mud_profile_memory.py supersede-learning \"<old>\" \"<new>\"`",
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
    print(
        "Usage: mud_profile_memory.py "
        "[record-learning|record-evidence|merge-learnings|set-learning-status|"
        "supersede-learning|run-auto-stale|list-learnings|record-function|"
        "write-summary|build-prompt] [args]"
    )
    return 2


def fail(message: str) -> int:
    print(message, file=sys.stderr)
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

    if cmd == "record-evidence":
        if len(sys.argv) < 5:
            return usage()
        learning_text = sys.argv[2].strip()
        kind = sys.argv[3].strip()
        source = sys.argv[4].strip()
        detail = " ".join(sys.argv[5:]).strip()
        if not learning_text or not source:
            return usage()
        try:
            evidence_count = record_evidence(learning_text, kind, source, detail)
        except (KeyError, ValueError) as exc:
            return fail(str(exc))
        print(f"evidence_count={evidence_count}")
        return 0

    if cmd == "merge-learnings":
        if len(sys.argv) < 4:
            return usage()
        from_text = sys.argv[2].strip()
        to_text = sys.argv[3].strip()
        note = " ".join(sys.argv[4:]).strip()
        if not from_text or not to_text:
            return usage()
        try:
            from_count, merged_count = merge_learnings(from_text, to_text, note=note)
        except (KeyError, ValueError) as exc:
            return fail(str(exc))
        print(f"merged=1 from_count={from_count} merged_count={merged_count}")
        return 0

    if cmd == "set-learning-status":
        if len(sys.argv) < 4:
            return usage()
        status = sys.argv[2].strip()
        text = " ".join(sys.argv[3:]).strip()
        if not text:
            return usage()
        try:
            applied_status = set_learning_status(text, status)
        except (KeyError, ValueError) as exc:
            return fail(str(exc))
        print(f"learning_status={applied_status}")
        return 0

    if cmd == "supersede-learning":
        if len(sys.argv) < 4:
            return usage()
        old_text = sys.argv[2].strip()
        new_text = sys.argv[3].strip()
        note = " ".join(sys.argv[4:]).strip()
        if not old_text or not new_text:
            return usage()
        try:
            old_count, new_count = supersede_learning(old_text, new_text, note=note)
        except (KeyError, ValueError) as exc:
            return fail(str(exc))
        print(f"superseded=1 old_count={old_count} new_count={new_count}")
        return 0

    if cmd == "run-auto-stale":
        changed = run_auto_stale_policy()
        print(f"auto_stale_changed={changed}")
        return 0

    if cmd == "list-learnings":
        status_filter = sys.argv[2].strip() if len(sys.argv) >= 3 else ""
        try:
            print(list_learnings(status_filter))
        except ValueError as exc:
            return fail(str(exc))
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
