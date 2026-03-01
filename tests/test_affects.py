import unittest

from mud.affects import AffectEntry, AffectTracker
from mud.app import MudApp


class AffectTrackerTests(unittest.TestCase):
    def test_tick_delta_counts_single_tick_per_time_change(self) -> None:
        self.assertEqual(MudApp._tick_delta(450, 480), 1)  # 7:30 -> 8:00
        self.assertEqual(MudApp._tick_delta(480, 480), 0)
        self.assertEqual(MudApp._tick_delta(-1, 480), 0)

    def test_cast_creates_unknown_duration_affect(self) -> None:
        tracker = AffectTracker()
        changed = tracker.observe_outgoing_command("cast haste self")
        self.assertTrue(changed)
        affects = tracker.active_affects()
        self.assertEqual(len(affects), 1)
        self.assertEqual(affects[0]["name"].lower(), "haste")
        self.assertIsNone(affects[0]["duration_cycles"])

    def test_unquoted_cast_uses_single_spell_token(self) -> None:
        tracker = AffectTracker()
        changed = tracker.observe_outgoing_command("cast stone skin self")
        self.assertTrue(changed)
        affects = tracker.active_affects()
        self.assertEqual(len(affects), 1)
        self.assertEqual(affects[0]["name"].lower(), "stone")

    def test_snapshot_updates_duration_from_unknown(self) -> None:
        tracker = AffectTracker()
        tracker.observe_outgoing_command("cast haste self")
        changed = tracker.process_output(
            "You are affected by the following spells:\n"
            "Spell: haste             : modifies dexterity by 1 for 4 cycles, (2 hours)\n"
            "HP:100/100 Mana:100/100 MV:100/100 >\n"
        )
        self.assertTrue(changed)
        affects = tracker.active_affects()
        self.assertEqual(len(affects), 1)
        self.assertEqual(affects[0]["duration_cycles"], 4)

    def test_tick_decrements_and_removes_at_negative_one(self) -> None:
        tracker = AffectTracker()
        tracker.process_output(
            "You are affected by the following spells:\n"
            "Spell: sanctuary         : modifies none by 0 for 1 cycles, (1/2 hour)\n"
            "HP:100/100 Mana:100/100 MV:100/100 >\n"
        )
        tracker.on_tick(1)
        affects = tracker.active_affects()
        self.assertEqual(len(affects), 1)
        self.assertEqual(affects[0]["duration_cycles"], 0)
        tracker.on_tick(1)
        self.assertEqual(tracker.active_affects(), [])

    def test_snapshot_missing_entry_removes_affect(self) -> None:
        tracker = AffectTracker()
        tracker.process_output(
            "You are affected by the following spells:\n"
            "Spell: haste             : modifies dexterity by 1 for 2 cycles, (1 hour)\n"
            "HP:100/100 Mana:100/100 MV:100/100 >\n"
        )
        tracker.process_output(
            "You are affected by the following spells:\n"
            "Spell: sanctuary         : modifies none by 0 for 2 cycles, (1 hour)\n"
            "HP:100/100 Mana:100/100 MV:100/100 >\n"
        )
        affects = tracker.active_affects()
        names = {a["name"].lower() for a in affects}
        self.assertEqual(names, {"sanctuary"})

    def test_inferred_sanctuary_gain_and_fade(self) -> None:
        tracker = AffectTracker()
        changed = tracker.process_output("You are surrounded by a white aura.\n")
        self.assertTrue(changed)
        self.assertEqual({a["name"].lower() for a in tracker.active_affects()}, {"sanctuary"})

        changed = tracker.process_output("The white aura around your body fades.\n")
        self.assertTrue(changed)
        self.assertEqual(tracker.active_affects(), [])

    def test_render_window_status_only_no_history_timestamps(self) -> None:
        tracker = AffectTracker()
        tracker.observe_outgoing_command("cast haste self")
        tracker.on_tick(1)  # generate tick/history events internally
        window = tracker.render_window()
        self.assertIn("Affects\n-------\n", window)
        self.assertNotIn("History", window)
        self.assertNotIn("00:", window)

    def test_display_dedupes_timestamp_prefixed_and_unknown_entries(self) -> None:
        tracker = AffectTracker()
        tracker._affects = {
            "haste": AffectEntry(name="haste", duration_cycles=9, source="snapshot"),
            "haste-ts": AffectEntry(name="12:34:56 haste", duration_cycles=None, source="cast"),
            "prot-a": AffectEntry(name="protection good", duration_cycles=20, source="snapshot"),
            "prot-b": AffectEntry(name="[12:34] protection good", duration_cycles=5, source="cast"),
        }

        active = tracker.active_affects()
        by_name = {a["name"].lower(): a for a in active}
        self.assertEqual(set(by_name), {"haste", "protection good"})
        self.assertEqual(by_name["haste"]["duration_cycles"], 9)
        self.assertEqual(by_name["protection good"]["duration_cycles"], 20)


if __name__ == "__main__":
    unittest.main()
