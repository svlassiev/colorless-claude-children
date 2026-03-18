# Hiking Photo Gallery — Modernization Plan

## Architecture

Keeping **frontend + backend + MongoDB** — modernizing the stack, not flattening it.

```
                         Target Architecture
                         ==================

Browser → nginx (Vue 3 SPA) → Ktor 3 API (Kotlin/JDK 21) → MongoDB 7
                                       ↕
                                 Google Cloud Storage
                               gs://colorless-days-children/

CI/CD: GitHub Actions → Docker Hub → kubectl apply + deploy to GKE
```

| Component | Current | Target |
|-----------|---------|--------|
| **Domain** | serg.vlassiev.info/hiking | same |
| **GCP project** | thematic-acumen-225120 | same |
| **GKE cluster** | sixty-years-to-death | same |
| **UI image** | svlassiev/hiking | same |
| **API image** | svlassiev/hiking-api | same |
| **Repos** | svlassiev/hiking-ui, svlassiev/hiking-api | same |

---

## Analysis Summary

### Critical Bugs (found 2026-03-16)

| # | Location | Issue | Status |
|---|----------|-------|--------|
| 1 | `hiking-api` `Cache.kt:39-42` | `timelineHead` assigned twice, `timelineTail` never set → tail endpoint returns empty | FIXED |
| 2 | `hiking-ui` `mutations.js` (9 locations) | `state = {...state, lists: X}` reassigns local param, not Vuex store → reactivity broken | FIXED |
| 3 | `hiking-ui` `mutations.js:166` | `Array.includes()` with callback always returns false → dead branch | FIXED |
| 4 | `hiking-ui` `mutations.js:150-152` | `DELETE_IMAGE.SUCCESS` returns `images.filter()` (array) instead of list object | FIXED |

### Memory / Performance Issues

| # | Location | Issue | Status |
|---|----------|-------|--------|
| 5 | `hiking-api` `Cache.kt:44-51` | All images loaded into RAM at startup (unbounded) | Phase 3 |
| 6 | `hiking-api` `ImageClient.kt:17-33` | `loadTimelineData` materializes entire DB into memory | Phase 3 |
| 7 | `hiking-api` `ImageManager.kt:109` | `FileInputStream` never closed → FD leaks | Phase 3 |
| 8 | `hiking-api` `logback.xml` | Root log level `trace` → massive log volume | Phase 3 |
| 9 | `hiking-ui` `SimpleTimeline.vue:94` | O(n×m) image lookup via `.find()` on every render | FIXED |
| 10 | `hiking-ui` `SimpleTimeline.vue:21-47` | No virtual scrolling, all entries in DOM simultaneously | Phase 4 |
| 11 | `hiking-ui` `ImageItem.vue:17` | Full-res images on desktop, no srcset | FIXED |
| 12 | `hiking-ui` `EditImageListTimelineItems.vue:124` | Full lodash import (~70KB) for one `debounce` call | FIXED |
| 13 | `hiking-ui` `main.js:7` | Full Firebase SDK import (hundreds of KB) | FIXED |
| 14 | `hiking-ui` `main.js:1` | Deprecated `babel-polyfill` (~90KB) | FIXED |
| 15 | `hiking-ui` `k8s/hiking.yml:47` | 2560M memory limit for nginx static server | FIXED → 128M |
| 16 | `hiking-api` k8s | 2560M memory limit for API | FIXED → 512M |
| 17 | `hiking-api` k8s/mongo.yml | 3 MongoDB replicas × 100Gi SSDs + sidecar | FIXED → 1 replica, lower memory |

### Security Issues

| # | Location | Issue | Status |
|---|----------|-------|--------|
| 18 | `hiking-api` `Edit.kt` | Firebase ID token as query parameter | Phase 5 |
| 19 | `hiking-api` k8s/mongo.yml | `default` SA has `cluster-admin` | FIXED (removed) |
| 20 | `hiking-api` `Application.kt:39` | CORS `anyHost()` | Phase 5 |
| 21 | `hiking-ui` `main.js:12-20` | Firebase config hardcoded | Low risk (Firebase keys are public by design) |

