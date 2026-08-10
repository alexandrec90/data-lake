---
name: ship
description: 'Ship the completed task branch: verify it, commit the intended diff, push it, and open or reuse its GitHub pull request.'
argument-hint: 'Optional PR title'
---

# Ship the current task

Run each step in order. Stop on failure; never open a PR for an unverified branch.

1. Run `python scripts/ship.py --preflight`. It must report a `claude/` task branch
   and the repository's detected default branch.
2. Review `git status` and the complete diff. Run the targeted tests for the changed
   behavior. Stage only the intended files, then commit with an imperative subject and
   a body explaining why. Use `$ARGUMENTS` as the subject when supplied.
3. Run `python scripts/ship.py`. This requires a clean tree, runs the changed-scope
   lint gate, and pushes the current branch with retry handling. Fix any failure and
   rerun this step.
4. Run `gh pr view --json number,url,state` to find an existing PR for the branch.
   Reuse it when present. Otherwise inspect the repository's PR template and run
   `gh pr create` with the detected base branch, current branch, commit subject (or
   `$ARGUMENTS`), and a concise body covering the change and verification.
5. Report the PR number and URL.

Do not enable auto-merge, wait on CI, or start an autofix loop unless the user asks.

## Do not clean up after yourself

Shipping ends at the PR. If this branch is in an ephemeral box, **leave the box
alone** — do not reap it, do not delete the branch, do not stop its stack.

`worktree.py reconcile` owns that, and it waits for the PR to actually merge before
destroying anything. Reaping here would do it on the strength of the push instead,
which is the one moment the work exists only locally if the PR was never created.
