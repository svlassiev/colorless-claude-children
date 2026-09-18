# Colorless Claude Children

Personal photo gallery site for serg.vlassiev.info.

## Identity Guardrail

This is a **personal project**. All operations MUST use the `svlassiev` identity, NOT the Epidemic Sound work identity.

Before running any command that authenticates or pushes to an external service, verify the active identity:

| Service | Required Identity | Check Command | Switch Command |
|---------|------------------|---------------|----------------|
| **Git (local)** | `svlassiev` personal email | `git config user.email` | `git config user.email <personal-email>` |
| **GitHub CLI** | `svlassiev` | `gh auth status` | `gh auth login` (re-auth as svlassiev) |
| **gcloud** | `svlassiev@gmail.com`, project `thematic-acumen-225120` | `gcloud config get-value account` | `gcloud config set account svlassiev@gmail.com` |
| **Docker Hub** | `svlassiev` | `docker info \| grep Username` | `docker login -u svlassiev` |

**NEVER** use `a work/employer email`, `a work username`, or any `employer projects` project for this repo.

**Credential hygiene:** Always `docker logout` immediately after pushing. Docker credentials are stored in macOS Keychain and shared across all terminal sessions — leaving them active risks accidental pushes from other contexts. As of 2026-09-18 this machine has a single gcloud configuration, `default`, set to the personal account (there is no `development` config). If a work configuration is added again, activate the personal one for this repo and switch back afterwards.

## Project Overview

- Static photo gallery: HTML/CSS/vanilla JS + `nginx:1.31.4-alpine`. The version is pinned because the site pod's 32M memory limit OOM-killed 1.31.6 on 2026-09-18 and took the site down. Before bumping nginx, raise the limit in `k8s/deployment.yml`.
- Photos served from GCS bucket `gs://colorless-days-children/`
- Deployed to GKE in project `thematic-acumen-225120`
- Docker image: `svlassiev/colorless-days-children` (same repo name as before, v2.0+)
- Domain: `serg.vlassiev.info`
- Adding a photo album: README "Adding new albums" (`scripts/add_album.py`: UUID folder, 80px thumbnail + 1024px viewer sizes, then hiking-api restart for share links)

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