---

## Framework Upgrade Targets

### hiking-api (Kotlin/Ktor)

| Dependency | Current | Target | Notes |
|-----------|---------|--------|-------|
| **Kotlin** | 1.3.61 | 2.1.x | Major language changes (coroutines, sealed, value classes) |
| **Ktor** | 1.2.4 | 3.1.x | Complete plugin API rewrite in 2.0. Ktor 3.x uses Kotlin 2.x |
| **JDK** | 8 | 21 (LTS) | Alpine-based Eclipse Temurin |
| **Gradle** | 4.10 | 8.12+ | `compile` → `implementation`, Kotlin DSL optional |
| **Shadow** | 2.0.1 | 8.x | Fat JAR packaging |
| **KMongo** | 3.11.0 | mongodb-driver-kotlin 5.x | KMongo deprecated → official Kotlin driver |
| **google-cloud-storage** | 1.90.0 | 2.x | Minor API changes |
| **firebase-admin** | 8.1.0 | 9.x | |
| **logback** | 1.2.1 | 1.5.x | |
| **Repositories** | jcenter + bintray | mavenCentral | jcenter/bintray are shut down |

### hiking-ui (Vue)

| Dependency | Current | Target | Notes |
|-----------|---------|--------|-------|
| **Vue** | 2.6 | 3.5.x | Composition API, `<script setup>`, Teleport |
| **Vuetify** | 2.3 | 3.7.x | Complete rewrite, some components renamed |
| **Vuex** | 3.5 | Pinia 3.x | Official Vue 3 state management |
| **Vue Router** | 3.4 | 4.5.x | Minor API changes |
| **Build tool** | Vue CLI 4 / Webpack 4 | Vite 6.x | 10-50x faster HMR |
| **Firebase** | 9.6 (compat) | 11.x (modular) | Tree-shakable modular API |
| **axios** | 0.25 | 1.7.x | |
| **lodash** | 4.17 (full) | lodash-es (tree-shake) or just `lodash/debounce` | Already partially fixed |
| **vue-moment** | 4.1 (moment.js) | dayjs | moment.js in maintenance, dayjs is 2KB |
| **vue-observe-visibility** | 0.4 | native IntersectionObserver | Vue 3 directive or composable |
| **node-sass** | 7.0 | (removed) | Deprecated; `sass` (dart-sass) already present |
| **babel-polyfill** | 6.26 | (removed) | Already removed from import |
| **es6-promise** | 4.2 | (remove) | Unused, native Promise everywhere |
| **vue-animated** | 0.1 | (remove) | Unused |
| **Dockerfile** | `node:lts-alpine` | `node:22-alpine` | Pinned in Phase 2 |

---

## Phases

### Phase 1: Quick Wins — DONE

Bug fixes, memory limits, import optimizations.

### Phase 2: GitHub Actions CI/CD — DONE (pipeline works, deployment blocked)

**What was done:**
- [x] 2.1 Add `.github/workflows/deploy.yml` to `hiking-api`
- [x] 2.2 Add `.github/workflows/deploy.yml` to `hiking-ui`
- [x] 2.3 Configure GitHub secrets on both repos
- [x] 2.4 Commit Phase 1 fixes + workflow files + build fixes
- [x] 2.5 Push to master → CI/CD triggered

**Build fixes required (both repos were unbuildable before our changes):**
- `hiking-api`: `openjdk:8-jre-alpine` and `gradle:4.9-jdk8` removed from Docker Hub. Fixed with `eclipse-temurin:8-jdk` / `eclipse-temurin:8-jre`. Replaced dead jcenter/bintray repos with mavenCentral in `build.gradle`. Removed deprecated JVM flags.
- `hiking-ui`: `node-sass` fails on modern Node (needs Python + node-gyp). Removed — `sass` (dart-sass) already present. Removed stale `webpack@^5` override (conflicts with vue-cli-service 4.x bundled webpack 4). Pinned eslint to 7.x (`CLIEngine` removed in 8). Pinned `node:22-alpine`. Skipped stale lockfile in Docker build (`npm install --legacy-peer-deps` from package.json only).

