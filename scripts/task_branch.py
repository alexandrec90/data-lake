"""Shared helpers for starting a task on a fresh `claude/<slug>` branch.

Used by `scripts/hooks/branch-per-task.py` (UserPromptSubmit: records the task's
intended name), `scripts/hooks/branch-on-write.py` (PreToolUse: cuts the branch
when work actually begins), and `scripts/ship.py` for the per-worktree
shipped-marker contract.

**The cut happens at the first edit, not at the prompt.** UserPromptSubmit is the
one moment with the prompt text and *no* evidence of intent: a read-only question
asked from the default branch would cut a branch named after the question, and
"fix PR #42, it has conflicts" would cut one the PR will never see, because the
agent has to check that PR's branch out before it can do anything. Deferring the
decision to the first mutating tool call fixes both for free -- by then, an agent
that checked another branch out is no longer on the default branch, so
`auto_branch_decision` correctly declines.

Pure and stdlib-only so the hooks can import it before the venv is active. Tested
in `scripts/hooks/tests/test_task_branch.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

# Fallback default branch. The real default is resolved per-repo by
# `detect_default_branch()` (callers pass it in), so this is only the value the
# pure helpers assume when a caller does not override -- keeping the module
# project-agnostic while the branch name stays out of the logic.
DEFAULT_BRANCH = "master"
BRANCH_PREFIX = "claude/"
SLUG_MAX_LEN = 40
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

# Words that carry no naming value. Stripped by `topic()` so the slug is built
# from what the task is *about* rather than from whatever words the prompt
# happened to open with. Deliberately conservative: articles, pronouns, aux
# verbs, prepositions, conjunctions, politeness and hedging -- never a verb that
# says what to do (add/fix/remove/rename/...), which is the most useful word in
# a branch name.
_FILLER: frozenset[str] = frozenset({
    # determiners and pronouns
    "a", "an", "the", "this", "that", "these", "those", "it", "its", "there", "here",
    "i", "we", "you", "me", "my", "our", "your", "us",
    # auxiliaries and modals
    "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "doing", "done", "have", "has", "had", "get", "got",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    # politeness, hedging, discourse markers
    "please", "kindly", "thanks", "thank", "hey", "hi", "ok", "okay",
    "now", "just", "also", "really", "very", "quite", "maybe", "perhaps", "actually",
    "think", "thinks", "want", "wants", "need", "needs", "like", "let", "lets",
    # prepositions, conjunctions, interrogatives, quantifiers
    "to", "of", "in", "on", "at", "by", "for", "with", "from", "into", "onto", "as",
    "and", "or", "but", "so", "if", "then", "than", "when", "while", "because",
    "what", "which", "who", "whom", "how", "why", "where",
    "not", "no", "dont", "doesnt", "isnt", "wasnt", "cant",
    "some", "any", "all", "more", "most", "less", "much", "many",
    "possible", "instead", "about", "up", "out", "over", "again", "still",
})  # fmt: skip
# How many content words to keep. Six fits inside SLUG_MAX_LEN in practice while
# staying long enough to distinguish two tasks on the same area of the code.
TOPIC_WORDS_MAX = 6
_SENTENCE_SPLIT_RE = re.compile(r"[.?!\n]")

# Per-worktree marker file (under the worktree's git dir) recording the branch
# `/ship` last shipped. Once a branch is shipped it is "spent", so starting new work
# should leave it -- but only the *prompt* knows whether the next task is new work or
# a follow-up on that same PR, so this marker now drives an advisory note rather than
# an automatic checkout (see `spent_branch_notice`). Resolve the path with
# `worktree_file`. Project-agnostic name so the harness vendors unchanged.
SHIPPED_MARKER_NAME = "agent-shipped"

# Per-worktree marker recording the slug the *next* branch cut should use. Written by
# the UserPromptSubmit hook (which has the prompt text) and consumed by the PreToolUse
# hook that does the cutting (which does not). Overwritten on every prompt, so a cut
# is always named after the most recent statement of intent.
TASK_INTENT_MARKER_NAME = "agent-task-intent"

# Per-worktree counter of consecutive blocked stops, so pre-stop verification can
# re-check a fix instead of blocking exactly once. Read by `stop.py`; named here
# because this is the module that owns per-worktree marker names.
STOP_ROUNDS_MARKER_NAME = "agent-stop-rounds"


def worktree_file(git: Callable[..., subprocess.CompletedProcess[str]], name: str) -> Path | None:
    """Path of the per-worktree marker `name`, or None when git cannot resolve it.

    Markers live under the worktree's own git dir so parallel worktrees never share
    one, and `git rev-parse --git-path` is the only correct way to find that: for a
    linked worktree it returns that worktree's private dir, where a hand-built
    `<repo>/.git/<name>` would collide with every sibling checkout.

    `git(*args)` is injected (same contract as `detect_default_branch`) so this is
    unit-testable without spawning git.
    """
    try:
        result = git("rev-parse", "--git-path", name)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    return Path(raw) if raw else None


def detect_default_branch(
    git: Callable[..., subprocess.CompletedProcess[str]], fallback: str = "main"
) -> str:
    """The remote's default branch, project-agnostically. Replaces a hardcoded name.

    Resolves `origin/HEAD` (the symbolic ref set at clone time); if that is not set,
    probes `origin/main` then `origin/master`; if neither exists, returns `fallback`.
    `git(*args)` must return a CompletedProcess capturing stdout -- injected so this
    stays pure and unit-testable without spawning git.
    """
    head = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = (head.stdout or "").strip()
    if head.returncode == 0 and ref.startswith("refs/remotes/origin/"):
        return ref.rsplit("/", 1)[1]
    for candidate in ("main", "master"):
        if (
            git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}").returncode
            == 0
        ):
            return candidate
    return fallback


def parse_prompt(raw_stdin: str) -> str:
    """Extract the prompt text from a UserPromptSubmit payload; '' when absent."""
    try:
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("prompt", "")
    return value if isinstance(value, str) else ""


def topic(text: str, max_words: int = TOPIC_WORDS_MAX) -> str:
    """Condense a prompt down to the few words worth naming a branch after.

    Slugifying the prompt directly names the branch after its first 40 characters,
    which is usually preamble: "The hook that creates the branch - I think it uses
    a generic name..." becomes `the-hook-that-creates-the`. This keeps the first
    sentence (prompts state the task first, then elaborate), drops `_FILLER`, and
    keeps the leading `max_words` content words in their original order -- giving
    `hook-creates-branch-generic-name` from the same prompt, at no token cost.

    Returns '' when nothing survives; callers fall back to plain `slugify`.
    """
    # First sentence with something in it. A one-word opener ("Question.", "Hey!")
    # is a lead-in, not the task, so skip past it to the next sentence.
    segments = _SENTENCE_SPLIT_RE.split(text.strip().lower())
    words: list[str] = []
    for segment in segments:
        words = _SLUG_STRIP_RE.sub(" ", segment).split()
        if len(words) > 1:
            break
    kept = [w for w in words if w not in _FILLER]
    return "-".join(kept[:max_words])


def slugify(text: str, max_len: int = SLUG_MAX_LEN) -> str:
    """Turn free text into a branch-safe slug (lowercase, hyphenated)."""
    slug = _SLUG_STRIP_RE.sub("-", text.strip().lower()).strip("-")
    if len(slug) > max_len:
        # Trim at a word boundary so the slug stays readable, not mid-token.
        slug = slug[:max_len].rsplit("-", 1)[0] if "-" in slug[:max_len] else slug[:max_len]
        slug = slug.strip("-")
    return slug or "task"


def slug_from_prompt(text: str, max_len: int = SLUG_MAX_LEN) -> str:
    """The branch slug for a prompt: its topic, or the raw text when none survives."""
    return slugify(topic(text) or text, max_len=max_len)


def should_branch(current_branch: str, default_branch: str = DEFAULT_BRANCH) -> bool:
    """True only when sitting on the default branch (the hook's auto-trigger).

    A blank branch means detached HEAD -- never branch from there automatically.
    """
    return bool(current_branch) and current_branch == default_branch


def branch_name(slug: str, existing: set[str], today: _dt.date | None = None) -> str:
    """Unique `claude/<slug>-<mmdd>` name, disambiguated with -N against existing."""
    today = today or _dt.date.today()
    base = f"{BRANCH_PREFIX}{slug}-{today:%m%d}"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def checkout_base(tree_dirty: bool, default_branch: str = DEFAULT_BRANCH) -> str | None:
    """Which ref to base a new branch on when auto-branching from the default branch.

    Clean tree -> branch from `origin/<default_branch>` (start current). Dirty tree ->
    None: branch from the current HEAD carrying the uncommitted changes, because
    resetting onto the default branch could clobber them.
    """
    return None if tree_dirty else f"origin/{default_branch}"


def checkout_argv(name: str, base: str | None) -> list[str]:
    """Git args cutting branch `name` off `base`, never inheriting an upstream.

    `git checkout -b <name> origin/<default>` branches from a *remote-tracking*
    ref, which git's default `branch.autoSetupMerge=true` reads as "track it": the
    new branch's upstream becomes `origin/<default>`. Nothing surfaces that until
    the first push, and then a bare `git push` -- or VS Code's "Sync Changes",
    which pushes HEAD to the configured upstream -- lands the task's commits
    straight on the default branch. No feature branch is ever published, so no
    editor offers "Publish Branch" and there is nothing to open a PR from.

    `--no-track` leaves the upstream unset, so the first push has to name a
    destination and editors offer to publish instead. It is passed
    unconditionally, including when `base` is None: `autoSetupMerge=always` would
    otherwise track a *local* start point too, and this ships into repos whose
    git config we do not control.
    """
    return ["checkout", "--no-track", "-b", name] + ([base] if base else [])


def auto_branch_decision(
    current_branch: str,
    tree_dirty: bool,
    default_branch: str = DEFAULT_BRANCH,
) -> tuple[bool, str | None]:
    """Should the hook auto-branch now, and on which base?

    Returns (should_branch, base_ref). base_ref None means "branch from the
    current HEAD carrying uncommitted changes" (only for the dirty-default case).

    One trigger: sitting on the default branch. Carry a dirty tree rather than
    clobber it. Anything else -- a feature branch, a detached HEAD -- declines,
    which is what makes this safe to call before *every* edit rather than once per
    prompt: the second edit of a task is already on the branch the first one cut.

    The shipped-branch trigger this used to carry is gone. It fired whenever a
    prompt arrived on the branch `/ship` had just shipped with a clean tree, which
    is exactly the state a "the PR gate went red, fix it" prompt arrives in -- so it
    yanked the session onto a fresh branch off the default and the fix landed
    somewhere the PR would never see it. Deferring the cut to the first edit does
    not help there (the first edit is still on the spent branch), so that case is
    now an advisory note instead: see `spent_branch_notice`.
    """
    if should_branch(current_branch, default_branch):
        return True, checkout_base(tree_dirty, default_branch)
    return False, None


def spent_branch_notice(
    current_branch: str,
    shipped_branch: str,
    tree_dirty: bool,
    suggested_branch: str,
    default_branch: str = DEFAULT_BRANCH,
) -> str:
    """Advice for a prompt arriving on an already-shipped branch, or '' when N/A.

    Replaces the automatic checkout described in `auto_branch_decision`. "This branch
    is spent" and "this prompt continues that PR" are both true in the same state, and
    only the prompt distinguishes them -- so the one component that can read the prompt
    gets to decide, and the hook confines itself to stating the situation and the exact
    command.

    Returns '' unless we are sitting on the shipped branch with a clean tree. The
    clean-tree guard means the note stops the moment any work exists, which also bounds
    how often it can repeat: while it keeps firing, nothing has happened yet.
    """
    if not current_branch or current_branch != shipped_branch or tree_dirty:
        return ""
    return (
        f"Branch '{current_branch}' has already been shipped (`/ship` opened a PR from "
        f"it), and the tree is clean.\n"
        f"- If this prompt starts NEW work, leave it -- the branch is spent:\n"
        f"    git fetch origin {default_branch} && git checkout --no-track -b "
        f"{suggested_branch} origin/{default_branch}\n"
        f"- If this prompt continues that PR (a review comment, a red gate, a merge "
        f"conflict), stay on '{current_branch}' and push there."
    )


def platform_manages_branch(env: Mapping[str, str]) -> bool:
    """True in a cloud/remote session (Claude Code on the web / mobile), where the
    platform owns the branch lifecycle -- so the auto-branch hook must stand down.

    On the web/mobile the platform checks out and tracks a `claude/...` branch and
    handles commit/PR. Cutting a *new* local branch here -- which the shipped-marker
    path would do on the prompt after `/ship` -- diverges HEAD from the branch the
    mobile app tracks, so later work lands on a branch its UI never shows. The
    primary-checkout (`master`) trigger never fires remotely anyway (the platform
    never leaves you on `master`), so standing the whole hook down is a no-op there
    and closes the shipped-marker hole. Mirrors the `CLAUDE_CODE_REMOTE` guard in
    `.claude/hooks/session-start.sh`.
    """
    return env.get("CLAUDE_CODE_REMOTE") == "true"
