#!/usr/bin/env python3
"""Update a job's status.json so the UI can show progress.

Usage:
    python scripts/status.py <job_dir> <stage> "<message>"

Stages (in pipeline order):
    created, ingest, transcript, ledger, research, lecture, audit,
    scenes, render, done, error
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

STAGES = ["created", "ingest", "transcript", "ledger", "research",
          "lecture", "audit", "scenes", "render", "done", "error"]


def update(job_dir: Path, stage: str, message: str) -> None:
    if stage not in STAGES:
        raise SystemExit(f"unknown stage '{stage}', expected one of {STAGES}")
    path = job_dir / "status.json"
    status = {"stage": "created", "message": "", "updated": "", "history": []}
    if path.exists():
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status["stage"] = stage
    status["message"] = message
    status["updated"] = now
    status.setdefault("history", []).append(
        {"stage": stage, "message": message, "at": now})
    path.write_text(json.dumps(status, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    print(f"[{stage}] {message}")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    update(Path(sys.argv[1]), sys.argv[2], " ".join(sys.argv[3:]))
