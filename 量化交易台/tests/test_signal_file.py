"""Read-only terminal checks with synthetic signal reports."""
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("terminal_server", Path(__file__).resolve().parents[1] / "serve.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class SignalFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.enterContext(patch.object(server, "LOGS", self.root))

    def write(self, run, **fields):
        file = self.root / "TQQQ" / "intraday" / run / "latest.json"
        file.parent.mkdir(parents=True, exist_ok=True)
        report = {"version": "intraday-v1", "ticker": "TQQQ", "snapshots": [{"ticker": "TQQQ", "price": 10}], **fields}
        file.write_text(json.dumps(report), encoding="utf-8")

    def test_bad_optional_rows_do_not_hide_main_signal(self):
        for rows in (None, 8, {}, "unavailable"):
            with self.subTest(rows=rows):
                self.write("20260101", analysis={"sell_impact": {"status": "HISTORICAL", "rows": rows}})
                report = server.latest_signal("TQQQ")
                self.assertEqual(report["snapshots"][0]["price"], 10)
                self.assertEqual(report["analysis"]["sell_impact"]["rows"], [])

    def test_newest_run_wins_and_invalid_core_never_uses_older_signal(self):
        self.assertIsNone(server.latest_signal("TQQQ"))
        self.write("20260102", reason="new")
        self.write("20260101", reason="old")
        self.assertEqual(server.latest_signal("TQQQ")["reason"], "new")
        self.write("20260102", snapshots=[None])
        with self.assertRaises(ValueError):
            server.latest_signal("TQQQ")

    def test_strategy_directories_and_forward_fields_are_separate(self):
        self.write("run", reason="trend")
        folder=self.root/"TQQQ"/"opening0935"/"run"
        folder.mkdir(parents=True)
        report=dict(version="intraday-v1",strategy_id="opening0935-v1",ticker="TQQQ",snapshots=[],
                    opening={"target":.5},forward_events=[{"qty":35,"private":"sample",
                    "closing_result":{"fees":5,"net":65}}])
        (folder/"latest.json").write_text(json.dumps(report),encoding="utf-8")
        self.assertEqual(server.latest_signal("TQQQ")["reason"],"trend")
        visible=server.latest_signal("TQQQ","opening0935")
        self.assertEqual(visible["opening"]["target"],.5)
        self.assertEqual(visible["forward_events"][0]["closing_result"]["net"],65)
        self.assertNotIn("private",visible["forward_events"][0])
