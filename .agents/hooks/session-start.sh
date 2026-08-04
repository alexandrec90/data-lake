#!/bin/bash
# SessionStart hook — provisions the sandbox so `scripts/lint-all.py` and the
# pytest suites are runnable from turn one. Without this, a Claude Code on the
# web session boots without the pinned toolchain (no ruff/mypy/pytest, no
# frontend node_modules), so the full lint suite can only run in CI — surfacing
# lint drift one slow gate round at a time instead of in a single local pass.
#
# Synchronous + idempotent. Remote-only: local dev machines already have the
# venv + node_modules, and container state is cached after this completes, so
# re-runs are cheap (venv reused, pip/npm no-op when satisfied).
set -uo pipefail

# --- LOCAL sessions only: keep this branch current with origin/master ---------
# Parallel worktrees drift from master the longer their branches live; on a
# local session this rebases the checked-out branch onto origin/master so it
# begins current, with no manual command. scripts/hooks/session-sync.py refuses
# a dirty tree, disables autostash, and re-signs (--gpg-sign) so replayed commits
# stay Verified.
#
# This MUST NOT run in cloud/remote (Claude Code on the web) sessions: there the
# platform owns the branch lifecycle (branch, commit, signing, PR), and rebasing
# from inside rewrites commit SHAs, diverges the branch from the remote the
# mobile app tracks, and strips signatures. So it lives inside the local branch
# of the remote guard, and the cloud-provisioning steps below never reach it.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  (
    cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
    branch="$(git branch --show-current 2>/dev/null)"
    # Resolve the repo's default branch (main/master/...) rather than assuming one:
    # origin/HEAD (set at clone), else probe origin/main then origin/master.
    default_branch="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)"
    default_branch="${default_branch#origin/}"
    if [ -z "$default_branch" ]; then
      if git rev-parse --verify --quiet refs/remotes/origin/main >/dev/null 2>&1; then
        default_branch="main"
      else
        default_branch="master"
      fi
    fi
    # Nothing to do on the default branch itself, or with detached HEAD.
    if [ -n "$branch" ] && [ "$branch" != "$default_branch" ]; then
      echo "[session-start] Syncing '$branch' onto origin/$default_branch (no-op if tree is dirty)..."
      if ! python3 scripts/hooks/session-sync.py; then
        # On conflicts the rebase is left in progress. Auto-abort so the session
        # starts in a known-clean state; resolve conflicts manually instead.
        rebase_merge="$(git rev-parse --git-path rebase-merge 2>/dev/null)"
        rebase_apply="$(git rev-parse --git-path rebase-apply 2>/dev/null)"
        if [ -d "$rebase_merge" ] || [ -d "$rebase_apply" ]; then
          git rebase --abort 2>/dev/null
          echo "[session-start] origin/$default_branch had conflicting changes — auto-sync aborted, branch left untouched. Sync manually to resolve."
        else
          echo "[session-start] Branch sync skipped (dirty tree or offline) — sync manually when ready."
        fi
      fi
    fi
  ) || true
  # Local machines already have the venv + node_modules; nothing to provision.
  exit 0
fi

# --- REMOTE (Claude Code on the web) sandbox provisioning only below ----------
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

# Install the Python toolchain into a project venv. A venv (rather than the
# system interpreter) avoids the Debian-managed-package conflict — pip refuses
# to upgrade distro-owned packages like PyYAML — and `scripts/lint-all.py`
# already prepends ./.venv/bin to PATH, so ruff/mypy/pytest resolve without any
# extra wiring. Match CI: runtime locks + dev linters together.
#
# uv does the heavy install — an order of magnitude faster than pip on a cold
# sandbox — bootstrapped by one small pip install.
#
# THIS FILE IS VENDORED BYTE-IDENTICAL INTO EVERY PROJECT, so it must not assume
# one dependency model. It previously read `requirements-dev.txt` unconditionally
# and expanded `${uv_version:?...}`; `:?` on an empty value *exits* a
# non-interactive shell, and `||` cannot catch a parameter-expansion failure. So
# in any project without pip-tools locks — every generated project, and
# ibkr_trader — provisioning died right here, before the PATH export below, and
# left an empty venv with no tooling. Only remote sandboxes were affected (local
# machines return above), which is why it went unnoticed.
#
# Detection, not configuration: the lockfile on disk is authoritative and cannot
# drift the way a manifest field can. `[python] install_command` in
# .devkit.toml overrides for a project that fits none of these shapes.
echo "[session-start] Installing Python toolchain into .venv (runtime + dev linters)..."
[ -d .venv ] || python3 -m venv .venv

