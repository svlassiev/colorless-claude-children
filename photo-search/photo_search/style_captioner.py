"""Re-caption the photo corpus in the owner's voice — bilingual {ru, en}.

Reads the private style card (~/.cache/photo-search/style/caption_style.md —
distilled from the owner's own texts, never committed) plus each photo's
place/date context from the manifest meta, and writes a NEW cache:

    ~/.cache/photo-search/caption_cache_styled.jsonl   (blob_path, sha, ru, en)

The original caption_cache.jsonl and the serving path are untouched — the
styled cache is wired into serving in a separate, reviewable step. Append-only
and keyed by blob_path, so re-runs resume where they stopped.

Rules baked into the prompt: no personal names, no identity guesses,
1-2 sentences, the owner's register (see the style card).

Run: uv run python -m photo_search.style_captioner [--limit N] [--workers 8]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from google import genai
from google.cloud import storage
from google.genai import types

from photo_search.paths import (
    BUCKET,
    CAPTION_LOCATION,
    CAPTION_MODEL,
    META_PATH,
    PROJECT,
    STYLED_CAPTION_CACHE,
)

STYLE_CARD_PATH = Path.home() / ".cache" / "photo-search" / "style" / "caption_style.md"
STYLED_CACHE = STYLED_CAPTION_CACHE

PROMPT = """Ты пишешь подпись к фотографии из личного семейного фотоархива.

Пиши в голосе автора архива. Вот его стиль-карта и примеры его собственных
подписей — следуй им точно:

{style}

Контекст фотографии (метаданные, считай их достоверными; в кадре их может
не быть видно):
{context}

{hedge_rule}

Напиши подпись:
- "ru" — по-русски, в голосе автора, по умолчанию 1-2 предложения.
- "en" — то же содержание по-английски: естественный, сдержанный тон,
  без открыточного энтузиазма.
- Никаких имён людей, никаких догадок о том, кто изображён и кем люди
  друг другу приходятся.
