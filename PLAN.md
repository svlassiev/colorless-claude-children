# Colorless Claude Children — Modernization Plan

## Goal

Rebuild [serg.vlassiev.info](http://serg.vlassiev.info) from a 2.6GB Docker image
into a lightweight container that serves only HTML/CSS/JS,
while photos are loaded from Google Cloud Storage.
The visual style of the 20+ year-old site is preserved exactly.

---

## Architecture

| Component | Technology |
|-----------|-----------|
| **Web server** | `nginx:alpine` |
| **App code** | HTML + CSS + vanilla JS |
| **Photos** | Google Cloud Storage (public bucket) |
| **TLS** | GCP-managed certificates (auto-renewed) |
| **Container** | `svlassiev/colorless-days-children` on Docker Hub |
| **Cluster** | GKE `sixty-years-to-death` (`e2-micro`, `europe-north1-a`) |
| **CI/CD** | GitHub Actions → Docker Hub → `kubectl apply` + deploy to GKE |
| **Repo** | [svlassiev/colorless-claude-children](https://github.com/svlassiev/colorless-claude-children) |

---

## What was done

### Phase 1: Build the static site

- Extracted 87 albums from `Main.kt` into `albums.json` (title, folder, count, pathName)
- Rewrote Kotlin/JS (~372 lines) into vanilla JS (`app.js`, ~260 lines)
- CSS copied as-is. HTML structure preserved (same div IDs, class names)
- Dockerized with `nginx:alpine`. Health check at `/healthz`
- Discovered: GCS had 43 folders missing (not 37), 3 had `.JPG` uppercase, Docker image is 92MB uncompressed (not ~7MB)

### Phase 2: Upload photos to GCS

- Renamed `.JPG` → `.jpg` in 3 folders (197, pohod1497, Karelia2007)
- Uploaded all 43 missing folders. Bucket was already publicly readable.

### Phase 3: Publish

- Image pushed as `svlassiev/colorless-days-children:2.0.1` (amd64)
- K8s manifests in `k8s/` with modern API versions
- Deployment: 16M request / 32M limit (was 10M / 2560M), liveness/readiness probes
- GitHub Actions auto-deploy: build → Docker Hub push → `kubectl apply -f k8s/` → deploy
- GCP service account `github-deploy` with `roles/container.developer`
- Cluster was memory-starved (`e2-micro`, 625Mi allocatable, 98% committed). Scaled down `hiking-api`, `sixty-lds`, `mongo-cdc`. Used `scale 0 → 1` to break rolling update deadlock.

### Phase 4: GCS-only albums

- Added 9 new albums with camera filenames (IMG_XXXX, DSC_XXXX, DSCN0XXX)
- Created `albums-files.json` with explicit file lists (~160 photos). `useFiles` flag in `albums.json`.
- Thumbnails: `_thumbnail` suffix for new albums, `1_` prefix for old albums
- Albums placed in chronological order. 96 total (87 original + 9 new).
- UUID-named folders in GCS belong to `hiking-api` — left alone.

### Phase 5: Push to GitHub

- Repo: [svlassiev/colorless-claude-children](https://github.com/svlassiev/colorless-claude-children) (public)
- GitHub Actions secrets: `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, `GCP_SA_KEY`
- Auto-deploy verified. Required `GITHUB_TOKEN=` prefix and `gh auth refresh --scopes workflow`.

### Post-launch improvements

- Switched to GCP-managed TLS certificates. Cleaned up cert-manager namespace, tiller, 6 CRDs, 2 stale API services.
- HTTP → HTTPS redirect via GKE FrontendConfig (301)
- Preview images constrained to viewport (`max-width: 100%`). New-style albums use `_1024` resized variant.
- Mobile support: viewport meta tag, CSS grid layout (4 columns desktop, 2 columns mobile at 600px breakpoint). Replaced table-based grid with div + CSS grid.
- CI/CD applies all `k8s/` manifests on every push — infra changes deploy automatically.

---

## Outcome

| Metric | Before | After |
|--------|--------|-------|
| Docker image size | 2.6 GB | 92 MB (uncompressed) |
| Memory limit | 2560 MB | 32 MB |
| Build time | Minutes (Kotlin compile) | Seconds (COPY only) |
| Photo storage | In Docker image | GCS bucket |
| Startup time | Slow (large image pull) | ~1 second |
| Albums | 87 | 96 |
| TLS | Expired cert-manager (broken since April 2022) | GCP-managed (auto-renewed) |
| CI/CD | Manual docker push + kubectl | GitHub Actions auto-deploy |
| Mobile | Not supported | Responsive 2/4 column grid |

---

## GCP cost analysis

Project `thematic-acumen-225120` hosts colorless-days-children, hiking-api, hiking-ui, and hiking-mongo.

### Cost history

Note: GKE Standard control plane ($0.10/hr = $73/mo) is covered by free tier ($74.40/mo credit for 1 zonal cluster per billing account). It was never an actual cost despite appearing in earlier estimates.

| Date | Monthly cost | What changed | Savings |
|------|-------------|--------------|---------|
| Before 2026-03-16 | **~$76** | Baseline: 6× mongo PVCs, 2× LBs, e2-micro, 100GB disk | — |
| 2026-03-16 | ~$51 | Deleted sixty-lds Ingress, service, deployment, managed cert, static IP | -$25 |
| 2026-03-17 (early) | ~$27 | Deleted all 6 mongo PVCs (600Gi) + StatefulSets. Data dumped locally. | -$24 |
| 2026-03-17 (current) | **~$36** | Upgraded e2-micro→e2-small (+$6), reduced disk 100→30GB (-$7), added 1Gi PVC (+$0.04). All services online. | -$1 net |

Total savings from baseline: **~$40/mo (53% reduction)** while going from 1 running service to 4.

### Current resources and costs (verified from cloud 2026-03-17)

**Compute & storage (actual GCP resources):**

| Resource | Details | On-demand $/mo | With SUD* |
|----------|---------|---------------|-----------|
| GKE control plane | Standard tier, $0.10/hr | $0 | $0 (free tier) |
| e2-small node | 1× `pool-e2-small`, 2 vCPU (shared), 2GB RAM | ~$12.78 | ~$8.95 |
| Boot disk | 30 GB pd-balanced | ~$3.00 | ~$3.00 |
| MongoDB PVC | 1 GB pd-standard | ~$0.04 | ~$0.04 |
| **Compute subtotal** | | **~$15.82** | **~$11.99** |

*SUD = Sustained Use Discount (~30% for full-month usage, applied automatically)

**Networking:**

| Resource | Details | $/mo |
|----------|---------|------|
| Load balancer | 1 Ingress → 2 forwarding rules (HTTP+HTTPS) | ~$18.25 |
| Static IP | `colorless-days-children` (34.95.96.158), in use | $0 |
| 3 backend services | colorless (32507), hiking (30123), hiking-api (30862) | $0 (included in LB) |
| **Networking subtotal** | | **~$18.25** |

**Storage & other:**

| Resource | Details | $/mo |
|----------|---------|------|
| GCS bucket | 4.23 GiB standard, `gs://colorless-days-children/` | ~$0.08 |
| ~~Disk snapshot~~ | Deleted. Local backups in `hiking-api/data/` | $0 |
| Logging/monitoring | GKE system logs + metrics (under free tier) | ~$0 |
| **Other subtotal** | | **~$2.68** |

| | On-demand | With SUD |
|---|-----------|---------|
| **Monthly total** | **~$36.75** | **~$32.92** |

### Running workloads

```
PODS (4 running on e2-small node):
  colorless-days-children   16M req / 32M lim     nginx static gallery
  hiking                    10M req / 128M lim    nginx Vue SPA
  hiking-api                256Mi req / 384Mi lim  Ktor JVM API
  hiking-mongo              128Mi req / 256Mi lim  MongoDB 7 standalone

NODE MEMORY: 85% allocated (~1.2Gi of ~1.4Gi), 15% headroom
SITES:       serg.vlassiev.info (200), /hiking (200), /hiking-api/folders (39 albums)
```

### Autopilot evaluation (2026-03-17) — decided against

GKE Autopilot was evaluated and rejected:
- **Cannot convert in-place** — requires new cluster, migrate workloads, recreate secrets, wait for cert provisioning, update CI/CD
- **Minimum pod resources: 250m CPU + 512Mi memory per pod** — our pods request 10-256Mi; Autopilot bumps all to 512Mi minimum
- **Compute cost: ~$10/pod × 4 pods = ~$40/mo** vs current e2-small at ~$13/mo for all pods
- **3x more expensive** with zero benefit for these workloads
- **Decision: stay on GKE Standard.** Free tier covers control plane. Right-sized node is cheapest.

### Remaining optimization opportunities

| Action | Savings | Effort | Notes |
|--------|---------|--------|-------|
| Delete GCP snapshot | ~$2.60/mo | Low | Clean BSON backup exists locally (2MB). Snapshot is redundant. |
| Spot/preemptible node | ~$8/mo | Low | e2-small spot ≈ $3.80/mo vs $12.78 on-demand. Risk: node can be preempted (5-30min downtime). Acceptable for personal photo gallery? |
| | | | |
| **Total possible** | **~$10.60/mo** | | → could bring total to ~$26/mo (on-demand) or ~$22/mo (spot) |

**Irreducible floor (~$21/mo):**

| Cost | Amount | Why it can't be reduced |
|------|--------|------------------------|
| Load balancer | ~$18/mo | Required for TLS with GCP-managed certificates via GKE Ingress. Only way to eliminate: move off GKE Ingress entirely (Cloud Run, or self-managed TLS with Let's Encrypt). |
| Boot disk 30GB | ~$3/mo | Minimum practical GKE node disk. |
| GCS + PVC | ~$0.12/mo | Negligible. |

**The load balancer ($18/mo) is 55% of the total bill.** It's the single largest cost and the hardest to eliminate. The only alternatives are:
1. **Cloud Run** — each service gets free TLS, but path-based routing (`/hiking/*`, `/hiking-api/*`) requires an external Application Load Balancer (~$18/mo anyway) or URL masks
2. **Self-managed TLS** — run cert-manager or nginx with Let's Encrypt on the node, expose via NodePort + DNS. Eliminates the GCP LB but adds operational complexity
3. **Accept it** — $18/mo for managed TLS + HTTPS redirect + path routing is reasonable for production hosting

---

## TODO

### Site
- [ ] Lazy loading for thumbnail grid images
- [x] ~~Social media link previews (Open Graph meta tags)~~ — implemented. See below.
- [ ] Share button UX polish (share icon styling, toast notifications)

### Social media link previews (Open Graph)

**Goal:** Share photo/album links in Telegram, Facebook, VK, etc. and see a rich preview card with the image, title, and description.

**How it works:** Social media crawlers fetch the URL and read `<meta property="og:...">` tags from `<head>`. Crawlers don't execute JavaScript — tags must be in the server-rendered HTML.

**Shareable URLs:**

| URL | What | OG image | Redirects to | Status |
|-----|------|----------|-------------|--------|
| `serg.vlassiev.info/share/{folder}` | Colorless album | First photo in album | `/folderIndex.html?folder={folder}` | DONE |
| `serg.vlassiev.info/share/{folder}/{n}` | Colorless photo | Resolved from albums.json | `/preview.html?folder={folder}&n={n}` | DONE |
| `serg.vlassiev.info/share/hiking/album/{listId}` | Hiking album | First image V1024 variant | `/hiking/timeline` | DONE (no deep link yet) |
| `serg.vlassiev.info/share/hiking/image/{imageId}` | Hiking photo | Image V1024 variant | `/hiking` | DONE (no deep link yet) |

**Architecture:** All `/share/*` routes are served by hiking-api (Ktor).

```
Ingress: serg.vlassiev.info/share/* → hiking-api

hiking-api returns minimal HTML:
  <head>
    <meta property="og:title" content="Kailash — Photo 24">
    <meta property="og:image" content="https://storage.googleapis.com/...">
    <meta property="og:type" content="website">
    <meta name="twitter:card" content="summary_large_image">
  </head>
  <body><script>window.location = '/#photo=Kailash/24'</script></body>
```

**Implementation details:**
- Colorless previews: no DB needed — GCS URL is `{folder}/{n}.jpg` by convention, folder name is the title
- Hiking previews: MongoDB lookup for album name + image variant URL
- Shared HTML template, ~50 lines of Kotlin (3-4 routes)
- One new Ingress path rule: `/share/*` → hiking-api
- No changes to colorless-days-children or hiking-ui codebases

**Trade-off:** Colorless previews depend on hiking-api being up. Acceptable since both run on the same node.

**Implementation in hiking-api:**
```
src/
  api/
    View.kt              ← existing public API
    Edit.kt              ← existing admin API
    share/
      Share.kt           ← routing: shareApi("/share", ...) — delegates to providers
      ShareHtmlRenderer.kt ← shared OG HTML template (used by both)
      ColorlessShareProvider.kt ← /share/{folder}, /share/{folder}/{n} — no DB, GCS URL from path
      HikingShareProvider.kt    ← /share/hiking/album/{id}, /share/hiking/image/{id} — MongoDB lookup
```
Colorless and hiking logic are structurally separated into providers. `Share.kt` wires routing, `ShareHtmlRenderer.kt` is the shared HTML template. One new Ingress path rule: `/share/*` → hiking-api.

### Infrastructure
- [x] ~~Evaluate GKE Autopilot~~ — rejected (3-5x more expensive). See above.
- [x] ~~Investigate `hiking-api` mongo PVCs~~ — data dumped, PVCs deleted, MongoDB restored. See HIKING-PLAN.md.
- [x] ~~Consolidate or remove `sixty-lds` Ingress~~ — deleted. Saved ~$25/month.
- [x] ~~Reduce boot disk~~ — 100GB → 30GB via node pool replacement. Saved ~$7/mo.
- [x] ~~Bring hiking back online~~ — all 4 pods running, sites verified. See HIKING-PLAN.md.
- [x] ~~Delete mongo GCP snapshot~~ — deleted. Local backups in `hiking-api/data/` (raw tar + BSON). Saves ~$2.60/mo.
- [ ] Evaluate spot/preemptible node (saves ~$8/mo, risk: occasional downtime)
- [ ] `metrics.k8s.io/v1beta1` kubectl warnings — stale API from old cert-manager

### Content
- [ ] Migrate `albums-files.json` to a database for dynamic album management
- [ ] Verify album titles for the 9 new albums (currently guessed from folder names)
