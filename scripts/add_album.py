#!/usr/bin/env python3
"""Add a new photo album to the gallery (serg.vlassiev.info).

Two steps, with a human review in between (see README "Adding new albums"):

  python3 scripts/add_album.py prepare <photo-dir> --title "<title>"
      Local only, free. Picks a new UUID folder (like the hiking albums' bucket
      prefixes; --folder overrides it), orders the JPEGs chronologically (EXIF
      capture time), writes the original + size variants into
      .album-staging/<folder>/ and prints the order for review. Nothing is
      uploaded or edited. To change the order, edit the "files" list in
      .album-staging/<folder>/album.json before publishing.

  python3 scripts/add_album.py publish --folder <folder>
      Uploads the staged files to gs://colorless-days-children/<folder>/ (refuses
      if that prefix already has objects), checks every variant the site needs
      is publicly reachable, appends the album to albums.json +
      albums-files.json, and removes the staging dir.

Files per photo, matching what the bucket already holds for file-based albums
(sizes are the long edge; portrait photos get e.g. 60x80 / 768x1024):

  <stem>.jpg            original, byte-for-byte (keeps EXIF date/GPS that the
                        /explore indexer reads)
  <stem>_thumbnail.jpg  80 px    album grid + prev/next in the viewer
  <stem>_1024.jpg       1024 px  photo viewer + share-link preview image
  <stem>_800.jpg        800 px   not used by the site today; kept for parity
  <stem>_2048.jpg       2048 px  not used by the site today; kept for parity

Variants have the EXIF rotation baked into the pixels and EXIF/GPS stripped
(the ICC colour profile is kept). Requires ImageMagick 7 (`magick`) and gcloud.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ALBUMS_JSON = REPO / "albums.json"
ALBUMS_FILES_JSON = REPO / "albums-files.json"
STAGING = REPO / ".album-staging"

BUCKET = "colorless-days-children"
PUBLIC_BASE = f"https://storage.googleapis.com/{BUCKET}"
EXPECTED_ACCOUNT = "svlassiev@gmail.com"

VARIANTS = {"thumbnail": 80, "800": 800, "1024": 1024, "2048": 2048}
JPEG_QUALITY = "82"
SITE_VARIANTS = ("thumbnail", "1024")  # what app.js / hiking-api actually request

# app.js puts names through encodeURI(), which leaves '#' and '?' alone — keep
# stems to a conservative charset.
UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._~-]")
FOLDER_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# The /explore indexer treats stems ending like this as downsized variants and
# would skip the original (photo-search/photo_search/indexer.py).
INDEXER_VARIANT_RE = re.compile(r"_(256|512|800|1024|2048|thumbnail|[0-9])$", re.IGNORECASE)


def die(msg):
    sys.exit(f"error: {msg}")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


# ---------------------------------------------------------------- prepare


META_FIELDS = ("DateTimeOriginal", "OffsetTimeOriginal", "DateTimeDigitized",
               "OffsetTimeDigitized", "DateTime", "Model", "GPSLatitude")
NAME_DATE_RE = re.compile(r"(20\d\d)(\d\d)(\d\d)[_-](\d\d)(\d\d)(\d\d)")  # PXL_/IMG_20260907_051323
CAMERA_COUNTER_RE = re.compile(r"([A-Za-z]+)_(\d{4})(?!\d)")  # DSC_5370, IMG_0563 — not IMG_2019…


def read_meta(path):
    fmt = "|".join(f"%[EXIF:{f}]" for f in META_FIELDS) + "|%w|%h"
    out = run(["magick", "identify", "-ping", "-format", fmt, f"{path}[0]"])
    *values, w, h = (v.strip() for v in out.split("|"))
    exif = dict(zip(META_FIELDS, values))

    # Capture time, best source first.
    date, offset, source = exif["DateTimeOriginal"], exif["OffsetTimeOriginal"], ""
    if not date:
        date, offset = exif["DateTimeDigitized"], exif["OffsetTimeDigitized"]
    if not date and (m := NAME_DATE_RE.search(path.stem)):
        date, source = "{}:{}:{} {}:{}:{}".format(*m.groups()), "file name"
    if not date and exif["DateTime"]:  # last-modified; editors bump it
        date, source = exif["DateTime"], "EXIF DateTime"
    utc_offset = None
    if m := re.fullmatch(r"([+-])(\d\d):(\d\d)", offset):
        utc_offset = (-1 if m[1] == "-" else 1) * timedelta(hours=int(m[2]), minutes=int(m[3]))
    return {"local": datetime.strptime(date[:19], "%Y:%m:%d %H:%M:%S") if date else None,
            "offset": utc_offset, "borrowed": False, "source": source, "model": exif["Model"],
            "gps": bool(exif["GPSLatitude"]), "size": f"{w}x{h}"}


def order_chronologically(photos, metas):
    """Sort by UTC capture time, so a trip across time zones comes out right.

    Edited copies (Google Photos "~2") keep the local time but lose the
    offset; they borrow it from the photo nearest in local time.
    """
    known = [m for m in metas if m["local"] and m["offset"] is not None]
    for m in metas:
        if m["local"] and m["offset"] is None and known:
            m["offset"] = min(known, key=lambda k: abs(k["local"] - m["local"]))["offset"]
            m["borrowed"] = True

    def key(pm):
        photo, m = pm
        return (m["local"] - (m["offset"] or timedelta()) if m["local"] else datetime.max, photo.name)

    return sorted(zip(photos, metas), key=key)


def format_date(m):
    if not m["local"]:
        return "— no date —"
    text = m["local"].strftime("%Y:%m:%d %H:%M:%S")
    if m["offset"] is not None:
        minutes = int(m["offset"].total_seconds()) // 60
        tz = f"{'-' if minutes < 0 else '+'}{abs(minutes) // 60:02}:{abs(minutes) % 60:02}"
        text += f" ({tz})" if m["borrowed"] else f" {tz}"
    return text + ("*" if m["source"] else "")


def out_of_counter_order(names):
    """Camera counters (DSC_5370, IMG_0563…) only go up. A photo whose date
    puts it off the longest rising run of its counter has a suspect date."""
    runs = {}
    for name in names:
        if m := CAMERA_COUNTER_RE.match(name):
            runs.setdefault(m[1], []).append((int(m[2]), name))
    suspects = []
    for items in runs.values():
        best = [[item] for item in items]  # longest rising run ending at i
        for i in range(len(items)):
            for j in range(i):
                if items[j][0] < items[i][0] and len(best[j]) + 1 > len(best[i]):
                    best[i] = best[j] + [items[i]]
        keep = set(max(best, key=len))
        suspects += [name for number, name in items if (number, name) not in keep]
    return suspects


def make_variants(src, dst_dir, stem):
    cmd = ["magick", f"{src}[0]", "-auto-orient", "+profile", "!icc,*", "-quality", JPEG_QUALITY]
    for suffix, px in VARIANTS.items():
        cmd += ["(", "+clone", "-resize", f"{px}x{px}",
                "-write", str(dst_dir / f"{stem}_{suffix}.jpg"), "+delete", ")"]
    run(cmd + ["null:"])


def prepare(args):
    src_dir = Path(args.photo_dir).expanduser().resolve()
    if not src_dir.is_dir():
        die(f"{src_dir} is not a directory")
    check_folder_name(args.folder)

    files = sorted(p for p in src_dir.iterdir() if p.is_file() and not p.name.startswith("."))
    photos = [p for p in files if p.suffix.lower() in (".jpg", ".jpeg")]
    skipped = [p.name for p in files if p not in photos]
    if not photos:
        die(f"no .jpg/.jpeg files in {src_dir}")

    with ThreadPoolExecutor(8) as pool:
        metas = list(pool.map(read_meta, photos))
    order = order_chronologically(photos, metas)

    stems, used = [], set()
    for p, _ in order:
        stem = UNSAFE_CHARS.sub("_", p.stem)
        candidate, i = stem, 2
        while candidate.lower() in used:  # e.g. IMG_0001 from two phones
            candidate, i = f"{stem}-{i}", i + 1
        used.add(candidate.lower())
        stems.append(candidate)

    out_dir = STAGING / args.folder
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    def stage(item):
        (src, _), stem = item
        shutil.copyfile(src, out_dir / f"{stem}.jpg")
        make_variants(src, out_dir, stem)

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(stage, zip(order, stems)))

    names = [f"{s}.jpg" for s in stems]
    (out_dir / "album.json").write_text(json.dumps(
        {"folder": args.folder, "title": args.title, "source": str(src_dir), "files": names},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n{args.title}  →  {args.folder}/  ({len(names)} photos, staged in {out_dir})\n")
    print(f"{'n':>3}  {'taken (local time)':29} {'size':>9}  {'camera':14}  file")
    for n, ((src, m), name) in enumerate(zip(order, names), 1):
        renamed = "" if name == src.name else f"   (from {src.name})"
        gps = " gps" if m["gps"] else ""
        print(f"{n:>3}  {format_date(m):29} {m['size']:>9}  "
              f"{m['model'][:14]:14}  {name}{renamed}{gps}")

    notes = []
    if skipped:
        notes.append(f"skipped (not JPEG): {', '.join(skipped)}")
    if undated := sum(1 for _, m in order if not m["local"]):
        notes.append(f"{undated} photo(s) have no date at all — placed last, by file name")
    for source in ("file name", "EXIF DateTime"):
        if flagged := [name for (_, m), name in zip(order, names) if m["source"] == source]:
            notes.append(f"* no EXIF capture time, dated from its {source}: {', '.join(flagged)}"
                         " — check the position")
    if borrowed := sum(1 for _, m in order if m["borrowed"]):
        notes.append(f"{borrowed} photo(s) had no timezone offset (edited copies lose it) — "
                     "borrowed from the nearest photo by local time, shown in (brackets)")
    if suspects := out_of_counter_order(names):
        notes.append(f"date disagrees with the camera's file counter: {', '.join(suspects)} "
                     "— its clock was off (e.g. still on home time in flight) or the date was edited; check the position")
    if len({m["model"] for _, m in order if m["model"]}) > 1:
        notes.append("several cameras — check their clocks agree, or the order will interleave wrongly")
    if gps := sum(1 for _, m in order if m["gps"]):
        notes.append(f"{gps} original(s) carry GPS coordinates; the bucket is public "
                     "(variants are stripped, originals are uploaded as-is)")
    digests = {}
    for (src, _), name in zip(order, names):
        digests.setdefault(hashlib.sha256(src.read_bytes()).hexdigest(), []).append(name)
    if dupes := [" = ".join(group) for group in digests.values() if len(group) > 1]:
        notes.append(f"byte-identical photos: {'; '.join(dupes)}")
    if odd := [s for s in stems if INDEXER_VARIANT_RE.search(s)]:
        notes.append(f"the /explore indexer would skip these as 'variants': {', '.join(odd)}")
    for note in notes:
        print(f"\nNOTE: {note}")
    print(f"\nReview, then: python3 scripts/add_album.py publish --folder {args.folder}")


# ---------------------------------------------------------------- publish


def check_folder_name(folder):
    if not FOLDER_RE.match(folder):
        die(f"folder '{folder}' — use letters, digits, '-' and '_' only")
    albums = json.loads(ALBUMS_JSON.read_text(encoding="utf-8"))
    if any(a["folder"] == folder for a in albums):
        die(f"folder '{folder}' is already in albums.json")


def format_albums_files(data):
    """Serialise albums-files.json in its hand-kept layout (4 names per line)."""
    blocks = []
    for key, album in data.items():
        files = album["files"]
        rows = [", ".join(json.dumps(f, ensure_ascii=False) for f in files[i:i + 4])
                for i in range(0, len(files), 4)]
        blocks.append(
            f"  {json.dumps(key, ensure_ascii=False)}: {{\n"
            f"    \"title\": {json.dumps(album['title'], ensure_ascii=False)},\n"
            f"    \"files\": [\n" + ",\n".join(f"      {r}" for r in rows) + "\n    ]\n  }")
    return "{\n" + ",\n".join(blocks) + "\n}\n"


def is_public(path):
    url = f"{PUBLIC_BASE}/{urllib.parse.quote(path, safe='/~')}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False


def publish(args):
    staged = STAGING / args.folder
    manifest_path = staged / "album.json"
    if not manifest_path.exists():
        die(f"nothing staged for '{args.folder}' — run prepare first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    check_folder_name(args.folder)
    if len(set(manifest["files"])) != len(manifest["files"]):  # after a hand reorder
        die("album.json lists a file twice")

    # Guard against mangling albums-files.json before anything is uploaded.
    files_text = ALBUMS_FILES_JSON.read_text(encoding="utf-8")
    albums_files = json.loads(files_text)
    if format_albums_files(albums_files) != files_text:
        die("albums-files.json layout changed — update format_albums_files() first")

    account = run(["gcloud", "config", "get-value", "account"]).strip()
    if account != EXPECTED_ACCOUNT:
        die(f"gcloud account is '{account}', expected {EXPECTED_ACCOUNT} (see CLAUDE.md)")

    uploads = []
    for name in manifest["files"]:
        stem = name[:-len(".jpg")]
        uploads += [staged / name] + [staged / f"{stem}_{v}.jpg" for v in VARIANTS]
    if missing := [p.name for p in uploads if not p.exists()]:
        die(f"staged files missing: {', '.join(missing[:5])}")

    # An empty prefix is a fresh album; a prefix holding only files of this
    # upload is an interrupted earlier publish (--no-clobber skips those).
    prefix = f"gs://{BUCKET}/{args.folder}/"
    listing = subprocess.run(["gcloud", "storage", "ls", prefix], capture_output=True, text=True)
    if listing.returncode != 0 and "matched no objects" not in listing.stderr:
        die(f"could not list {prefix}: {listing.stderr.strip()}")
    existing = {line.removeprefix(prefix) for line in listing.stdout.split()}
    if foreign := existing - {p.name for p in uploads}:
        die(f"{prefix} already holds other objects ({', '.join(sorted(foreign)[:3])}) "
            "— pick another folder name")

    print(f"Uploading {len(uploads)} files to {prefix} ...")
    subprocess.run(["gcloud", "storage", "cp", "--no-clobber", "--content-type=image/jpeg",
                    *map(str, uploads), prefix], check=True)

    needed = [f"{args.folder}/{n[:-4]}_{v}.jpg" for n in manifest["files"] for v in SITE_VARIANTS]
    with ThreadPoolExecutor(8) as pool:
        broken = [p for p, ok in zip(needed, pool.map(is_public, needed)) if not ok]
    if broken:
        die(f"not publicly reachable: {', '.join(broken[:5])} — albums.json left untouched")

    albums = json.loads(ALBUMS_JSON.read_text(encoding="utf-8"))
    albums.append({"title": manifest["title"], "folder": args.folder,
                   "count": 0, "pathName": "", "useFiles": True})
    ALBUMS_JSON.write_text(json.dumps(albums, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    albums_files[args.folder] = {"title": manifest["title"], "files": manifest["files"]}
    ALBUMS_FILES_JSON.write_text(format_albums_files(albums_files), encoding="utf-8")

    shutil.rmtree(staged)
    if not any(STAGING.iterdir()):
        STAGING.rmdir()
    print(f"\nPublished {len(manifest['files'])} photos; albums.json + albums-files.json updated.")
    print("Next: review `git diff`, commit, push to main (CI deploys the site), then restart")
    print("hiking-api so share links resolve the new album — see README 'Adding new albums'.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="order photos + generate variants locally")
    p.add_argument("photo_dir")
    p.add_argument("--folder", default=str(uuid.uuid4()),
                   help="bucket folder / URL key (default: a new UUID)")
    p.add_argument("--title", required=True, help='album title as shown on the site, e.g. "…поход."')
    p.set_defaults(func=prepare)
    p = sub.add_parser("publish", help="upload the staged album and register it")
    p.add_argument("--folder", required=True)
    p.set_defaults(func=publish)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
