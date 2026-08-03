"""Caption the manually selected cats with a local Ollama vision model.

Nothing is uploaded. Source images are read-only, downscaled to a 384px JPEG
in memory, and scored one at a time. Results use the same cache schema as
vision_triage.py, keyed by sourceKey, so interrupted runs resume cleanly.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parent.parent
PICKS = ROOT / "tools" / "review" / "picks.json"
CACHE = ROOT / "tools" / ".cache" / "scores.json"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

PROMPT = """
You curate Cat of the Day with impeccable, unhinged taste. Pretty is boring;
funny personality wins. Judge this one image. Funny 4-5 means loaf/puddle,
blep/derp, cursed frozen action, terrible angle, dramatic judgment, tiny-box
predicament, or an inexplicably cursed expression. Blur is fine when the cat
is magnificent. Quality is only the honest technical read. Caption: under 8
words, present tense, dry meme voice, not a literal description. Anchor the
joke to something actually visible: pose, expression, angle, action, object,
or setting. If nothing is happening, invent a tiny motive. Never use these
stock phrases: "existential dread", "is a vibe", "but make it", "seen some
things", "judges your life choices", "clearly plotting", "world domination",
"contemplates the abyss", "His Majesty", "apex predator", "life decisions".
Never begin with "clearly", "he is", or "he's". Use 2-7 words and finish a
complete thought. Silently draft
three options, reject the two most generic, and return the strangest specific
one. Tags: 1-3 short lowercase vibe words. Return only the JSON.
""".strip()

STYLE_MODES = (
    "Caption flavor: an alarming workplace memo.",
    "Caption flavor: a tiny medieval prophecy.",
    "Caption flavor: a formal legal complaint against physics.",
    "Caption flavor: a small villain announcing a bad plan.",
    "Caption flavor: failed engineering described with confidence.",
    "Caption flavor: the cat accuses the viewer of one specific offense.",
    "Caption flavor: nature documentary narration gone wrong.",
    "Caption flavor: a kitchen or household emergency.",
    "Caption flavor: an extremely minor cosmic omen.",
    "Caption flavor: customer service has stopped helping.",
    "Caption flavor: sports commentary for a terrible maneuver.",
    "Caption flavor: a first-person demand from the cat.",
    "Caption flavor: a computer loading/error message.",
    "Caption flavor: a domestic betrayal of historic importance.",
    "Caption flavor: give the cat an absurdly dignified title.",
    "Caption flavor: calmly report the most suspicious visible detail.",
)

STOCK_PHRASES = (
    "existential dread", "is a vibe", "but make it", "seen some things",
    "life choice", "life decision", "clearly", "world domination",
    "contemplates the abyss", "his majesty", "apex predator",
)

SCHEMA = {
    "type": "object",
    "properties": {
        "is_cat": {"type": "boolean"},
        "quality": {"type": "integer", "minimum": 1, "maximum": 5},
        "cuteness": {"type": "integer", "minimum": 1, "maximum": 5},
        "funny": {"type": "integer", "minimum": 1, "maximum": 5},
        "has_text_or_watermark": {"type": "boolean"},
        "nsfw_or_gore": {"type": "boolean"},
        "caption": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["is_cat", "quality", "cuteness", "funny",
                 "has_text_or_watermark", "nsfw_or_gore", "caption", "tags"],
}


def load_picks() -> list[dict]:
    doc = json.loads(PICKS.read_text(encoding="utf-8"))
    rows = doc.get("picks", doc) if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise SystemExit("tools/review/picks.json has no picks array")
    return rows


def load_cache() -> dict:
    if not CACHE.exists():
        return {"schema": 1, "scores": {}, "usage": {}}
    doc = json.loads(CACHE.read_text(encoding="utf-8"))
    if "scores" not in doc:
        doc = {"schema": 1, "scores": doc, "usage": {}}
    return doc


def save_cache(doc: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE)


def tiny_jpeg(path: str) -> str:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened) or opened
        image = image.convert("RGB")
        image.thumbnail((384, 384), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=76, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def normalize(raw: dict, key: str) -> dict:
    def score(name: str) -> int:
        return max(1, min(5, int(raw.get(name, 1))))

    caption = str(raw.get("caption", "")).translate(str.maketrans({
        "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...",
    }))
    caption = " ".join(caption.strip(" \"'`").split())
    caption = caption.replace("I'm'll", "I'll").replace("Don't'", "Don't")
    if not caption:
        raise ValueError("empty caption")
    # Accept a slightly overlong draft during the bulk pass. The explicit
    # --rescore-outliers cleanup removes anything above seven words and reruns
    # just those cats one at a time, avoiding a full six-image batch retry.
    if not 2 <= len(caption.split()) <= 12:
        raise ValueError("caption must be a complete 2-12 word thought")
    tags = []
    for value in raw.get("tags", []) if isinstance(raw.get("tags"), list) else []:
        tag = re.sub(r"[^a-z0-9-]", "", str(value).lower())
        if tag and tag not in tags:
            tags.append(tag)
    return {
        "sourceKey": key,
        "is_cat": bool(raw.get("is_cat", True)),
        "quality": score("quality"),
        "cuteness": score("cuteness"),
        "funny": score("funny"),
        "has_text_or_watermark": bool(raw.get("has_text_or_watermark", False)),
        "nsfw_or_gore": bool(raw.get("nsfw_or_gore", False)),
        "caption": caption,
        "tags": tags[:3],
    }


def score_batch(rows: list[dict], model: str, retries: int) -> tuple[dict, str | None]:
    keys = [row["sourceKey"] for row in rows]
    directions = []
    for index, key in enumerate(keys, 1):
        style = STYLE_MODES[int(key[:8], 16) % len(STYLE_MODES)]
        directions.append(f"IMAGE {index}: {style} Variation {key[8:14].upper()}.")
    item_schema = dict(SCHEMA)
    batch_schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "minItems": len(rows),
                                   "maxItems": len(rows), "items": item_schema}},
        "required": ["items"],
    }
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "format": batch_schema,
        "messages": [{"role": "user", "content": PROMPT
                      + f"\n\nThere are exactly {len(rows)} images. Return exactly {len(rows)} items "
                        "in image order. Make every caption distinct.\n"
                      + "\n".join(directions)
                      + "\nDo not print the private variation tokens.",
                      "images": [tiny_jpeg(row["path"]) for row in rows]}],
        "options": {"temperature": 0.72, "num_predict": 220 * len(rows)},
        "keep_alive": "15m",
    }
    body = json.dumps(payload).encode("utf-8")
    last = "unknown error"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=180) as response:
                doc = json.loads(response.read().decode("utf-8"))
            content = doc.get("message", {}).get("content", "")
            items = json.loads(content).get("items", [])
            if len(items) != len(rows):
                raise ValueError(f"expected {len(rows)} results, got {len(items)}")
            parsed = {key: normalize(raw, key) for key, raw in zip(keys, items)}
            return parsed, None
        except Exception as exc:
            last = str(exc)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    return {}, last


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description="Local personality captions via Ollama")
    ap.add_argument("--model", default=os.getenv("LOCAL_VISION_MODEL", "qwen3.5:9b"))
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--rescore-outliers", action="store_true",
                    help="drop duplicate/stock captions from cache before resuming")
    args = ap.parse_args()
    if not 1 <= args.workers <= 3:
        ap.error("--workers must be 1..3")
    if not 1 <= args.batch_size <= 6:
        ap.error("--batch-size must be 1..6")

    picks = load_picks()
    cache = load_cache()
    scores = cache.setdefault("scores", {})
    if args.rescore_outliers:
        seen = set()
        removed = []
        for key, score in list(scores.items()):
            caption = " ".join(str(score.get("caption", "")).split())
            lowered = caption.lower()
            bad = (not 2 <= len(caption.split()) <= 7
                   or any(phrase in lowered for phrase in STOCK_PHRASES)
                   or lowered in seen)
            if bad:
                removed.append(key)
                scores.pop(key, None)
            else:
                seen.add(lowered)
        save_cache(cache)
        print(f"Outliers removed for clean rescore: {len(removed):,}")
    pending = [row for row in picks if row["sourceKey"] not in scores]
    if args.limit:
        pending = pending[:args.limit]
    print(f"Local model: {args.model}")
    print(f"Selected cats: {len(picks):,}; cached: {len(scores):,}; pending this run: {len(pending):,}")
    print("Source handling: read-only; 384px in-memory JPEG; no upload; cost $0.00")
    if not pending:
        return 0

    errors = []
    done = 0
    batches = [pending[i:i + args.batch_size]
               for i in range(0, len(pending), args.batch_size)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(score_batch, batch, args.model, args.retries) for batch in batches]
        for future in concurrent.futures.as_completed(futures):
            result, error = future.result()
            if result:
                scores.update(result)
            else:
                errors.append(("batch", error))
            done += len(result) if result else args.batch_size
            done = min(done, len(pending))
            if len(scores) % 20 < args.batch_size or done == len(pending):
                cache["model"] = args.model
                cache["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                save_cache(cache)
                print(f"  {done:,}/{len(pending):,} processed; {len(scores):,} cached; {len(errors):,} errors", flush=True)
    if errors:
        print("Errors (safe to rerun):")
        for key, error in errors[:10]:
            print(f"  {key[:12]}: {error}")
    print(f"Cache: {CACHE}")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
