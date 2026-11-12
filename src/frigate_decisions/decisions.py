"""Clients for native "decisions" APIs.

Internally everything uses the Gemini Decisions shape:
  * input blocks:  {"type": "text", "text"} | {"type": "image"|"video"|"audio"|"document",
                    "data": <base64>, "mime_type", "resolution"?}
  * questions:     {key: {"type": "noul"|"choice"|"score", "instructions", "criteria"?, ...}}
  * answers:       {key: {"type", "value", "confidence", "probabilities"?, "legend"?}}

Providers translate to and from their own wire format:
  * GeminiProvider     POST {base}/v1beta/decisions          (gemini-decision-1)
  * OpenAIProvider     POST {base}/v1/decisions              (gpt-6-luna)
  * SystemOneProvider  POST {base}/v1/systemone              (TypeSafe Jev), or any
                       compatible gateway such as OpenRouter's /api/alpha/decisions
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class DecisionsError(RuntimeError):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(f"HTTP {status}: {code or ''} {message}".strip())
        self.status = status
        self.code = code


@dataclass
class Media:
    data: bytes
    mime_type: str
    kind: str  # image | video | audio | document

    def block(self, resolution: str | None = None) -> dict[str, Any]:
        b: dict[str, Any] = {
            "type": self.kind,
            "data": base64.b64encode(self.data).decode("ascii"),
            "mime_type": self.mime_type,
        }
        if resolution and self.kind in {"image", "video"}:
            b["resolution"] = resolution
        return b


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def normalize_answer(answer: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Gemini/systemone answer into {type, value, confidence, ...extras}.

    value is the probability for noul, the chosen key for choice, and the
    probability-weighted level index for score.
    """
    atype = answer.get("type")
    out: dict[str, Any] = {"type": atype, "confidence": answer.get("confidence")}
    if atype == "noul":
        out["value"] = answer.get("noul")
    elif atype == "choice":
        out["value"] = answer.get("choice")
        out["probabilities"] = answer.get("probabilities")
    elif atype == "score":
        out["value"] = answer.get("score")
        out["probabilities"] = answer.get("probabilities")
        out["legend"] = answer.get("legend")
    else:
        out["value"] = None
        out["raw"] = answer
    return out


