---
description: Cut a release — bump version in lockstep, tag, and push to trigger CI publish
argument-hint: major|minor|patch
allowed-tools: Bash(git:*), Bash(helm:*), Read, Edit
---

Cut a new release of `deepagents-adapter`. The bump type is: **$ARGUMENTS**

## Background (how releases work here)

A release is triggered by pushing a `vX.Y.Z` git tag. Two GitHub Actions workflows fire on `tags: ['v*']`:

- `.github/workflows/build-image.yaml` — builds and pushes the adapter image to `ghcr.io/language-operator/deepagents-adapter`, tagged `X.Y.Z`, `X.Y`, `X`, and `sha-<commit>` (the leading `v` is stripped by `docker/metadata-action`).
- `.github/workflows/release-chart.yaml` — runs `helm package chart` and pushes to `oci://ghcr.io/language-operator/charts`. The chart package version comes from `version:` in `chart/Chart.yaml`, **not** from the git tag.

Version is kept in **lockstep**: `chart/Chart.yaml` `version`, `chart/Chart.yaml` `appVersion`, the pinned `image.tag` in `chart/values.yaml`, and the git tag all become the same `X.Y.Z`. This is a single combined image built by this repo (no split upstream/adapter tags).

## Steps

Perform these in order. If any precondition fails, stop and report the problem — do not continue.

**0. Validate the argument.** `$ARGUMENTS` must be exactly one of `major`, `minor`, or `patch`. If it is missing or anything else, print usage (`/release major|minor|patch`) and stop.

**1. Preconditions.**
- Current branch is `main`: `git rev-parse --abbrev-ref HEAD`. If not, stop.
- Working tree is clean: `git status --porcelain` must be empty. If there are uncommitted changes, stop and tell the user to commit or stash them first — the release commit must contain only the version bump.
- `git fetch origin`, then confirm `main` is not behind `origin/main` (compare `git rev-parse HEAD` with `git rev-parse origin/main`, or use `git rev-list --left-right --count main...origin/main`). If behind, stop and tell the user to pull.

**2. Determine the current (baseline) version.**
- Latest tag: `git describe --tags --match 'v*' --abbrev=0` (may be empty if no tags exist yet — that's fine).
- Read `version:` and `appVersion:` from `chart/Chart.yaml`.
- Baseline = the **highest** semver among {latest tag with `v` stripped, chart `version`, `appVersion`}.

**3. Compute the next version** from the bump type:
- `patch` → `X.Y.(Z+1)`
- `minor` → `X.(Y+1).0`
- `major` → `(X+1).0.0`

Print: `Releasing vX.Y.Z (was <baseline>)`.

**4. Edit version locations** (use the Edit tool):
- `chart/Chart.yaml`: set `version: X.Y.Z` and `appVersion: "X.Y.Z"`.
- `chart/values.yaml`: under `image:`, set `tag: X.Y.Z`. This is the combined adapter image built by this repo.

**5. Validate the chart renders.** Run `helm lint chart` and `helm template chart >/dev/null`. If either fails, stop and report (the version is not yet committed, so nothing to roll back).

**6. Commit and tag.**
- `git commit -am "chore(release): vX.Y.Z"` — verify the diff contains only `chart/Chart.yaml` and `chart/values.yaml`.
- `git tag -a vX.Y.Z -m "Release vX.Y.Z"`.

**7. Confirm, then push.**
- Show a summary: the new version, the file diff (`git show --stat HEAD`), the new tag, and exactly what pushing will trigger — a live publish of the image and chart to `ghcr.io`.
- Ask the user to explicitly confirm the push.
- On **yes**: `git push --follow-tags origin main` (pushes the commit and the new tag together).
- On **no**: leave the commit and tag in place locally. Tell the user how to undo (`git tag -d vX.Y.Z` then `git reset --soft HEAD~1`) or push later (`git push --follow-tags origin main`).

**8. Report.** After a successful push, report:
- The pushed tag `vX.Y.Z`.
- The two workflows now running (`Build and Push Image`, `Release Helm Chart`) — suggest watching them with `gh run watch` or the Actions tab.
- The resulting artifacts: `ghcr.io/language-operator/deepagents-adapter:X.Y.Z` and `oci://ghcr.io/language-operator/charts/deepagents:X.Y.Z`.
