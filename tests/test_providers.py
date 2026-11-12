import json
from pathlib import Path

import httpx
import pytest
import yaml

from frigate_decisions import decisions as dmod
from frigate_decisions.config import (
    ConfigError, Settings, parse_question_config, validate_for_provider,
)
from frigate_decisions.decisions import (
    DecisionsError, GeminiProvider, OpenAIProvider, SystemOneProvider, make_provider,
)

ROOT = Path(__file__).resolve().parents[1]

QUESTIONS = {
    "real_person": {"type": "noul", "instructions": "Is a person visible?", "true_description": "a human"},
    "who": {"type": "choice", "instructions": "Who?", "criteria": {"resident": "lives here", "unknown": "other"}},
    "concern": {"type": "score", "instructions": "How concerning?", "criteria": ["none", "some", "high"]},
}
BLOCKS = [
    {"type": "text", "text": "Camera front door\n"},
    {"type": "image", "data": "QUJD", "mime_type": "image/jpeg", "resolution": "medium"},
]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(dmod.time, "sleep", lambda s: None)


def capture(response_json, status=200):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=response_json)

    return seen, httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- OpenAI
OPENAI_RESPONSE = {
    "model": "gpt-6-luna",
    "answers": [
        {"type": "predicate", "name": "real_person", "probability": 0.93},
        {"type": "choice", "name": "who", "choice": "unknown", "confidence": 0.8,
         "probabilities": [{"value": "resident", "probability": 0.2}, {"value": "unknown", "probability": 0.8}]},
        {"type": "score", "name": "concern", "score": 1.4, "confidence": 0.6,
         "probabilities": [{"value": 0, "label": "0", "probability": 0.1},
                           {"value": 1, "label": "1", "probability": 0.4},
                           {"value": 2, "label": "2", "probability": 0.5}]},
    ],
    "usage": {"input_tokens": 321, "output_tokens": 0},
}


def test_openai_request_shape():
    seen, http = capture(OPENAI_RESPONSE)
    OpenAIProvider("sk-test", http=http).decide(BLOCKS, QUESTIONS)
    req = seen[0]
    assert str(req.url) == "https://api.openai.com/v1/decisions"
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["model"] == "gpt-6-luna"
    content = body["input"][0]["content"]
    assert body["input"][0]["role"] == "user"
    assert content[0] == {"type": "input_text", "text": "Camera front door\n"}
    assert content[1] == {"type": "input_image", "image_url": "data:image/jpeg;base64,QUJD", "detail": "auto"}
    qs = {q["name"]: q for q in body["questions"]}
    assert qs["real_person"]["type"] == "predicate" and "Yes means: a human" in qs["real_person"]["instructions"]
    assert qs["who"]["choices"] == [{"value": "resident", "description": "lives here"},
                                    {"value": "unknown", "description": "other"}]
    assert qs["concern"]["levels"][2] == {"label": "2", "description": "high"}


def test_openai_answer_normalization():
    _, http = capture(OPENAI_RESPONSE)
    out = OpenAIProvider("k", http=http).decide(BLOCKS, QUESTIONS)
    a = out["answers"]
    assert a["real_person"] == {"type": "noul", "value": 0.93, "confidence": 0.93}
    assert a["who"]["value"] == "unknown" and a["who"]["probabilities"] == {"resident": 0.2, "unknown": 0.8}
    assert a["concern"]["value"] == 1.4 and a["concern"]["probabilities"]["2"] == 0.5
    assert a["concern"]["legend"] == {"0": "none", "1": "some", "2": "high"}
    assert out["usage"]["total_input_tokens"] == 321


def test_openai_refusal_and_video_rejected():
    _, http = capture({"answers": [{"type": "refusal", "name": "who", "refusal": "can't"}], "usage": {}})
    out = OpenAIProvider("k", http=http).decide(BLOCKS, QUESTIONS)
    assert out["answers"]["who"]["type"] == "refusal" and out["answers"]["who"]["value"] is None
    with pytest.raises(ValueError, match="text and images only"):
        OpenAIProvider("k", http=http).decide([{"type": "video", "data": "x", "mime_type": "video/mp4"}], QUESTIONS)


# ------------------------------------------------------------- SystemOne
SYSTEMONE_RESPONSE = {
    "model": "jev-1.14.0",
    "answers": {
        "real_person": {"type": "noul", "noul": 0.88, "confidence": 0.88},
        "who": {"type": "choice", "choice": "resident", "confidence": 0.7,
                "probabilities": {"resident": 0.7, "unknown": 0.3}},
        "concern": {"type": "score", "score": 0.4, "confidence": 0.65, "legend": {"0": "none"},
                    "probabilities": {"0": 0.65, "1": 0.3, "2": 0.05}},
    },
    "usage": {"input_tokens": 200, "output_tokens": 0},
}