# Bootstrap uv. When a pip-tools dev lock pins it, honour that pin so the
# installer version stays single-sourced; otherwise take the latest. Note the
# `:+` (substitute if set) rather than `:?` (die if unset) — that difference is
# the bug described above.
uv_pin="$(sed -nE 's/^uv==([^ ;]+).*/\1/p' requirements-dev.txt 2>/dev/null | head -n 1)"
./.venv/bin/python -m pip install --quiet --disable-pip-version-check \
  "uv${uv_pin:+==${uv_pin}}" \
  || echo "[session-start] WARN: uv bootstrap failed — dependency install may fail"
uv_run="./.venv/bin/python -m uv"

install_command="$(python3 scripts/hooks/harness_config.py python.install_command 2>/dev/null)"
if [ -n "$install_command" ]; then
  echo "[session-start] install: .devkit.toml install_command"
  sh -c "$install_command" \
    || echo "[session-start] WARN: install_command failed — ruff/mypy/pytest may be unavailable"
elif [ -f uv.lock ]; then
  # uv-native project: the lock pins everything, and uv manages ./.venv itself.
  # --all-extras --all-groups because the lint/test toolchain lives in extras or
  # dependency-groups depending on the project, and we need it either way.
  echo "[session-start] install: uv sync (uv.lock)"
  $uv_run sync --all-extras --all-groups \
    || echo "[session-start] WARN: uv sync failed — ruff/mypy/pytest may be unavailable"
elif [ -f requirements-dev.txt ]; then
  # pip-tools model: fully-pinned compiled locks, runtime + dev together.
  echo "[session-start] install: uv pip install (requirements locks)"
  $uv_run pip install --quiet --python ./.venv/bin/python \
    -r requirements.txt -r requirements-dev.txt \
    || echo "[session-start] WARN: uv install failed — ruff/mypy/pytest may be unavailable"
elif [ -f pyproject.toml ]; then
  # Unlocked pyproject: resolves fresh, so builds are not reproducible — but a
  # working toolchain beats no toolchain. Commit a lock to get out of this branch.
  echo "[session-start] install: uv pip install -e '.[dev]' (unlocked pyproject)"
  $uv_run pip install --quiet --python ./.venv/bin/python -e ".[dev]" \
    || echo "[session-start] WARN: editable install failed — ruff/mypy/pytest may be unavailable"
else
  echo "[session-start] WARN: no uv.lock, requirements-dev.txt or pyproject.toml — skipping Python install"
fi

# Persist the venv on PATH for every turn, so bare ruff/pytest/python resolve to
# it (not only under lint-all.py's internal PATH shim).
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"${CLAUDE_PROJECT_DIR:-.}/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

