# Contributing

Thanks for helping. This project's rules are short because the niche punishes
sloppiness — every rule below exists because skipping it once cost days.

## The ten rules

1. **Red before green.** A fix lands with a test that failed first. No
   reproducing test, no merge.
2. **One PR = one concern.** If the diff needs a paragraph to explain what is
   NOT part of it, split it.
3. **Never edit a test until it passes.** If the test is wrong, say why in the
   PR and change it in the same PR, visibly.
4. **No smuggled changes.** Reformat-only hunks, dependency bumps and drive-by
   fixes go in their own PRs.
5. **Explain the why, not the what.** Commit messages and code comments state
   the reason; the diff already shows the what.
6. **Docs in the same PR.** If a change makes any README/CHANGELOG sentence
   untrue, that PR fixes the sentence too.
7. **Rollback is one action.** Every change must be revertable on its own;
   no PR depends on "also apply the next PR first".
8. **Verify against the real thing.** Claims about clients/upstream behavior
   come from runs you actually performed (see `docs/CLIENT_MATRIX.md`).
9. **No credential material, ever.** Not in logs, not in test fixtures, not in
   issue reports, not in screenshots. Shapes and counts, not values.
10. **Personal use only.** No PR will be accepted that makes a public relay
    easier to run — that is a design stance, not a limitation.

## Commit messages

`type(scope): imperative summary` — e.g. `fix(pool): rotate empty-SSE retries
off the failed token (B10)`. The body states the why and cites the issue/PR.
Types: `feat`, `fix`, `test`, `docs`, `build`, `ci`, `chore`, `refactor`,
`perf`, `style`.

## Process

- Branch from `main`, one branch per concern (`fix/…`, `feat/…`).
- Run the full suite locally (`uv sync --extra dev --extra tests`, then
  `.venv/bin/pytest` and `.venv/bin/ruff check .`) before pushing.
- CI runs the same two gates; a red CI blocks the merge regardless of intent.
- New user-visible behavior updates `CHANGELOG.md` (with a citation) and the
  README in the same PR.