**CI/CD status:**
- Docker images build and push to Docker Hub: WORKING
- K8s manifest apply: WORKING (after fixing immutable StatefulSet fields)
- Pod deployment: BLOCKED — `e2-micro` node doesn't have enough memory

**Why deployment is blocked:**

The GKE cluster `sixty-years-to-death` has a single `e2-micro` node with ~625Mi allocatable memory. The hiking stack needs:

| Service | Memory request | Memory limit |
|---------|---------------|-------------|
| colorless-days-children | 16M | 32M |
| hiking (UI) | 10M | 128M |
| hiking-api | 10M | 512M |
| mongo-cdc | 64M | 512M |
| mongo-cdc-sidecar | 10M | 256M |
| **Total requests** | **~110M** | |
| **Total limits** | | **~1.4 GB** |

Requests (110M) fit for scheduling, but actual memory usage will likely exceed the 625Mi allocatable. The rollout timed out at 120s — pods may be stuck Pending or OOMKilled.

**MongoDB data safety note:** Kept `--replSet rs0`, sidecar, and 100Gi PVC in mongo.yml. Existing data was written in replica set mode — removing replSet would make mongod refuse to start. The sidecar reconfigures the replica set for the single remaining member. PVCs for `mongo-cdc-1` and `mongo-cdc-2` remain orphaned (data preserved, ~$16/month waste).

**To unblock deployment:**
- [ ] Check cluster state: `kubectl get pods`, `kubectl top nodes`
- [ ] Option A: Reduce hiking-api memory limit further (256M might work if the unbounded cache is small)
- [ ] Option B: Scale down something else temporarily
- [x] Option D: Delete orphaned mongo PVCs — DONE (saves ~$24/month)
- [ ] Verify site works at serg.vlassiev.info/hiking (after Phase 2.5)

### Phase 2.5: Unblock Deployment + Cost Reduction

**Current monthly cost (after PVC deletion):**

| Resource | Cost | Notes |
|----------|------|-------|
| GKE control plane | $0 | $74/mo covered by free tier (1 zonal cluster per billing account) |
| e2-micro node | ~$6 | 625Mi allocatable, ~600Mi used by system pods |
| Boot disk 100GB | ~$10 | Default GKE size, could be 10GB |
| Load balancer | ~$18 | 1 Ingress (HTTP+HTTPS forwarding rules) |
| GCS bucket | ~$0.10 | 4.5GB storage |
| ~~Disk snapshot~~ | ~~$2.60~~ | Deleted. Local copies in `hiking-api/data/` |
| **Total** | **~$37** | Down from ~$150 |

**Remaining cost reduction opportunities:**

| Action | Savings | Effort | Notes |
|--------|---------|--------|-------|
| Reduce boot disk 100GB → 30GB | ~$7/mo | Low | Resize via node pool recreation |
| ~~Delete snapshot after BSON conversion~~ | ~~$2.60/mo~~ | Done | DONE |
| Cloud Run for colorless (kill cluster) | ~$24/mo | High | Only if hiking moves off K8s too |

**Autopilot vs Standard — decision: stay on Standard**

GKE Autopilot was evaluated and rejected for this workload:
- Autopilot enforces **250m CPU + 512Mi memory minimum per pod**
- Our 3 pods (colorless 16M, hiking 10M, hiking-api 10M) would each be bumped to 512Mi
- Autopilot compute cost: ~$10/pod/mo × 3 = **~$30/mo**
- Current Standard e2-micro: **~$6/mo** for all pods combined
- Autopilot is **5x more expensive** with no benefit for tiny workloads
- Autopilot cannot be converted in-place — requires creating a new cluster, migrating workloads, recreating secrets, waiting for cert provisioning, updating CI/CD
- Ingress, ManagedCertificates, FrontendConfig, static IPs all work the same on Autopilot — that's not the blocker. Cost is.

