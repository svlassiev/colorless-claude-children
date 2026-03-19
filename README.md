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

Old-style albums (sequential filenames like `Picture001.jpg`):
- Add entry to `albums.json` with `title`, `folder`, `count`, `pathName`
- Thumbnails use `1_` prefix: `1_Picture001.jpg`

New-style albums (camera filenames like `IMG_0562.jpg`):
- Add entry to `albums.json` with `"useFiles": true`
- Add file list to `albums-files.json`
- Thumbnails use `_thumbnail` suffix: `IMG_0562_thumbnail.jpg`

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
k8s/                — Kubernetes manifests (deployment, service, ingress, managed certs)
.github/workflows/  — GitHub Actions CI/CD
```
