"""Settings (from environment) and question config (from YAML)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
QUESTION_TYPES = {"noul", "choice", "score"}
MEDIA_TYPES = {"snapshot", "clip", "latest", "none"}
REVIEW_TYPES = {"new", "update", "end", "genai"}
SEVERITIES = {"alert", "detection"}


class ConfigError(ValueError):
    pass


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"environment variable {name} is required")
    return value


@dataclass
class Settings:
    gemini_api_key: str
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    gemini_auth_header: str = "x-goog-api-key"
    decision_model: str = "gemini-decision-1"
    request_timeout: float = 60.0

    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str = "frigate-decisions"
    frigate_topic_prefix: str = "frigate"
    topic_prefix: str = "frigate/decisions"
    discovery_prefix: str = "homeassistant"
    discovery_enabled: bool = True

    frigate_url: str = "http://frigate:5000"
    frigate_token: str | None = None
    frigate_verify_ssl: bool = True

    questions_file: Path = Path("/config/questions.yaml")
    workers: int = 4
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        def as_bool(v: str | None, default: bool) -> bool:
            if v is None or v == "":
                return default
            return v.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            gemini_api_key=_env("GEMINI_API_KEY", required=True) or "",
            gemini_base_url=(_env("GEMINI_BASE_URL") or cls.gemini_base_url).rstrip("/"),
            gemini_auth_header=_env("GEMINI_AUTH_HEADER") or cls.gemini_auth_header,
            decision_model=_env("DECISION_MODEL") or cls.decision_model,
            request_timeout=float(_env("REQUEST_TIMEOUT") or cls.request_timeout),
            mqtt_host=_env("MQTT_HOST") or cls.mqtt_host,
            mqtt_port=int(_env("MQTT_PORT") or cls.mqtt_port),
            mqtt_username=_env("MQTT_USERNAME") or None,
            mqtt_password=_env("MQTT_PASSWORD") or None,
            mqtt_client_id=_env("MQTT_CLIENT_ID") or cls.mqtt_client_id,
            frigate_topic_prefix=(_env("FRIGATE_TOPIC_PREFIX") or cls.frigate_topic_prefix).rstrip("/"),
            topic_prefix=(_env("TOPIC_PREFIX") or cls.topic_prefix).rstrip("/"),
            discovery_prefix=(_env("DISCOVERY_PREFIX") or cls.discovery_prefix).rstrip("/"),
            discovery_enabled=as_bool(_env("DISCOVERY_ENABLED"), True),
            frigate_url=(_env("FRIGATE_URL") or cls.frigate_url).rstrip("/"),
            frigate_token=_env("FRIGATE_TOKEN") or None,
            frigate_verify_ssl=as_bool(_env("FRIGATE_VERIFY_SSL"), True),
            questions_file=Path(_env("QUESTIONS_FILE") or str(cls.questions_file)),
            workers=int(_env("WORKERS") or cls.workers),
            log_level=(_env("LOG_LEVEL") or cls.log_level).upper(),
        )


@dataclass
class QuestionSet:
    name: str
    questions: dict[str, dict[str, Any]]
    context: str = ""
    media: str = "snapshot"
    resolution: str | None = None


@dataclass
class ReviewRule:
    set: str
    review_types: list[str] = field(default_factory=lambda: ["new"])
    severity: list[str] = field(default_factory=lambda: ["alert", "detection"])
    labels: list[str] | None = None
    zones: list[str] | None = None
    cooldown_seconds: float = 0.0


@dataclass
class Schedule:
    name: str
    at: str  # HH:MM local time
    set: str
    cameras: list[str]
    days: list[int] | None = None  # 0=Mon .. 6=Sun


@dataclass
class QuestionConfig:
    question_sets: dict[str, QuestionSet]
    cameras: dict[str, list[ReviewRule]]
    schedules: list[Schedule]

    def sets_for_camera(self, camera: str) -> set[str]:
        names = {r.set for r in self.cameras.get(camera, [])}
        for s in self.schedules:
            if camera in s.cameras:
                names.add(s.set)
        return names


DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _validate_question(set_name: str, key: str, q: dict[str, Any]) -> dict[str, Any]:
    where = f"question_sets.{set_name}.questions.{key}"
    if not KEY_RE.match(key):
        raise ConfigError(f"{where}: key must match [A-Za-z0-9_-]{{1,64}}")
    if not isinstance(q, dict):
        raise ConfigError(f"{where}: must be a mapping")
    qtype = q.get("type")
    if qtype not in QUESTION_TYPES:
        raise ConfigError(f"{where}: type must be one of {sorted(QUESTION_TYPES)}")
    if not q.get("instructions"):
        raise ConfigError(f"{where}: instructions is required")
    criteria = q.get("criteria")
    if qtype == "choice":
        if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255:
            raise ConfigError(f"{where}: choice criteria must map 2-255 option keys to descriptions")
    elif qtype == "score":
        if not isinstance(criteria, list) or not 2 <= len(criteria) <= 255:
            raise ConfigError(f"{where}: score criteria must list 2-255 levels, lowest first")
    elif criteria is not None:
        raise ConfigError(f"{where}: noul questions take no criteria (use true_description/false_description)")
    allowed = {"type", "instructions", "criteria", "true_description", "false_description"}
    return {k: v for k, v in q.items() if k in allowed}


def _as_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def parse_question_config(data: dict[str, Any]) -> QuestionConfig:
    if not isinstance(data, dict):
        raise ConfigError("questions file must be a mapping")

    sets: dict[str, QuestionSet] = {}
    for name, raw in (data.get("question_sets") or {}).items():
        if not isinstance(raw, dict) or not raw.get("questions"):
            raise ConfigError(f"question_sets.{name}: needs a non-empty 'questions' mapping")
        media = raw.get("media", "snapshot")
        if media not in MEDIA_TYPES:
            raise ConfigError(f"question_sets.{name}.media must be one of {sorted(MEDIA_TYPES)}")
        questions = {k: _validate_question(name, k, q) for k, q in raw["questions"].items()}
        if len(questions) > 1000:
            raise ConfigError(f"question_sets.{name}: at most 1000 questions")
        sets[name] = QuestionSet(
            name=name,
            questions=questions,
            context=raw.get("context", ""),
            media=media,
            resolution=raw.get("resolution"),
        )

    cameras: dict[str, list[ReviewRule]] = {}
    for cam, raw in (data.get("cameras") or {}).items():
        rules = []
        for i, r in enumerate((raw or {}).get("on_review") or []):
            if isinstance(r, str):
                r = {"set": r}
            where = f"cameras.{cam}.on_review[{i}]"
            if r.get("set") not in sets:
                raise ConfigError(f"{where}: unknown question set {r.get('set')!r}")
            types = _as_list(r.get("review_types")) or ["new"]
            if bad := set(types) - REVIEW_TYPES:
                raise ConfigError(f"{where}: unknown review_types {sorted(bad)}")
            sev = _as_list(r.get("severity")) or ["alert", "detection"]
            if bad := set(sev) - SEVERITIES:
                raise ConfigError(f"{where}: unknown severity {sorted(bad)}")
            if sets[r["set"]].media == "clip" and set(types) - {"end", "genai"}:
                raise ConfigError(f"{where}: clip media is only available on 'end'/'genai' reviews")
            rules.append(
                ReviewRule(
                    set=r["set"],
                    review_types=types,
                    severity=sev,
                    labels=_as_list(r.get("labels")),
                    zones=_as_list(r.get("zones")),
                    cooldown_seconds=float(r.get("cooldown_seconds", 0)),
                )
            )
        cameras[cam] = rules

    schedules: list[Schedule] = []
    for i, raw in enumerate(data.get("schedules") or []):
        where = f"schedules[{i}]"
        if raw.get("set") not in sets:
            raise ConfigError(f"{where}: unknown question set {raw.get('set')!r}")
        at = str(raw.get("at", ""))
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", at):
            raise ConfigError(f"{where}: 'at' must be HH:MM (24h)")
        if sets[raw["set"]].media == "clip":
            raise ConfigError(f"{where}: scheduled sets cannot use clip media")
        cams = raw.get("cameras", "all")
        cam_list = list(cameras) if cams == "all" else _as_list(cams) or []
        days = None
        if raw.get("days"):
            try:
                days = [DAY_NAMES.index(str(d).lower()[:3]) for d in _as_list(raw["days"]) or []]
            except ValueError as exc:
                raise ConfigError(f"{where}: days must be mon..sun") from exc
        schedules.append(
            Schedule(name=raw.get("name") or f"schedule_{i}", at=at, set=raw["set"], cameras=cam_list, days=days)
        )

    return QuestionConfig(question_sets=sets, cameras=cameras, schedules=schedules)


def load_question_config(path: Path) -> QuestionConfig:
    with open(path, encoding="utf-8") as fh:
        return parse_question_config(yaml.safe_load(fh) or {})
