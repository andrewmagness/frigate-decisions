import json
from pathlib import Path

import yaml

from frigate_decisions.config import Settings, parse_question_config
from frigate_decisions.engine import Engine
from frigate_decisions.report import load, main as report_main, summarise
from frigate_decisions.store import Recorder, read_jsonl

from tests.test_engine import FakeDecisions, FakeFrigate, review

ROOT = Path(__file__).resolve().parents[1]


def make_engine(tmp_path, recorder=True):
    published = []
    cfg = parse_question_config(yaml.safe_load((ROOT / "questions.example.yaml").read_text()))
    eng = Engine(Settings(decisions_api_key="x"), cfg, FakeDecisions(), FakeFrigate(),
                 lambda t, p, r: published.append((t, p, r)),
                 recorder=Recorder(tmp_path) if recorder else None)
    return eng, published


def test_events_get_ids_and_are_recorded(tmp_path):
    eng, published = make_engine(tmp_path)
    res = eng.handle_review(review(rid="a"))[0]
    assert len(res["id"]) == 12
    event = json.loads(published[-1][1])
    assert event["id"] == res["id"]
    state = json.loads(next(p for t, p, _ in published if t.endswith("/real_person")))
    assert state["event_id"] == res["id"]
    rows = read_jsonl(tmp_path / "events.jsonl")
    assert [r["id"] for r in rows] == [res["id"]]


def test_feedback_recorded_and_validated(tmp_path):
    eng, _ = make_engine(tmp_path)
    assert eng.handle_feedback({"id": "abc", "vote": "sideways"}) is None
    assert eng.handle_feedback({"vote": "up"}) is None
    rec = eng.handle_feedback({"id": "abc", "vote": "down", "user": "u1", "source": "telegram"})
    assert rec["vote"] == "down" and rec["user"] == "u1"
    assert len(read_jsonl(tmp_path / "feedback.jsonl")) == 1


def test_feedback_without_data_dir_is_ignored(tmp_path):
    eng, _ = make_engine(tmp_path, recorder=False)
    assert eng.handle_feedback({"id": "abc", "vote": "up"}) is None


def test_report_joins_last_vote(tmp_path, capsys):
    eng, _ = make_engine(tmp_path)
    up = eng.handle_review(review(rid="1"))[0]
    down = eng.handle_review(review(rid="2"))[0]
    eng.handle_review(review(rid="3"))  # unrated
    eng.handle_feedback({"id": up["id"], "vote": "down"})
    eng.handle_feedback({"id": up["id"], "vote": "up"})      # changed mind: last vote wins
    eng.handle_feedback({"id": down["id"], "vote": "down"})
    events = load(tmp_path)
    s = summarise(events)["front_door/arrival"]
    assert s["runs"] == 3 and s["votes"] == {"up": 1, "down": 1, "none": 1}
    assert s["questions"]["real_person"]["up"]["mean"] == 0.9
    assert s["questions"]["who"]["down"] == {"resident": 1}
    assert [d["id"] for d in s["down_voted"]] == [down["id"]]
    assert report_main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "front_door/arrival: 3 runs" in out and "👎" in out
