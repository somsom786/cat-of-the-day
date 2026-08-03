"""
Personality-first manual review of every decodable source image.

Unlike curate.py's technical shortlist, this tool deliberately does not reject
small, blurry, portrait, monochrome-looking, or odd-ratio photographs. It
uses the cached validation records only to avoid decoding the same source a
second time, makes local thumbnails/contact sheets, and writes a static review
page with a client-side picks.json download.

Nothing below cat-pictures is ever written.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageFile, ImageOps, ImageDraw, ImageFont

ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = 300_000_000

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = PROJECT_ROOT / "tools" / ".cache" / "hashes.json"
REVIEW_DIR = PROJECT_ROOT / "tools" / "review"
THUMBS_DIR = REVIEW_DIR / "manual-thumbs"
SHEETS_DIR = REVIEW_DIR / "manual-sheets"
POOL_FILE = REVIEW_DIR / "manual-pool.json"

CELL_W = 180
CELL_H = 210
COLS = 6
ROWS = 10
PER_SHEET = COLS * ROWS
THUMB = 160


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def short_path(value: str | Path) -> str:
    text = str(value)
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


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


def load_records() -> list[dict]:
    if not CACHE_FILE.exists():
        raise SystemExit("tools/.cache/hashes.json is missing; run tools/curate.py once first.")
    doc = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    records = []
    seen = set()
    for record in doc.values():
        if not isinstance(record, dict) or not record.get("ok"):
            continue
        path = str(record.get("path", ""))
        if "cat-pictures" not in path.lower():
            continue
        if not Path(path).exists():
            continue
        key = record.get("sourceKey")
        if not key:
            import hashlib
            key = hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        record = dict(record)
        record["sourceKey"] = key
        records.append(record)
    records.sort(key=lambda item: str(item["path"]).casefold())
    return records


def font(size: int, bold: bool = False):
    candidates = [
        "C:/Windows/Fonts/consolab.ttf" if bold else "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    return ImageFont.load_default()


def make_thumb(record: dict) -> str:
    key = record["sourceKey"]
    out = THUMBS_DIR / f"{key}.jpg"
    if not out.exists():
        with Image.open(long_path(Path(record["path"]))) as opened:
            image = ImageOps.exif_transpose(opened) or opened
            image = image.convert("RGB")
            image.thumbnail((THUMB, THUMB), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (THUMB, THUMB), "#333333")
            x = (THUMB - image.width) // 2
            y = (THUMB - image.height) // 2
            canvas.paste(image, (x, y))
            out.parent.mkdir(parents=True, exist_ok=True)
            canvas.save(out, format="JPEG", quality=78, optimize=True)
    return f"manual-thumbs/{key}.jpg"


def make_sheet(records: list[dict], sheet_number: int) -> str:
    width = COLS * CELL_W
    height = ROWS * CELL_H
    canvas = Image.new("RGB", (width, height), "#ffffcc")
    draw = ImageDraw.Draw(canvas)
    label_font = font(14, True)
    detail_font = font(11)
    for offset, record in enumerate(records):
        col = offset % COLS
        row = offset // COLS
        x = col * CELL_W
        y = row * CELL_H
        thumb = Image.open(THUMBS_DIR / f"{record['sourceKey']}.jpg").convert("RGB")
        tx = x + (CELL_W - THUMB) // 2
        canvas.paste(thumb, (tx, y + 4))
        index = record["reviewIndex"]
        draw.text((x + 7, y + THUMB + 8), f"#{index:04d}", fill="#000080", font=label_font)
        ar = record["w"] / max(1, record["h"])
        detail = f"{record['w']}x{record['h']}  {ar:.2f}"
        draw.text((x + 7, y + THUMB + 30), detail, fill="#000000", font=detail_font)
        draw.line((x, y, x + CELL_W - 1, y), fill="#c0c0c0", width=2)
    path = SHEETS_DIR / f"sheet-{sheet_number:03d}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, format="JPEG", quality=82, optimize=True)
    return f"manual-sheets/{path.name}"


def write_page(records: list[dict], sheet_paths: list[str]) -> None:
    cards = []
    payload = []
    for record in records:
        item = {
            "reviewIndex": record["reviewIndex"],
            "sourceKey": record["sourceKey"],
            "srcHash": record.get("src_hash", ""),
            "path": record["path"],
            "w": record["w"], "h": record["h"],
            "thumb": make_thumb(record),
            "checked": False,
        }
        payload.append(item)
        cards.append(
            f'<article class="card" data-search="{html.escape(item["path"].lower())}">'
            f'<label><input type="checkbox" class="pick" data-index="{item["reviewIndex"]}">'
            f'<img loading="lazy" src="{html.escape(item["thumb"])}"'
            f' alt="Source image {item["reviewIndex"]}, {item["w"]} by {item["h"]} pixels">'
            f'<span><b>#{item["reviewIndex"]:04d}</b> {item["w"]}x{item["h"]}</span>'
            f'<small>{html.escape(Path(item["path"]).parent.name)}</small></label></article>'
        )
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    sheet_links = " ".join(
        f'<a href="{html.escape(path)}">SHEET {i + 1:03d}</a>'
        for i, path in enumerate(sheet_paths)
    )
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAT OF THE DAY // ALL-CAT MANUAL REVIEW</title>
<style>
:root {{ --navy:#000080; --paper:#ffffcc; --hot:#ff00ff; --acid:#00ff00; --silver:#c0c0c0; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--navy); color:#000; font:16px "Times New Roman",serif; }}
main {{ width:min(1320px,calc(100% - 20px)); margin:10px auto; padding:12px; background:var(--paper); border:5px outset var(--silver); }}
h1 {{ margin:0; color:var(--hot); font:34px "Comic Sans MS",cursive; text-shadow:2px 2px #000; }}
.warning {{ background:#fff; border:3px inset var(--silver); padding:10px; margin:10px 0; }}
.toolbar {{ position:sticky; top:0; z-index:2; display:flex; flex-wrap:wrap; gap:7px; align-items:center; padding:8px; background:var(--silver); border:3px outset var(--silver); font-family:"Courier New",monospace; }}
button,input[type=search] {{ border:3px outset var(--silver); background:#fff; color:#000; padding:6px 9px; font:inherit; }} button {{ background:var(--silver); font-weight:bold; cursor:pointer; }}
button:focus-visible,input:focus-visible {{ outline:3px solid var(--hot); outline-offset:2px; }} #count {{ background:#000; color:var(--acid); padding:6px; }}
.sheets {{ line-height:2; font-family:"Courier New",monospace; }} .sheets a {{ display:inline-block; margin-right:8px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:7px; }}
.card {{ border:3px outset var(--silver); background:#fff; padding:4px; }} .card label {{ display:block; cursor:pointer; }}
.card img {{ display:block; width:100%; aspect-ratio:1; object-fit:contain; background:#333; }} .card span,.card small {{ display:block; font-family:"Courier New",monospace; font-size:12px; padding:2px; }}
.card small {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
@media(max-width:500px) {{ h1{{font-size:25px}} main{{padding:6px}} .grid{{grid-template-columns:repeat(2,minmax(0,1fr));gap:5px}} }}
</style></head><body><main>
<h1>CAT OF THE DAY! // ALL-CAT MANUAL REVIEW</h1>
<div class="warning"><b>PERSONALITY FIRST.</b> This is every decodable source image: {len(records):,} total. No image was rejected for low resolution, blur, portrait orientation, aspect ratio, or technical ugliness. Look for loafs, bleps, cursed geometry, dramatic expressions, mid-action disasters, and cats in human predicaments. Select only the cats that make you laugh or feel something.</div>
<div class="toolbar"><button id="all" type="button">CHECK ALL</button><button id="none" type="button">UNCHECK ALL</button><button id="download" type="button">DOWNLOAD picks.json</button><input id="search" type="search" placeholder="filter filename/block"><span id="count"></span></div>
<p class="sheets"><b>CONTACT SHEETS:</b> {sheet_links}</p><hr>
<section class="grid">{''.join(cards)}</section>
<script>
const cards={data}; const boxes=[...document.querySelectorAll('.pick')]; const count=document.querySelector('#count');
function update(){{count.textContent=boxes.filter(x=>x.checked).length+' / '+boxes.length+' SELECTED';}}
boxes.forEach(x=>x.addEventListener('change',update));
document.querySelector('#all').onclick=()=>{{boxes.forEach(x=>x.checked=true);update();}};
document.querySelector('#none').onclick=()=>{{boxes.forEach(x=>x.checked=false);update();}};
document.querySelector('#search').oninput=(event)=>{{const q=event.target.value.toLowerCase();document.querySelectorAll('.card').forEach(card=>card.hidden=q&&!card.dataset.search.includes(q));}};
document.querySelector('#download').onclick=()=>{{const picks=cards.filter((_,i)=>boxes[i].checked).map(x=>({{sourceKey:x.sourceKey,srcHash:x.srcHash,path:x.path,w:x.w,h:x.h}}));const blob=new Blob([JSON.stringify({{schema:1,manual:true,picks}},null,2)],{{type:'application/json'}});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='picks.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}};
update();
</script></main></body></html>'''
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    (REVIEW_DIR / "index.html").write_text(page, encoding="utf-8")
    POOL_FILE.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def main() -> int:
    setup_console()
    records = load_records()
    for index, record in enumerate(records, 1):
        record["reviewIndex"] = index
    print(f"Manual review pool: {len(records):,} decodable images")
    print("No resolution, aspect, sharpness, colour, or portrait filter is applied.")
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    SHEETS_DIR.mkdir(parents=True, exist_ok=True)
    for done, record in enumerate(records, 1):
        make_thumb(record)
        if done % 100 == 0 or done == len(records):
            print(f"\r  thumbnails {done:,}/{len(records):,}", end="", flush=True)
    print()
    sheet_paths = []
    for start in range(0, len(records), PER_SHEET):
        chunk = records[start:start + PER_SHEET]
        sheet_paths.append(make_sheet(chunk, start // PER_SHEET + 1))
    write_page(records, sheet_paths)
    print(f"  contact sheets: {len(sheet_paths):,}")
    print(f"  review page   : {REVIEW_DIR / 'index.html'}")
    print("  source images : untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
