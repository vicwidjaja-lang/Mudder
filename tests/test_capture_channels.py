import unittest

from mud.app import MudApp


class CaptureChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        # _extract_capture_lines does not depend on app runtime state.
        self.app = object.__new__(MudApp)

    def test_extracts_ooc_variants(self) -> None:
        text = (
            "X OOC KINGDOM: 'hello there'\n"
            "Y OOC: 'MESSAGE'\n"
            "Z OOC CLAN: 'ready'\n"
        )
        got = self.app._extract_capture_lines(text)
        self.assertIn("X (ooc kingdom) 'hello there'", got)
        self.assertIn("Y (ooc) 'MESSAGE'", got)
        self.assertIn("Z (ooc clan) 'ready'", got)

    def test_extracts_kingdom_and_clan_colon_formats(self) -> None:
        text = (
            "A KINGDOM: 'for the crown'\n"
            "B CLAN: 'group up'\n"
        )
        got = self.app._extract_capture_lines(text)
        self.assertIn("A (kingdom) 'for the crown'", got)
        self.assertIn("B (clan) 'group up'", got)


if __name__ == "__main__":
    unittest.main()
