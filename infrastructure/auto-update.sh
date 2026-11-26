#!/usr/bin/env bash
# Keeps this device in step with GitHub: when the branch has new commits (for example an
# adapter the AI repair service just pushed), pull them, check them and redeploy.
#
#   make auto-update                 # runs until stopped with Ctrl+C
#   infrastructure/auto-update.sh --once
#
# It only ever fast-forwards. It never discards local work, never merges, never pushes:
# if this checkout has commits or changes that stand in the way, it says so and waits.
#
# Settings (environment):
#   AUTO_UPDATE_BRANCH      branch to follow                    (default: main)
#   AUTO_UPDATE_INTERVAL_S  seconds between looks at GitHub     (default: 120)
#   AUTO_UPDATE_CHECK       command that must succeed before deploying; empty = none
#                           (default: make test-contracts test-adapters)
#   AUTO_UPDATE_DEPLOY      command that puts the new code into service
#                           (default: make up)
set -u

cd "$(dirname "$0")/.." || exit 1
BRANCH=${AUTO_UPDATE_BRANCH:-main}
INTERVAL=${AUTO_UPDATE_INTERVAL_S:-120}
CHECK=${AUTO_UPDATE_CHECK-make test-contracts test-adapters}
DEPLOY=${AUTO_UPDATE_DEPLOY-make up}

PENDING="$(git rev-parse --git-dir)/courier-auto-update-pending"
LOG="$(git rev-parse --git-dir)/courier-auto-update-check.log"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

update_once() {
    if [ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
        say "this checkout is not on '$BRANCH'; nothing is updated"
        return 1
    fi
    if ! git fetch --quiet origin "$BRANCH" 2>/dev/null; then
        say "GitHub could not be reached; trying again later"
        return 1
    fi
    local here there
    here=$(git rev-parse HEAD)
    there=$(git rev-parse "origin/$BRANCH")
    if [ "$here" != "$there" ] && ! git merge-base --is-ancestor "$there" "$here"; then
        # (When this checkout is merely ahead, with commits not pushed yet, there is nothing to pull.)
        if ! git merge-base --is-ancestor "$here" "$there"; then
            say "this checkout and GitHub have gone different ways; update it by hand (git pull --rebase)"
            return 1
        fi
        say "new on GitHub:"
        git log --format='    %h %an: %s' "$here..$there"
        if ! git merge --ff-only --quiet "$there" 2>/dev/null; then
            say "could not update: local changes stand in the way of the new commits (see: git status)"
            return 1
        fi
        say "updated to $(git rev-parse --short HEAD)"
        git rev-parse HEAD >"$PENDING"
    fi
    # Pulled but not in service yet: this pull, or an earlier one whose deployment failed.
    [ -f "$PENDING" ] || return 0
    if [ "$(cat "$PENDING")" = "refused:$(git rev-parse HEAD)" ]; then
        return 1   # its checks failed; wait for a newer commit instead of running them again
    fi
    if [ -n "$CHECK" ]; then
        say "checking: $CHECK"
        if ! bash -c "$CHECK" >"$LOG" 2>&1; then
            say "CHECKS FAILED. The new code is in this folder but was NOT deployed; what is running"
            say "stays as it was. Details: $LOG"
            echo "refused:$(git rev-parse HEAD)" >"$PENDING"
            return 1
        fi
    fi
    if [ -n "$DEPLOY" ]; then
        say "deploying: $DEPLOY"
        if ! bash -c "$DEPLOY"; then
            say "DEPLOYING FAILED; it is tried again at the next look"
            return 1
        fi
    fi
    rm -f "$PENDING"
    say "done: this device runs $(git rev-parse --short HEAD)"
}

if [ "${1:-}" = "--once" ]; then
    update_once
    exit $?
fi
say "following '$BRANCH' on GitHub every ${INTERVAL}s. Stop with Ctrl+C."
while true; do
    update_once
    sleep "$INTERVAL"
done