def test_systemone_request_shape_and_answers():
    seen, http = capture(SYSTEMONE_RESPONSE)
    out = SystemOneProvider("ts-key", http=http).decide(BLOCKS, QUESTIONS)
    req = seen[0]
    assert str(req.url) == "https://api.typesafe.ai/v1/systemone"
    assert req.headers["authorization"] == "Bearer ts-key"
    body = json.loads(req.content)
    assert body["model"] == "jev-latest"
    assert body["state"][1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}
    assert body["questions"]["who"]["criteria"] == QUESTIONS["who"]["criteria"]
    assert "true_description" not in body["questions"]["real_person"]
    assert out["answers"]["real_person"]["value"] == 0.88
    assert out["answers"]["who"]["value"] == "resident"
    assert out["usage"]["total_input_tokens"] == 200


def test_systemone_text_only_state_is_string_and_openrouter_path():
    seen, http = capture(SYSTEMONE_RESPONSE)
    SystemOneProvider("or-key", base_url="https://openrouter.ai/api", path="/alpha/decisions",
                      model="typesafe/jev-1.14", http=http).decide([BLOCKS[0]], QUESTIONS)
    assert str(seen[0].url) == "https://openrouter.ai/api/alpha/decisions"
    body = json.loads(seen[0].content)
    assert body["state"] == "Camera front door\n" and body["model"] == "typesafe/jev-1.14"


@pytest.mark.parametrize("cls", [GeminiProvider, OpenAIProvider, SystemOneProvider])
def test_errors_raise(cls):
    _, http = capture({"error": {"code": "invalid_request", "message": "bad"}}, status=400)
    with pytest.raises(DecisionsError) as exc:
        cls("k", http=http).decide([BLOCKS[0]], QUESTIONS)
    assert exc.value.status == 400 and exc.value.code == "invalid_request"


# ------------------------------------------------------- factory / config
def test_make_provider_defaults_and_overrides():
    p = make_provider(Settings(decisions_api_key="k"))
    assert isinstance(p, GeminiProvider) and p.model == "gemini-decision-1"
    assert p.url.endswith("/v1beta/decisions")
    p = make_provider(Settings(decisions_api_key="k", decisions_provider="openai"))
    assert isinstance(p, OpenAIProvider) and p.model == "gpt-6-luna"
    p = make_provider(Settings(decisions_api_key="k", decisions_provider="systemone",
                               decisions_base_url="https://openrouter.ai/api", decisions_path="/alpha/decisions",
                               decision_model="upstage/solar-decide"))
    assert p.url == "https://openrouter.ai/api/alpha/decisions" and p.model == "upstage/solar-decide"
    with pytest.raises(ConfigError):
        Settings(decisions_api_key="k", decisions_provider="anthropic")


def test_from_env_backwards_compatible(monkeypatch):
    for k in ("DECISIONS_PROVIDER", "DECISIONS_API_KEY", "DECISIONS_BASE_URL", "DECISION_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "legacy")
    s = Settings.from_env()
    assert s.decisions_provider == "gemini" and s.decisions_api_key == "legacy"
    monkeypatch.setenv("DECISIONS_PROVIDER", "openai")
    with pytest.raises(ConfigError, match="DECISIONS_API_KEY"):
        Settings.from_env()  # GEMINI_API_KEY is not reused for other providers


def test_example_config_valid_for_all_providers():
    cfg = parse_question_config(yaml.safe_load((ROOT / "questions.example.yaml").read_text()))
    for provider in ("gemini", "openai", "systemone"):
        validate_for_provider(cfg, provider)


def test_provider_limits():
    base = {"question_sets": {"s": {"questions": {
        "q": {"type": "score", "instructions": "x", "criteria": [str(i) for i in range(12)]}}}}}
    cfg = parse_question_config(base)
    validate_for_provider(cfg, "gemini")
    with pytest.raises(ConfigError, match="score levels"):
        validate_for_provider(cfg, "openai")
    clip = parse_question_config({"question_sets": {"c": {"media": "clip", "questions": {
        "q": {"type": "noul", "instructions": "x"}}}}})
    with pytest.raises(ConfigError, match="not supported by systemone"):
        validate_for_provider(clip, "systemone")
