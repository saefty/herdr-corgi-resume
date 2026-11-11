#!/usr/bin/env python3
"""Tests for hook.py: python3 test_hook.py. Runs in a temp HOME and HERDR_APP_HOME; never touches yours."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "hook.py"
TMP = Path(tempfile.mkdtemp(prefix="resume-test-"))
ENV = {**os.environ, "HOME": str(TMP / "home"), "HERDR_APP_HOME": str(TMP / "app"), "HC_TERMINAL_ID": "t7",
       "CLAUDE_CODE_ENTRYPOINT": "cli"}
ENV.pop("CLAUDE_CONFIG_DIR", None)
os.environ.update({"HOME": ENV["HOME"], "HERDR_APP_HOME": ENV["HERDR_APP_HOME"]})
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True  # no __pycache__ in the mod folder (Herdr reloads on every file there)
import hook as H  # noqa: E402

RECORDS = TMP / "app" / "data" / "resume"


def run_hook(event, sid, env=None, **extra):
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(
        {"hook_event_name": event, "session_id": sid, "cwd": "/work/repo", **extra}),
        env=env or ENV, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == "", r
    return json.loads((RECORDS / "t7.json").read_text()) if (RECORDS / "t7.json").exists() else None


def test_hook():
    rec = run_hook("SessionStart", "a1", source="startup")
    assert rec["terminal"] == "t7" and rec["session_id"] == "a1" and rec["cwd"] == "/work/repo"
    assert rec["config_dir"] is None and isinstance(rec["at"], int)
    rec = run_hook("SessionStart", "b2", source="clear")  # /clear: a new session id overwrites
    assert rec["session_id"] == "b2"
    assert run_hook("SessionEnd", "a1", reason="other")["session_id"] == "b2"  # not ours: kept
    assert run_hook("SessionEnd", "b2", reason="other") is None  # ours: deleted
    assert run_hook("SessionEnd", "b2", reason="other") is None  # nothing there: still quiet
    rec = run_hook("SessionStart", "c3", env={**ENV, "CLAUDE_CONFIG_DIR": str(TMP / "home" / ".claude-x")})
    assert rec["config_dir"] == str(TMP / "home" / ".claude-x")
    assert run_hook("SessionStart", "d4", env={**ENV, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})["session_id"] == "c3"
    (RECORDS / "t7.json").unlink()
    no_terminal = {k: v for k, v in ENV.items() if k != "HC_TERMINAL_ID"}
    assert run_hook("SessionStart", "e5", env=no_terminal) is None and not any(RECORDS.iterdir())
    r = subprocess.run([sys.executable, str(HOOK)], input="not json", env=ENV, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_settings():
    home = TMP / "home"
    for d in (".claude", ".claude-x", ".claude-empty", ".claude-p"):
        (home / d).mkdir(parents=True, exist_ok=True)
    (home / ".claude-p" / "projects").mkdir()
    theirs = {"model": "x", "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "mine.sh"}]}],
                                      "Stop": [{"hooks": [{"type": "command", "command": "stop.sh"}]}]}}
    (home / ".claude-x" / "settings.json").write_text(json.dumps(theirs))
    dirs = H.config_dirs(home)
    assert [d.name for d in dirs] == [".claude", ".claude-p", ".claude-x"], dirs  # .claude-empty has neither
    cmd = H.hook_command()

    out = H.change(dirs, H.add_hooks, cmd)
    assert all(d["installed"] and "error" not in d for d in out), out
    x = json.loads((home / ".claude-x" / "settings.json").read_text())
    assert x["model"] == "x" and x["hooks"]["Stop"] == theirs["hooks"]["Stop"]
    assert x["hooks"]["SessionStart"][0] == theirs["hooks"]["SessionStart"][0] and len(x["hooks"]["SessionStart"]) == 2
    assert x["hooks"]["SessionEnd"] == [{"hooks": [{"type": "command", "command": cmd}]}]
    assert json.loads((home / ".claude-x" / "settings.json.resume.bak").read_text()) == theirs
    assert not (home / ".claude" / "settings.json.resume.bak").exists()  # there was no file to back up

    snapshot = {d: (d / "settings.json").read_text() for d in dirs}
    H.change(dirs, H.add_hooks, cmd)  # idempotent
    assert {d: (d / "settings.json").read_text() for d in dirs} == snapshot

    H.change(dirs, H.remove_hooks, cmd)
    assert json.loads((home / ".claude-x" / "settings.json").read_text()) == theirs
    assert json.loads((home / ".claude" / "settings.json").read_text()) == {}
    assert not any(d["installed"] for d in H.change(dirs, H.remove_hooks, cmd))

    (home / ".claude-x" / "settings.json").write_text("{broken")
    bad = [d for d in H.change(dirs, H.add_hooks, cmd) if d["label"] == ".claude-x"][0]
    assert "error" in bad and (home / ".claude-x" / "settings.json").read_text() == "{broken"


def test_quoting():
    weird = TMP / "it's a dir \\ with $HOME"
    weird.mkdir()
    cfg = TMP / "cfg 'q'"
    bin_ = TMP / "bin"
    bin_.mkdir()
    (bin_ / "claude").write_text('#!/bin/sh\nprintf "%s|%s|%s" "$PWD" "${CLAUDE_CONFIG_DIR-unset}" "$*"\n')
    (bin_ / "claude").chmod(0o755)
    env = {**ENV, "PATH": f"{bin_}:{os.environ['PATH']}"}
    env.pop("CLAUDE_CONFIG_DIR", None)
    with_cfg = H.resume_command({"cwd": str(weird), "session_id": "s-1", "config_dir": str(cfg)})
    without = H.resume_command({"cwd": str(weird), "session_id": "s-1", "config_dir": None})
    assert "CLAUDE_CONFIG_DIR" not in without
    assert H.resume_command({"cwd": "/a\nb", "session_id": "s", "config_dir": None}) is None
    shells = [s for s in ("sh", "bash", "zsh", "fish") if shutil.which(s)]
    for sh in shells:
        for cmd, want_cfg in ((with_cfg, str(cfg)), (without, "unset")):
            r = subprocess.run([sh, "-c", cmd], env=env, capture_output=True, text=True)
            pwd, got_cfg, args = r.stdout.split("|")
            assert os.path.realpath(pwd) == os.path.realpath(weird), (sh, cmd, r)
            assert got_cfg == want_cfg and args == "--resume s-1", (sh, cmd, r)
    print(f"  quoting checked in: {', '.join(shells)}")


def test_candidates():
    RECORDS.mkdir(parents=True, exist_ok=True)
    for f in RECORDS.iterdir():
        f.unlink()
    transcript = TMP / "t.jsonl"
    transcript.write_text("{}\n")
    rec = lambda term, sid, **kw: (RECORDS / f"{term}.json").write_text(json.dumps(
        {"terminal": term, "session_id": sid, "cwd": "/w", "config_dir": None, "transcript": str(transcript), "at": 1, **kw}))
    rec("t1", "s1")                                  # shell now: offered
    rec("t2", "s2")                                  # Claude still runs: not offered
    rec("t3", "s3", transcript=str(TMP / "none"))    # never had a message: not offered
    rec("t4", "s4")                                  # terminal gone: forgotten
    rec("t5", "s5", config_dir="/h/.claude-x")
    ls = [{"address": "t1", "agent": "fish", "state": "live", "where": "repo / 1"},
          {"address": "t2", "agent": "claude-code", "state": "live"},
          {"address": "t3", "agent": "zsh", "state": "live"},
          {"address": "t5", "agent": "bash", "state": "live"}]
    assert H.candidates(None) == [] and (RECORDS / "t4.json").exists()  # core unreachable: nothing pruned
    got = {c["terminal"]: c for c in H.candidates(ls)}
    assert sorted(got) == ["t1", "t5"], got
    assert got["t1"]["where"] == "repo / 1" and got["t1"]["label"] == ".claude" and got["t5"]["label"] == ".claude-x"
    assert got["t5"]["command"] == "cd '/w'; env CLAUDE_CONFIG_DIR='/h/.claude-x' claude --resume 's5'"
    assert not (RECORDS / "t4.json").exists()
    H.dismiss(["t1", "../t5"])
    assert not (RECORDS / "t1.json").exists() and (RECORDS / "t5.json").exists()


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn()
                print(f"ok {name}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("all passed")