class DecisionsProvider(Protocol):
    name: str
    model: str

    def decide(self, input_blocks: list[dict[str, Any]],
               questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Return {"answers": {key: normalized}, "usage": {...}}."""


def _error_details(resp: httpx.Response) -> tuple[str | None, str]:
    try:
        err = resp.json().get("error") or {}
        if isinstance(err, str):
            return None, err
        code = err.get("code") or err.get("type")
        return (str(code) if code is not None else None), str(err.get("message") or resp.text[:300])
    except ValueError:
        return None, resp.text[:300]


class _HTTPProvider:
    name = "base"

    def __init__(self, api_key: str, base_url: str, path: str, model: str, headers: dict[str, str],
                 timeout: float = 60.0, max_attempts: int = 3, http: httpx.Client | None = None):
        self.model = model
        self.url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        self.max_attempts = max_attempts
        self._http = http or httpx.Client(timeout=timeout)
        self._headers = {"Content-Type": "application/json", **headers}

    def _post_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        delay = 1.0
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self._http.post(self.url, json=body, headers=self._headers)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise DecisionsError(0, f"transport error: {exc}") from exc
                log.warning("%s transport error (attempt %d): %s", self.name, attempt, exc)
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 200:
                return resp.json()
            code, message = _error_details(resp)
            if resp.status_code in RETRY_STATUS and attempt < self.max_attempts:
                log.warning("%s HTTP %s (attempt %d), retrying in %.0fs", self.name, resp.status_code,
                            attempt, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise DecisionsError(resp.status_code, message, code)
        raise DecisionsError(0, "exhausted retries")  # pragma: no cover


# --------------------------------------------------------------------- Gemini
class GeminiProvider(_HTTPProvider):
    """Google Gemini Decisions API. Request/response are already canonical."""

    name = "gemini"

    def __init__(self, api_key: str, base_url: str = "https://generativelanguage.googleapis.com",
                 model: str = "gemini-decision-1", auth_header: str = "x-goog-api-key",
                 timeout: float = 60.0, max_attempts: int = 3, http: httpx.Client | None = None,
                 path: str = "/v1beta/decisions"):
        super().__init__(api_key, base_url, path, model, {auth_header: api_key}, timeout, max_attempts, http)

    def decide(self, input_blocks, questions):
        data = self._post_with_retry({"model": self.model, "input": input_blocks, "questions": questions})
        answers = {k: normalize_answer(v) for k, v in (data.get("answers") or {}).items()}
        return {"answers": answers, "usage": data.get("usage") or {}}


# Backwards-compatible name used by earlier versions.
DecisionsClient = GeminiProvider


def _data_url(block: dict[str, Any]) -> str:
    return f"data:{block['mime_type']};base64,{block['data']}"


def _only_text_and_images(provider: str, blocks: list[dict[str, Any]]) -> None:
    for b in blocks:
        if b["type"] not in {"text", "image"}:
            raise ValueError(f"{provider} decisions accept text and images only, not {b['type']} "
                             "(use media: snapshot or latest)")


# --------------------------------------------------------------------- OpenAI
class OpenAIProvider(_HTTPProvider):
    """OpenAI Decisions API (POST /v1/decisions)."""

    name = "openai"
    _DETAIL = {"low": "low", "medium": "auto", "high": "high", "ultra_high": "original"}

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com", model: str = "gpt-6-luna",
                 timeout: float = 60.0, max_attempts: int = 3, http: httpx.Client | None = None,
                 path: str = "/v1/decisions"):
        super().__init__(api_key, base_url, path, model, {"Authorization": f"Bearer {api_key}"},
                         timeout, max_attempts, http)

    @classmethod
    def build_input(cls, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        _only_text_and_images("openai", blocks)
        content: list[dict[str, Any]] = []
        for b in blocks:
            if b["type"] == "text":
                content.append({"type": "input_text", "text": b["text"]})
            else:
                part = {"type": "input_image", "image_url": _data_url(b)}
                if b.get("resolution"):
                    part["detail"] = cls._DETAIL.get(b["resolution"], "auto")
                content.append(part)
        return [{"role": "user", "content": content}]

    @staticmethod
    def build_questions(questions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for key, q in questions.items():
            if q["type"] == "noul":
                instructions = q["instructions"]
                extra = []
                if q.get("true_description"):
                    extra.append(f"Yes means: {q['true_description']}")
                if q.get("false_description"):
                    extra.append(f"No means: {q['false_description']}")
                if extra:
                    instructions = instructions.rstrip() + "\n" + "\n".join(extra)
                out.append({"type": "predicate", "name": key, "instructions": instructions})
            elif q["type"] == "choice":
                out.append({"type": "choice", "name": key, "instructions": q["instructions"],
                            "choices": [{"value": v, "description": d} for v, d in q["criteria"].items()]})
            else:
                out.append({"type": "score", "name": key, "instructions": q["instructions"],
                            "levels": [{"label": str(i), "description": d} for i, d in enumerate(q["criteria"])]})
        return out

    @staticmethod
    def normalize(answer: dict[str, Any], question: dict[str, Any] | None) -> dict[str, Any]:
        atype = answer.get("type")
        if atype == "predicate":
            p = answer.get("probability")
            conf = answer.get("confidence", None if p is None else max(p, 1 - p))
            return {"type": "noul", "value": p, "confidence": conf}
        if atype == "choice":
            probs = {str(p["value"]): p["probability"] for p in answer.get("probabilities") or []}
            return {"type": "choice", "value": answer.get("choice"), "confidence": answer.get("confidence"),
                    "probabilities": probs}
        if atype == "score":
            probs = {}
            legend = {}
            for i, p in enumerate(answer.get("probabilities") or []):
                idx = str(p.get("label", p.get("value", i)))
                probs[idx] = p["probability"]
            if question and question.get("criteria"):
                legend = {str(i): d for i, d in enumerate(question["criteria"])}
            return {"type": "score", "value": answer.get("score"), "confidence": answer.get("confidence"),
                    "probabilities": probs, "legend": legend}
        if atype == "refusal":
            return {"type": "refusal", "value": None, "confidence": None,
                    "error": answer.get("refusal") or "refused"}
        return {"type": atype, "value": None, "confidence": None, "raw": answer}

    def decide(self, input_blocks, questions):
        data = self._post_with_retry({"model": self.model, "input": self.build_input(input_blocks),
                                      "questions": self.build_questions(questions)})
        answers = {}
        for ans in data.get("answers") or []:
            name = ans.get("name")
            if name is not None:
                answers[name] = self.normalize(ans, questions.get(name))
        usage = data.get("usage") or {}
        if "input_tokens" in usage and "total_input_tokens" not in usage:
            usage = {**usage, "total_input_tokens": usage["input_tokens"]}
        return {"answers": answers, "usage": usage}


# ------------------------------------------------------------------ SystemOne
class SystemOneProvider(_HTTPProvider):
    """TypeSafe Jev (POST /v1/systemone) and compatible gateways, e.g. OpenRouter's
    POST /api/alpha/decisions, which route to many decision models."""

    name = "systemone"

    def __init__(self, api_key: str, base_url: str = "https://api.typesafe.ai", model: str = "jev-latest",
                 timeout: float = 60.0, max_attempts: int = 3, http: httpx.Client | None = None,
                 path: str = "/v1/systemone"):
        super().__init__(api_key, base_url, path, model, {"Authorization": f"Bearer {api_key}"},
                         timeout, max_attempts, http)

    @staticmethod
    def build_state(blocks: list[dict[str, Any]]) -> Any:
        _only_text_and_images("systemone", blocks)
        if all(b["type"] == "text" for b in blocks):
            return "".join(b["text"] for b in blocks)
        parts: list[dict[str, Any]] = []
        for b in blocks:
            if b["type"] == "text":
                parts.append({"type": "text", "text": b["text"]})
            else:
                parts.append({"type": "image_url", "image_url": {"url": _data_url(b)}})
        return parts

    @staticmethod
    def build_questions(questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out = {}
        for key, q in questions.items():
            item = {"type": q["type"], "instructions": q["instructions"]}
            if q["type"] == "noul":
                extra = [f"{label}: {q[f]}" for f, label in
                         (("true_description", "Yes means"), ("false_description", "No means")) if q.get(f)]
                if extra:
                    item["instructions"] = q["instructions"].rstrip() + "\n" + "\n".join(extra)
            else:
                item["criteria"] = q["criteria"]
            out[key] = item
        return out

    def decide(self, input_blocks, questions):
        data = self._post_with_retry({"model": self.model, "state": self.build_state(input_blocks),
                                      "questions": self.build_questions(questions)})
        answers = {k: normalize_answer(v) for k, v in (data.get("answers") or {}).items()}
        usage = data.get("usage") or {}
        if "input_tokens" in usage and "total_input_tokens" not in usage:
            usage = {**usage, "total_input_tokens": usage["input_tokens"]}
        return {"answers": answers, "usage": usage}


PROVIDERS = {"gemini": GeminiProvider, "openai": OpenAIProvider, "systemone": SystemOneProvider}


def make_provider(settings: Any, http: httpx.Client | None = None) -> DecisionsProvider:
    """Build the configured provider from Settings."""
    p = settings.decisions_provider
    common: dict[str, Any] = {"timeout": settings.request_timeout, "http": http,
                              "model": settings.decision_model}
    if settings.decisions_base_url:
        common["base_url"] = settings.decisions_base_url
    if settings.decisions_path:
        common["path"] = settings.decisions_path
    if p == "gemini":
        return GeminiProvider(settings.decisions_api_key, auth_header=settings.gemini_auth_header, **common)
    if p == "openai":
        return OpenAIProvider(settings.decisions_api_key, **common)
    if p == "systemone":
        return SystemOneProvider(settings.decisions_api_key, **common)
    raise ValueError(f"unknown decisions provider {p!r}")
