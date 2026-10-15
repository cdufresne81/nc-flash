# Release Channels: Stable and Experimental

NC Flash ships on two channels. The in-app updater (Settings > General >
Updates > **Update channel**) decides which one a user sees.

| | Branch | Tag | GitHub release | Offered to |
|---|---|---|---|---|
| Stable | `master` | `vX.Y.Z` | normal ("latest") | everyone (default) |
| Experimental | `dev` | `vX.Y.Z-beta.N` | pre-release (automatic) | users on the Experimental channel |

The release workflow marks any tag containing `-` as a pre-release. Stable
installs only read `releases/latest`, which GitHub never fills with a
pre-release, and the updater also refuses a `-beta.N` tag there. So a beta
can't reach a stable user, even from an install older than the channel
feature.

## Branch flow

```
feature/x ──PR──► dev ──tag vX.Y.Z-beta.N──► Experimental users
    │
    └──PR (once solid)──► master ──tag vX.Y.Z──► everyone
master ──merge after every stable release──► dev
```

- **Branch features from `master`, not `dev`**, so each one can graduate on
  its own. A feature built on top of another unfinished feature can't ship
  until that one does.
- **New features** go to `dev` first (PR into `dev`). When one is solid, open
  a second PR from the **same feature branch** into `master`. Never merge all
  of `dev` into `master`: that would ship every unfinished experiment with it.
- **Never merge or rebase `dev` into a feature branch** (GitHub's "Update
  branch" button on a PR into `dev` does exactly that). The branch would then
  carry all of `dev`, and its PR into `master` would ship every experiment.
  To resolve a conflict with `dev`, merge `master` into the feature branch,
  or resolve it in the PR into `dev` on a throwaway branch.
- Fixes found during beta testing go on the **feature branch** (then PR into
  `dev` again), never directly on `dev`, so they graduate with the feature.
- **Bug fixes** to shipped code go straight to `master` (PR).
- After every stable release, merge `master` into `dev` so `dev` stays a
  superset of `master`. **If a beta newer than that release is out, tag the
  next beta right away**: `2.22.1 < 2.23.0-beta.1`, so beta users never get a
  stable hotfix until a beta contains it.

## Tagging

- **Stable:** `vX.Y.Z` on `master`, exactly as before (CHANGELOG section,
  release branch, PR, tag).
- **Beta:** `vX.Y.Z-beta.N` on `dev`, where `X.Y.Z` is the **next** stable
  version, and `N` counts up from 1 (`v2.23.0-beta.1`, `-beta.2`, …). Only
  this exact form is recognised: `-rc1`, `-beta1` or `-alpha.1` tags are
  published but ignored by the updater.
- The release workflow refuses a stable tag whose commit is not on `master`.
- Betas get no CHANGELOG version section: their release notes are the
  `## [Unreleased]` section on `dev`. Feature entries are written on the
  feature branch, so they reach `master` with it; expect to resolve
  `[Unreleased]` conflicts on each master→dev merge.
- Version order: `2.22.0 < 2.23.0-beta.1 < 2.23.0-beta.2 < 2.23.0`. When
  `v2.23.0` ships, beta users move to it, which drops any experiment that
  didn't graduate. The next beta is `v2.24.0-beta.1`.

## Behaviour notes

- Switching channel resets the daily throttle, so the next launch checks the
  new channel right away.
- No downgrades: a user who goes back to Stable keeps the beta until a stable
  release is newer. *Help > Check for Updates* says so.
- A beta build counts as a release build (it updates itself and the
  workspace definitions), unlike a source checkout (`dev`). Definitions a
  beta merged into the workspace stay after switching back to Stable; the
  next stable release newer than the beta merges its own on top.
- An open beta update dialog isn't closed when the user switches to Stable;
  installing still takes a click.

Code: `src/utils/update_check.py` (channel fetch, version order),
`src/ui/app_updater.py` (controller + dialog), `AppSettings.get_update_channel`.
