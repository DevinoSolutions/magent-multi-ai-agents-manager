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
   approval click before the package is pushed to PyPI. The repo's `pypi`
   environment currently has **no** required reviewer (its only rule allows
   `v*` tags), so a tag push publishes without a pause.
3. No secrets are needed in this environment — Trusted Publishing uses the
   job's short-lived OIDC token (`permissions: id-token: write`). That
   permission is currently granted at the **workflow** level, so every job in
   `release.yml` can mint a token; PyPI accepts it only from a job running in
   the `pypi` environment, which is the publish job. Scoping it to the publish
   job alone is a follow-up.

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

   For an rc, the pending section becomes the rc's and a fresh pending one
   opens above it; see **Pre-release changelog** below.

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

5. **Tag and push.** The tag (`vX.Y.Z`) is what triggers the pipeline. It must
   be `v` plus the canonical PEP 440 form of the `pyproject.toml` version (what
   `packaging.version.Version` prints: `v3.20.0rc1`, not `v3.20.0-rc1`). Push the
   commit first, then the tag:

   ```bash
   git push origin main
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

That's it. On the tag push the workflow will:

1. **build** — `python -m build --wheel` produces the wheel, `twine check
   --strict` validates the metadata, and the tag must be exactly `v` plus the
   canonical PEP 440 form of the version inside the built wheel, or the
   run stops before anything is published.
2. **smoke** — installs the built wheel into a clean, no-extras venv on Linux,
   Windows, and macOS and runs `magent --version` / `magent --help`.
3. **publish** — after any required-reviewer approval, uploads the wheel
   to PyPI via Trusted Publishing (no token).
4. **github-release** — creates a GitHub Release for the tag with
   auto-generated notes and the built artifacts attached.

> **Wheel-only releases.** No sdist is built or published, on purpose.
> hatchling's default sdist ships the whole tree (tests, docs, agent plans), and
> a PyPI upload can never be taken back, so a private string that reaches an
> sdist cannot be scrubbed afterwards. Users on a supported platform install the
> pure-Python wheel; `pip install` on a platform that needs the sdist is not
> supported. `tests/dist/` still builds an sdist locally to scan it for private
> strings; that is a test fixture, never an upload.

The `github-release` job publishes GitHub's **auto-generated** notes — no manual
release step is required. For a curated changelog, edit the Release after the run
finishes and paste in the matching `CHANGELOG.md` section.

> **Pre-releases** use the same steps with a PEP 440 pre-release version: set
> `version = "X.Y.0rc1"` in `pyproject.toml` and tag `vX.Y.0rc1`, spelled
> exactly that way (PyPI gets the `pyproject.toml` version, not the tag, so a
> `vX.Y.0rc1` tag over an `X.Y.0` build fails the `build` job, and so do
> non-canonical spellings like `vX.Y.0-rc1`). The `build` job classifies the tag with `packaging`
> (`a`/`b`/`rc`/`.dev` all count), and `github-release` then marks the Release
> as a pre-release that never becomes **Latest**. PyPI and `pip` treat the
> version as a pre-release too: a plain `pip install` skips it, so testers need
> `pip install --pre magent-multi-ai-agents-manager` or an exact `==X.Y.0rc1`.
> magent cuts only `rcN` pre-releases; `a`/`b`/`.dev` versions are not used for
> its releases. The workflow would still classify one as a pre-release, which is
> the fail-safe direction.
>
> **Pre-release changelog.** Only an `rcN` gets a `CHANGELOG.md` section, headed
> like a final one. When cutting `X.Y.0rc1`, rename step 2's pending
> `## [X.Y.0] - UNRELEASED` heading to `## [X.Y.0rc1] - YYYY-MM-DD` and open a
> fresh `## [X.Y.0] - UNRELEASED` above it for post-rc work; each later `rcN`
> does the same. At the final, stamp that pending section and make it summarize
> everything since the last final release, rc changes included; the rc sections
> stay below it as history. Link references stay in heading order, newest
> first, and the final's link starts at the last **final** tag, not at the rc
> (`vLASTFINAL` is the previous final release's tag):
>
> ```text
> [X.Y.0]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/vLASTFINAL...vX.Y.0
> [X.Y.0rc2]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/vX.Y.0rc1...vX.Y.0rc2
> [X.Y.0rc1]: https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/compare/vLASTFINAL...vX.Y.0rc1
> ```
>
> These links only resolve if the tags are spelled exactly `vX.Y.0rc1` (never
> `vX.Y.0-rc1`), which the `build` job enforces.

---

## Dry run (exercise the pipeline without releasing)

To validate build + smoke without publishing, trigger the workflow manually:

- **GitHub UI:** *Actions → Release → Run workflow* (leave **dry_run** checked).
- **CLI:** `gh workflow run release.yml -f dry_run=true`

The `build` and `smoke` jobs run; `publish` and `github-release`
are skipped. `workflow_dispatch` can **never** publish — publishing is gated to
`push` events on `v*` tags — so a dispatch is always safe, whatever the
`dry_run` value.

A dry run from a branch does **not** exercise the tag checks (the pre-release
classification and the tag-vs-built-version guard) or `github-release`: they
need a tag, so their first real run is the tag push itself. With no required
reviewer on `pypi`, that push publishes without a pause. The checks fail
closed: they run in the `build` job before the artifacts are uploaded, so a bad
tag stops the run with nothing published.

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
- **`tag vX does not match the built version Y (expected tag vY)`** — nothing
  was published; the `build` job stopped before the upload. Two cases:
  - *`pyproject.toml` is wrong* (the tag is the version you meant): delete the
    tag, fix the version, then re-create the tag **on the fixed commit** and
    push it. Re-pushing the old local tag would point at the old commit again.

    ```bash
    git push origin :refs/tags/vX   # delete the tag on GitHub
    git tag -d vX                   # and locally
    # fix pyproject.toml, run `uv lock`, commit, push the commit
    git tag vX                      # re-create it on the fixed commit
    git push origin vX
    ```

  - *The tag is the typo* (`pyproject.toml` is right): delete it and tag the
    same commit with the right name. Nothing in `pyproject.toml` changes.

    ```bash
    commit=$(git rev-list -n 1 vX)  # the commit the wrong tag points at
    git push origin :refs/tags/vX
    git tag -d vX
    git tag vY "$commit"
    git push origin vY
    ```

  An `InvalidVersion: Invalid version: '...'` from the classify step means the
  tag is not PEP 440 at all; recover the same way as a tag typo.
- **`File already exists` from PyPI** — that version was already uploaded. PyPI
  is immutable; bump to a new version and tag again.
- **`uv lock --check` fails in CI** — you bumped the version without running
  `uv lock`. Run it, commit `uv.lock`, and re-tag if the tag already moved.
