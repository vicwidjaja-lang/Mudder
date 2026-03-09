import json
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mud_profile_memory.py"
MODULE_SPEC = importlib.util.spec_from_file_location("mud_profile_memory", MODULE_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
memory = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(memory)


class MudProfileMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._state = self._root / ".codex-mud"

        self._orig = {
            "ROOT": memory.ROOT,
            "STATE_DIR": memory.STATE_DIR,
            "SYSTEM_PROMPT_FILE": memory.SYSTEM_PROMPT_FILE,
            "LEARNINGS_LOG": memory.LEARNINGS_LOG,
            "COUNTS_FILE": memory.COUNTS_FILE,
            "PROFILE_CONTEXT": memory.PROFILE_CONTEXT,
            "LAST_SUMMARY": memory.LAST_SUMMARY,
            "FUNCTION_IDEAS": memory.FUNCTION_IDEAS,
        }

        memory.ROOT = self._root
        memory.STATE_DIR = self._state
        memory.SYSTEM_PROMPT_FILE = self._root / "configs" / "system_prompt_dsl_operator.txt"
        memory.LEARNINGS_LOG = self._state / "learnings.jsonl"
        memory.COUNTS_FILE = self._state / "learning_counts.json"
        memory.PROFILE_CONTEXT = self._state / "profile_context.md"
        memory.LAST_SUMMARY = self._state / "last_session_summary.md"
        memory.FUNCTION_IDEAS = self._state / "function_ideas.md"

        memory.ensure_state_dir()

    def tearDown(self) -> None:
        memory.ROOT = self._orig["ROOT"]
        memory.STATE_DIR = self._orig["STATE_DIR"]
        memory.SYSTEM_PROMPT_FILE = self._orig["SYSTEM_PROMPT_FILE"]
        memory.LEARNINGS_LOG = self._orig["LEARNINGS_LOG"]
        memory.COUNTS_FILE = self._orig["COUNTS_FILE"]
        memory.PROFILE_CONTEXT = self._orig["PROFILE_CONTEXT"]
        memory.LAST_SUMMARY = self._orig["LAST_SUMMARY"]
        memory.FUNCTION_IDEAS = self._orig["FUNCTION_IDEAS"]
        self._tmp.cleanup()

    def _record_many(self, text: str, count: int = 3) -> None:
        for _ in range(count):
            memory.record_learning(text)

    def test_set_learning_status_stale_suppresses_promoted_text(self) -> None:
        text = "Old sanctuary policy from prior route loop."
        self._record_many(text, count=3)

        before = memory.PROFILE_CONTEXT.read_text(encoding="utf-8")
        self.assertIn(text, before)

        status = memory.set_learning_status(text, "stale")
        self.assertEqual(status, "stale")

        after = memory.PROFILE_CONTEXT.read_text(encoding="utf-8")
        self.assertNotIn(text, after)
        self.assertIn("Stale/superseded learnings hidden from policy context: 1.", after)

    def test_conditional_learning_is_rendered_in_conditional_section(self) -> None:
        text = "Potion policy depends on current inventory."
        self._record_many(text, count=3)

        status = memory.set_learning_status(text, "conditional")
        self.assertEqual(status, "conditional")

        context = memory.PROFILE_CONTEXT.read_text(encoding="utf-8")
        self.assertIn("Conditional learnings:", context)
        self.assertIn(text, context)
        self.assertNotIn("Stale/superseded learnings hidden from policy context", context)

    def test_supersede_learning_marks_old_stale_and_promotes_replacement(self) -> None:
        old_text = "Always quaff glist for sanctuary uptime."
        new_text = "If glist is unavailable, disable sanctuary and keep haste on."
        self._record_many(old_text, count=3)

        old_count, new_count = memory.supersede_learning(
            old_text,
            new_text,
            note="Contradicted by no-potion combat logs.",
        )
        self.assertEqual(old_count, 3)
        self.assertGreaterEqual(new_count, memory.PROMOTION_THRESHOLD)

        counts = memory.load_counts()
        old_key = memory.normalize(old_text)
        new_key = memory.normalize(new_text)
        self.assertEqual(counts[old_key]["status"], "stale")
        self.assertEqual(counts[old_key]["superseded_by"], new_key)
        self.assertEqual(counts[new_key]["status"], "active")

        context = memory.PROFILE_CONTEXT.read_text(encoding="utf-8")
        self.assertNotIn(old_text, context)
        self.assertIn(new_text, context)

    def test_load_counts_normalizes_legacy_rows(self) -> None:
        payload = {
            "legacy learning": {
                "canonical": "Legacy Learning",
                "count": "4",
                "first_seen": "2026-01-01T00:00:00Z",
                "last_seen": "2026-01-02T00:00:00Z",
            }
        }
        memory.COUNTS_FILE.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

        counts = memory.load_counts()
        row = counts["legacy learning"]
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["count"], 4)

        memory.regenerate_profile_context(counts)
        context = memory.PROFILE_CONTEXT.read_text(encoding="utf-8")
        self.assertIn("Legacy Learning", context)

    def test_merge_learnings_combines_count_and_evidence(self) -> None:
        source_text = "Old phrasing for sanctuary fallback."
        target_text = "If glist is unavailable, disable sanctuary and keep haste on."
        self._record_many(source_text, count=2)
        self._record_many(target_text, count=1)

        memory.record_evidence(source_text, "support", "fight-log", "confirmed in corridor loop")

        from_count, merged_count = memory.merge_learnings(source_text, target_text)
        self.assertEqual(from_count, 2)
        self.assertEqual(merged_count, 3)

        counts = memory.load_counts()
        source_key = memory.normalize(source_text)
        target_key = memory.normalize(target_text)
        self.assertNotIn(source_key, counts)
        self.assertEqual(counts[target_key]["count"], 3)
        self.assertIn(source_key, counts[target_key].get("merged_from", []))
        self.assertTrue(counts[target_key].get("evidence"))

    def test_auto_stale_policy_applies_age_and_contradictions(self) -> None:
        now = datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc)
        old_dt = now - timedelta(days=memory.AUTO_CONDITIONAL_AGE_DAYS + 5)
        stale_dt = now - timedelta(days=memory.AUTO_STALE_AGE_DAYS + 5)

        payload = {
            "old learning": {
                "canonical": "Old Learning",
                "count": 3,
                "first_seen": old_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "last_seen": old_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "active",
                "status_origin": "auto",
            },
            "contradicted learning": {
                "canonical": "Contradicted Learning",
                "count": 3,
                "first_seen": stale_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "last_seen": stale_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "active",
                "status_origin": "auto",
                "contradictions": 2,
            },
        }
        memory.COUNTS_FILE.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

        counts = memory.load_counts()
        changed = memory.apply_auto_stale_policy(counts, now=now)
        self.assertGreaterEqual(changed, 1)
        self.assertEqual(counts["old learning"]["status"], "conditional")
        self.assertEqual(counts["contradicted learning"]["status"], "stale")

    def test_promoted_context_hides_low_confidence_learning(self) -> None:
        low_conf = "Low confidence policy that got contradicted."
        high_conf = "Stable leveling policy with clean support."
        self._record_many(low_conf, count=3)
        self._record_many(high_conf, count=3)

        memory.record_evidence(low_conf, "contradict", "combat-log", "conflicting result")
        memory.record_evidence(high_conf, "support", "combat-log", "reconfirmed")

        counts = memory.load_counts()
        memory.regenerate_profile_context(counts)
        context = memory.PROFILE_CONTEXT.read_text(encoding="utf-8")

        self.assertIn(high_conf, context)
        self.assertNotIn(low_conf, context)
        self.assertIn("Low-confidence promoted candidates hidden", context)


if __name__ == "__main__":
    unittest.main()
