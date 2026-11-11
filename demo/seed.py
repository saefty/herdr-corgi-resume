#!/usr/bin/env python3
"""Seeds a demo Herdr home from fixtures/sessions.json: python3 seed.py <demo home>.

Writes an invented user home (<demo home>/user) with ~/.claude and ~/.claude-oss, one transcript per session whose
only line is the last turn's usage (all the cost estimate reads), the mod's records in <demo home>/data/resume, and
<demo home>/fixture.json: the RESUME_FIXTURE that stands in for that home and for `hc ls --json`."""
import json
import os
import sys
import time
from pathlib import Path

here = Path(__file__).resolve().parent
home = Path(sys.argv[1]).resolve()
user = home / "user"
sessions = json.loads((here / "fixtures" / "sessions.json").read_text())
now = time.time()
terminals = []
for s in sessions:
    cwd = user / s["cwd"]
    cwd.mkdir(parents=True, exist_ok=True)
    config = user / s["config"]
    project = config / "projects" / str(cwd).replace("/", "-")
    project.mkdir(parents=True, exist_ok=True)
    transcript = project / f"{s['session_id']}.jsonl"
    write = s["tokens"] - 4000  # the last turn: most of the context was a cache read, the rest new
    usage = {"input_tokens": 6, "cache_read_input_tokens": 3994, "cache_creation_input_tokens": write,
             "cache_creation": {"ephemeral_1h_input_tokens": write if s["ttl"] == "1h" else 0,
                                "ephemeral_5m_input_tokens": 0 if s["ttl"] == "1h" else write}}
    transcript.write_text(json.dumps({"type": "assistant", "message": {"model": s["model"], "usage": usage}}) + "\n")
    at = now - s["age_min"] * 60
    os.utime(transcript, (at, at))  # the dialog's age is the transcript's last change
    record = {"terminal": s["terminal"], "session_id": s["session_id"], "cwd": str(cwd),
              "config_dir": None if s["config"] == ".claude" else str(config), "transcript": str(transcript),
              "at": int(at)}
    (home / "data" / "resume").mkdir(parents=True, exist_ok=True)
    (home / "data" / "resume" / f"{s['terminal']}.json").write_text(json.dumps(record))
    terminals.append({"address": s["terminal"], "agent": "zsh", "state": "live", "where": s["where"]})
(home / "fixture.json").write_text(json.dumps({"home": str(user), "terminals": terminals}, indent=1))