**Conclusion:** GKE Standard with a right-sized node is the best fit. The free tier covers the $74/mo control plane for one zonal cluster. The only variable is node size.

**Memory problem — why pods can't schedule:**

The e2-micro node has 625Mi allocatable. System pods (kube-dns, fluentbit, metrics, etc.) consume ~600Mi in requests. Only ~25Mi remains — not enough for hiking-api or a database.

**Why both paths require e2-small**

The e2-micro node has 625Mi allocatable. System pods consume ~595Mi. Only ~30Mi free.
hiking-api is a JVM application — it needs at least 128-256Mi even without MongoDB.
**There is no way to fit a JVM on e2-micro alongside the GKE system pods.**

Both paths require upgrading from e2-micro to e2-small (+$6/mo).

| Workload | Memory request | e2-micro (625Mi) | e2-small (~1750Mi) |
|----------|---------------|-----------------|-------------------|
| System pods | ~595Mi | ~595Mi | ~595Mi |
| colorless-days-children | 16Mi | 16Mi | 16Mi |
| hiking (nginx) | 16Mi | — | 16Mi |
| hiking-api (JVM) | 256Mi | — | 256Mi |
| MongoDB (Path A only) | 256Mi | — | 256Mi |
| **Total** | | 611Mi (full) | 883-1139Mi (fits) |

**Database strategy — two paths**

---

**Path A: Quick fix — MongoDB on e2-small (keep existing code)**

Upgrade to e2-small, deploy MongoDB, restore data. Zero code changes.

| Step | What | Notes |
|------|------|-------|
| A.1 | Create new node pool with e2-small, 30GB disk | `gcloud container node-pools create ...` |
| A.2 | Cordon + drain old e2-micro pool | Moves colorless-days-children to new node |
| A.3 | Delete old node pool | Frees the e2-micro + 100GB disk |
| A.4 | Convert local dump to BSON | Start local mongo, strip replset metadata, `mongodump` |
| A.5 | Deploy MongoDB (standalone, no replica set) | Fresh mongo:7 with 5Gi PVC |
| A.6 | Restore data | `mongorestore` from BSON into the new MongoDB pod |
| A.7 | Scale up hiking-api and hiking | Push to repos → CI/CD triggers deploy |
| A.8 | Verify site works | serg.vlassiev.info/hiking |

**Cost:** ~$43/mo (+$6 for e2-small, -$7 for smaller boot disk).
**Timeline:** Can be done in one session.
**Pros:** Site back online fast, no code changes, proven stack.
**Cons:** Still running MongoDB in K8s (operational burden), keeps legacy code.

---

**Path B: Modernize — Firestore on e2-small (change data layer)**

Upgrade to e2-small, replace MongoDB with Firestore. No DB pod needed — more room for future workloads.

| Step | What | Notes |
|------|------|-------|
| B.1 | Create new node pool with e2-small, 30GB disk | Same as Path A |
| B.2 | Cordon + drain + delete old pool | Same as Path A |
| B.3 | Convert local dump to BSON | Start local mongo, strip replset metadata, `mongodump` |
| B.4 | Write migration script: BSON → Firestore | Read BSON, write to Firestore collections |
| B.5 | Rewrite hiking-api data layer | Replace `Repository.kt` (KMongo) with Firestore SDK calls |
| B.6 | Test locally, push → auto-deploy | |
| B.7 | Verify site works | serg.vlassiev.info/hiking |

**Data model mapping (MongoDB → Firestore):**
```
MongoDB                    Firestore
─────────                  ─────────
db.imagesLists collection  → /imagesLists/{listId}
db.images collection       → /images/{imageId}
```

