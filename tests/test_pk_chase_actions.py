import asyncio
import tempfile
import time
import unittest

from mud.app import MudApp


class PkChaseActionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

    def tearDown(self) -> None:
        self._loop.close()
        asyncio.set_event_loop(None)

    def _make_app(self) -> tuple[MudApp, list[str], list[str]]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = MudApp(host="127.0.0.1", port=4000, config_dir=tmp.name)
        printed: list[str] = []
        sent: list[str] = []

        app._print = printed.append  # type: ignore[method-assign]

        async def fake_send(cmd: str) -> bool:
            sent.append(cmd)
            return True

        app.client.send = fake_send  # type: ignore[method-assign]
        return app, printed, sent

    def test_k_with_explicit_target_sets_target_and_casts(self) -> None:
        app, _printed, sent = self._make_app()
        self._loop.run_until_complete(app._handle_input("k raije"))

        self.assertEqual(sent, ["k raije"])
        self.assertEqual(app._pk_target_by_char.get("default"), "raije")

    def test_t_sets_target_only_without_firing(self) -> None:
        app, _printed, sent = self._make_app()
        self._loop.run_until_complete(app._handle_input("t (raije)"))
        self._loop.run_until_complete(app._handle_input("k"))

        self.assertEqual(sent, ["k raije"])
        self.assertEqual(app._pk_target_by_char.get("default"), "raije")

    def test_l_retargets_and_k_uses_new_target(self) -> None:
        app, _printed, sent = self._make_app()
        self._loop.run_until_complete(app._handle_input("k firsttarget"))
        self._loop.run_until_complete(app._handle_input("l secondtarget"))
        self._loop.run_until_complete(app._handle_input("k"))

        self.assertEqual(sent, ["k firsttarget", "l secondtarget", "k secondtarget"])
        self.assertEqual(app._pk_target_by_char.get("default"), "secondtarget")

    def test_parenthesized_target_is_normalized_for_l_and_m(self) -> None:
        app, _printed, sent = self._make_app()
        self._loop.run_until_complete(app._handle_input("l (secondtarget)"))
        self._loop.run_until_complete(app._handle_input("m"))

        self.assertEqual(sent, ["l secondtarget", "m secondtarget"])
        self.assertEqual(app._pk_target_by_char.get("default"), "secondtarget")

    def test_directional_shorthand_moves_then_casts_for_k_and_l(self) -> None:
        app, _printed, sent = self._make_app()
        self._loop.run_until_complete(app._handle_input("t vict"))
        self._loop.run_until_complete(app._handle_input("nek"))
        self._loop.run_until_complete(app._handle_input("nwl"))

        self.assertEqual(sent, ["ne", "k vict", "nw", "l vict"])

    def test_directional_shorthand_with_target_updates_shared_target(self) -> None:
        app, _printed, sent = self._make_app()
        self._loop.run_until_complete(app._handle_input("t oldtarget"))
        self._loop.run_until_complete(app._handle_input("nk newtarget"))
        self._loop.run_until_complete(app._handle_input("l"))

        self.assertEqual(sent, ["n", "k newtarget", "l newtarget"])
        self.assertEqual(app._pk_target_by_char.get("default"), "newtarget")

    def test_chase_actions_can_be_customized_with_ch1_ch2(self) -> None:
        app, _printed, sent = self._make_app()
        self._loop.run_until_complete(app._handle_input("ch1 c acid"))
        self._loop.run_until_complete(app._handle_input("ch2 cast 'lightning bolt' {target}"))
        self._loop.run_until_complete(app._handle_input("k playerx"))
        self._loop.run_until_complete(app._handle_input("l"))

        self.assertEqual(sent, ["c acid playerx", "cast 'lightning bolt' playerx"])

    def test_k_without_target_warns_and_does_not_send(self) -> None:
        app, printed, sent = self._make_app()
        self._loop.run_until_complete(app._handle_input("k"))

        self.assertEqual(sent, [])
        self.assertTrue(any("No chase target set" in line for line in printed))

    def test_hash_ch_command_sets_chase_action_not_target(self) -> None:
        app, _printed, sent = self._make_app()
        self._loop.run_until_complete(app._handle_input("#ch 1 c acid"))
        self._loop.run_until_complete(app._handle_input("#t raije"))
        self._loop.run_until_complete(app._handle_input("k"))

        self.assertEqual(sent, ["c acid raije"])

    def test_hash_t_command_sets_target_for_followup_chase(self) -> None:
        app, _printed, sent = self._make_app()
        self._loop.run_until_complete(app._handle_input("#t (raije)"))
        self._loop.run_until_complete(app._handle_input("k"))

        self.assertEqual(sent, ["k raije"])

    def test_kkkk_spams_nine_chase1_then_one_where(self) -> None:
        app, printed, sent = self._make_app()
        self._loop.run_until_complete(app._handle_input("t raije"))
        self._loop.run_until_complete(app._handle_input("kkkk"))

        self.assertEqual(sent, (["k raije"] * 9) + ["where"])
        self.assertTrue(any("(spamming x raije)" in line for line in printed))

    def test_spam_output_filter_condenses_they_arent_here_and_echoed_commands(self) -> None:
        app, _printed, _sent = self._make_app()
        app._pk_spam_target = "raije"
        app._pk_spam_until = time.monotonic() + 3.0
        app._pk_spam_echo_suppress = {"k raije", "where"}

        raw = "k raije\nThey aren't here.\nwhere\nThey aren't here.\nRoom text here.\n"
        filtered = app._apply_pk_spam_output_filter(raw)

        self.assertIn("Room text here.", filtered)
        self.assertIn("(spamming x raije) They aren't here. x2", filtered)
        self.assertNotIn("\nk raije\n", "\n" + filtered)
        self.assertNotIn("\nwhere\n", "\n" + filtered)

    def test_spam_output_filter_keeps_world_prompt_when_echo_is_suppressed(self) -> None:
        app, _printed, _sent = self._make_app()
        app._pk_spam_target = "raije"
        app._pk_spam_until = time.monotonic() + 3.0
        app._pk_spam_echo_suppress = {"k raije", "where"}

        raw = "HP:1455/1455  Mana:1332/1332  MV:400/400 > k raije\n"
        filtered = app._apply_pk_spam_output_filter(raw)

        self.assertIn("HP:1455/1455  Mana:1332/1332  MV:400/400 >", filtered)
        self.assertNotIn("k raije", filtered)

    def test_chs_scans_room_entry_and_refines_shorthand_target(self) -> None:
        app, printed, _sent = self._make_app()
        app._auto_whoami = False
        enqueued: list[tuple[str, str]] = []

        async def fake_enqueue(cmd: str, source: str = "generic") -> None:
            enqueued.append((cmd, source))

        app._enqueue = fake_enqueue  # type: ignore[method-assign]
        self._loop.run_until_complete(app._handle_input("t rai"))
        self._loop.run_until_complete(app._handle_input("chs"))
        self._loop.run_until_complete(app._process_output("Room A\n[Exits: east]\n"))
        self._loop.run_until_complete(
            app._process_output("Room B\n[Exits: west]\nRaije is here.\n")
        )

        self.assertIn(("k raije", "pk-chs"), enqueued)
        self.assertEqual(app._pk_target_by_char.get("default"), "raije")
        self.assertTrue(any("target refined" in line for line in printed))

    def test_chstp_disables_scan(self) -> None:
        app, _printed, _sent = self._make_app()
        app._auto_whoami = False
        enqueued: list[tuple[str, str]] = []

        async def fake_enqueue(cmd: str, source: str = "generic") -> None:
            enqueued.append((cmd, source))

        app._enqueue = fake_enqueue  # type: ignore[method-assign]
        self._loop.run_until_complete(app._handle_input("t raije"))
        self._loop.run_until_complete(app._handle_input("chs"))
        self._loop.run_until_complete(app._handle_input("chstp"))
        self._loop.run_until_complete(app._process_output("Room A\n[Exits: east]\n"))
        self._loop.run_until_complete(
            app._process_output("Room B\n[Exits: west]\nRaije is here.\n")
        )

        self.assertEqual(enqueued, [])
        self.assertFalse(app._pk_chase_scan_enabled("default"))

    def test_chs_turns_off_on_void_and_echoes(self) -> None:
        app, printed, _sent = self._make_app()
        app._auto_whoami = False
        self._loop.run_until_complete(app._handle_input("t raije"))
        self._loop.run_until_complete(app._handle_input("chs"))
        self._loop.run_until_complete(
            app._process_output("You disappear into the void.\n")
        )

        self.assertFalse(app._pk_chase_scan_enabled("default"))
        self.assertTrue(any("OFF (void detected)" in line for line in printed))

    def test_chs_ambiguous_target_does_not_update_or_fire(self) -> None:
        app, printed, _sent = self._make_app()
        app._auto_whoami = False
        enqueued: list[tuple[str, str]] = []

        async def fake_enqueue(cmd: str, source: str = "generic") -> None:
            enqueued.append((cmd, source))

        app._enqueue = fake_enqueue  # type: ignore[method-assign]
        self._loop.run_until_complete(app._handle_input("t rai"))
        self._loop.run_until_complete(app._handle_input("chs"))
        self._loop.run_until_complete(app._process_output("Room A\n[Exits: east]\n"))
        self._loop.run_until_complete(
            app._process_output(
                "Room B\n[Exits: west]\nRaije is here.\nRaisen is here.\n"
            )
        )

        self.assertEqual(enqueued, [])
        self.assertEqual(app._pk_target_by_char.get("default"), "rai")
        self.assertTrue(any("ambiguous target match" in line for line in printed))

    def test_chs_flee_scan_can_fire_without_room_change(self) -> None:
        app, _printed, _sent = self._make_app()
        app._auto_whoami = False
        enqueued: list[tuple[str, str]] = []

        async def fake_enqueue(cmd: str, source: str = "generic") -> None:
            enqueued.append((cmd, source))

        app._enqueue = fake_enqueue  # type: ignore[method-assign]
        self._loop.run_until_complete(app._handle_input("t raije"))
        self._loop.run_until_complete(app._handle_input("chs"))
        self._loop.run_until_complete(
            app._process_output("You flee headlong from combat!\nRaije is here.\n")
        )

        self.assertIn(("k raije", "pk-chs"), enqueued)


if __name__ == "__main__":
    unittest.main()
