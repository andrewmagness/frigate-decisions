"""Minimal client for the Gemini Decisions API (POST /v1beta/decisions)."""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

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
    """Flatten an API answer into {type, value, confidence, ...extras}.

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


class DecisionsClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com",
        model: str = "gemini-decision-1",
        auth_header: str = "x-goog-api-key",
        timeout: float = 60.0,
        max_attempts: int = 3,
        http: httpx.Client | None = None,
    ):
        self.model = model
        self.url = f"{base_url.rstrip('/')}/v1beta/decisions"
        self.max_attempts = max_attempts
        self._http = http or httpx.Client(timeout=timeout)
        self._headers = {auth_header: api_key, "Content-Type": "application/json"}

    def decide(self, input_blocks: list[dict[str, Any]], questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Return {"answers": {key: normalized}, "usage": {...}}."""
        body = {"model": self.model, "input": input_blocks, "questions": questions}
        delay = 1.0
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self._http.post(self.url, json=body, headers=self._headers)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise DecisionsError(0, f"transport error: {exc}") from exc
                log.warning("decisions transport error (attempt %d): %s", attempt, exc)
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 200:
                data = resp.json()
                answers = {k: normalize_answer(v) for k, v in (data.get("answers") or {}).items()}
                return {"answers": answers, "usage": data.get("usage") or {}}
            code, message = _error_details(resp)
            if resp.status_code in RETRY_STATUS and attempt < self.max_attempts:
                log.warning("decisions HTTP %s (attempt %d), retrying in %.0fs", resp.status_code, attempt, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise DecisionsError(resp.status_code, message, code)
        raise DecisionsError(0, "exhausted retries")  # pragma: no cover


def _error_details(resp: httpx.Response) -> tuple[str | None, str]:
    try:
        err = resp.json().get("error") or {}
        code = err.get("code")
        return (str(code) if code is not None else None), str(err.get("message") or resp.text[:300])
    except ValueError:
        return None, resp.text[:300]
