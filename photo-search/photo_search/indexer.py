"""Phase 1: enumerate the bucket, extract EXIF, caption via Gemini Flash.

Outputs:
- ~/.cache/photo-search/caption_cache.jsonl  (append-only, key = blob.name)
- ~/.cache/photo-search/manifest.jsonl       (rewritten each run)

Cost-aware:
- Cache key is the GCS path. Re-runs on unchanged paths cost $0.
- --dry-run: enumerate + EXIF only, no captioning calls.
- --limit N: cap photos processed (smoke-test mode).
- Up-front cost estimate before any paid call.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any

from google import genai
from google.cloud import storage
from google.genai import types
from PIL import ExifTags, Image

from photo_search.paths import (
    BUCKET,
    CAPTION_CACHE,
    CAPTION_MODEL,
    LOCATION,
    MANIFEST_PATH,
    PROJECT,
    ensure_cache_dir,
)

# Pricing rough-estimate. Gemini 2.5 Flash: $0.30/1M input, $2.50/1M output.
# Per image: 258 image tokens + ~30 prompt tokens input, ~50 output tokens.
# (288 * 0.30 + 50 * 2.50) / 1_000_000 ≈ $0.000211 — call it $0.00025 to be safe.
PRICE_PER_CAPTION = 0.00025

SKIP_NAME_PREFIXES = ("1_",)  # old-album thumbnails on the static site
# Stems ending in _<size> or _thumbnail are downsized variants of an original.
# Sizes seen in the bucket: 256, 512, 800, 1024, 2048. Originals lack the suffix.
SIZE_VARIANT_RE = re.compile(r"_(256|512|800|1024|2048|thumbnail)$", re.IGNORECASE)

CAPTION_PROMPT = (
    "Describe this photo in 2 short sentences. Cover: people count, indoor/outdoor/"
    "nature scene, notable activity, season or lighting if obvious. "
    "Do not speculate about identities of people. No more than 50 words."
)


@dataclass
class PhotoMeta:
    id: str
    gcs_uri: str
    blob_path: str
    exif_date_iso: str | None
    exif_gps: tuple[float, float] | None
    caption: str
    sha: str
    bytes_size: int


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


def _is_photo(blob_name: str) -> bool:
    if not blob_name.lower().endswith((".jpg", ".jpeg")):
        return False
    stem = blob_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if any(stem.startswith(p) for p in SKIP_NAME_PREFIXES):
        return False
    if SIZE_VARIANT_RE.search(stem):
        return False
    return True


def _dms_to_dec(dms: Any, ref: Any) -> float:
    d, m, s = (float(x) for x in dms)
    val = d + m / 60 + s / 3600
    if ref in ("S", "W"):
        val = -val
    return val


def _extract_exif(img_bytes: bytes) -> tuple[str | None, tuple[float, float] | None]:
    """Return (date_iso, gps). Robust to missing or malformed EXIF."""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        raw = img._getexif()
        if not raw:
            return None, None
        tags = {ExifTags.TAGS.get(k, k): v for k, v in raw.items()}

        date_iso = None
        date_str = tags.get("DateTimeOriginal") or tags.get("DateTime")
        if isinstance(date_str, str) and len(date_str) >= 10:
            d = date_str[:10].replace(":", "-")
            if d[:4].isdigit() and d[4] == "-" and d[7] == "-":
                date_iso = d

        gps: tuple[float, float] | None = None
        gps_info = tags.get("GPSInfo")
        if isinstance(gps_info, dict):
            named = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_info.items()}
            try:
                lat = _dms_to_dec(named["GPSLatitude"], named.get("GPSLatitudeRef", "N"))
                lon = _dms_to_dec(named["GPSLongitude"], named.get("GPSLongitudeRef", "E"))
                gps = (lat, lon)
            except (KeyError, TypeError, ValueError):
                pass
        return date_iso, gps
    except Exception:
        return None, None


def _load_cache() -> dict[str, dict]:
    """Map blob_path → cache row."""
    if not CAPTION_CACHE.exists():
        return {}
    out: dict[str, dict] = {}
    for line in CAPTION_CACHE.open():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        out[row["blob_path"]] = row
    return out


def _append_cache(row: dict) -> None:
    with CAPTION_CACHE.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _caption_image(client: genai.Client, img_bytes: bytes) -> str:
    resp = client.models.generate_content(
        model=CAPTION_MODEL,
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
            CAPTION_PROMPT,
        ],
    )
    return (resp.text or "").strip()


def _process_blob(blob: Any, client: genai.Client | None) -> dict:
    """Download, extract EXIF, optionally caption. Returns the cache row.

    Runs entirely on a worker thread. The genai Client is thread-safe, so
    concurrent calls share one client instance.
    """
    img_bytes = blob.download_as_bytes()
    date_iso, gps = _extract_exif(img_bytes)
    sha = _sha(img_bytes)

    if client is None:
        caption = ""
    else:
        try:
            caption = _caption_image(client, img_bytes)
        except Exception as e:  # noqa: BLE001
            print(f"  caption failed for {blob.name}: {e}", file=sys.stderr)
            caption = ""

    return {
        "blob_path": blob.name,
        "gcs_uri": f"gs://{BUCKET}/{blob.name}",
        "sha": sha,
        "exif_date_iso": date_iso,
        "exif_gps": list(gps) if gps else None,
        "caption": caption,
        "bytes_size": len(img_bytes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="photo-search-indexer")
    parser.add_argument("--limit", type=int, default=None, help="cap photos (smoke-test)")
    parser.add_argument("--dry-run", action="store_true", help="no captioning, no $$ spent")
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="just enumerate the bucket and print counts. No downloads, no $$.",
    )
    parser.add_argument(
        "--workers", type=int, default=10, help="concurrent caption workers (default 10)"
    )
    args = parser.parse_args()

    ensure_cache_dir()
    cache = _load_cache()
    print(f"cache: {len(cache)} captions already on disk", file=sys.stderr)

    storage_client = storage.Client(project=PROJECT)
    bucket = storage_client.bucket(BUCKET)

    print("listing bucket...", file=sys.stderr)
    all_blobs = list(bucket.list_blobs())
    blobs = [b for b in all_blobs if _is_photo(b.name)]
    print(f"bucket: {len(all_blobs)} blobs total, {len(blobs)} eligible originals", file=sys.stderr)

    if args.count_only:
        # Show a few examples of what's filtered vs kept.
        skipped = [b for b in all_blobs if not _is_photo(b.name)][:5]
        kept = blobs[:5]
        summary = {
            "blobs_total": len(all_blobs),
            "eligible_originals": len(blobs),
            "skipped_examples": [b.name for b in skipped],
            "kept_examples": [b.name for b in kept],
            "estimated_cost_caption": round(len(blobs) * PRICE_PER_CAPTION, 4),
            "estimated_cost_embed": round(len(blobs) * 0.0002, 4),
        }
        print(json.dumps(summary, indent=2))
        return 0

    if args.limit:
        blobs = blobs[: args.limit]
        print(f"limited to {len(blobs)} for this run", file=sys.stderr)

    to_caption = [b for b in blobs if b.name not in cache]
    if not args.dry_run:
        est_cost = len(to_caption) * PRICE_PER_CAPTION
        print(
            f"will caption {len(to_caption)} new photos "
            f"(reuse {len(blobs) - len(to_caption)} cached). estimated cost: ~${est_cost:.4f}",
            file=sys.stderr,
        )

    client: genai.Client | None = None
    if to_caption and not args.dry_run:
        client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)

    # Process uncached blobs concurrently. The genai Client is thread-safe;
    # each worker handles download + EXIF + caption for one photo. Wall-clock
    # drops from ~3.8s/photo single-threaded to ~3.8/workers s/photo at saturation.
    uncached = [b for b in blobs if b.name not in cache]
    if uncached:
        workers = max(1, args.workers)
        print(f"processing {len(uncached)} uncached blobs with {workers} workers", file=sys.stderr)
        start = time.time()
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_process_blob, b, None if args.dry_run else client): b
                for b in uncached
            }
            for fut in as_completed(futures):
                blob = futures[fut]
                try:
                    row = fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"  process failed for {blob.name}: {e}", file=sys.stderr)
                    continue
                cache[blob.name] = row
                if not args.dry_run:
                    _append_cache(row)
                completed += 1
                if completed % 25 == 0 or completed == len(uncached):
                    elapsed = time.time() - start
                    rate = completed / elapsed if elapsed else 0
                    eta_s = (len(uncached) - completed) / rate if rate else 0
                    print(
                        f"  [{completed:>5}/{len(uncached)}] elapsed={elapsed:.1f}s "
                        f"rate={rate:.1f}/s eta={eta_s:.0f}s",
                        file=sys.stderr,
                    )

    # Build manifest in stable bucket-order (cached or freshly processed).
    metas: list[PhotoMeta] = []
    for blob in blobs:
        row = cache.get(blob.name)
        if not row:
            continue  # processing failed; skip
        gps_t = tuple(row["exif_gps"]) if row.get("exif_gps") else None
        metas.append(
            PhotoMeta(
                id=row["sha"],
                gcs_uri=row["gcs_uri"],
                blob_path=row["blob_path"],
                exif_date_iso=row.get("exif_date_iso"),
                exif_gps=gps_t,
                caption=row.get("caption", ""),
                sha=row["sha"],
                bytes_size=row["bytes_size"],
            )
        )

    if not args.dry_run:
        with MANIFEST_PATH.open("w") as f:
            for m in metas:
                f.write(json.dumps(asdict(m)) + "\n")
        print(f"\nwrote {len(metas)} rows to {MANIFEST_PATH}", file=sys.stderr)

    # Always print a JSON summary to stdout for easy piping.
    summary = {
        "photos_listed": len(blobs),
        "photos_with_exif_date": sum(1 for m in metas if m.exif_date_iso),
        "photos_with_gps": sum(1 for m in metas if m.exif_gps),
        "photos_captioned": sum(1 for m in metas if m.caption),
        "cache_size": len(cache),
        "dry_run": args.dry_run,
        "actual_cost": (
            None if args.dry_run else round(len(to_caption) * PRICE_PER_CAPTION, 4)
        ),
    }
    print(json.dumps(summary, indent=2))

    # Auto-push updated artefacts to the private GCS bucket. Cheap (Class A
    # ops at $0.005/1k → ~$0). Idempotent — no-op if remote already up to date.
    # Skip on dry-run/count-only to keep those modes fully offline.
    if not args.dry_run and not args.count_only:
        try:
            from photo_search.cloud_cache import push_to_gcs

            pushed = push_to_gcs()
            if pushed:
                print(f"pushed {pushed} cache file(s) to GCS", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"warning: cloud-cache push failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
