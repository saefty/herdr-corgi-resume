#!/usr/bin/env python3
"""Resume Claude Code sessions in Herdr terminals after a reboot. Standard library only.

As a Claude Code hook (no arguments, the hook's JSON on stdin; never prints, always exits 0):
  SessionStart  writes <app home>/data/resume/<HC_TERMINAL_ID>.json: which session runs in this terminal
  SessionEnd    deletes that file when its session id matches
For the mod (each prints one JSON object):
  status        the Claude config dirs and whether our hooks are in their settings.json
  setup         adds our SessionStart and SessionEnd hooks to every config dir's settings.json
  remove        takes exactly those entries out again
  candidates    records whose terminal is live but not running Claude, each with its resume command;
                with --cost also what resuming writes to the prompt cache (tokens, model, ttl, cost)
  dismiss T...  deletes the records of these terminals
<app home> is $HERDR_APP_HOME, else ~/.config/herdr-app.
RESUME_FIXTURE (demo and screenshots only): a JSON file {home, terminals} that stands in for the home folder and
for `hc ls --json`, so the dialog shows invented sessions instead of the real terminals and config dirs.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVENTS = ("SessionStart", "SessionEnd")
CLAUDE = {"claude", "claude-code"}  # what `hc ls` reports as `agent` while Claude Code runs
TERMINAL = re.compile(r"[A-Za-z0-9_.-]+")


def app_home():
    return Path(os.environ.get("HERDR_APP_HOME") or Path.home() / ".config" / "herdr-app")


def fixture():
    f = os.environ.get("RESUME_FIXTURE")
    return read_json(f, {}) if f else None


def user_home():
    return Path((fixture() or {}).get("home") or Path.home())


def records_dir():
    return app_home() / "data" / "resume"


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_atomic(path, text):
    """Write next to the target, then rename over it; keeps an existing file's permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- the hook ---------------------------------------------------------------------------------------------

def hook(data, env=os.environ):
    term, sid = env.get("HC_TERMINAL_ID"), data.get("session_id")
    if not term or not TERMINAL.fullmatch(term) or not isinstance(sid, str) or not sid:
        return
    # `claude -p` or an SDK app started inside the terminal (say, from Claude's own shell) inherits
    # HC_TERMINAL_ID too; only the interactive session counts.
    if env.get("CLAUDE_CODE_ENTRYPOINT", "cli") != "cli":
        return
    path = records_dir() / f"{term}.json"
    event = data.get("hook_event_name")
    if event == "SessionStart":
        config = env.get("CLAUDE_CONFIG_DIR")
        write_atomic(path, json.dumps({
            "terminal": term, "session_id": sid, "cwd": data.get("cwd") or os.getcwd(),
            "config_dir": os.path.abspath(os.path.expanduser(config)) if config else None,
            "transcript": data.get("transcript_path"), "at": int(time.time())}))
    elif event == "SessionEnd" and (read_json(path) or {}).get("session_id") == sid:
        path.unlink(missing_ok=True)


# --- hook setup in settings.json --------------------------------------------------------------------------

def hook_command():
    return f"python3 {shlex.quote(os.path.abspath(__file__))}"


def config_dirs(home=None):
    """~/.claude, plus every ~/.claude-* folder that holds a settings.json or projects/."""
    home = Path(home or user_home())
    dirs = [home / ".claude"] if (home / ".claude").is_dir() else []
    return dirs + sorted(p for p in home.glob(".claude-*")
                         if p.is_dir() and ((p / "settings.json").is_file() or (p / "projects").is_dir()))


def _ours(group, cmd):
    return any(isinstance(h, dict) and h.get("command") == cmd for h in group.get("hooks") or [])


def installed(settings, cmd):
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    return isinstance(hooks, dict) and all(
        any(isinstance(g, dict) and _ours(g, cmd) for g in hooks.get(ev) or []) for ev in EVENTS)


def add_hooks(settings, cmd):
    """Adds one matcher-less group per event holding our command, unless that event already has it."""
    hooks = settings.setdefault("hooks", {})
    for ev in EVENTS:
        if not any(isinstance(g, dict) and _ours(g, cmd) for g in hooks.get(ev) or []):
            hooks.setdefault(ev, []).append({"hooks": [{"type": "command", "command": cmd}]})
    return settings


