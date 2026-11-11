#!/bin/sh
# Builds the demo: a separate Herdr home (demo/home) with three invented Claude sessions to resume, seeded from
# demo/fixtures/sessions.json by demo/seed.py, and their hooks set up in an invented user home (user/ inside demo/home, with
# ~/.claude and ~/.claude-oss). RESUME_FIXTURE (fixture.json in demo/home) stands in for that home and for `hc ls`, so
# the dialog never lists your real terminals or config dirs. Then:
#   HERDR_APP_HOME="$PWD/demo/home" RESUME_FIXTURE="<demo home>/fixture.json" /path/to/Herdr   (a second window)
#   HERDR_APP_HOME="$PWD/demo/home" herdr-app do layout.switch --name demo   (the watcher alone: the notification)
#   HERDR_APP_HOME="$PWD/demo/home" herdr-app do mod.open --name resume      (the dialog)
# `sh demo/setup.sh --no-hooks` leaves the hooks out (the "not set up" state). Resume in the demo fails: the
# terminals t1..t3 are invented. Run from the repo root. Rebuilding is safe: demo/home is recreated.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
home="$here/home"
rm -rf "$home"
mkdir -p "$home/mods" "$home/layouts"
cp -R "$root/mods/resume" "$home/mods/"
cp "$here/layouts/demo.json" "$home/layouts/"
printf '{ "appearance": "dark", "telemetry": false }\n' > "$home/settings.json"
printf '{ "frame.welcomed": "skipped" }\n' > "$home/shared.json"   # no first-run welcome over the demo
# nor the "Updated to ..." toast next to ours
printf '{ "last_run": "%s", "presented": [] }\n' "$(herdr-app --version | awk '{print $NF}')" > "$home/update-notes.json"
python3 "$here/seed.py" "$home"
if [ "${1-}" != "--no-hooks" ]; then
  HERDR_APP_HOME="$home" RESUME_FIXTURE="$home/fixture.json" python3 "$home/mods/resume/hook.py" setup > /dev/null
fi
echo "demo ready: HERDR_APP_HOME=$home RESUME_FIXTURE=$home/fixture.json"
