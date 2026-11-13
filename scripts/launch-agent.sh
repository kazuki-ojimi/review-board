#!/usr/bin/env bash
# Register review-board as a macOS LaunchAgent so the server starts at login.
# Run with --help for usage.
set -euo pipefail

readonly LABEL="local.review-board"
readonly PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
readonly LOG="$HOME/Library/Logs/review-board.log"
readonly PORT=8765
readonly COMMANDS="install|uninstall|status|--help"
# launchctl exits with this code when the service is not loaded
# (check: launchctl print "gui/$(id -u)/no.such.label"; echo $?).
readonly NOT_LOADED=113

usage() {
  echo "usage: $0 $COMMANDS" >&2
  exit 1
}

cmd_help() {
  cat <<EOF
Register review-board as a macOS LaunchAgent so the server starts at login.

Usage: $0 $COMMANDS
  install    write $PLIST and start
             the server on port $PORT (re-running replaces the registration)
  uninstall  stop the agent and remove the plist
  status     show whether the agent is loaded, with its state, pid and last exit code

Requires uv and gh on PATH when running install. The absolute path of uv and
the directories of uv and gh are written into the plist because launchd does
not use your shell's PATH.
Logs: $LOG
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

domain() {
  printf 'gui/%s' "$(id -u)"
}

service() {
  printf '%s/%s' "$(domain)" "$LABEL"
}

# Prints "yes" or "no"; exits on any launchctl failure other than "not found".
is_loaded() {
  local err rc=0
  err=$(launchctl print "$(service)" 2>&1 >/dev/null) || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo yes
  elif [ "$rc" -eq "$NOT_LOADED" ]; then
    echo no
  else
    die "launchctl print failed (exit $rc): $err"
  fi
}

require_path() {
  local name="$1" path
  path=$(command -v -- "$name") || die "$name not found on PATH"
  case "$path" in
    /*) printf '%s' "$path" ;;
    *) die "$name resolved to a non-absolute path: $path" ;;
  esac
}

# is_loaded's output is assigned first so that its die stops this script.
stop_if_loaded() {
  local loaded
  loaded=$(is_loaded)
  if [ "$loaded" = yes ]; then
    launchctl bootout "$(service)"
  fi
}

cmd_install() {
  [ "$(uname -s)" = Darwin ] || die "launchd is only available on macOS"
  local repo_dir uv gh tmp
  repo_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
  [ -f "$repo_dir/src/main.py" ] || die "src/main.py not found under $repo_dir"
  uv=$(require_path uv)
  gh=$(require_path gh)

  mkdir -p -- "$(dirname -- "$PLIST")" "$(dirname -- "$LOG")"
  tmp=$(mktemp -- "${TMPDIR:-/tmp}/review-board-plist.XXXXXX")
  trap 'rm -f -- "$tmp"' EXIT

  # plutil escapes values, so paths with XML special characters stay valid.
  plutil -create xml1 -- "$tmp"
  plutil -insert Label -string "$LABEL" -- "$tmp"
  plutil -insert ProgramArguments -array -- "$tmp"
  local arg
  for arg in "$uv" run --project "$repo_dir" python "$repo_dir/src/main.py" --port "$PORT" --no-open; do
    plutil -insert ProgramArguments -string "$arg" -append -- "$tmp"
  done
  plutil -insert WorkingDirectory -string "$repo_dir" -- "$tmp"
  plutil -insert EnvironmentVariables -dictionary -- "$tmp"
  plutil -insert EnvironmentVariables.PATH \
    -string "$(dirname -- "$uv"):$(dirname -- "$gh"):/usr/bin:/bin:/usr/sbin:/sbin" -- "$tmp"
  plutil -insert RunAtLoad -bool true -- "$tmp"
  plutil -insert KeepAlive -bool true -- "$tmp"
  plutil -insert StandardOutPath -string "$LOG" -- "$tmp"
  plutil -insert StandardErrorPath -string "$LOG" -- "$tmp"
  plutil -lint -- "$tmp" >/dev/null

  stop_if_loaded
  mv -f -- "$tmp" "$PLIST"
  trap - EXIT
  launchctl bootstrap "$(domain)" "$PLIST"
  echo "installed: $PLIST"
  echo "logs: $LOG"
  echo "open http://127.0.0.1:$PORT/"
}

cmd_uninstall() {
  stop_if_loaded
  if [ -f "$PLIST" ]; then
    rm -f -- "$PLIST"
    echo "removed: $PLIST"
  else
    echo "not installed: $PLIST does not exist"
  fi
}

cmd_status() {
  local loaded out
  loaded=$(is_loaded)
  if [ "$loaded" = no ]; then
    echo "not loaded"
    return
  fi
  out=$(launchctl print "$(service)") || die "launchctl print failed"
  printf '%s\n' "$out" | grep -E -- '^[[:space:]]*(state|pid|last exit code) =' || echo "loaded"
}

[ "$#" -eq 1 ] || usage
case "$1" in
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  status) cmd_status ;;
  -h | --help) cmd_help ;;
  *) usage ;;
esac