**Cost:** ~$40/mo (+$6 for e2-small, -$7 for smaller boot disk, $0 for Firestore free tier).
**Timeline:** Part of Phase 3 (Ktor upgrade). Requires code changes.
**Pros:** No DB to manage, free tier, more headroom on node.
**Cons:** Vendor lock-in (Firestore), requires code rewrite, can't use standard Mongo tooling.

---

**Steps completed:**
- [x] 2.5.1 Created new e2-small node pool with 30GB boot disk
- [x] 2.5.2 Cordoned, drained, deleted old e2-micro pool
- [x] 2.5.3 Verified colorless-days-children migrated to new node (HTTP 200)
- [x] 2.5.4 Converted MongoDB dump to clean BSON (39 albums, 2208 images, 2MB)
- [x] 2.5.5 Deployed lightweight standalone MongoDB (mongo:7, 1Gi PVC, 256Mi limit)
- [x] 2.5.6 Restored data via mongodump/mongorestore (old mongo:4.2 → new mongo:7)
- [x] 2.5.7 Updated hiking-api connection string and memory limits
- [x] 2.5.8 All 4 pods running: colorless, hiking, hiking-api, hiking-mongo
- [x] 2.5.9 Verified: serg.vlassiev.info (200), /hiking (200), /hiking-api/folders (39 albums)
- [x] 2.5.10 GCP snapshot deleted (saves ~$2.60/mo). Local backups retained:
  - `hiking-api/data/mongo-cdc-raw.tar.gz` — raw WiredTiger snapshot (241MB)
  - `hiking-api/data/mongo-raw/` — extracted raw data (640MB)
  - `hiking-api/data/bson-dump/colorless-days-children/` — clean BSON export (2MB, 39 albums + 2208 images)

**Clean BSON backup saved locally:** `hiking-api/data/bson-dump/colorless-days-children/` (2MB).
Can restore to any MongoDB with: `mongorestore --db colorless-days-children /path/to/bson-dump/colorless-days-children`

### Phase 3: Upgrade hiking-api (Kotlin/Ktor)

Modernize the backend in-place. The API surface stays the same — same endpoints, same GCS bucket.

- [ ] 3.1 Upgrade Gradle 4.10 → 8.x (wrapper + build script, `compile` → `implementation`)
- [ ] 3.2 Upgrade Kotlin 1.3 → 2.1, JDK 8 → 21
- [ ] 3.3 Upgrade Ktor 1.2 → 3.x (rewrite plugin installation, routing DSL)
- [ ] 3.4 Replace KMongo with official MongoDB Kotlin driver (KMongo deprecated)
- [ ] 3.5 Upgrade google-cloud-storage, firebase-admin, logback
- [ ] 3.6 Fix remaining issues: FD leaks (`ImageManager.kt`), log level (`logback.xml` trace→info), unbounded cache, CORS
- [ ] 3.7 Update Dockerfile (JDK 21 Alpine — smaller image, proper multi-arch)
- [ ] 3.8 Test locally, push → auto-deploy

### Phase 4: Upgrade hiking-ui (Vue 3)

Modernize the frontend. Same visual design, same API endpoints.

- [ ] 4.1 Scaffold Vite + Vue 3 project, migrate source files
- [ ] 4.2 Vuetify 2 → Vuetify 3 (component API changes, theme system)
- [ ] 4.3 Vuex → Pinia (straightforward 1:1 migration)
- [ ] 4.4 Vue Router 3 → 4
- [ ] 4.5 Firebase compat → modular API
- [ ] 4.6 vue-moment → dayjs
- [ ] 4.7 vue-observe-visibility → native IntersectionObserver composable
- [ ] 4.8 Remove dead deps (es6-promise, vue-animated, core-js)
- [ ] 4.9 Add virtual scrolling for the timeline (vue-virtual-scroller or manual)
- [ ] 4.10 Deep linking + scroll-to-image for share URLs (see below)
- [ ] 4.11 Test locally, push → auto-deploy

