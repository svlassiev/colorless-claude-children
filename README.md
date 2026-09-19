# colorless-claude-children

Source code for [serg.vlassiev.info](http://serg.vlassiev.info) — a personal photo gallery that's been around for 20+ years.

This is a rewrite of [colorless-days-children](https://github.com/svlassiev/colorless-days-children) (Kotlin/JS, 2.6GB Docker image with all photos baked in) into a lightweight static site (~7MB image) that loads photos from Google Cloud Storage.

## How it works

- Static HTML/CSS/vanilla JS served by nginx
- Photos stored in GCS bucket `gs://colorless-days-children/`
- Album metadata in `albums.json` (sequential naming) and `albums-files.json` (camera filenames)
- Deployed to GKE cluster in `thematic-acumen-225120` project
- Domain: `serg.vlassiev.info`

## Local development

```bash
docker build -t colorless-claude-children .
docker run -p 8080:80 colorless-claude-children
# open http://localhost:8080
```

For GKE deployment, build for amd64:

```bash
docker buildx build --platform linux/amd64 --load -t svlassiev/colorless-days-children:2.0.1 .
```

## Deployment

Pushes to `main` trigger GitHub Actions workflow (`.github/workflows/deploy.yml`) that:

1. Builds Docker image for linux/amd64
2. Pushes to Docker Hub as `svlassiev/colorless-days-children`
3. Applies all K8s manifests from `k8s/` (infrastructure changes)
4. Deploys new image to GKE cluster `sixty-years-to-death` in `europe-north1-a`

All infrastructure is managed as code — changes to `k8s/` files are applied automatically on push. No manual `kubectl apply` needed.

The workflow needs these repository secrets:
- `DOCKERHUB_USERNAME` — Docker Hub username
- `DOCKERHUB_TOKEN` — Docker Hub access token
- `GCP_SA_KEY` — GCP service account JSON key with `roles/container.developer`

## Adding new albums

Albums appear in `albums.json` order (the home page shows the last 10), so a new album goes at the end. Photos live in `gs://colorless-days-children/<folder>/`. The site never resizes anything: every size it shows must already be in the bucket.

New albums are file-based: `"useFiles": true` in `albums.json`, plus the filenames in `albums-files.json` (list order = display order). For each photo the bucket holds:

| Object | Long edge | Used by |
|---|---|---|
| `<stem>.jpg` | original, as shot | archive; the /explore indexer (reads EXIF date + GPS) |
| `<stem>_thumbnail.jpg` | 80 px | album grid, prev/next in the photo viewer |
| `<stem>_1024.jpg` | 1024 px | photo viewer, share-link preview image |
| `<stem>_800.jpg`, `<stem>_2048.jpg` | 800 / 2048 px | not used by the site today; kept to match earlier albums |

"Long edge" means a portrait photo gets 60×80 and 768×1024. These sizes match what the earlier albums already hold in the bucket (hiking-api's uploader made them).

`scripts/add_album.py` handles the sizes, the ordering, the upload and the JSON edits:

1. **Prerequisites:** ImageMagick 7 (`brew install imagemagick`), plus gcloud signed in as `svlassiev@gmail.com` (see the identity guardrail in `CLAUDE.md`).
2. **Prepare** (local only, free, safe to re-run):
   ```bash
   python3 scripts/add_album.py prepare ~/Pictures/<dir> --title "Атлантический трип"
   ```
   Takes every `.jpg`/`.jpeg` in the directory, so curate the directory first. It picks the album's folder, orders the photos by EXIF capture time, and writes each original plus the four variants to `.album-staging/<folder>/` (gitignored). Then it prints the order and warns about skipped non-JPEG files, photos without a capture time, missing timezone offsets, mixed cameras, GPS data in the originals, and duplicates. Variants have the EXIF rotation baked in and their EXIF/GPS stripped. Originals are uploaded byte-for-byte.
   - **Folder = a new UUID**, e.g. `a6c7fc23-a916-47fe-b3f2-63a93d1848da`. That's the same style as the hiking albums' bucket prefixes. The readable names (`Kailash`, `10tradfall`, …) date from the pre-cloud site and aren't used for new albums. The folder is the URL key and can't change later without breaking links.
   - Ordering uses UTC, so a trip across time zones sorts correctly. Edited copies (Google Photos `~2`) lose the timezone offset, so they borrow the offset of the photo nearest in local time; the table shows borrowed offsets in brackets. When a photo's date disagrees with the camera's file counter (`DSC_5370` taken after `DSC_5372`), `prepare` flags it. Usually the phone clock was still on home time during a flight.
   - Some photos need placing by hand: messenger saves (their date is the save or export time) and the flagged ones above. Edit the `files` list in `.album-staging/<folder>/album.json`. Its order is the album order.
3. **Publish:**
   ```bash
   python3 scripts/add_album.py publish --folder <uuid printed by prepare>
   ```
   Uploads the files to the bucket and checks that every thumbnail and `_1024` image is publicly reachable. Only then does it append the album to `albums.json` and `albums-files.json` and delete the staging directory. It refuses a folder that already holds other objects, and it's safe to re-run after an interrupted upload.
4. **Check locally:** `docker build`/`docker run` (see Local development). Open http://localhost:8080 and check the list, the grid and the viewer.
5. **Release:** commit `albums.json` and `albums-files.json` and push to `main`. CI deploys in about 2 minutes. Browsers cache `albums.json` for 1h (`nginx.conf`).
6. **Restart hiking-api** so share links (`/share/<folder>/<n>`) know the new album. It reads `albums.json` once per pod and keeps it:
   ```bash
   CTX=gke_thematic-acumen-225120_europe-north1-a_sixty-years-to-death
   kubectl --context $CTX rollout restart deployment/hiking-api
   kubectl --context $CTX rollout status deployment/hiking-api
   curl -s https://serg.vlassiev.info/share/<folder>/1 | grep og:image   # expect the _1024 URL
   ```
7. **Optional, and billed:** photos show up in /explore search only after the photo-search index is rebuilt (about $0.0005 per photo, see `photo-search/README.md` "Rebuilding the index") and explore is redeployed, because its image bakes in `albums.json`.

To reorder a published album or hide a photo, edit its list in `albums-files.json`. The bucket objects can stay where they are.

Legacy albums (before ~2011) use sequential names instead: `count` + `pathName` in `albums.json`, `Picture001.jpg`, with a `1_Picture001.jpg` thumbnail. Don't create new ones in this format.

## TLS certificates

HTTPS is handled by [GCP-managed certificates](https://cloud.google.com/kubernetes-engine/docs/how-to/managed-certs) — Google automatically provisions and renews Let's Encrypt certs. No cert-manager running on the cluster.

Managed certificate resources are defined in `k8s/managed-certs.yml` and referenced by the Ingress via the `networking.gke.io/managed-certificates` annotation.

Domains covered:
- `serg.vlassiev.info`, `www.serg.vlassiev.info`
- `xn--60-llcdbsrkrwijg.xn--p1ai` (60летдосмерти.рф)

To check certificate status:
```bash
kubectl get managedcertificates
```

Provisioning takes ~10-15 minutes after first apply. Status goes from `Provisioning` → `Active`.

## CV

A personal CV page is served at [serg.vlassiev.info/cv/](https://serg.vlassiev.info/cv/). Source in `cv/index.html` — self-contained HTML with Computer Modern font, dark/light theme toggle, and Open Graph meta tags for social sharing.

## Sharing

Each photo preview page has a "Поделиться" (Share) link. On mobile it opens the native share sheet, on desktop it copies the share URL to clipboard.

Share URLs (`serg.vlassiev.info/share/{folder}/{n}`) are served by hiking-api, which returns Open Graph meta tags so social media platforms (Telegram, Facebook, VK) show a rich preview card with the photo.

## Project structure

```
index.html          — home page, last 10 albums
all.html            — all albums
folderIndex.html    — album thumbnail grid (4x4 with pagination)
preview.html        — single photo viewer with prev/next
app.js              — all rendering logic
styles.css          — original CSS preserved from the 2003 site
albums.json         — album metadata (96 albums)
albums-files.json   — file lists for camera-filename albums
nginx.conf          — gzip, cache headers, /healthz endpoint
Dockerfile          — nginx:alpine + static files
Dockerfile.explore  — image for the /explore Cloud Run service (see Subprojects)
k8s/                — Kubernetes manifests (deployment, service, ingress, managed certs)
.github/workflows/  — GitHub Actions CI/CD
```

## Subprojects

The repo also hosts a small set of Python packages that power `serg.vlassiev.info/explore` — a search/RAG companion to the static gallery. They live as siblings here so they can share types and be built into one image:

| Package | Purpose |
|---|---|
| [`photo-search/`](photo-search/README.md) | Multimodal RAG over the public photo bucket (Vertex AI multimodal embeddings + Gemini 2.5 Pro). |
| [`log-search/`](log-search/README.md) | Text RAG over a private working journal — code public, corpus and index never enter the repo. |
| [`search-common/`](search-common/README.md) | Shared library — auth (Firebase ID token verification + email allow-list), Firestore-backed rate limiting, env-driven settings. |
| [`explore/`](explore/README.md) | FastAPI service that wraps both corpora behind a single `/explore/api/ask` endpoint. Deployed to Cloud Run; reached via the existing GKE Ingress through an nginx-proxy pod (`k8s/explore-proxy.yml`). |

The `/explore` route on `serg.vlassiev.info` is path-mounted onto the same Ingress as the static site (no separate subdomain). Auth uses Firebase Auth (Google sign-in, popup mode) with an email allow-list — anonymous queries against the public photo corpus work without sign-in.
