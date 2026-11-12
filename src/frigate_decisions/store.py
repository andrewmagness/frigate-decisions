"""Append-only JSONL log of decision events and user feedback.

Enabled by setting DATA_DIR. Two files are written:
  events.jsonl    one line per Decisions run (the same JSON as frigate/decisions/events)
  feedback.jsonl  one line per vote received on {TOPIC_PREFIX}/feedback
Use `frigate-decisions-report` to join and summarise them.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

VOTES = {"up", "down"}


class Recorder:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.feedback_path = self.dir / "feedback.jsonl"
        self._lock = threading.Lock()

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with self._lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def event(self, result: dict[str, Any]) -> None:
        self._append(self.events_path, result)

    def feedback(self, event_id: str, vote: str, **extra: Any) -> dict[str, Any]:
        if vote not in VOTES:
            raise ValueError(f"vote must be one of {sorted(VOTES)}")
        record = {"id": str(event_id), "vote": vote, "ts": time.time(),
                  **{k: v for k, v in extra.items() if v is not None}}
        self._append(self.feedback_path, record)
        return record


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out