**4.10 Deep linking for shared photos/albums:**

Currently `/share/hiking/image/{id}` redirects to `/hiking` (top of feed) because the Vue SPA has no deep linking support. The user who clicks a shared link sees the timeline but not the specific photo.

Implementation plan:
- Share redirect changes from `/hiking` to `/hiking?image={imageId}` (or `/hiking?album={listId}`)
- `SimpleTimeline.vue` reads query param on mount, finds the image in `timelineEntries`, and scrolls to it via `$vuetify.goTo()` or `element.scrollIntoView()`
- `HikingTimeline.vue` reads album query param, auto-expands that album's `ListTimeline`
- Requires images to be loaded before scrolling — may need to load all images up to the target first, then scroll
- Update share redirect in `hiking-api/Share.kt` once deep linking works

### Phase 5: Polish (future)

- [ ] Move Firebase ID token from query param to Authorization header
- [ ] Restrict CORS to `serg.vlassiev.info`
- [ ] Move Firebase config to env vars (build-time injection)
- [ ] Add liveness/readiness probes to API and UI deployments
- [ ] Clean up raw WiredTiger dump (241MB tar) — BSON backup is sufficient (2MB)

---

## Progress Log

### Phase 1 Log

#### 2026-03-16: Quick wins implemented

**hiking-api changes:**
- **Cache bug fixed** (`Cache.kt:41`): `timelineHead =` → `timelineTail =`. The tail endpoint was silently returning empty data.
- **API memory limit**: 2560M → 512M (`hiking-api.yml`)
- **MongoDB simplified** (`mongo.yml`): 3 → 1 replica, memory 2560M → 512M/256M, removed cluster-admin ClusterRoleBinding. Kept replSet, sidecar, 100Gi PVC (data safety).

**hiking-ui changes:**
- **Vuex mutations fixed** (`mutations.js`): All `state = {...state, X}` no-ops replaced with direct `state.X =` assignments. Also fixed `DELETE_IMAGE.SUCCESS` which returned `images.filter()` (an array) instead of the list object.
- **includes → some** (`mutations.js`): `lists.includes(cb)` always returned false; changed to `lists.some(cb)`.
- **UI memory limit**: 2560M → 128M (`hiking.yml`).
- **Nginx cache headers** (`nginx.conf`): Added `Cache-Control: immutable, max-age=1y` for hashed static assets, `no-cache` for HTML.
- **Lodash**: `import lodash` → `import debounce from 'lodash/debounce'` (saves ~65KB gzipped).
- **Firebase**: `import firebase from "firebase"` → `firebase/compat/app` + `firebase/compat/auth` in main.js, Login.vue, router.js, Edit.vue (drops unused Firestore/Database/Storage/Analytics from bundle).
- **babel-polyfill**: Removed deprecated `import 'babel-polyfill'` from main.js (saves ~90KB).
- **Responsive images** (`ImageItem.vue`, `ListTimeline.vue`): Added breakpoint-based variant selection — xs→V800, sm→V1024, md+→V2048. Previously loaded full-res originals on all non-xs viewports.
- **O(1) image lookup** (`SimpleTimeline.vue`): Added computed `imageMap` (Map keyed by imageId), replaced `.find()` with `.get()`.
- Removed 4 `console.log` debug statements from `actions.js`.

### Phase 2 Log

#### 2026-03-16: CI/CD pipelines added, build rot fixed

**Infrastructure rot discovered — both repos were unbuildable:**
- `hiking-api`: Docker base images (`openjdk:8-jre-alpine`, `gradle:4.9-jdk8`) removed from Docker Hub. `jcenter()` and `kotlin.bintray.com/ktor` repos dead. Fixed with Eclipse Temurin images and mavenCentral.
- `hiking-ui`: `node-sass` fails on Node 22+ (needs native compilation). `webpack@^5` in package.json conflicts with `@vue/cli-service@4.x` (webpack 4). `eslint@^8` removed `CLIEngine` needed by `eslint-loader`. Fixed by removing node-sass, webpack override, and pinning eslint 7.x. Pinned `node:22-alpine`, skip stale lockfile in Dockerfile.

