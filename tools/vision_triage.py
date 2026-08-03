"""
PHASE 1.5 -- OpenAI-compatible vision triage.

This script only reads the Phase 1 shortlist in data/survivors.json. It never
walks the raw source folder, never sends full-resolution images, and never
spends money unless --confirm-spend is explicitly supplied.

The preferred API is AgentRouter's OpenAI-compatible endpoint, not the Claude
Code connection and not Anthropic's Messages Batch API. For recovery work it
can also use the official OpenAI endpoint when OPENAI_API_KEY is available.
Results are cached by a stable hash of the source path in
tools/.cache/scores.json.

Typical flow (PowerShell):

    python tools/curate.py --no-encode --shortlist 2000
    $env:AGENTROUTER_API_KEY = "..."
    python tools/vision_triage.py --preflight
    python tools/vision_triage.py --confirm-spend
    # open tools/review/index.html and download tools/review/picks.json
    python tools/curate.py --picks tools/review/picks.json
    python tools/build.py
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import html
import io
import json
import os
import re
import sys
import time
from pathlib import Path

from PIL import Image, ImageFile, ImageOps

ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = 300_000_000

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = PROJECT_ROOT / "tools" / ".cache"
SURVIVORS_FILE = DATA_DIR / "survivors.json"
SCORES_FILE = CACHE_DIR / "scores.json"
REVIEW_DIR = PROJECT_ROOT / "tools" / "review"
THUMBS_DIR = REVIEW_DIR / "thumbs"
ENDPOINT = "https://agentrouter.org/v1"
OPENAI_ENDPOINT = "https://api.openai.com/v1"

DEFAULT_BATCH_SIZE = 10
DEFAULT_WORKERS = 6
DEFAULT_MAX_RETRIES = 3
DEFAULT_TOP_CARDS = 400
IMAGE_TOKENS = 150
PROMPT_TOKENS_PER_REQUEST = 1200
OUTPUT_TOKENS_PER_IMAGE = 70

# AgentRouter may expose aliases with different names. These are only
# estimates; --input-rate/--output-rate can override them for the account.
RATE_HINTS = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-haiku": (0.25, 1.25),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-flash": (0.075, 0.30),
}

VISION_HINTS = (
    "vision", "4o", "4.1", "mini", "haiku", "flash", "gemini",
    "sonnet", "opus", "claude",
)

SCORING_PROMPT = r"""
You are curating a "Cat of the Day" site and you have impeccable, unhinged
taste in funny cats. A technically perfect studio portrait of a purebred is a
2 at most: pretty is boring, we are not a calendar. You are hunting for cats
that make people involuntarily exhale through their nose. Funny explicitly
outranks cuteness and quality for this site.

Score funny 4-5 for a perfect loaf or impossible cat puddle; derping such as a
blep, tongue, crossed eyes or one fang; a cursed frozen mid-sneeze, yawn,
jump, fall or zoomie; a deeply unflattering angle; huge dramatic offended,
plotting, screaming or judgmental energy; a cat stuck in a relatable human
predicament such as a box, sink or wedge; or anything fundamentally and
unplaceably cursed. Reward weird, expressive, badly-timed and slightly-cursed
over polished. A little blur or grain is fine when the cat is magnificent.

Quality is an honest technical read (focus, lighting, composition), separate
from cuteness. A funny 5 and quality 2 is a good result. Drop non-cats, gore,
NSFW, and visible text or watermarks. Captions are under 8 words, present
tense, and meme-like, not literal descriptions. Tags are 1-3 short vibe words.

For each image, return one item in a JSON array with exactly these fields plus
the supplied sourceKey:
{"sourceKey":"...","is_cat":true,"quality":1,"cuteness":1,
"funny":1,"has_text_or_watermark":false,"nsfw_or_gore":false,
"caption":"under 8 words","tags":["loaf"]}