- Место и дату используй только если органично — не отчитывайся ими.
- Названия мест используй РОВНО так, как они даны в контексте: не выдумывай
  тип улицы (улица/проспект/переулок) и не переиначивай названия. Если в
  контексте «проспект» — это проспект."""

# Hedge-density control. Captions are generated one call at a time, so the
# style card's "hedges are rare" cannot be enforced per call — the model
# hedges every time "to be safe" (confirmed by the owner's review of the
# first sample batch: «пожалуй/похоже/весьма/вполне» in almost every
# caption). A deterministic per-photo gate (sha hash, ~20%) sets the
# corpus-level density instead.
HEDGE_ALLOWED = (
    "Особая инструкция для этой подписи: допустим ОДИН хедж или усилитель "
    "(«пожалуй», «похоже», «весьма», «вполне» и т.п.) — если он органичен."
)
HEDGE_FORBIDDEN = (
    "Особая инструкция для этой подписи: БЕЗ хеджей и усилителей — никаких "
    "«пожалуй», «похоже», «весьма», «вполне», «наверное», «кажется», «вроде». "
    "Прямая констатация."
)


_HEDGE_RE = re.compile(
    r"пожалуй|похоже|весьма|вполне|наверное|кажется|вроде|по-моему", re.IGNORECASE
)


def _hedge_rule(sha: str) -> str:
    return HEDGE_ALLOWED if int(sha[:8], 16) % 5 == 0 else HEDGE_FORBIDDEN


SCHEMA = {
    "type": "OBJECT",
    "properties": {"ru": {"type": "STRING"}, "en": {"type": "STRING"}},
    "required": ["ru", "en"],
}

_write_lock = threading.Lock()


def _context_for(r: dict) -> str:
    parts = [f"Дата съёмки: {r.get('exif_date_iso') or 'неизвестна'}"]
    if r.get("place_names"):
        parts.append(f"Место: {', '.join(r['place_names'])}")
    # place_detail carries the full official form («проспект Маршала
    # Блюхера») — without it the model invents street types from the short
    # canonical name (owner caught «улица Маршала Блюхера» in the samples).
    detail = re.sub(r"https?://\S+", "", r.get("place_detail") or "").strip(" —-")
    if detail:
        parts.append(f"Полное название места: {detail}")
    ctx = re.sub(r"https?://\S+", "", r.get("place_context") or "").strip(" —-")
    if ctx:
        parts.append(f"Заметка о месте/событии: {ctx}")
    return "\n".join(parts)


def _generate(
    r: dict, img: bytes, client: genai.Client, style: str, hedge_rule: str
) -> dict:
    """One schema-validated generation with transient-error backoff."""
    backoffs = [5, 15, 30, 60, 90]
    last_err: Exception | None = None
    for attempt, delay in enumerate(backoffs):
        try:
            resp = client.models.generate_content(
                model=CAPTION_MODEL,
                contents=[
                    types.Part.from_bytes(data=img, mime_type="image/jpeg"),
                    PROMPT.format(style=style, context=_context_for(r),
                                  hedge_rule=hedge_rule),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=SCHEMA,
                    max_output_tokens=2000,
                ),
            )
            cap = json.loads(resp.text or "{}")
            if cap.get("ru") and cap.get("en"):
                return cap
            raise ValueError("empty ru/en in response")
        except Exception as e:  # noqa: BLE001
            last_err = e
            msg = str(e)
            transient = ("429" in msg or "RESOURCE_EXHAUSTED" in msg or "empty" in msg
                 or "503" in msg or "timeout" in msg.lower() or "timed out" in msg.lower())
            if transient and attempt < len(backoffs) - 1:
                time.sleep(delay)
                continue
            raise
    raise RuntimeError(f"unreachable: {last_err}")


def _caption_one(
    r: dict, client: genai.Client, bucket: storage.Bucket, style: str
) -> dict:
    img = bucket.blob(r["blob_path"]).download_as_bytes()
    rule = _hedge_rule(r["sha"])
    cap = _generate(r, img, client, style, rule)
    # Enforcement: the model sneaks hedges past the FORBIDDEN instruction in
    # a fair share of captions (37% density measured vs the 20% gate). One
    # reinforced retry fixes most; the second result is accepted either way
    # so a stubborn caption can't wedge the run.
    if rule is HEDGE_FORBIDDEN and _HEDGE_RE.search(cap["ru"]):
        retry_rule = (
            HEDGE_FORBIDDEN
            + " В прошлой попытке ты нарушил это правило — перепиши подпись "
            "заново, полностью убрав такие слова."
        )
        cap = _generate(r, img, client, style, retry_rule)
    return {"blob_path": r["blob_path"], "sha": r["sha"],
            "ru": cap["ru"].strip(), "en": cap["en"].strip()}


def main() -> int:
    ap = argparse.ArgumentParser(prog="photo-search-style-captioner")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if not STYLE_CARD_PATH.exists():
        print(f"style card missing: {STYLE_CARD_PATH}", file=sys.stderr)
        return 1
    style = STYLE_CARD_PATH.read_text()

    rows = [json.loads(l) for l in META_PATH.open()]
    done: set[str] = set()
    if STYLED_CACHE.exists():
        done = {json.loads(l)["blob_path"] for l in STYLED_CACHE.open()}
    todo = [r for r in rows if r["blob_path"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"corpus: {len(rows)}, already styled: {len(done)}, to do: {len(todo)}",
          file=sys.stderr)
    print(f"model: {CAPTION_MODEL}@{CAPTION_LOCATION}", file=sys.stderr)
    if not todo:
        print("nothing to do.", file=sys.stderr)
        return 0

    # Per-request timeout: without it, a laptop sleep mid-flight leaves all
    # workers blocked forever on dead sockets (observed 2026-08-27 — the run
    # hung at 4,051/6,004 after an overnight sleep). 3 min covers slow calls;
    # a timed-out photo is retried by the transient-error loop.
    client = genai.Client(
        vertexai=True, project=PROJECT, location=CAPTION_LOCATION,
        http_options=types.HttpOptions(timeout=180_000),
    )
    bucket = storage.Client(project=PROJECT).bucket(BUCKET)

    ok = failed = 0
    start = time.time()
    with STYLED_CACHE.open("a", encoding="utf-8") as out, ThreadPoolExecutor(
        max_workers=max(1, args.workers)
    ) as ex:
        futures = {ex.submit(_caption_one, r, client, bucket, style): r for r in todo}
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                row = fut.result()
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  FAILED {r['blob_path']}: {str(e)[:120]}", file=sys.stderr)
                continue
            with _write_lock:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
            ok += 1
            if ok % 200 == 0:
                rate = ok / (time.time() - start)
                eta = (len(todo) - ok - failed) / rate / 60 if rate else 0
                print(f"  [{ok}/{len(todo)}] {rate:.1f}/s eta {eta:.0f}m", file=sys.stderr)

    print(json.dumps({"styled": ok, "failed": failed, "total_in_cache": len(done) + ok}),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