def remove_hooks(settings, cmd):
    """Takes our command out; drops a group, event or `hooks` only when that left it empty."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for ev in EVENTS:
        groups = hooks.get(ev)
        if not isinstance(groups, list) or not any(isinstance(g, dict) and _ours(g, cmd) for g in groups):
            continue
        kept = []
        for g in groups:
            if isinstance(g, dict) and _ours(g, cmd):
                g = {**g, "hooks": [h for h in g["hooks"] if not (isinstance(h, dict) and h.get("command") == cmd)]}
                if not g["hooks"]:
                    continue
            kept.append(g)
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]
    if not hooks:
        del settings["hooks"]
    return settings


def dir_state(d, cmd):
    return {"dir": str(d), "label": d.name, "installed": installed(read_json(d / "settings.json", {}), cmd)}


def change(dirs, fn, cmd):
    out = []
    for d in dirs:
        f = d / "settings.json"
        try:
            text = f.read_text() if f.exists() else "{}"
            before = json.loads(text)
            if not isinstance(before, dict) or not isinstance(before.get("hooks", {}), dict):
                raise ValueError("settings.json has a shape this mod doesn't change")
            after = fn(json.loads(text), cmd)
            if after != before:
                bak = d / "settings.json.resume.bak"
                if f.exists() and not bak.exists():
                    shutil.copy2(f, bak)  # once: the file as it was before this mod first touched it
                write_atomic(f, json.dumps(after, indent=2, ensure_ascii=False) + "\n")
            out.append(dir_state(d, cmd))
        except (OSError, ValueError) as e:
            out.append({**dir_state(d, cmd), "error": str(e)})
    return out


# --- after a restart ----------------------------------------------------------------------------------------

def sh_quote(s):
    """One word for sh, bash, zsh and fish: single quotes, with ' and \\ escaped outside them
    (fish reads \\' and \\\\ as escapes even inside single quotes)."""
    return "'" + "".join({"'": "'\\''", "\\": "'\\\\'"}.get(c, c) for c in s) + "'"


def resume_command(rec):
    parts = [rec.get("cwd"), rec.get("session_id"), rec.get("config_dir") or ""]
    if not all(isinstance(p, str) for p in parts) or not parts[0] or not parts[1]:
        return None
    if any(ord(c) < 32 or c == "\x7f" for p in parts for c in p):
        return None  # `hc send` presses Enter: a line break would run half a command
    env = f"env CLAUDE_CONFIG_DIR={sh_quote(rec['config_dir'])} " if rec.get("config_dir") else ""
    return f"cd {sh_quote(rec['cwd'])}; {env}claude --resume {sh_quote(rec['session_id'])}"


# Input list price, $ per million tokens (Anthropic API, as of 2026-11). The longest prefix of the model id wins.
PRICES = {
    "claude-fable-5-2": 8, "claude-mythos-5-2": 8,
    "claude-fable-5-1": 10, "claude-fable-5": 10, "claude-mythos-5-1": 10,
    "claude-opus-5-5": 4, "claude-opus-5": 5,
    "claude-opus-4-8": 5, "claude-opus-4-7": 5, "claude-opus-4-6": 5, "claude-opus-4-5": 5,
    "claude-opus-4-1": 15, "claude-opus-4": 15,
    "claude-sonnet-5-5": 2, "claude-sonnet-5": 2,
    "claude-sonnet-4-6": 3, "claude-sonnet-4-5": 3, "claude-sonnet-4": 3,
    "claude-haiku-5-5": 0.10, "claude-haiku-4-5": 1,
}
CACHE_WRITE = {"5m": 1.25, "1h": 2}  # a cache write costs the input price times this


def input_price(model, tokens):
    """$ per million input tokens for this model id (may carry a date suffix or "[1m]"), or None when unknown."""
    hits = [p for p in PRICES if model == p or model.startswith((p + "-", p + "["))]
    if not hits:
        return None
    p = max(hits, key=len)
    return 0.50 if p == "claude-haiku-5-5" and tokens > 100_000 else PRICES[p]


def last_usage(path, chunk=1 << 16):
    """(model, usage) of the transcript's last assistant line that has a usage, read backwards in chunks
    (a transcript can be 50+ MB), or None."""
    try:
        with open(path, "rb") as f:
            pos, tail = f.seek(0, os.SEEK_END), b""
            while pos > 0:
                step = min(chunk, pos)
                pos -= step
                f.seek(pos)
                lines = (f.read(step) + tail).split(b"\n")
                tail = lines.pop(0) if pos > 0 else b""  # may start mid-line: finish it with the next chunk
                for line in reversed(lines):
                    if b'"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    m = d.get("message") if isinstance(d, dict) and d.get("type") == "assistant" else None
                    if (isinstance(m, dict) and isinstance(m.get("usage"), dict) and not d.get("isSidechain")
                            and m.get("model") != "<synthetic>"):  # <synthetic>: an error Claude Code wrote itself
                        return m.get("model"), m["usage"]
    except OSError:
        pass
    return None


def cache_estimate(transcript):
    """What resuming writes to the prompt cache: the last turn's whole context (the cache expired with the reboot).
    {tokens, model, ttl, cost} with cost in $ at API list price, None when the model is unknown."""
    hit = last_usage(transcript) if transcript else None
    if not hit:
        return None
    model, u = hit
    n = lambda v: v if isinstance(v, int) else 0
    tokens = sum(n(u.get(k)) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    ttl = "1h" if n((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens")) > 0 else "5m"
    price = input_price(model, tokens) if isinstance(model, str) else None
    cost = round(tokens * price * CACHE_WRITE[ttl] / 1e6, 4) if price is not None else None
    return {"tokens": tokens, "model": model, "ttl": ttl, "cost": cost}


def live_terminals():
    fx = fixture()
    if fx is not None:
        return fx.get("terminals") or []
    r = subprocess.run(["hc", "ls", "--json"], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout).get("terminals") or []


def candidates(terminals, cost=False):
    """`terminals`: `hc ls --json`'s list, or None when it failed (then nothing is pruned).
    `cost`: add each row's `cache` estimate (reads the transcripts' tails; the dialog asks, the watcher doesn't)."""
    by = {t.get("address"): t for t in terminals or []}
    out = []
    for f in sorted(records_dir().glob("*.json")):
        rec = read_json(f)
        if not isinstance(rec, dict):
            continue
        t = by.get(rec.get("terminal"))
        if t is None:
            # ponytail: the terminal is gone (closed, never restored): forget it, so a later terminal that gets
            # the same address isn't offered someone else's session. Skipped when the core lists nothing.
            if terminals:
                f.unlink(missing_ok=True)
            continue
        if t.get("state") != "live" or t.get("agent") in CLAUDE:
            continue
        transcript = rec.get("transcript")
        if transcript and not os.path.exists(transcript):
            continue  # never got a message: nothing to resume
        cmd = resume_command(rec)
        if not cmd:
            continue
        cfg = rec.get("config_dir")
        out.append({**rec, "command": cmd, "where": t.get("where") or rec["terminal"],
                    "label": os.path.basename(cfg.rstrip("/")) if cfg else ".claude",
                    "last": int(os.path.getmtime(transcript)) if transcript else rec.get("at"),
                    **({"cache": cache_estimate(transcript)} if cost else {})})
    return out


def dismiss(terms):
    for t in terms:
        if TERMINAL.fullmatch(t):
            (records_dir() / f"{t}.json").unlink(missing_ok=True)


def main(argv):
    if len(argv) < 2:
        try:
            hook(json.load(sys.stdin))
        except Exception:
            pass  # a hook must never get in Claude's way
        return 0
    cmd, arg = hook_command(), argv[1]
    if arg == "status":
        out = {"dirs": [dir_state(d, cmd) for d in config_dirs()], "home": str(user_home())}
    elif arg in ("setup", "remove"):
        out = {"dirs": change(config_dirs(), add_hooks if arg == "setup" else remove_hooks, cmd)}
    elif arg == "candidates":
        try:
            terminals = live_terminals()
        except (OSError, ValueError, subprocess.SubprocessError):
            terminals = None
        out = {"candidates": candidates(terminals, "--cost" in argv[2:]), "core": terminals is not None}
    elif arg == "dismiss":
        dismiss(argv[2:])
        out = {"ok": True}
    else:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
