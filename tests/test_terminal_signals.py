import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def signal_file(tmp_path, monkeypatch):
    path=Path(__file__).resolve().parents[1]/"量化交易台"/"serve.py"
    spec=importlib.util.spec_from_file_location("quant_terminal_server",path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module,"LOGS",tmp_path)
    file=tmp_path/"TQQQ"/"intraday"/"20260101"/"latest.json"
    file.parent.mkdir(parents=True)
    return module,file


def test_bad_minute_snapshot_is_a_report_error(signal_file):
    module,file=signal_file
    file.write_text(json.dumps({"version":"intraday-v1","ticker":"TQQQ","snapshots":[None]}))
    with pytest.raises(ValueError,match="snapshots"):
        module.latest_signal("TQQQ")


def test_optional_sections_and_older_reports(signal_file):
    module,file=signal_file
    file.write_text(json.dumps({"version":"intraday-v1","ticker":"TQQQ","snapshots":[],
                                "analysis":"unavailable","premarket_context":False}))
    answer=module.latest_signal("TQQQ")
    assert answer["ticker"]=="TQQQ" and "analysis" not in answer
    assert module.latest_signal("QQQ") is None