Return JSON only. Never add markdown, commentary, or a preamble.
""".strip()


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def long_path(path: Path) -> str:
    value = os.fspath(path)
    if os.name != "nt":
        return value
    if not os.path.isabs(value):
        value = os.path.abspath(value)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value.lstrip("\\")
    return "\\\\?\\" + value


def short_path(value: str | Path) -> str:
    text = str(value)
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def source_key(path: str) -> str:
    import hashlib
    return hashlib.sha256(short_path(path).encode("utf-8", "surrogatepass")).hexdigest()


def load_survivors(input_file: Path) -> list[dict]:
    if not input_file.exists():
        raise SystemExit(
            f"{input_file} is missing. Run the Phase 1 shortlist first."
        )
    doc = json.loads(input_file.read_text(encoding="utf-8"))
    if isinstance(doc, dict) and isinstance(doc.get("picks"), list):
        doc = doc["picks"]
    if not isinstance(doc, list):
        raise SystemExit(f"{input_file} must contain an array or a picks array")
    for item in doc:
        item.setdefault("sourceKey", source_key(item["path"]))
    return doc


def load_score_doc() -> dict:
    if not SCORES_FILE.exists():
        return {"schema": 1, "scores": {}, "usage": {}}
    try:
        doc = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("scores"), dict):
            return doc
        if isinstance(doc, dict):
            return {"schema": 1, "scores": doc, "usage": {}}
    except (OSError, json.JSONDecodeError):
        print("WARNING: scores cache is unreadable; starting a new cache.")
    return {"schema": 1, "scores": {}, "usage": {}}


def save_score_doc(doc: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SCORES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    tmp.replace(SCORES_FILE)


def model_id(model) -> str:
    return str(getattr(model, "id", None) or
               (model.get("id") if isinstance(model, dict) else "") or model)


def model_rate(model: str, input_override: float | None,
               output_override: float | None) -> tuple[float, float]:
    if input_override is not None and output_override is not None:
        return input_override, output_override
    lowered = model.lower()
    for hint, rates in RATE_HINTS.items():
        if hint in lowered:
            return (input_override if input_override is not None else rates[0],
                    output_override if output_override is not None else rates[1])
    return (input_override if input_override is not None else 3.0,
            output_override if output_override is not None else 15.0)


def import_openai():
    try:
        from openai import OpenAI
        return OpenAI
    except ImportError as exc:
        raise SystemExit(
            "The openai SDK is not installed. Run: "
            "python -m pip install openai"
        ) from exc


def make_client():
    agent_key = os.environ.get("AGENTROUTER_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    override = os.environ.get("VISION_BASE_URL", "").strip()
    if agent_key:
        key = agent_key
        endpoint = override or ENDPOINT
        provider = "AgentRouter"
    elif openai_key:
        key = openai_key
        endpoint = override or OPENAI_ENDPOINT
        provider = "OpenAI"
    else:
        raise SystemExit(
            "Neither AGENTROUTER_API_KEY nor OPENAI_API_KEY is set. "
            "No vision request was made."
        )
    OpenAI = import_openai()
    return OpenAI(api_key=key, base_url=endpoint), provider, endpoint


def list_models(client) -> list[str]:
    result = client.models.list()
    data = getattr(result, "data", result)
    models = sorted({model_id(item) for item in data if model_id(item)})
    return models


def choose_model(models: list[str], requested: str | None) -> str:
    if requested:
        return requested
    env_model = os.environ.get("VISION_MODEL", "").strip()
    if env_model:
        return env_model
    candidates = [m for m in models if any(h in m.lower() for h in VISION_HINTS)]
    if not candidates:
        raise SystemExit(
            "No likely vision-capable model was found in AgentRouter's catalog. "
            "Set VISION_MODEL to an exact model id from the printed list."
        )
    # Prefer names that signal a cheap, capable model. The exact catalog id is
    # printed so the user can override this selection without guessing.
    def rank(name: str) -> tuple[int, str]:
        low = name.lower()
        if "mini" in low or "flash" in low or "haiku" in low:
            return (0, low)
        if "sonnet" in low:
            return (1, low)
        if "4o" in low or "4.1" in low:
            return (2, low)
        return (3, low)
    return sorted(candidates, key=rank)[0]


def compact_jpeg(path: str, max_edge: int = 384) -> str:
    with Image.open(long_path(Path(path))) as opened:
        image = ImageOps.exif_transpose(opened) or opened
        image = image.convert("RGB")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=76, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def response_text(response) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "") if message else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            value = getattr(item, "text", None)
            if value is None and isinstance(item, dict):
                value = item.get("text")
            if value:
                parts.append(str(value))
        return "\n".join(parts)
    return str(content or "")


def parse_json_payload(text: str):
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    candidates = [cleaned]
    start_array, end_array = cleaned.find("["), cleaned.rfind("]")
    if 0 <= start_array < end_array:
        candidates.append(cleaned[start_array:end_array + 1])
    start_obj, end_obj = cleaned.find("{"), cleaned.rfind("}")
    if 0 <= start_obj < end_obj:
        candidates.append(cleaned[start_obj:end_obj + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                for key in ("scores", "results", "items", "data"):
                    if isinstance(payload.get(key), list):
                        return payload[key]
                return [payload]
            if isinstance(payload, list):
                return payload
        except json.JSONDecodeError:
            continue
    raise ValueError("model response did not contain parseable JSON")


def normalize_score(raw: dict, expected_key: str | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("score item is not an object")
    def integer(name: str) -> int:
        value = raw.get(name)
        if isinstance(value, bool):
            raise ValueError(f"{name} is boolean")
        return max(1, min(5, int(value)))

    def boolean(name: str) -> bool:
        value = raw.get(name)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1"}
        return bool(value)

    key = str(raw.get("sourceKey") or raw.get("source_key") or expected_key or "")
    caption = " ".join(str(raw.get("caption", "")).split())
    caption = caption.strip('"\'`')
    words = caption.split()
    if len(words) > 7:
        caption = " ".join(words[:7])
    tags_raw = raw.get("tags", [])
    if isinstance(tags_raw, str):
        tags_raw = re.split(r"[,\s]+", tags_raw)
    tags = []
    for tag in tags_raw if isinstance(tags_raw, list) else []:
        clean = re.sub(r"[^a-z0-9-]", "", str(tag).lower())
        if clean and clean not in tags:
            tags.append(clean)
    return {
        "sourceKey": key,
        "is_cat": boolean("is_cat"),
        "quality": integer("quality"),
        "cuteness": integer("cuteness"),
        "funny": integer("funny"),
        "has_text_or_watermark": boolean("has_text_or_watermark"),
        "nsfw_or_gore": boolean("nsfw_or_gore"),
        "caption": caption,
        "tags": tags[:3],
    }


def build_message(batch: list[dict]) -> list[dict]:
    content = [{"type": "text", "text": SCORING_PROMPT}]
    for item in batch:
        content.append({
            "type": "text",
            "text": f"\nIMAGE sourceKey={item['sourceKey']}",
        })
        content.append({
            "type": "image_url",
            "image_url": {"url": compact_jpeg(item["path"]), "detail": "low"},
        })
    return [{"role": "user", "content": content}]


def score_batch(client, model: str, batch: list[dict], max_retries: int):
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            request = {
                "model": model,
                "messages": build_message(batch),
            }
            output_limit = max(1600, len(batch) * 140 + 400)
            if model.lower().startswith("gpt-5"):
                request["max_completion_tokens"] = output_limit
            else:
                request["temperature"] = 0.2
                request["max_tokens"] = output_limit
            response = client.chat.completions.create(**request)
            payload = parse_json_payload(response_text(response))
            if len(payload) != len(batch):
                # A one-image mapping can still be safely assigned. For a
                # partial batch, retry the whole batch so nothing is silently
                # dropped.
                raise ValueError(f"expected {len(batch)} results, got {len(payload)}")
            expected = [item["sourceKey"] for item in batch]
            parsed = {}
            for index, raw in enumerate(payload):
                result = normalize_score(raw, expected[index])
                if result["sourceKey"] not in expected:
                    result["sourceKey"] = expected[index]
                parsed[result["sourceKey"]] = result
            missing = set(expected) - set(parsed)
            if missing:
                raise ValueError(f"response omitted {len(missing)} image(s)")
            usage = getattr(response, "usage", None)
            usage_doc = {
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            }
            return parsed, usage_doc, None
        except Exception as exc:  # network, API, parsing, or image decode
            last_error = exc
            if attempt < max_retries:
                delay = min(30.0, 2.0 ** attempt)
                time.sleep(delay)
    return {}, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, str(last_error)


def estimate(count: int, batch_size: int, input_rate: float, output_rate: float) -> dict:
    requests = (count + batch_size - 1) // batch_size
    prompt = count * IMAGE_TOKENS + requests * PROMPT_TOKENS_PER_REQUEST
    completion = count * OUTPUT_TOKENS_PER_IMAGE
    dollars = prompt / 1_000_000 * input_rate + completion / 1_000_000 * output_rate
    return {"images": count, "requests": requests, "prompt": prompt,
            "completion": completion, "dollars": dollars}


def score_value(score: dict) -> int:
    return int(score.get("funny", 0)) * 3 + int(score.get("cuteness", 0)) + int(score.get("quality", 0))


def accepted(score: dict) -> bool:
    return (bool(score.get("is_cat")) and not bool(score.get("nsfw_or_gore"))
            and not bool(score.get("has_text_or_watermark"))
            and int(score.get("quality", 0)) > 1)


def review_thumb(item: dict) -> str:
    key = item["sourceKey"]
    out = THUMBS_DIR / f"{key}.jpg"
    if out.exists():
        return f"thumbs/{key}.jpg"
    try:
        with Image.open(long_path(Path(item["path"]))) as opened:
            image = ImageOps.exif_transpose(opened) or opened
            image = image.convert("RGB")
            image.thumbnail((240, 240), Image.Resampling.LANCZOS)
            out.parent.mkdir(parents=True, exist_ok=True)
            image.save(out, format="JPEG", quality=78, optimize=True)
        return f"thumbs/{key}.jpg"
    except Exception as exc:
        print(f"WARNING: could not make review thumb for {item['path']}: {exc}")
        return ""


def write_review(survivors: list[dict], score_map: dict, top_cards: int) -> None:
    cards = []
    for item in survivors:
        score = score_map.get(item["sourceKey"])
        if not isinstance(score, dict):
            continue
        if not accepted(score):
            continue
        card = dict(item)
        card["score"] = score
        card["blended"] = score_value(score)
        card["thumb"] = review_thumb(item)
        if card["thumb"]:
            cards.append(card)
    cards.sort(key=lambda item: (item["blended"], item.get("score", {}).get("funny", 0),
                                 item.get("score", {}).get("quality", 0)), reverse=True)
    cards = cards[:top_cards]
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    payload = []
    for index, card in enumerate(cards):
        score = card["score"]
        payload.append({
            "sourceKey": card["sourceKey"], "srcHash": card["srcHash"],
            "path": card["path"], "caption": score.get("caption", ""),
            "tags": score.get("tags", []), "funny": score.get("funny"),
            "cuteness": score.get("cuteness"), "quality": score.get("quality"),
            "blended": card["blended"], "thumb": card["thumb"],
            "checked": index < 800,
        })

    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    cards_html = []
    for index, card in enumerate(payload):
        score = card
        caption = html.escape(score.get("caption") or "(caption pending)")
        tags = " ".join("#" + html.escape(tag) for tag in score.get("tags", []))
        checked = " checked" if score["checked"] else ""
        cards_html.append(
            f'<article class="card" data-blended="{score["blended"]}">'
            f'<label><input type="checkbox" class="pick" data-index="{index}"'
            f' data-source-key="{html.escape(score["sourceKey"])}"{checked}>'
            f'<img src="{html.escape(score["thumb"])}" loading="lazy"'
            f' alt="{caption}"></label>'
            f'<div class="card-info"><b>FUNNY {score["funny"]} / CUTE {score["cuteness"]} / '
            f'QUALITY {score["quality"]}</b><strong>{caption}</strong>'
            f'<span>BLENDED {score["blended"]} &middot; {tags}</span></div></article>'
        )
    cards_markup = "\n".join(cards_html)
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAT OF THE DAY // VISION REVIEW</title>
<style>
:root {{ color-scheme: light; --navy:#000080; --paper:#ffffcc; --hot:#ff00ff; --acid:#00ff00; --silver:#c0c0c0; --ink:#000; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--navy); color:var(--ink); font:16px "Times New Roman",serif; }}
main {{ width:min(1180px,calc(100% - 24px)); margin:14px auto; background:var(--paper); border:5px outset var(--silver); padding:12px; }}
h1 {{ margin:0; color:var(--hot); font:32px "Comic Sans MS",cursive; text-shadow:2px 2px #000; }}
.notice {{ border:3px inset var(--silver); background:#fff; padding:10px; margin:12px 0; }}
.toolbar {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; font-family:"Courier New",monospace; }}
button {{ border:3px outset var(--silver); background:var(--silver); color:#000; padding:7px 10px; font-weight:bold; cursor:pointer; }}
button:active {{ border-style:inset; }} button:focus-visible,input:focus-visible {{ outline:3px solid var(--hot); outline-offset:2px; }}
#count {{ background:#000; color:var(--acid); padding:4px 7px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:10px; }}
.card {{ border:3px outset var(--silver); background:#fff; padding:5px; }}
.card label {{ display:block; cursor:pointer; }} .card img {{ display:block; width:100%; aspect-ratio:1; object-fit:cover; background:#ddd; }}
.card-info {{ padding:5px 2px 2px; font-family:"Courier New",monospace; font-size:12px; }}
.card-info b,.card-info span {{ display:block; font-size:11px; }} .card-info strong {{ display:block; font:17px "Comic Sans MS",cursive; margin:3px 0; }}
code {{ font-family:"Courier New",monospace; }}
@media (max-width:500px) {{ h1{{font-size:25px}} main{{padding:7px}} .grid{{grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}} .card-info strong{{font-size:14px}} }}
</style></head><body><main>
<h1>CAT OF THE DAY! // VISION REVIEW</h1>
<div class="notice"><b>MANUAL FINAL CALL:</b> the funny cats rise to the top. Uncheck anything that does not land, then download the JSON. This file is static and works without a server.</div>
<div class="toolbar"><button id="all" type="button">CHECK ALL</button><button id="none" type="button">UNCHECK ALL</button><button id="download" type="button">DOWNLOAD picks.json</button><span id="count"></span></div>
<hr><section class="grid">{cards_markup}</section>
<script>
const cards={data_json};
const boxes=[...document.querySelectorAll('.pick')];
const count=document.querySelector('#count');
function update(){{count.textContent=boxes.filter(x=>x.checked).length+' / '+boxes.length+' SELECTED';}}
boxes.forEach(x=>x.addEventListener('change',update));
document.querySelector('#all').onclick=()=>{{boxes.forEach(x=>x.checked=true);update();}};
document.querySelector('#none').onclick=()=>{{boxes.forEach(x=>x.checked=false);update();}};
document.querySelector('#download').onclick=()=>{{
 const picks=cards.filter((_,i)=>boxes[i].checked).map(x=>({{sourceKey:x.sourceKey,srcHash:x.srcHash,path:x.path,caption:x.caption,tags:x.tags,funny:x.funny,cuteness:x.cuteness,quality:x.quality,blended:x.blended}}));
 const blob=new Blob([JSON.stringify({{schema:1,picks}},null,2)],{{type:'application/json'}});
 const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='picks.json'; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}};
update();
</script></main></body></html>'''
    (REVIEW_DIR / "index.html").write_text(page, encoding="utf-8")
    print(f"  review sheet: {REVIEW_DIR / 'index.html'} ({len(cards):,} cards)")


def print_estimate(model: str, pending: int, batch_size: int,
                   input_rate: float | None, output_rate: float | None) -> dict:
    in_rate, out_rate = model_rate(model, input_rate, output_rate)
    info = estimate(pending, batch_size, in_rate, out_rate)
    print("\nPRE-FLIGHT COST ESTIMATE (no paid call made yet)")
    print(f"  model          : {model}")
    print(f"  pending images : {pending:,}")
    print(f"  requests       : {info['requests']:,} x {batch_size} images/request")
    print(f"  estimated input: {info['prompt']:,} tokens @ ${in_rate:.4f}/M")
    print(f"  estimated output: {info['completion']:,} tokens @ ${out_rate:.4f}/M")
    print(f"  projected spend: ${info['dollars']:.2f}")
    if info["dollars"] > 25:
        print("  HARD STOP: estimate exceeds the $25 safety ceiling.")
    return {**info, "input_rate": in_rate, "output_rate": out_rate}


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="Phase 1.5 OpenAI-compatible vision triage")
    ap.add_argument("--input", type=Path, default=SURVIVORS_FILE,
                    help="shortlist array or review picks JSON")
    ap.add_argument("--model", default=None, help="exact model id; otherwise VISION_MODEL/catalog")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    ap.add_argument("--top-cards", type=int, default=DEFAULT_TOP_CARDS)
    ap.add_argument("--limit", type=int, default=0,
                    help="score only the first N input rows (useful for a cheap smoke test)")
    ap.add_argument("--input-rate", type=float, default=None,
                    help="input USD per million tokens for estimate")
    ap.add_argument("--output-rate", type=float, default=None,
                    help="output USD per million tokens for estimate")
    ap.add_argument("--preflight", action="store_true",
                    help="discover model and print estimate, never score")
    ap.add_argument("--confirm-spend", action="store_true",
                    help="explicitly authorize scoring calls after the estimate")
    args = ap.parse_args()
    if not 1 <= args.batch_size <= 12:
        ap.error("--batch-size must be between 1 and 12")
    if not 1 <= args.workers <= 8:
        ap.error("--workers must be between 1 and 8")

    input_file = args.input if args.input.is_absolute() else PROJECT_ROOT / args.input
    survivors = load_survivors(input_file)
    if args.limit > 0:
        survivors = survivors[:args.limit]
    score_doc = load_score_doc()
    score_map = score_doc.setdefault("scores", {})
    pending_items = [item for item in survivors if item["sourceKey"] not in score_map]

    try:
        client, provider, endpoint = make_client()
        models = list_models(client)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: could not list vision models: {exc}")
        return 2

    print(f"Vision provider: {provider} ({endpoint})")
    print("Vision model catalog (exact ids):")
    for model in models:
        print(f"  {model}")
    model = choose_model(models, args.model)
    print(f"Selected vision model: {model}")
    if not models:
        print("ERROR: the provider returned an empty model catalog.")
        return 2

    estimate_doc = print_estimate(model, len(pending_items), args.batch_size,
                                  args.input_rate, args.output_rate)
    if estimate_doc["dollars"] > 25:
        return 3
    if args.preflight or not args.confirm_spend:
        print("\nPRE-FLIGHT ONLY. No scoring requests were sent.")
        print("After reviewing this estimate, rerun with --confirm-spend to authorize calls.")
        return 0
    if not pending_items:
        print("\nAll Phase 1 survivors already have cached scores. No paid calls needed.")
    else:
        print("\nScoring only uncached survivors. Images are resized to 384px and batched.")
        batches = [pending_items[i:i + args.batch_size]
                   for i in range(0, len(pending_items), args.batch_size)]
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        errors = []
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(score_batch, client, model, batch, args.max_retries)
                       for batch in batches]
            for future in concurrent.futures.as_completed(futures):
                result, usage, error = future.result()
                if error:
                    errors.append(error)
                score_map.update(result)
                for key in usage_total:
                    usage_total[key] += usage.get(key, 0)
                done += 1
                print(f"\r  batches {done:,}/{len(batches):,}  scores cached {len(score_map):,}", end="", flush=True)
                score_doc["model"] = model
                score_doc["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                score_doc["usage"] = usage_total
                save_score_doc(score_doc)
        print()
        if errors:
            print(f"WARNING: {len(errors):,} batch(es) failed after retries; they remain uncached and can be rerun.")
            for error in errors[:5]:
                print(f"  {error}")
        in_rate = estimate_doc["input_rate"]
        out_rate = estimate_doc["output_rate"]
        actual = (usage_total["prompt_tokens"] / 1_000_000 * in_rate
                  + usage_total["completion_tokens"] / 1_000_000 * out_rate)
        print("\nACTUAL USAGE REPORTED BY API")
        print(f"  input tokens  : {usage_total['prompt_tokens']:,}")
        print(f"  output tokens : {usage_total['completion_tokens']:,}")
        print(f"  total tokens  : {usage_total['total_tokens']:,}")
        print(f"  estimated cost: ${actual:.2f} using the configured rate estimate")

    write_review(survivors, score_map, args.top_cards)
    accepted_count = sum(1 for item in survivors
                         if isinstance(score_map.get(item["sourceKey"]), dict)
                         and accepted(score_map[item["sourceKey"]]))
    print(f"\nAccepted for manual review: {accepted_count:,}")
    print(f"Scores cache: {SCORES_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
