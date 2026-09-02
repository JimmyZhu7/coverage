#!/bin/bash
# Install Coverage's four local background jobs as launchd agents.
#
# These are the local stand-ins for four of render.yaml's services — see
# docs/see-it-locally.md, "The background jobs", for which stands in for
# which. Nothing on the Opportunities feed or the CRM needs them to render;
# what they buy is the app being CURRENT: mail turning into touches, queued
# work actually getting claimed, listings not going stale.
#
# The templates beside this script hold `__REPO__` where an absolute path has
# to go, because a plist cannot expand a variable — launchd reads it as
# literal text. This script is the substitution step, which is also why the
# plists are not simply symlinked into ~/Library/LaunchAgents.
#
# Idempotent: `bootout` before `bootstrap`, both tolerant of the job not being
# loaded, so re-running after editing a template is the normal way to apply a
# change. Safe to run on a fresh clone and safe to run again an hour later.
#
#   ./scripts/launchd/install.sh              # all four
#   ./scripts/launchd/install.sh gmailpoll    # just one (name after the dot)
#   ./scripts/launchd/install.sh --uninstall  # bootout + remove all four
#
# Check afterwards with `launchctl list | grep coverage`, and read the logs at
# /tmp/coverage-*.log (refresh writes to ~/Library/Logs/coverage-refresh.log
# instead — it is a shell script with its own logging).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

ALL_JOBS=(gmailpoll gmailbackfill autopilot refresh)

uninstall=0
if [ "${1-}" = "--uninstall" ]; then
  uninstall=1
  shift
fi

if [ "$#" -gt 0 ]; then
  JOBS=("$@")
else
  JOBS=("${ALL_JOBS[@]}")
fi

mkdir -p "$AGENTS"

for job in "${JOBS[@]}"; do
  label="com.coverage.$job"
  template="$HERE/$label.plist"
  target="$AGENTS/$label.plist"

  if [ ! -f "$template" ]; then
    echo "no template for '$job' — expected $template" >&2
    echo "known jobs: ${ALL_JOBS[*]}" >&2
    exit 1
  fi

  # `bootout` on a job that was never loaded exits non-zero; that is the
  # normal first-install case, not a failure, hence the guard. Always done
  # before writing the file so a running job is never left pointing at a
  # plist that has since changed underneath it.
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true

  if [ "$uninstall" = "1" ]; then
    rm -f "$target"
    echo "removed  $label"
    continue
  fi

  # `python -` rather than sed: a repo path can legitimately contain a slash
  # or an ampersand, both of which sed's replacement text reads as syntax.
  REPO="$REPO" python3 - "$template" "$target" <<'PY'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as fh:
    body = fh.read()
with open(dst, "w", encoding="utf-8") as fh:
    fh.write(body.replace("__REPO__", os.environ["REPO"]))
PY

  # Fail loudly here rather than at bootstrap: a malformed plist bootstraps
  # with an opaque "Input/output error", and the useful message is this one.
  plutil -lint "$target" > /dev/null

  launchctl bootstrap "$DOMAIN" "$target"
  echo "loaded   $label"
done

if [ "$uninstall" = "0" ]; then
  echo
  echo "repo: $REPO"
  echo "check: launchctl list | grep coverage"
fi
