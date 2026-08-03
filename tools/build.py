"""
PHASE 2 -- static site generator.

Reads data/manifest.json + data/order.json and writes the whole site into
dist/. No framework, no bundler, no npm. The only runtime dependency of the
generated site is a browser.

This script NEVER touches dist/img/ -- that tree belongs to curate.py.

Usage:
    python tools/build.py
    python tools/build.py --site-url https://cats.example.com
    python tools/build.py --out ../preview --launch-day 2026-03-01   # QA build
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import random
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SITE_SRC = PROJECT_ROOT / "site"
EPOCH = dt.date(1970, 1, 1)
FACTS_FILE = DATA_DIR / "facts.json"
SEED_FACTS = 20250401
FACT_DAY_OFFSET = 173

DEFAULT_SITE_URL = "https://cat-of-the-day.pages.dev"
DEFAULT_GUESTBOOK = "https://github.com/YOUR-USERNAME/cat-of-the-day/discussions"

COUNTER_BASE = 31337
COUNTER_PER_DAY = 137

# ---------------------------------------------------------------------------
# Date helpers -- identical arithmetic to app.js
# ---------------------------------------------------------------------------

def day_number(d: dt.date) -> int:
    """floor(Date.UTC(y, m, d) / 86400000)"""
    return (d - EPOCH).days


def day_to_date(n: int) -> dt.date:
    return EPOCH + dt.timedelta(days=n)


def iso(d: dt.date) -> str:
    return d.isoformat()


def pretty(d: dt.date) -> str:
    return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")


def rfc822(d: dt.date) -> str:
    return dt.datetime(d.year, d.month, d.day, 12, 0, 0,
                       tzinfo=dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def counter_for(day: int, launch: int) -> int:
    h = 2166136261
    for ch in iso(day_to_date(day)):
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return COUNTER_BASE + (day - launch) * COUNTER_PER_DAY + (h % 89)


def cat_caption(entry: dict, number: int) -> str:
    """Use the vision meme caption, with a useful cat-only fallback."""
    caption = " ".join(str(entry.get("caption", "")).split()).strip()
    return caption or f"Cat #{number}."


def e(s: str) -> str:
    return html.escape(str(s), quote=True)


# ---------------------------------------------------------------------------
# Hand-authored SVG furniture. Nothing here was downloaded.
# ---------------------------------------------------------------------------

def svg_88x31(kind: str) -> str:
    """88x31 web buttons, drawn by hand. No logos, no ripped artwork."""
    head = ('<svg xmlns="http://www.w3.org/2000/svg" width="88" height="31" '
            'viewBox="0 0 88 31" shape-rendering="crispEdges" role="img" ')

    if kind == "netscape":
        return (head + 'aria-label="Best viewed in Netscape">'
                '<rect width="88" height="31" fill="#000000"/>'
                '<rect x="0" y="0" width="24" height="31" fill="#000080"/>'
                '<text x="12" y="23" fill="#00FF00" font-family="Courier New,monospace"'
                ' font-size="21" font-weight="bold" text-anchor="middle">N</text>'
                '<text x="27" y="14" fill="#FFFFFF" font-family="Courier New,monospace"'
                ' font-size="8" font-weight="bold">BEST VIEWED IN</text>'
                '<text x="27" y="26" fill="#00FF00" font-family="Courier New,monospace"'
                ' font-size="11" font-weight="bold">NETSCAPE</text>'
                '</svg>')

    if kind == "notepad":
        return (head + 'aria-label="Made with Notepad">'
                '<rect width="88" height="31" fill="#C0C0C0"/>'
                '<rect x="4" y="5" width="19" height="21" fill="#FFFFFF" stroke="#000000"/>'
                '<g fill="#000080">'
                '<rect x="7" y="9" width="13" height="1.5"/>'
                '<rect x="7" y="13" width="13" height="1.5"/>'
                '<rect x="7" y="17" width="13" height="1.5"/>'
                '<rect x="7" y="21" width="8" height="1.5"/>'
                '</g>'
                '<text x="27" y="14" fill="#000000" font-family="Courier New,monospace"'
                ' font-size="8" font-weight="bold">MADE WITH</text>'
                '<text x="27" y="26" fill="#000000" font-family="Courier New,monospace"'
                ' font-size="11" font-weight="bold">NOTEPAD</text>'
                '</svg>')

    if kind == "cats":
        # Toe pads pulled clear of the main pad -- at 88x31 an overlapping paw
        # just renders as a green blob.
        return (head + 'aria-label="Powered by cats">'
                '<rect width="88" height="31" fill="#FF00FF"/>'
                '<rect x="0" y="0" width="26" height="31" fill="#000000"/>'
                '<g fill="#00FF00" transform="translate(13,19)">'
                '<ellipse cx="0" cy="2.6" rx="5.4" ry="4.1"/>'
                '<ellipse cx="-5.6" cy="-4.6" rx="1.9" ry="2.4"/>'
                '<ellipse cx="-1.9" cy="-6.8" rx="1.9" ry="2.4"/>'
                '<ellipse cx="1.9" cy="-6.8" rx="1.9" ry="2.4"/>'
                '<ellipse cx="5.6" cy="-4.6" rx="1.9" ry="2.4"/>'
                '</g>'
                '<text x="29" y="14" fill="#000000" font-family="Courier New,monospace"'
                ' font-size="8" font-weight="bold">POWERED BY</text>'
                '<text x="29" y="27" fill="#000000" font-family="Courier New,monospace"'
                ' font-size="12" font-weight="bold">CATS!</text>'
                '</svg>')

    # kind == "noai"
    return (head + 'aria-label="No AI cats here">'
            '<rect width="88" height="31" fill="#000000"/>'
            '<g transform="translate(14,15.5)">'
            '<circle r="10" fill="none" stroke="#FF00FF" stroke-width="2.6"/>'
            '<text x="0" y="4" fill="#FFFFFF" font-family="Courier New,monospace"'
            ' font-size="11" font-weight="bold" text-anchor="middle">AI</text>'
            '<line x1="-7.1" y1="7.1" x2="7.1" y2="-7.1" stroke="#FF00FF"'
            ' stroke-width="2.6"/>'
            '</g>'
            '<text x="27" y="14" fill="#00FF00" font-family="Courier New,monospace"'
            ' font-size="9" font-weight="bold">NO AI CATS</text>'
            '<text x="27" y="26" fill="#00FF00" font-family="Courier New,monospace"'
            ' font-size="9" font-weight="bold">HERE. EVER.</text>'
            '</svg>')


CONSTRUCTION_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="44" height="38" viewBox="0 0 44 38"'
    ' role="img" aria-label="An animated traffic cone and shovel">'
    # cone
    '<path d="M20 4 L28 30 L12 30 Z" fill="#FF6A00" stroke="#000" stroke-width="1.5"/>'
    '<path d="M16.2 16 L23.8 16 L24.8 21 L15.2 21 Z" fill="#FFFFFF"/>'
    '<rect x="8" y="30" width="24" height="5" rx="1" fill="#FF6A00"'
    ' stroke="#000" stroke-width="1.5"/>'
    # bobbing shovel
    '<g class="digger">'
    '<rect x="33" y="8" width="2.6" height="17" fill="#8B5A2B" stroke="#000"'
    ' stroke-width="1"/>'
    '<path d="M30.4 24 L38.2 24 L36.6 32 L32 32 Z" fill="#C0C0C0" stroke="#000"'
    ' stroke-width="1.2"/>'
    '</g>'
    '</svg>'
)

VIEWER_ICON = (
    '<svg class="viewer-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16"'
    ' viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
    '<rect x="0.5" y="0.5" width="15" height="15" fill="#FFFFFF" stroke="#000000"/>'
    '<rect x="2" y="2" width="12" height="12" fill="#000080"/>'
    '<circle cx="5.5" cy="5.5" r="1.6" fill="#FFFF00"/>'
    '<path d="M2 14 L6.5 8 L9.5 11.5 L11.5 9 L14 14 Z" fill="#00A000"/>'
    '</svg>'
)

INFO_ICON = (
    '<svg class="fact-info-icon" xmlns="http://www.w3.org/2000/svg" width="52" height="52"'
    ' viewBox="0 0 52 52" role="img" aria-label="Information">'
    '<circle cx="26" cy="26" r="22" fill="#0000AA" stroke="#FFFFFF" stroke-width="3"/>'
    '<circle cx="26" cy="15" r="3.5" fill="#FFFFFF"/>'
    '<path d="M22 22 H28 V39 H31 V43 H21 V39 H24 V26 H22 Z" fill="#FFFFFF"/>'
    '</svg>'
)


# ---------------------------------------------------------------------------
# Page shell
# ---------------------------------------------------------------------------

def head(cfg: dict, *, title: str, desc: str, path: str,
         og_image: str | None = None, og_w: int = 0, og_h: int = 0,
         preload: str | None = None) -> str:
    url = cfg["site_url"].rstrip("/") + path
    img = og_image or (cfg["site_url"].rstrip("/") + "/img/og/"
                       + iso(day_to_date(cfg["today"])) + ".jpg")
    if not og_image and not (og_w and og_h):
        og_w, og_h = 1200, 630
    image_type = "image/jpeg" if img.lower().endswith((".jpg", ".jpeg")) else "image/webp"
    parts = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>{e(title)}</title>',
        f'<meta name="description" content="{e(desc)}">',
        f'<link rel="canonical" href="{e(url)}">',
        '<meta name="theme-color" content="#000080">',
        # Open Graph -- baked in at build time. Client rendering these would
        # mean no unfurl on Discord/Bluesky/X, which is the whole distribution
        # mechanism for a site like this.
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="CAT OF THE DAY">',
        f'<meta property="og:title" content="{e(title)}">',
        f'<meta property="og:description" content="{e(desc)}">',
        f'<meta property="og:url" content="{e(url)}">',
        f'<meta property="og:image" content="{e(img)}">',
        f'<meta property="og:image:alt" content="{e(desc)}">',
        f'<meta property="og:image:type" content="{image_type}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{e(title)}">',
        f'<meta name="twitter:description" content="{e(desc)}">',
        f'<meta name="twitter:image" content="{e(img)}">',
    ]
    if og_w and og_h:
        parts.insert(-4, f'<meta property="og:image:width" content="{og_w}">')
        parts.insert(-4, f'<meta property="og:image:height" content="{og_h}">')
    parts += [
        '<link rel="icon" href="/favicon.svg" type="image/svg+xml">',
        '<link rel="alternate" type="application/rss+xml" title="Cat of the Day"'
        ' href="/cats.xml">',
    ]
    if preload:
        parts.append(f'<link rel="preload" as="image" href="{e(preload)}"'
                     ' type="image/avif" fetchpriority="high">')
    parts.append('<link rel="stylesheet" href="/style.css">')
    # Applied before first paint so the toggle never flashes.
    parts.append('<script>try{if(localStorage.getItem("catday.motion")==="off")'
                 'document.documentElement.className="no-motion";}catch(e){}</script>')
    return "\n  ".join(parts)


def marquee(cfg: dict) -> str:
    spans = "".join(
        ('<span class="pink">' if item["kind"] == "MEME" else '<span>')
        + e(f'{item["kind"]}: {item["text"]}') + '</span>'
        for item in cfg["marquee_facts"]
    )
    # Emitted twice: the CSS translates the track by exactly -50%, so the
    # second copy lands where the first began and the band is never empty.
    return ('<div class="marquee" aria-hidden="true">'
            f'<div class="marquee-track">{spans}{spans}</div></div>')


def masthead(sub: str) -> str:
    return (
        '<p class="crown">+-+-+ ESTABLISHED ON THE INFORMATION SUPERHIGHWAY +-+-+</p>'
        '<h1>CAT OF THE DAY!</h1>'
        f'<p class="subtitle"><span class="star">*~*</span> {sub} '
        '<span class="star">*~*</span> <span class="blink">NEW!</span></p>'
    )


def counter_block(value: int) -> str:
    digits = "".join(f"<b>{d}</b>" for d in f"{value:06d}")
    return (
        '<div class="counter-box">'
        '<p class="counter-label">TOTALLY REAL VISITOR COUNTER</p>'
        f'<div class="odometer" data-odometer="{value}" role="img"'
        f' aria-label="Visitor counter showing {value}">{digits}</div>'
        '<p class="counter-disclaimer">(it is not real)</p>'
        '</div>'
    )


def construction() -> str:
    return (
        '<div class="construction" role="img"'
        ' aria-label="An under construction sign, permanently">'
        '<div class="hazard"></div>'
        '<div class="construction-mid">'
        f'{CONSTRUCTION_SVG}'
        '<span class="construction-text">UNDER CONSTRUCTION</span>'
        f'{CONSTRUCTION_SVG}'
        '</div>'
        '<div class="hazard"></div>'
        '</div>'
    )


def buttons88() -> str:
    items = "".join(f"<li>{svg_88x31(k)}</li>"
                    for k in ("netscape", "notepad", "cats", "noai"))
    return f'<ul class="buttons88">{items}</ul>'


def footer(cfg: dict, last_updated: str) -> str:
    return (
        f'{construction()}'
        '<hr>'
        f'{buttons88()}'
        '<p class="webring">'
        '<a class="guestbook" href="' + e(cfg["guestbook_url"]) + '"'
        ' rel="noopener">SIGN MY GUESTBOOK</a></p>'
        '<hr class="thin">'
        '<div class="footer">'
        '<p><button type="button" class="motion-toggle">'
        '[ MOTION: ON -- MAKE IT STOP ]</button></p>'
        '<p><a href="/cats.xml">SUBSCRIBE TO THE CAT RSS FEED</a></p>'
        f'<p>LAST UPDATED: {e(last_updated)}</p>'
        f'<p>{cfg["count"]} CATS &middot; {cfg["fact_count"]} FACTS &middot; '
        'ONE OF EACH PER DAY</p>'
        '<p>NO BACKEND &middot; NO DATABASE &middot; NO COOKIES</p>'
        '<p>THIS PAGE IS BEST ENJOYED AT 800x600 OR HIGHER</p>'
        '</div>'
    )


def shell(cfg: dict, *, head_html: str, body: str) -> str:
    return (
        '<!doctype html>\n'
        '<html lang="en">\n<head>\n  '
        + head_html +
        '\n</head>\n<body>\n'
        '<a class="skip-link" href="#main">Skip to the cat</a>\n'
        '<div class="panel">\n'
        + body +
        '\n</div>\n'
        f'<script>window.CATDAY={json.dumps(cfg["client"])};</script>\n'
        '<script src="/app.js" defer></script>\n'
        '</body>\n</html>\n'
    )


# ---------------------------------------------------------------------------
# Cat picture
# ---------------------------------------------------------------------------

def picture(entry: dict, *, hero: bool, alt: str, have_avif: bool) -> str:
    cid = entry["id"]
    style = f'background-image:url({entry["lqip"]});background-color:{entry["dominant"]}'
    attrs = (' fetchpriority="high" decoding="async" data-hero'
             if hero else ' loading="lazy" decoding="async"')
    sources = ""
    if have_avif:
        sources = f'<source srcset="/img/full/{cid}.avif" type="image/avif">'
    return (
        '<picture>'
        f'{sources}'
        f'<source srcset="/img/full/{cid}.webp" type="image/webp">'
        f'<img src="/img/full/{cid}.webp" width="{entry["w"]}" height="{entry["h"]}"'
        f' alt="{e(alt)}" style="{style}"{attrs}>'
        '</picture>'
    )


def viewer(entry: dict, alt: str, have_avif: bool, hero: bool) -> str:
    return (
        '<div class="viewer">'
        '<div class="viewer-bar">'
        f'{VIEWER_ICON}'
        '<span class="viewer-title">CAT_OF_THE_DAY.JPG &mdash; Paint Shop Pro</span>'
        '<span class="viewer-btns" aria-hidden="true">'
        '<span>_</span><span>&#9633;</span><span>&times;</span></span>'
        '</div>'
        '<div class="viewer-menu" aria-hidden="true">'
        '<span><u>F</u>ile</span><span><u>E</u>dit</span><span><u>I</u>mage</span>'
        '<span><u>C</u>olors</span><span><u>H</u>elp</span>'
        '</div>'
        '<div class="viewer-canvas">'
        f'{picture(entry, hero=hero, alt=alt, have_avif=have_avif)}'
        '</div>'
        f'<p class="viewer-caption">{e(cat_caption(entry, int(entry["id"].split("-")[1])))}</p>'
        '<div class="viewer-status" aria-hidden="true">'
        f'<span>{entry["w"]} x {entry["h"]}</span>'
        '<span>16.7 MILLION COLORS</span>'
        f'<span class="grow">{e(entry["id"].upper())}.JPG</span>'
        '</div>'
        '</div>'
    )


def fact_dialog(fact: dict) -> str:
    note = ""
    if fact.get("note"):
        note = f'<p class="fact-note">{e(fact["note"])}</p>'
    kind = str(fact["kind"]).upper()
    return (
        '<section class="fact-dialog" aria-labelledby="fact-title">'
        '<div class="fact-titlebar"><span id="fact-title">DID U KNOW???</span>'
        '<span class="fact-close" aria-hidden="true">&times;</span></div>'
        '<div class="fact-body">'
        f'{INFO_ICON}'
        '<div class="fact-copy">'
        f'<span class="fact-badge fact-{e(kind.lower())}">{e(kind)}</span>'
        f'<p class="fact-text">{e(fact["text"])}</p>{note}'
        '</div></div></section>'
    )


def og_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Prefer Comic Sans locally; retain a safe runner fallback."""
    candidates = (
        Path("C:/Windows/Fonts/comicbd.ttf"),
        Path("C:/Windows/Fonts/comic.ttf"),
        Path("/usr/share/fonts/opentype/comic-neue/ComicNeue-Bold.otf"),
        Path("/usr/share/fonts/truetype/comic-neue/ComicNeue-Bold.ttf"),
        Path("/usr/share/fonts/truetype/comicneue/ComicNeue-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    )
    for path in candidates:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap_caption(draw: ImageDraw.ImageDraw, caption: str,
                 font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = caption.split()
    lines: list[str] = []
    line = ""
    for word in words:
        trial = f"{line} {word}".strip()
        width = draw.textbbox((0, 0), trial, font=font)[2]
        if line and width > max_width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines[:2]


def generate_og_card(out: Path, day: int, entry: dict) -> Path:
    """Write a crawler-friendly 1200x630 JPEG with a social-safe crop."""
    d = iso(day_to_date(day))
    target = out / "img" / "og" / f"{d}.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = PROJECT_ROOT / "dist" / "img" / "full" / f'{entry["id"]}.webp'
    if not source.exists():
        raise FileNotFoundError(f"missing full image for OG card: {source}")

    canvas = Image.new("RGB", (1200, 630), "#000080")
    with Image.open(source) as opened:
        cat = ImageOps.exif_transpose(opened) or opened
        cat = cat.convert("RGB")
        cat.thumbnail((1152, 472), Image.Resampling.LANCZOS)
        x = (1200 - cat.width) // 2
        y = 18 + (472 - cat.height) // 2
        # Chunky silver frame keeps the image readable against navy.
        ImageDraw.Draw(canvas).rectangle(
            (x - 6, y - 6, x + cat.width + 5, y + cat.height + 5),
            fill="#C0C0C0", outline="#FFFFFF", width=3,
        )
        canvas.paste(cat, (x, y))

    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 506, 1199, 629), fill="#000080")
    caption = cat_caption(entry, int(entry["id"].split("-")[1]))
    size = 54
    font = og_font(size)
    lines = wrap_caption(draw, caption, font, 1100)
    while (len(lines) > 1 or draw.textbbox((0, 0), lines[0], font=font)[2] > 1100) and size > 36:
        size -= 3
        font = og_font(size)
        lines = wrap_caption(draw, caption, font, 1100)
    line_height = size + 8
    top = 516 + max(0, (104 - line_height * len(lines)) // 2)
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        x = (1200 - (box[2] - box[0])) // 2
        draw.text((x + 3, top + 3), line, font=font, fill="#000000")
        draw.text((x, top), line, font=font, fill="#FFFFCC",
                  stroke_width=2, stroke_fill="#000000")
        top += line_height
    canvas.save(target, "JPEG", quality=80, optimize=True, progressive=True,
                subsampling="4:2:0")
    return target


def nav(cfg: dict, day: int) -> str:
    launch, today = cfg["launch"], cfg["today"]
    prev_ok = day > launch
    next_ok = day < today

    if prev_ok:
        prev = (f'<a class="btn" data-nav-prev href="/cat/{iso(day_to_date(day-1))}/"'
                ' rel="prev">&lsaquo;&lsaquo; YESTERDAY\'S CAT</a>')
    else:
        prev = ('<a class="btn" data-nav-prev aria-disabled="true" role="link"'
                ' title="This is the very first cat. There is no yesterday.">'
                '&lsaquo;&lsaquo; NO EARLIER CATS</a>')

    if next_ok:
        nxt = (f'<a class="btn" data-nav-next href="/cat/{iso(day_to_date(day+1))}/"'
               ' rel="next">TOMORROW\'S CAT &rsaquo;&rsaquo;</a>')
    else:
        # The joke lands exactly where it should: at the edge of time.
        nxt = ('<a class="btn" data-nav-next aria-disabled="true" role="link"'
               ' title="Tomorrow has not happened yet.">NOT YET!! &rsaquo;&rsaquo;</a>')

    random_day = max(launch, min(today - 1, day - 1))
    random_href = f'/cat/{iso(day_to_date(random_day))}/'
    return (
        f'<div class="controls">{prev}{nxt}</div>'
        '<div class="controls-secondary">'
        '<a class="btn" href="/archive/">THE WHOLE CAT ARCHIVE</a>'
        f'<a class="btn" href="{random_href}" data-random-cat>RANDOM CAT</a>'
        '</div>'
        '<p class="counter-disclaimer" style="text-align:center">'
        'TIP: use the &larr; and &rarr; arrow keys</p>'
    )


def cat_page(cfg: dict, day: int, *, is_index: bool) -> str:
    d = day_to_date(day)
    entry = cfg["entry_for"](day)
    n = int(entry["id"].split("-")[1])
    day_no = day - cfg["launch"] + 1
    alt = cat_caption(entry, n)
    title = ("CAT OF THE DAY! " + pretty(d)) if is_index else \
            (f"Cat of the Day &mdash; {iso(d)}").replace("&mdash;", "--")
    desc = (f"{cat_caption(entry, n)} The cat of the day for {pretty(d)}. "
            "A new cat every single day.")
    path = "/" if is_index else f"/cat/{iso(d)}/"
    site = cfg["site_url"].rstrip("/")

    head_html = head(
        cfg, title=title, desc=desc, path=path,
        og_image=f'{site}/img/og/{iso(d)}.jpg',
        og_w=1200, og_h=630,
        preload=(f'/img/full/{entry["id"]}.avif' if cfg["have_avif"] else None),
    )

    sub = "TODAY'S CAT IS RIGHT HERE" if is_index else "FROM THE ARCHIVE"
    body = (
        masthead(sub)
        + marquee(cfg)
        + '<hr>'
        + '<main id="main">'
        # The window and its controls are one unit and share a width, so the
        # buttons stay visually attached to the window they drive.
        + f'<div class="viewer-unit" style="--ar:{round(entry["w"]/entry["h"], 4)}">'
        + viewer(entry, alt, cfg["have_avif"], hero=True)
        + nav(cfg, day)
        + '</div>'
        + fact_dialog(cfg["fact_for"](day))
        + '<div class="caption">'
        + f'<p class="day">{e(pretty(d))}</p>'
        + f'<p class="meta">DAY {day_no} OF THE CAT PROJECT &middot; '
        + f'CAT #{n} OF {cfg["count"]} &middot; {e(iso(d))}</p>'
        + '</div>'
        + '</main>'
        + '<hr>'
        + '<div class="prose"><p>This is the cat for this day. Tomorrow there '
        + 'will be a different cat. The cat is chosen by the date itself, so '
        + 'everybody on earth is looking at the same cat as you are right now. '
        + 'There is no database. There is no server. There is just a date and '
        + 'a very long list of cats.</p></div>'
        + counter_block(counter_for(day, cfg["launch"]))
        + '<hr>'
        + footer(cfg, cfg["last_updated"])
    )

    local = dict(cfg["client"])
    local["date"] = iso(d)
    scoped = dict(cfg)
    scoped["client"] = local
    return shell(scoped, head_html=head_html, body=body)


def archive_page(cfg: dict) -> str:
    days = list(range(cfg["today"], cfg["launch"] - 1, -1))
    cards = []
    for day in days:
        d = day_to_date(day)
        entry = cfg["entry_for"](day)
        n = int(entry["id"].split("-")[1])
        style = (f'background-image:url({entry["lqip"]});'
                 f'background-color:{entry["dominant"]}')
        is_today = day == cfg["today"]
        cards.append(
            f'<li><a href="/cat/{iso(d)}/">'
            f'<img src="/img/thumb/{entry["id"]}.webp" width="320" height="320"'
            f' loading="lazy" decoding="async" alt="{e(cat_caption(entry, n))}"'
            f' style="{style}">'
            f'<span class="cap"><b>{e(d.strftime("%b %d, %Y"))}</b>'
            f'{e(cat_caption(entry, n))} &middot; CAT #{n}'
            f'{" &middot; TODAY" if is_today else ""}</span>'
            '</a></li>'
        )

    head_html = head(
        cfg,
        title="THE WHOLE CAT ARCHIVE -- Cat of the Day",
        desc=f"Every cat so far. {len(days)} day(s) of cats, newest first.",
        path="/archive/",
    )
    body = (
        masthead("THE WHOLE CAT ARCHIVE")
        + marquee(cfg)
        + '<hr>'
        + '<main id="main">'
        + '<div class="controls-secondary">'
        + '<a class="btn" href="/">&lsaquo;&lsaquo; BACK TO TODAY\'S CAT</a>'
        + f'<a class="btn" href="/cat/{iso(day_to_date(max(cfg["launch"], cfg["today"] - 1)))}/" data-random-cat>RANDOM CAT</a>'
        + '</div>'
        + f'<p class="caption meta" style="margin-top:14px">'
        + f'{len(days)} DAY(S) OF CATS &middot; NEWEST FIRST</p>'
        + f'<ul class="grid">{"".join(cards)}</ul>'
        + '</main>'
        + '<hr>'
        + footer(cfg, cfg["last_updated"])
    )
    return shell(cfg, head_html=head_html, body=body)


def notfound_page(cfg: dict) -> str:
    head_html = head(cfg, title="404 -- NO CAT HERE",
                     desc="There is no cat at this address.", path="/404.html")
    body = (
        masthead("SOMETHING WENT WRONG")
        + '<hr>'
        + '<main id="main">'
        + '<p class="big404">404</p>'
        + '<p class="caption day" style="font-size:24px">NO CAT HERE</p>'
        + '<div class="prose"><p>You have reached a page that does not exist. '
        + 'The cat you are looking for may have wandered off, or may never '
        + 'have existed in the first place. Cats are like that.</p></div>'
        + '<div class="controls-secondary">'
        + '<a class="btn" href="/">TAKE ME TO TODAY\'S CAT</a>'
        + '<a class="btn" href="/archive/">THE WHOLE CAT ARCHIVE</a>'
        + '</div>'
        + '</main>'
        + '<hr>'
        + footer(cfg, cfg["last_updated"])
    )
    return shell(cfg, head_html=head_html, body=body)


def rss(cfg: dict) -> str:
    site = cfg["site_url"].rstrip("/")
    days = list(range(cfg["today"], max(cfg["launch"], cfg["today"] - 29) - 1, -1))
    items = []
    for day in days:
        d = day_to_date(day)
        entry = cfg["entry_for"](day)
        n = int(entry["id"].split("-")[1])
        link = f"{site}/cat/{iso(d)}/"
        img = f'{site}/img/full/{entry["id"]}.webp'
        thumb = f'{site}/img/thumb/{entry["id"]}.webp'
        fact = cfg["fact_for"](day)
        fact_note = f' <em>{e(fact["note"])}</em>' if fact.get("note") else ""
        desc = (f'<p><img src="{img}" alt="{e(cat_caption(entry, n))}" width="{entry["w"]}"'
                f' height="{entry["h"]}"></p>'
                f'<p><strong>{e(cat_caption(entry, n))}</strong></p>'
                f'<p><strong>{e(fact["kind"])}:</strong> {e(fact["text"])}{fact_note}</p>')
        items.append(
            "    <item>\n"
            f"      <title>Cat of the Day -- {iso(d)} (cat #{n})</title>\n"
            f"      <link>{e(link)}</link>\n"
            f"      <guid isPermaLink=\"true\">{e(link)}</guid>\n"
            f"      <pubDate>{rfc822(d)}</pubDate>\n"
            f"      <description>{e(desc)}</description>\n"
            f"      <enclosure url=\"{e(thumb)}\" type=\"image/webp\" length=\"0\"/>\n"
            "    </item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        '  <channel>\n'
        '    <title>Cat of the Day</title>\n'
        f'    <link>{e(site)}/</link>\n'
        '    <description>A new cat every single day. No exceptions.</description>\n'
        '    <language>en</language>\n'
        f'    <lastBuildDate>{rfc822(day_to_date(cfg["today"]))}</lastBuildDate>\n'
        f'    <atom:link href="{e(site)}/cats.xml" rel="self"'
        ' type="application/rss+xml"/>\n'
        + "\n".join(items) + "\n"
        '  </channel>\n'
        '</rss>\n'
    )


def sitemap(cfg: dict) -> str:
    site = cfg["site_url"].rstrip("/")
    urls = [(f"{site}/", iso(day_to_date(cfg["today"])), "daily", "1.0"),
            (f"{site}/archive/", iso(day_to_date(cfg["today"])), "daily", "0.7")]
    for day in range(cfg["today"], cfg["launch"] - 1, -1):
        d = iso(day_to_date(day))
        urls.append((f"{site}/cat/{d}/", d, "yearly", "0.5"))
    body = "".join(
        f"  <url><loc>{e(u)}</loc><lastmod>{m}</lastmod>"
        f"<changefreq>{c}</changefreq><priority>{p}</priority></url>\n"
        for u, m, c, p in urls
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + body + '</urlset>\n')


HEADERS = """/img/*
  Cache-Control: public, max-age=31536000, immutable
/
  Cache-Control: public, max-age=300
/cat/*
  Cache-Control: public, max-age=3600
"""


def robots(cfg: dict) -> str:
    return ("User-agent: *\n"
            "Allow: /\n\n"
            f"Sitemap: {cfg['site_url'].rstrip('/')}/sitemap.xml\n")


def load_facts() -> list[dict]:
    if not FACTS_FILE.exists():
        raise SystemExit("data/facts.json is missing")
    facts = json.loads(FACTS_FILE.read_text(encoding="utf-8"))
    if not isinstance(facts, list) or len(facts) < 500:
        raise SystemExit("data/facts.json must contain at least 500 entries")
    valid_kinds = {"FACT", "MYTH", "RUMOR", "MEME", "LORE"}
    ids = set()
    for index, fact in enumerate(facts, 1):
        if not isinstance(fact, dict):
            raise SystemExit(f"fact #{index} is not an object")
        if fact.get("kind") not in valid_kinds or not str(fact.get("text", "")).strip():
            raise SystemExit(f"fact #{index} has an invalid kind or empty text")
        if fact.get("id") in ids:
            raise SystemExit(f"duplicate fact id: {fact.get('id')}")
        ids.add(fact.get("id"))
    shuffled = list(facts)
    random.Random(SEED_FACTS).shuffle(shuffled)
    return shuffled


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(description="Phase 2 static site generator")
    ap.add_argument("--out", default=str(PROJECT_ROOT / "dist"))
    ap.add_argument("--site-url", default=None)
    ap.add_argument("--guestbook", default=None)
    ap.add_argument("--launch-day", default=None,
                    help="YYYY-MM-DD override (QA/preview only)")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD override (testing)")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    manifest_path = DATA_DIR / "manifest.json"
    order_path = DATA_DIR / "order.json"
    if not manifest_path.exists() or not order_path.exists():
        print("ERROR: data/manifest.json or data/order.json missing.")
        print("       Run `python tools/curate.py` first.")
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    facts = load_facts()
    order_doc = json.loads(order_path.read_text(encoding="utf-8"))
    order = order_doc["order"]
    by_id = {m["id"]: m for m in manifest}
    n_cats = len(order)

    # ---- persistent site config -------------------------------------------
    # LAUNCH_DAY is written once and then reused forever. If the daily workflow
    # recomputed it from "now", the whole sequence would slide by a day on
    # every rebuild and permalinks would start lying about their own cat.
    site_cfg_path = DATA_DIR / "site.json"
    site_cfg = {}
    if site_cfg_path.exists():
        site_cfg = json.loads(site_cfg_path.read_text(encoding="utf-8"))

    today = (dt.date.fromisoformat(args.today) if args.today
             else dt.datetime.now(dt.timezone.utc).date())

    if args.launch_day:
        launch_date = dt.date.fromisoformat(args.launch_day)
    elif "launch_date" in site_cfg:
        launch_date = dt.date.fromisoformat(site_cfg["launch_date"])
    else:
        launch_date = today

    site_url = args.site_url or site_cfg.get("site_url") or DEFAULT_SITE_URL
    guestbook = args.guestbook or site_cfg.get("guestbook_url") or DEFAULT_GUESTBOOK

    # Persist only for the canonical build, never for a scratch QA build.
    if out == (PROJECT_ROOT / "dist").resolve():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        site_cfg_path.write_text(json.dumps({
            "launch_date": iso(launch_date),
            "launch_day": day_number(launch_date),
            "site_url": site_url,
            "guestbook_url": guestbook,
        }, indent=1), encoding="utf-8")

    launch = day_number(launch_date)
    today_n = day_number(today)
    if today_n < launch:
        print(f"ERROR: today ({iso(today)}) is before launch ({iso(launch_date)}).")
        return 1

    have_avif = (PROJECT_ROOT / "dist" / "img" / "full" / f"{order[0]}.avif").exists()

    def entry_for(day: int) -> dict:
        idx = ((day - launch) % n_cats + n_cats) % n_cats
        return by_id[order[idx]]

    def fact_for(day: int) -> dict:
        idx = ((day - launch + FACT_DAY_OFFSET) % len(facts) + len(facts)) % len(facts)
        return facts[idx]

    marquee_pool = [fact for fact in facts if fact["kind"] in {"FACT", "MEME"}]
    marquee_rng = random.Random(SEED_FACTS ^ today_n)
    marquee_facts = marquee_rng.sample(marquee_pool, 6)

    cfg = {
        "site_url": site_url,
        "guestbook_url": guestbook,
        "launch": launch,
        "today": today_n,
        "count": n_cats,
        "fact_count": len(facts),
        "have_avif": have_avif,
        "entry_for": entry_for,
        "fact_for": fact_for,
        "marquee_facts": marquee_facts,
        "today_id": entry_for(today_n)["id"],
        "last_updated": pretty(today).upper(),
        "client": {
            "launch": launch,
            "today": today_n,
            "date": iso(today),
            "n": n_cats,
            "base": "/",
        },
    }

    print("=" * 72)
    print("CAT OF THE DAY -- PHASE 2 BUILD")
    print("=" * 72)
    print(f"  out          : {out}")
    print(f"  site url     : {site_url}")
    print(f"  launch date  : {iso(launch_date)}  (LAUNCH_DAY = {launch})")
    print(f"  today        : {iso(today)}  (day {today_n})")
    print(f"  cats         : {n_cats}   sequence repeats every {n_cats} days")
    print(f"  facts        : {len(facts)}   seed {SEED_FACTS}, offset {FACT_DAY_OFFSET}")
    print(f"  today's cat  : {cfg['today_id']}")
    print(f"  AVIF sources : {'yes' if have_avif else 'no (WebP only)'}")

    # ---- clean everything except dist/img (owned by curate.py) ------------
    out.mkdir(parents=True, exist_ok=True)
    for child in out.iterdir():
        if child.name == "img":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()

    # ---- static assets ----------------------------------------------------
    for name in ("style.css", "app.js", "bg.svg", "favicon.svg"):
        shutil.copy2(SITE_SRC / name, out / name)

    # ---- pages ------------------------------------------------------------
    days = list(range(launch, today_n + 1))
    for day in days:
        generate_og_card(out, day, entry_for(day))

    (out / "index.html").write_text(cat_page(cfg, today_n, is_index=True),
                                    encoding="utf-8")

    for day in days:
        pdir = out / "cat" / iso(day_to_date(day))
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "index.html").write_text(cat_page(cfg, day, is_index=False),
                                         encoding="utf-8")

    (out / "archive").mkdir(parents=True, exist_ok=True)
    (out / "archive" / "index.html").write_text(archive_page(cfg), encoding="utf-8")
    (out / "404.html").write_text(notfound_page(cfg), encoding="utf-8")
    (out / "cats.xml").write_text(rss(cfg), encoding="utf-8")
    (out / "sitemap.xml").write_text(sitemap(cfg), encoding="utf-8")
    (out / "robots.txt").write_text(robots(cfg), encoding="utf-8")
    (out / "_headers").write_text(HEADERS, encoding="utf-8")

    html_files = sum(1 for _ in out.rglob("*.html"))
    img_files = sum(1 for _ in (out / "img").rglob("*")
                    if _.is_file()) if (out / "img").exists() else 0
    total_files = sum(1 for _ in out.rglob("*") if _.is_file())

    print(f"\n  permalinks   : {len(days)}  ({iso(day_to_date(launch))} .. {iso(today)})")
    print(f"  html pages   : {html_files}")
    print(f"  image files  : {img_files}")
    print(f"  total files  : {total_files}   (Cloudflare Pages limit: 20,000)")

    warn = []
    if "example.com" in site_url or "YOUR-" in site_url:
        warn.append(f"site_url looks like a placeholder ({site_url}). "
                    "Set it in data/site.json before you go live.")
    if "YOUR-USERNAME" in guestbook:
        warn.append("guestbook_url still contains YOUR-USERNAME. "
                    "Set it in data/site.json (see README).")
    if warn:
        print("\n  WARNINGS")
        for w in warn:
            print(f"    ! {w}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
