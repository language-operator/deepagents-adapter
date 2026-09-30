---
description: Update dependencies — Python lockfile, Docker base/tool pins, GitHub Actions — test, and open a PR
argument-hint: "[all|python|docker|actions]"
allowed-tools: Bash(uv:*), Bash(git:*), Bash(gh:*), Bash(make:*), Bash(helm:*), Bash(docker:*), Read, Edit
---

Update the dependencies of `deepagents-adapter`. The scope is: **$ARGUMENTS** (empty means `all`).

## Background (where dependencies live here)

| Surface | Where it is pinned | How it moves |
|---|---|---|
| Python packages | `pyproject.toml` (mostly unpinned) → **`uv.lock`** (exact pins) | `uv lock --upgrade` |
| `uv` binary | `Dockerfile` — `COPY --from=ghcr.io/astral-sh/uv:X.Y.Z` in **both** the builder and runtime stages | hand edit; both must match |
| Python base image | `Dockerfile` — `python:3.13-slim` in **both** stages | floats on patch; minor bumps are out of scope (see step 4) |
| coding-runtime base | `Dockerfile` — `ARG BASE=ghcr.io/language-operator/coding-runtime:<ver>-python@sha256:…` (only once issue #8 has landed) | hand edit; version **and** digest |
| GitHub Actions | `.github/workflows/*.yaml` — `uses: owner/action@vN` | hand edit, major tags |

The runtime venv is built `--no-dev` from `uv.lock` with `--frozen`, so **`uv.lock` is the real pin** for everything Python. `pyproject.toml` only needs to change if a lower bound must be raised or a package has to be capped.

## Steps

Perform these in order. If a precondition fails, stop and report — do not continue.

**0. Validate the argument.** `$ARGUMENTS` must be empty or one of `all`, `python`, `docker`, `actions`. Anything else: print usage (`/update-dependencies [all|python|docker|actions]`) and stop.

**1. Preconditions.**
- On `main`, working tree clean (`git status --porcelain` empty), and not behind `origin/main` after `git fetch origin`. Otherwise stop and tell the user why.
- Create a branch: `git switch -c chore/update-dependencies-<YYYY-MM-DD>` (today's date).

**2. Python (`python` or `all`).**
- Show what is outdated first: `uv tree --outdated --depth 1`.
- Upgrade the lockfile: `uv lock --upgrade`. Capture its `Updated <pkg> vA -> vB` lines — they go in the PR body.
- Call out every **major** version bump among the direct dependencies (`deepagents`, `langchain-openai`, `langchain-mcp-adapters`, `langgraph-checkpoint-sqlite`, `fastapi`, `uvicorn`, `pyyaml`, `a2a-sdk`, `httpx`, `pytest`, `pytest-asyncio`). For each one, skim its release notes (`gh release view --repo <owner>/<repo> <tag>` or the changelog) for breaking changes that touch how `agent_config.py` or `server.py` use it. `deepagents`, `langchain-*`, `langgraph-*` and `a2a-sdk` move fast and are the likeliest to break — check `create_deep_agent`, `FilesystemBackend`, the SQLite checkpointer, `MultiServerMCPClient`, and the A2A server wiring against their new APIs.
- If a bump breaks something that cannot be fixed cleanly in this PR, cap it in `pyproject.toml` (e.g. `"deepagents<0.N"`), re-run `uv lock`, and note it in the PR body as a follow-up rather than forcing the upgrade.

**3. Docker (`docker` or `all`).**
- **uv:** latest release is `gh release view --repo astral-sh/uv --json tagName --jq .tagName`. If newer than the pinned `ghcr.io/astral-sh/uv:X.Y.Z`, update **every** occurrence in `Dockerfile` so the builder and runtime stages stay identical.
- **Python base:** leave `python:3.13-slim` as is — it already floats on patch releases, and the builder and runtime stages must share the same base so the copied venv's interpreter paths stay valid. If a newer Python minor exists, just report it; do not bump it here.
- **coding-runtime base (only if `Dockerfile` has an `ARG BASE=ghcr.io/language-operator/coding-runtime…` line):** find the latest release with `gh release list --repo language-operator/coding-runtime` (or `gh api repos/language-operator/coding-runtime/tags`). Pin the new **version and digest** — resolve the digest with `docker buildx imagetools inspect ghcr.io/language-operator/coding-runtime:<ver>-python`. Never pin `:latest` or a `main` build.

**4. GitHub Actions (`actions` or `all`).**
- List the pins: `grep -n 'uses:' .github/workflows/*.yaml`.
- For each `owner/action@vN`, get the latest major: `gh release view --repo owner/action --json tagName --jq .tagName`. If the major is newer, bump `@vN` to the new major everywhere it is used (keep all workflows on the same major for a given action).
- For a major bump, skim the release notes for changed or removed inputs used in our workflows (e.g. `docker/metadata-action` tag patterns, `docker/build-push-action` inputs).

**5. Nothing to do?** If `git status --porcelain` is empty after steps 2–4, report that everything in scope is current, delete the branch (`git switch main && git branch -D <branch>`), and stop.

**6. Test.**
- `uv sync --frozen --group dev && uv run pytest -q` — fast check against the new lockfile.
- `make test` — builds the image (exercising the `Dockerfile` changes and the `--frozen --no-dev` sync) and runs the suite inside it. This is the same as the `image-test` CI job.
- If the chart or a workflow's helm step changed: `helm lint chart` and `helm template deepagents chart >/dev/null`.
- On a failure, fix it if the fix is small and clearly caused by the upgrade; otherwise fall back to capping that dependency (step 2) and re-test. Never skip or delete tests to get green.

**7. Commit and push.**
- Review `git diff --stat` — expect only `uv.lock`, possibly `pyproject.toml`, `Dockerfile`, `.github/workflows/*.yaml`, and any code fixes from step 6.
- One-line semantic commit: `git commit -am "chore(deps): update dependencies"` (use a more specific message if the scope was narrow, e.g. `chore(deps): bump uv to 0.12.0`).
- `git push -u origin <branch>`.

**8. Open a PR.** `gh pr create --title "<commit message>" --body "<body>"`, where the body lists:
- Python packages updated (from the `uv lock --upgrade` output), with **major bumps flagged** and a note of anything checked in their release notes.
- Docker pin changes (uv, coding-runtime) and GitHub Actions bumps.
- Anything deliberately held back or capped, and why.
- Newer versions reported but not taken (e.g. a new Python minor).

**9. Poll CI.** `gh pr checks <PR-number> --watch`. Both `image-test` and `chart-lint` must pass. Fix any failure on the branch and push again.

**10. Confirm before merging.** Show the user the PR link, the summary of what moved, and the CI result, and ask them to confirm the merge. On **yes**: `gh pr merge <PR-number> --squash --delete-branch`, then `git switch main && git pull`. On **no**: leave the PR open.

Do **not** cut a release as part of this command — if the user wants the updates shipped, they run `/release patch` afterwards.
