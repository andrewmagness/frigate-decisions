import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import yaml

from frigate_decisions.config import ConfigError, Settings, parse_question_config
from frigate_decisions.decisions import DecisionsClient, DecisionsError, Media, normalize_answer
from frigate_decisions.engine import Engine, render_context

ROOT = Path(__file__).resolve().parents[1]


def example_config():
    return parse_question_config(yaml.safe_load((ROOT / "questions.example.yaml").read_text()))


class FakeDecisions:
    def __init__(self):
        self.calls = []

    def decide(self, blocks, questions):
        self.calls.append((blocks, questions))
        answers = {}
        for k, q in questions.items():
            if q["type"] == "noul":
                answers[k] = {"type": "noul", "value": 0.9, "confidence": 0.9}
            elif q["type"] == "choice":
                answers[k] = {"type": "choice", "value": next(iter(q["criteria"])), "confidence": 0.8}
            else:
                answers[k] = {"type": "score", "value": 1.5, "confidence": 0.7}
        return {"answers": answers, "usage": {"total_input_tokens": 10}}


class FakeFrigate:
    def __init__(self):
        self.calls = []

    def _m(self, what):
        self.calls.append(what)
        return Media(b"\xff\xd8jpeg", "image/jpeg", "image")

    def event_snapshot(self, eid):
        return self._m(("snapshot", eid))

    def event_clip(self, eid):
        self.calls.append(("clip", eid))
        return Media(b"mp4", "video/mp4", "video")

    def latest(self, cam):
        return self._m(("latest", cam))

    def url(self, url):
        return self._m(("url", url))


@pytest.fixture
def env():
    published = []
    clock = [1000.0]
    eng = Engine(Settings(gemini_api_key="x"), example_config(), FakeDecisions(), FakeFrigate(),
                 lambda t, p, r: published.append((t, json.loads(p) if p.startswith("{") else p, r)),
                 clock=lambda: clock[0])
    return eng, published, clock


def review(rtype="new", camera="front_door", objects=("person",), rid="r1", severity="alert", **data):
    return {"type": rtype, "after": {"id": rid, "camera": camera, "severity": severity,
                                     "data": {"detections": ["e1"], "objects": list(objects), "zones": ["porch"],
                                              "sub_labels": [], **data}}}


def test_example_config_parses():
    cfg = example_config()
    assert {"arrival", "pool_safety", "camera_health", "bins"} <= set(cfg.question_sets)
    health = next(s for s in cfg.schedules if s.name == "morning_camera_health")
    assert set(health.cameras) == {"front_door", "driveway", "pool", "garage"}


def test_review_runs_set_and_publishes(env):
    eng, published, _ = env
    results = eng.handle_review(review())
    assert len(results) == 1 and "answers" in results[0]
    blocks, questions = eng.decisions.calls[0]
    assert blocks[0]["type"] == "text" and "Front Door" in blocks[0]["text"]
    assert blocks[1]["type"] == "image" and blocks[1]["resolution"] == "medium"
    assert eng.frigate.calls == [("snapshot", "e1")]
    topics = [t for t, _, _ in published]
    assert "frigate/decisions/front_door/concern" in topics
    assert topics[-1] == "frigate/decisions/events"
    event = published[-1][1]
    assert event["review_id"] == "r1" and event["set"] == "arrival"


def test_review_filters(env):
    eng, _, _ = env
    assert eng.handle_review(review(objects=("car",))) == []          # label filter
    assert eng.handle_review(review(rtype="end")) == []               # review type filter
    assert eng.handle_review(review(camera="unknown_cam")) == []      # unconfigured camera
    assert eng.decisions.calls == []


def test_review_dedupe_and_cooldown(env):
    eng, _, clock = env
    assert len(eng.handle_review(review())) == 1
    assert eng.handle_review(review()) == []                          # same review id
    # driveway has a 30s cooldown
    assert len(eng.handle_review(review(camera="driveway", rid="a"))) == 1
    clock[0] += 10
    assert eng.handle_review(review(camera="driveway", rid="b")) == []
    clock[0] += 25
    assert len(eng.handle_review(review(camera="driveway", rid="c"))) == 1


def test_request_with_image_url(env):
    eng, published, _ = env
    res = eng.handle_request({"request_id": "q1", "set": "bins", "camera": "garage",
                              "image_url": "http://ha.local/local/x.jpg"})
    assert res["request_id"] == "q1" and "answers" in res
    assert eng.frigate.calls == [("url", "http://ha.local/local/x.jpg")]
    bad = eng.handle_request({"set": "nope"})
    assert "error" in bad and published[-1][1]["error"]


