"""Core logic: match Frigate reviews / schedules / requests to question sets,
call the Decisions API, and emit MQTT messages. Transport-agnostic for testing."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Any, Callable

from .config import QuestionConfig, QuestionSet, ReviewRule, Schedule, Settings
from .decisions import DecisionsError, DecisionsProvider, Media, text_block
from .frigate import FrigateClient

log = logging.getLogger(__name__)

Publish = Callable[[str, str, bool], None]


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def render_context(template: str, values: dict[str, Any]) -> str:
    if not template:
        return ""
    try:
        return template.format_map(_SafeDict(values))
    except (ValueError, IndexError) as exc:  # stray braces in user text
        log.warning("context template error (%s); sending it unformatted", exc)
        return template


def _join(items: Any) -> str:
    if not items:
        return "none"
    if isinstance(items, (list, tuple, set)):
        flat = []
        for i in items:
            flat.append(str(i[0]) if isinstance(i, (list, tuple)) and i else str(i))
        return ", ".join(dict.fromkeys(flat)) or "none"
    return str(items)


class Engine:
    def __init__(
        self,
        settings: Settings,
        qconfig: QuestionConfig,
        decisions: DecisionsProvider,
        frigate: FrigateClient,
        publish: Publish,
        clock: Callable[[], float] = time.time,
    ):
        self.s = settings
        self.q = qconfig
        self.decisions = decisions
        self.frigate = frigate
        self.publish = publish
        self.clock = clock
        self._lock = threading.Lock()
        self._seen: OrderedDict[tuple, float] = OrderedDict()
        self._last_run: dict[tuple[str, str], float] = {}
        self._schedule_ran: dict[str, str] = {}

    # ------------------------------------------------------------------ topics
    def state_topic(self, camera: str, key: str) -> str:
        return f"{self.s.topic_prefix}/{camera}/{key}"

    @property
    def events_topic(self) -> str:
        return f"{self.s.topic_prefix}/events"

    @property
    def availability_topic(self) -> str:
        return f"{self.s.topic_prefix}/availability"

    @property
    def request_topic(self) -> str:
        return f"{self.s.topic_prefix}/request"

    @property
    def reviews_topic(self) -> str:
        return f"{self.s.frigate_topic_prefix}/reviews"

    # --------------------------------------------------------------- discovery
    def discovery_messages(self) -> list[tuple[str, str]]:
        msgs: list[tuple[str, str]] = []
        device = {
            "identifiers": ["frigate_decisions"],
            "name": "Frigate Decisions",
            "manufacturer": "frigate-decisions",
            "model": self.s.decision_model,
        }
        cams = set(self.q.cameras)
        for sched in self.q.schedules:
            cams.update(sched.cameras)
        for cam in sorted(cams):
            for set_name in sorted(self.q.sets_for_camera(cam)):
                qset = self.q.question_sets[set_name]
                for key, question in qset.questions.items():
                    uid = f"frigate_decisions_{cam}_{key}"
                    cfg: dict[str, Any] = {
                        "name": f"{cam.replace('_', ' ').title()} {key.replace('_', ' ')}",
                        "unique_id": uid,
                        "object_id": uid,
                        "state_topic": self.state_topic(cam, key),
                        "value_template": "{{ value_json.value }}",
                        "json_attributes_topic": self.state_topic(cam, key),
                        "availability_topic": self.availability_topic,
                        "device": device,
                        "icon": "mdi:scale-balance",
                    }
                    if question["type"] in {"noul", "score"}:
                        cfg["state_class"] = "measurement"
                    msgs.append((f"{self.s.discovery_prefix}/sensor/{uid}/config", json.dumps(cfg)))
        return msgs

    # ----------------------------------------------------------------- reviews
    def _dedupe(self, key: tuple) -> bool:
        """True if key was already processed."""
        with self._lock:
            if key in self._seen:
                return True
            self._seen[key] = self.clock()
            while len(self._seen) > 2000:
                self._seen.popitem(last=False)
            return False

    def _cooled_down(self, camera: str, set_name: str, cooldown: float) -> bool:
        if cooldown <= 0:
            return True
        now = self.clock()
        with self._lock:
            last = self._last_run.get((camera, set_name))
            if last is not None and now - last < cooldown:
                return False
            self._last_run[(camera, set_name)] = now
            return True

    @staticmethod
    def _rule_matches(rule: ReviewRule, rtype: str, severity: str, labels: list[str], zones: list[str]) -> bool:
        if rtype not in rule.review_types or severity not in rule.severity:
            return False
        if rule.labels and not set(rule.labels) & set(labels):
            return False
        if rule.zones and not set(rule.zones) & set(zones):
            return False
        return True

    def handle_review(self, msg: dict[str, Any]) -> list[dict[str, Any]]:
        rtype = msg.get("type")
        after = msg.get("after") or {}
        camera = after.get("camera")
        if not camera or camera not in self.q.cameras:
            return []
        data = after.get("data") or {}
        severity = after.get("severity") or ""
        labels = [str(o).split("-")[0] for o in (data.get("objects") or [])]
        zones = list(data.get("zones") or [])
        review_id = after.get("id")
        results = []
        for rule in self.q.cameras[camera]:
            if not self._rule_matches(rule, rtype, severity, labels, zones):
                continue
            if rtype != "update" and self._dedupe((review_id, rule.set, rtype)):
                continue
            if not self._cooled_down(camera, rule.set, rule.cooldown_seconds):
                log.debug("cooldown: skipping %s/%s", camera, rule.set)
                continue
            metadata = data.get("metadata") or {}
            values = {
                "camera": camera,
                "camera_name": camera.replace("_", " ").title(),
                "labels": _join(labels),
                "sub_labels": _join(data.get("sub_labels")),
                "zones": _join(zones),
                "severity": severity,
                "review_type": rtype,
                "genai_title": metadata.get("title", ""),
                "genai_scene": metadata.get("scene", ""),
            }
            detections = data.get("detections") or []
            results.append(
                self.run_set(
                    rule.set, camera, values,
                    source="review",
                    extra={"review_id": review_id, "review_type": rtype, "severity": severity,
                           "labels": labels, "sub_labels": data.get("sub_labels") or [], "zones": zones,
                           "detections": detections},
                    event_id=detections[0] if detections else None,
                )
            )
        return results

    # ---------------------------------------------------------------- requests
    def handle_request(self, msg: dict[str, Any]) -> dict[str, Any]:
        set_name = msg.get("set")
        camera = msg.get("camera") or "request"
        if set_name not in self.q.question_sets:
            result = {"source": "request", "request_id": msg.get("request_id"), "camera": camera,
                      "set": set_name, "error": f"unknown question set {set_name!r}"}
            self.publish(self.events_topic, json.dumps(result), False)
            return result
        values = {"camera": camera, "camera_name": camera.replace("_", " ").title(),
                  **{str(k): v for k, v in (msg.get("context") or {}).items()}}
        return self.run_set(
            set_name, camera, values, source="request",
            extra={"request_id": msg.get("request_id") or uuid.uuid4().hex[:12]},
            image_url=msg.get("image_url"),
        )

    # --------------------------------------------------------------- schedules
    def due_schedules(self, now: datetime) -> list[Schedule]:
        due = []
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")
        for sched in self.q.schedules:
            if sched.days is not None and now.weekday() not in sched.days:
                continue
            if hhmm == sched.at and self._schedule_ran.get(sched.name) != today:
                self._schedule_ran[sched.name] = today
                due.append(sched)
        return due

    def run_schedule(self, sched: Schedule) -> list[dict[str, Any]]:
        out = []
        for cam in sched.cameras:
            values = {"camera": cam, "camera_name": cam.replace("_", " ").title()}
            out.append(self.run_set(sched.set, cam, values, source="schedule", extra={"schedule": sched.name}))
        return out

    # -------------------------------------------------------------------- core
    def _media_for(self, qset: QuestionSet, camera: str, event_id: str | None, image_url: str | None) -> Media | None:
        if image_url:
            return self.frigate.url(image_url)
        if qset.media == "none":
            return None
        if qset.media == "latest" or event_id is None:
            return self.frigate.latest(camera)
        if qset.media == "clip":
            return self.frigate.event_clip(event_id)
        return self.frigate.event_snapshot(event_id)

    def run_set(self, set_name: str, camera: str, values: dict[str, Any], source: str,
                extra: dict[str, Any] | None = None, event_id: str | None = None,
                image_url: str | None = None) -> dict[str, Any]:
        qset = self.q.question_sets[set_name]
        values = {"time": datetime.now().strftime("%A %H:%M"), **values}
        result: dict[str, Any] = {"source": source, "camera": camera, "set": set_name, **(extra or {})}
        started = time.monotonic()
        try:
            media = self._media_for(qset, camera, event_id, image_url)
            blocks = []
            context = render_context(qset.context, values)
            if context:
                blocks.append(text_block(context.rstrip("\n") + "\n"))
            if media is not None:
                blocks.append(media.block(qset.resolution))
            if not blocks:
                raise ValueError("question set produced no input (no context and media: none)")
            resp = self.decisions.decide(blocks, qset.questions)
            result["answers"] = resp["answers"]
            result["usage"] = resp["usage"]
            result["media"] = media.kind if media else None
        except (DecisionsError, RuntimeError, ValueError) as exc:
            log.error("%s/%s (%s) failed: %s", camera, set_name, source, exc)
            result["error"] = str(exc)
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        result["ts"] = self.clock()

        for key, ans in (result.get("answers") or {}).items():
            state = {**ans, "set": set_name, "source": source, "ts": result["ts"],
                     "review_id": result.get("review_id")}
            self.publish(self.state_topic(camera, key), json.dumps(state), True)
        self.publish(self.events_topic, json.dumps(result), False)
        if "answers" in result:
            log.info("%s/%s (%s) in %dms: %s", camera, set_name, source, result["elapsed_ms"],
                     {k: v.get("value") for k, v in result["answers"].items()})
        return result