# Frontend toolchain — only for projects that have one. This block used to run
# unconditionally against a hardcoded `frontend/`, so a backend-only project spent
# the time on a guaranteed-failing `npm install` and then ran lockfile-restore
# logic against a path that does not exist. Both come from the manifest now.
frontend_enabled="$(python3 scripts/hooks/harness_config.py frontend.enabled 2>/dev/null)"
frontend_dir="$(python3 scripts/hooks/harness_config.py frontend.dir 2>/dev/null)"
if [ "$frontend_enabled" = "true" ] && [ -d "${frontend_dir:-frontend}" ]; then
  frontend_dir="${frontend_dir:-frontend}"
  echo "[session-start] Installing frontend toolchain (eslint/stylelint/tsc)..."
  # npm install (not ci) so a warm cached container reuses node_modules. The
  # container's npm may differ from the lockfile author's and rewrite lockfile
  # metadata on install; that churn trips the stop hook's dirty-tree check on
  # otherwise read-only sessions, so restore the lockfile if it was clean before.
  LOCKFILE="$frontend_dir/package-lock.json"
  lockfile_was_clean=false
  git diff --quiet -- "$LOCKFILE" 2>/dev/null && lockfile_was_clean=true
  npm install --prefix "$frontend_dir" --no-audit --no-fund \
    || echo "[session-start] WARN: npm install failed — frontend linters may be unavailable"
  if $lockfile_was_clean && ! git diff --quiet -- "$LOCKFILE" 2>/dev/null; then
    git checkout -- "$LOCKFILE" \
      && echo "[session-start] Restored $LOCKFILE (npm install metadata churn)"
  fi
else
  echo "[session-start] No frontend tier in .devkit.toml — skipping npm install"
fi

# Wire the pre-commit gate into .git/hooks. `.git/hooks/` is not committed, so a fresh
# clone — and every fresh sandbox — has the config file but none of the hooks it
# describes, and the gate silently does not exist. `pre-commit install` is idempotent and
# runs in well under a second.
#
# Detection, not configuration, like everything else here: the config on disk is the
# signal. Projects without one skip this entirely. Installing the hook does NOT run it —
# nothing is checked until a commit is made.
if [ -f .pre-commit-config.yaml ]; then
  global_hooks_path="$(git config --global --get core.hooksPath 2>/dev/null)"
  if [ -n "$global_hooks_path" ] && [ -f "$global_hooks_path/devkit_git_policy.py" ]; then
    # The global dispatcher invokes `pre-commit run` itself after its branch policy
    # passes. Installing here would target core.hooksPath and ask pre-commit to
    # overwrite that dispatcher, disabling the policy for every repository.
    echo "[session-start] Using the global Devkit dispatcher for pre-commit."
  else
    if [ -x ./.venv/bin/pre-commit ]; then
      precommit="./.venv/bin/pre-commit"
    elif command -v pre-commit >/dev/null 2>&1; then
      precommit="pre-commit"
    else
      precommit=""
    fi
    if [ -n "$precommit" ]; then
      echo "[session-start] Installing the pre-commit git hook..."
      "$precommit" install --install-hooks >/dev/null 2>&1 \
        || echo "[session-start] WARN: pre-commit install failed — commits will not be gated"
    else
      echo "[session-start] pre-commit not installed — skipping git hook wiring"
    fi
  fi
fi

# External lint binaries lint-all.py shells out to, installed to a PATH dir so
# `shutil.which(...)` finds them. Best-effort: the runner skips a missing tool
# cleanly and CI installs them regardless, but having them here keeps a local
# `lint-all.py` run faithful to the gate. NB positional args:
# download-actionlint.bash takes [[VERSION] DIR], NOT a -b flag.
BIN_DIR=/usr/local/bin
[ -w "$BIN_DIR" ] || BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
if ! command -v actionlint >/dev/null 2>&1; then
  echo "[session-start] Installing actionlint -> $BIN_DIR..."
  curl -sSfL https://raw.githubusercontent.com/rhysd/actionlint/main/scripts/download-actionlint.bash \
    | bash -s -- latest "$BIN_DIR" \
    || echo "[session-start] WARN: actionlint install skipped"
fi
if ! command -v dotenv-linter >/dev/null 2>&1; then
  echo "[session-start] Installing dotenv-linter -> $BIN_DIR..."
  curl -sSfL https://raw.githubusercontent.com/dotenv-linter/dotenv-linter/master/install.sh \
    | sh -s -- -b "$BIN_DIR" \
    || echo "[session-start] WARN: dotenv-linter install skipped"
fi

echo "[session-start] Done. Run 'python scripts/lint-all.py' before pushing a gated branch."