def test_schedules_due_once_per_day(env):
    eng, _, _ = env
    mon_7 = datetime(2026, 11, 9, 7, 0)  # a Monday
    assert [s.name for s in eng.due_schedules(mon_7)] == ["morning_camera_health"]
    assert eng.due_schedules(mon_7) == []
    mon_21 = datetime(2026, 11, 9, 21, 0)
    assert [s.name for s in eng.due_schedules(mon_21)] == ["bins_check"]
    tue_21 = datetime(2026, 11, 10, 21, 0)
    assert eng.due_schedules(tue_21) == []
    results = eng.run_schedule(next(s for s in eng.q.schedules if s.name == "morning_camera_health"))
    assert len(results) == 4


def test_decision_error_published(env):
    eng, published, _ = env

    def boom(*_):
        raise DecisionsError(400, "blocked", "safety")

    eng.decisions.decide = boom
    res = eng.handle_review(review(rid="err"))[0]
    assert "error" in res and "safety" in res["error"]
    assert published[-1][0] == "frigate/decisions/events"


def test_discovery(env):
    eng, _, _ = env
    msgs = dict(eng.discovery_messages())
    cfg = json.loads(msgs["homeassistant/sensor/frigate_decisions_pool_child_near_pool/config"])
    assert cfg["state_topic"] == "frigate/decisions/pool/child_near_pool"
    assert "homeassistant/sensor/frigate_decisions_garage_image_state/config" in msgs
    assert "homeassistant/sensor/frigate_decisions_garage_bins_at_curb/config" in msgs


def test_render_context_tolerates_missing_and_braces():
    assert render_context("hi {camera} {missing}", {"camera": "x"}) == "hi x "
    assert render_context("bad {", {}) == "bad {"


@pytest.mark.parametrize("bad", [
    {"question_sets": {"s": {"questions": {"q": {"type": "choice", "instructions": "x", "criteria": {"a": "1"}}}}}},
    {"question_sets": {"s": {"questions": {"bad key!": {"type": "noul", "instructions": "x"}}}}},
    {"question_sets": {"s": {"questions": {"q": {"type": "noul", "instructions": "x"}}}},
     "cameras": {"c": {"on_review": [{"set": "missing"}]}}},
    {"question_sets": {"s": {"media": "clip", "questions": {"q": {"type": "noul", "instructions": "x"}}}},
     "cameras": {"c": {"on_review": [{"set": "s", "review_types": ["new"]}]}}},
])
def test_config_validation(bad):
    with pytest.raises(ConfigError):
        parse_question_config(bad)


def test_normalize_answer():
    assert normalize_answer({"type": "noul", "noul": 0.7, "confidence": 0.7})["value"] == 0.7
    assert normalize_answer({"type": "choice", "choice": "a", "probabilities": {"a": 1}})["value"] == "a"
    assert normalize_answer({"type": "score", "score": 2.5, "legend": {}})["value"] == 2.5


def test_client_request_shape_and_retry():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        return httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.2, "confidence": 0.8}},
                                         "usage": {"total_output_tokens": 0}})

    client = DecisionsClient("secret", http=httpx.Client(transport=httpx.MockTransport(handler)))
    client_sleep = __import__("frigate_decisions.decisions", fromlist=["time"]).time
    orig = client_sleep.sleep
    client_sleep.sleep = lambda s: None
    try:
        out = client.decide([{"type": "text", "text": "x"}], {"q": {"type": "noul", "instructions": "?"}})
    finally:
        client_sleep.sleep = orig
    assert out["answers"]["q"]["value"] == 0.2
    req = seen[-1]
    assert req.url.path == "/v1beta/decisions" and req.headers["x-goog-api-key"] == "secret"
    body = json.loads(req.content)
    assert body["model"] == "gemini-decision-1" and "q" in body["questions"]


def test_client_raises_on_400():
    client = DecisionsClient("k", http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": {"code": "safety", "message": "blocked"}}))))
    with pytest.raises(DecisionsError) as exc:
        client.decide([{"type": "text", "text": "x"}], {"q": {"type": "noul", "instructions": "?"}})
    assert exc.value.code == "safety" and exc.value.status == 400
