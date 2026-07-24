# Releasing magent

magent publishes to [PyPI](https://pypi.org/project/magent-multi-ai-agents-manager/) through
`.github/workflows/release.yml` using **PyPI Trusted Publishing** (OIDC) — there
are **no API tokens or secrets** stored in the repository. The pipeline is
**dormant by design**: nothing is ever published until *both* of these are true:

1. The one-time PyPI-side + GitHub-side setup below is complete, **and**
2. A maintainer pushes a `vX.Y.Z` tag.

Pushing to a branch or opening a PR never publishes anything. `workflow_dispatch`
is always a safe **dry run** (build + smoke-test only).

---

## One-time setup (before the first release)

Do this once. The **first** publish is what claims the `magent-multi-ai-agents-manager` name on PyPI —
it works via a *pending* Trusted Publisher, so PyPI is configured **before** the
project exists.

### 1. PyPI account / organization

- Create a PyPI account (and, if desired, a `DevinoSolutions` PyPI organization)
  at <https://pypi.org>. Enable 2FA.

### 2. Add a *pending* Trusted Publisher on PyPI

Because the project does not exist on PyPI yet, add it as a **pending** publisher:

1. Go to <https://pypi.org/manage/account/publishing/>.
2. Under **Add a new pending publisher**, choose **GitHub** and enter **exactly**:

   | Field                    | Value                          |
   | ------------------------ | ------------------------------ |
   | PyPI Project Name        | `magent-multi-ai-agents-manager` |
   | Owner                    | `DevinoSolutions`              |
   | Repository name          | `magent-multi-ai-agents-manager`  |
   | Workflow name            | `release.yml`                  |
   | Environment name         | `pypi`                         |

3. Save. The first successful run of the publish job registers the project and
   claims the name.

> The repository's canonical name is `magent-multi-ai-agents-manager`. `gh` and web
> links may redirect from an older short name — use the canonical name here, it
> must match `github.repository` at publish time exactly.

### 3. Create the `pypi` environment in GitHub

1. In the repo, go to **Settings → Environments → New environment** and name it
   `pypi` (must match the `environment: name` in `release.yml`).
2. (Recommended) Under **Deployment protection rules**, add yourself / the
   release team as **Required reviewers**. Every publish then pauses for a manual
   approval click before the package is pushed to PyPI.
3. No secrets are needed in this environment — Trusted Publishing uses the
   job's short-lived OIDC token (`permissions: id-token: write`), which is
   already scoped to the publish job only.

---

## Cutting a release (every time)

From an up-to-date `main` (or a release branch that will be merged):

1. **Bump the version** in `pyproject.toml`:

   ```toml
   [project]
   version = "X.Y.Z"
   ```

2. **Stamp the changelog.** `CHANGELOG.md` ships the pending release under a
   `## [X.Y.Z] - UNRELEASED` heading. Replace `UNRELEASED` with the release date
   and update the matching link reference at the bottom of the file:

   ```text
   ## [X.Y.Z] - YYYY-MM-DD
   ```

3. **Refresh the lock** — `uv.lock` records the project's own version, so a
   version bump changes it and CI's `uv lock --check` will fail if it drifts:

   ```bash
   uv lock
   ```

4. **Commit** all three files:

   ```bash
   git add pyproject.toml uv.lock CHANGELOG.md
   git commit -m "chore(release): vX.Y.Z"
   ```

5. **Tag and push.** The tag (`vX.Y.Z`) is what triggers the pipeline — push the
   commit first, then the tag:

   ```bash
   git push origin main
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

That's it. On the tag push the workflow will:

1. **build** — `python -m build` produces the sdist + wheel and `twine check
   --strict` validates the metadata.
2. **smoke** — installs the built wheel into a clean, no-extras venv on Linux,
   Windows, and macOS and runs `magent --version` / `magent --help`.
3. **publish** — after any required-reviewer approval, uploads the sdist + wheel
   to PyPI via Trusted Publishing (no token).
4. **github-release** — creates a GitHub Release for the tag with
   auto-generated notes and the built artifacts attached.

The `github-release` job publishes GitHub's **auto-generated** notes — no manual
release step is required. For a curated changelog, edit the Release after the run
finishes and paste in the matching `CHANGELOG.md` section.

> Pre-release tags work too: a PEP 440 pre-release version (e.g. `1.1.0rc1`,
> tagged `v1.1.0rc1`) matches `v*`, and PyPI/`pip` treat it as a pre-release.

---

## Dry run (exercise the pipeline without releasing)

To validate build + smoke without publishing, trigger the workflow manually:

- **GitHub UI:** *Actions → Release → Run workflow* (leave **dry_run** checked).
- **CLI:** `gh workflow run release.yml -f dry_run=true`

The `build` and `smoke` jobs run; `publish` and `github-release` are skipped.
`workflow_dispatch` can **never** publish — publishing is gated to `push` events
on `v*` tags — so a dispatch is always safe, whatever the `dry_run` value.

> A `workflow_dispatch` run only appears once `release.yml` exists on the
> repository's **default branch** (a GitHub requirement for the manual trigger).

---

## Troubleshooting

- **`invalid-publisher` / `trusted publishing exchange failure`** — the PyPI
  pending-publisher fields don't match. Re-check owner (`DevinoSolutions`), repo
  (`magent-multi-ai-agents-manager`), workflow filename (`release.yml`), and
  environment (`pypi`). All four must match exactly.
- **Publish waits and never runs** — a Required reviewer must approve the `pypi`
  environment deployment (Actions run page → **Review deployments**).
- **`File already exists` from PyPI** — that version was already uploaded. PyPI
  is immutable; bump to a new version and tag again.
- **`uv lock --check` fails in CI** — you bumped the version without running
  `uv lock`. Run it, commit `uv.lock`, and re-tag if the tag already moved.