**CI/CD deployed to both repos:**
- `svlassiev/hiking-api` — `.github/workflows/deploy.yml`, secrets configured
- `svlassiev/hiking-ui` — `.github/workflows/deploy.yml`, secrets configured
- Both pipelines: checkout → Docker build+push → GCP auth → kubectl apply → deploy
- Secrets: `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, `GCP_SA_KEY` (shared with colorless-claude-children)

**First CI run results:**
- hiking-api: Docker build+push OK. First run failed on immutable StatefulSet `volumeClaimTemplates` change (100Gi→10Gi). Fixed in follow-up commit (kept 100Gi). Second run: build+push OK, manifests applied OK, **rollout timed out** (node memory).
- hiking-ui: Docker build+push OK, manifests applied OK, **rollout timed out** (node memory).

**Commits pushed:**
- `hiking-api`: `f8cade2` (Phase 1 fixes + CI/CD), `4b1a74a` (mongo.yml data safety fix)
- `hiking-ui`: `57ef6ae` (Phase 1 fixes + build fixes + CI/CD)

**Not yet resolved:** Deployment blocked by `e2-micro` memory constraints. Images are on Docker Hub and manifests are applied — pods will start once cluster has capacity.

#### 2026-03-17: MongoDB data dump completed

**Data safely backed up locally.** The cloud MongoDB doesn't need to stay running.

**What was done:**
- Created GCE disk snapshot `mongo-cdc-backup-2026-03-17` from PVC `mongo-cdc-persistent-storage-mongo-cdc-0`
- Spun up temp VM, mounted snapshot as disk, tarred the raw WiredTiger data directory
- Downloaded to local: `hiking-api/data/mongo-cdc-raw.tar.gz` (241MB compressed, 640MB raw)
- Cleaned up temp VM and temp disk. GCP snapshot later deleted (local copies sufficient).
- Added `data/` to `hiking-api/.gitignore`

**Data size:** 640MB raw (mostly oplog at 148MB + collections). Actual application data is small — this is a photo metadata database (image URLs, album structure, EXIF data). Photos themselves are in GCS.

**Format:** Raw MongoDB 4.2 WiredTiger data files with replica set (`rs0`) config. To restore:
```bash
# Extract and mount into mongo:4.2 container
tar xzf hiking-api/data/mongo-cdc-raw.tar.gz -C /path/to/mongo-data
docker run -v /path/to/mongo-data:/data/db -p 27017:27017 mongo:4.2 mongod --replSet rs0
# Then force single-member reconfig (version counter is at INT32_MAX from sidecar):
# mongo --eval 'var cfg={_id:"rs0",version:1,members:[{_id:0,host:"localhost:27017"}]};rs.reconfig(cfg,{force:true})'
```

**Note:** Replica set config version is stuck at INT32_MAX (2147483647) due to the k8s sidecar having force-reconfigured thousands of times. A fresh `rs.reconfig` with `{force:true}` should work locally but needs the version reset handled. Alternative: start mongod without `--replSet` and use `--repair` to convert to standalone.

**Cloud PVC status:** 6 PVCs deleted (see below). Saves ~$24/month.

#### 2026-03-17: PVCs deleted, cost reduction

**Deleted all 6 MongoDB PVCs** (600Gi total, ~$24/month):
- `mongo-persistent-storage-mongo-{0,1,2}` — orphaned from old `mongo` StatefulSet (already deleted)
- `mongo-cdc-persistent-storage-mongo-cdc-{0,1,2}` — from `mongo-cdc` StatefulSet

**Impact on hiking:** Resolved — data restored to new MongoDB (see below).
