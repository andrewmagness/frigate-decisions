"""Summarise recorded decisions against 👍/👎 feedback.

    frigate-decisions-report [DATA_DIR] [--since YYYY-MM-DD] [--json]

For every camera + question set it shows how many runs were voted up/down, and for
each question how the answers differ between 👍 and 👎 runs - the input for picking
thresholds. Down-voted runs are listed individually so they can be inspected.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from .store import read_jsonl


def load(data_dir: Path, since: float | None = None) -> list[dict[str, Any]]:
    events = [e for e in read_jsonl(data_dir / "events.jsonl") if e.get("id")]
    if since is not None:
        events = [e for e in events if (e.get("ts") or 0) >= since]
    votes: dict[str, str] = {}
    for fb in read_jsonl(data_dir / "feedback.jsonl"):  # last vote wins
        if fb.get("id") and fb.get("vote") in {"up", "down"}:
            votes[str(fb["id"])] = fb["vote"]
    for e in events:
        e["vote"] = votes.get(e["id"])
    return events


def summarise(events: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        groups[f"{e.get('camera')}/{e.get('set')}"].append(e)
    out: dict[str, Any] = {}
    for key, evs in sorted(groups.items()):
        g: dict[str, Any] = {
            "runs": len(evs),
            "errors": sum(1 for e in evs if e.get("error")),
            "votes": dict(Counter(e["vote"] or "none" for e in evs)),
            "questions": {},
            "down_voted": [],
        }
        qnames = sorted({q for e in evs for q in (e.get("answers") or {})})
        for q in qnames:
            per_vote: dict[str, Any] = {}
            for vote in ("up", "down", None):
                answers = [e["answers"][q] for e in evs if e["vote"] == vote and q in (e.get("answers") or {})]
                if not answers:
                    continue
                label = vote or "none"
                if answers[0].get("type") == "choice":
                    per_vote[label] = dict(Counter(str(a.get("value")) for a in answers).most_common())
                else:
                    vals = [a["value"] for a in answers if isinstance(a.get("value"), (int, float))]
                    if vals:
                        per_vote[label] = {"n": len(vals), "mean": round(mean(vals), 3),
                                           "min": round(min(vals), 3), "max": round(max(vals), 3)}
            g["questions"][q] = per_vote
        for e in evs:
            if e["vote"] == "down":
                g["down_voted"].append({
                    "id": e["id"], "when": _fmt(e.get("ts")), "review_id": e.get("review_id"),
                    "answers": {k: v.get("value") for k, v in (e.get("answers") or {}).items()},
                })
        out[key] = g
    return out


def _fmt(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return "?"


def _fmt_val(v: Any) -> str:
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def print_text(summary: dict[str, Any], total: int) -> None:
    print(f"{total} decision runs\n")
    for key, g in summary.items():
        votes = g["votes"]
        print(f"== {key}: {g['runs']} runs, 👍 {votes.get('up', 0)}  👎 {votes.get('down', 0)}  "
              f"unrated {votes.get('none', 0)}" + (f", {g['errors']} errors" if g["errors"] else ""))
        for q, per_vote in g["questions"].items():
            parts = []
            for label in ("up", "down", "none"):
                if label not in per_vote:
                    continue
                stats = per_vote[label]
                icon = {"up": "👍", "down": "👎", "none": "··"}[label]
                if "mean" in stats:
                    parts.append(f"{icon} mean {stats['mean']} [{stats['min']}–{stats['max']}] n={stats['n']}")
                else:
                    parts.append(f"{icon} " + ", ".join(f"{k}×{n}" for k, n in stats.items()))
            print(f"   {q:<20} " + " | ".join(parts))
        for d in g["down_voted"]:
            print(f"   👎 {d['when']} id={d['id']} review={d['review_id']} "
                  + " ".join(f"{k}={_fmt_val(v)}" for k, v in d["answers"].items()))
        print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("data_dir", nargs="?", default=os.environ.get("DATA_DIR", "/data"))
    ap.add_argument("--since", help="only runs on/after this date (YYYY-MM-DD)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args(argv)
    since = datetime.strptime(args.since, "%Y-%m-%d").timestamp() if args.since else None
    events = load(Path(args.data_dir), since)
    summary = summarise(events)
    if args.json:
        json.dump(summary, sys.stdout, indent=1, ensure_ascii=False)
        print()
    else:
        print_text(summary, len(events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
