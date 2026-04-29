# Colorless Claude Children

Personal photo gallery site for serg.vlassiev.info.

## Identity Guardrail

This is a **personal project**. All operations MUST use the `svlassiev` identity, NOT the Epidemic Sound work identity.

Before running any command that authenticates or pushes to an external service, verify the active identity:

| Service | Required Identity | Check Command | Switch Command |
|---------|------------------|---------------|----------------|
| **Git (local)** | `svlassiev` personal email | `git config user.email` | `git config user.email <personal-email>` |
| **GitHub CLI** | `svlassiev` | `gh auth status` | `gh auth login` (re-auth as svlassiev) |
| **gcloud** | `svlassiev@gmail.com` via `development` config | `gcloud config configurations list` | `gcloud config configurations activate development` |
| **Docker Hub** | `svlassiev` | `docker info \| grep Username` | `docker login -u svlassiev` |

**NEVER** use `a work/employer email`, `a work username`, or any `employer projects` project for this repo.

**Credential hygiene:** Always `docker logout` immediately after pushing. Docker credentials are stored in macOS Keychain and shared across all terminal sessions — leaving them active risks accidental pushes from other contexts. Same principle applies to `gcloud`: switch back to your default (ES) config after personal project work with `gcloud config configurations activate default`.

## Project Overview

- Static photo gallery: HTML/CSS/vanilla JS + nginx:alpine
- Photos served from GCS bucket `gs://colorless-days-children/`
- Deployed to GKE in project `thematic-acumen-225120`
- Docker image: `svlassiev/colorless-days-children` (same repo name as before, v2.0+)
- Domain: `serg.vlassiev.info`

## Progress Journaling

After completing each implementation step, append a short log entry to the relevant phase in `PLAN.md` documenting:
- What was actually done (vs what was planned)
- Any surprises, wrong assumptions, or deviations from the plan
- Decisions made and why

Format: add a `#### Log` subsection under the phase with timestamped entries. This helps future projects avoid the same wrong assumptions.

## Development

```bash
docker build -t colorless-claude-children .
docker run -p 8080:80 colorless-claude-children
# Open http://localhost:8080
```
