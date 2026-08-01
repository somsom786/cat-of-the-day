"""
PHASE 1 -- Curation pipeline.

Reads every `Cats.*` block (read-only), validates, filters, perceptually
dedupes, ranks, and re-encodes the winners into dist/img/. Writes
data/manifest.json and data/order.json.

Nothing inside a Cats.* folder is ever written, moved or renamed. Every output
byte is generated here -- no source file is ever copied through untouched.

Resumable: per-file probe results are cached in tools/.cache/hashes.json keyed
by (path, size, mtime), so a rerun skips the expensive decode+hash work.

Usage:
    python tools/curate.py
    python tools/curate.py --keep 1200            # top up the cat supply
    python tools/curate.py --no-cache             # force a full re-probe
    python tools/curate.py --budget-mb 250
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import os
import random
import shutil
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile, ImageOps, features

import imagehash

ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = 300_000_000

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "tools" / ".cache"
CACHE_FILE = CACHE_DIR / "hashes.json"
DIST_IMG = PROJECT_ROOT / "dist" / "img"
DATA_DIR = PROJECT_ROOT / "data"

# The daily sequence must be identical across rebuilds, forever.
SEED = 19980401

# --- filter thresholds -----------------------------------------------------
SHORT_EDGE_MIN = 800
SHORT_EDGE_FALLBACK = 600
ASPECT_MIN = 0.5
ASPECT_MAX = 2.0
MONO_STD_MIN = 8.0          # luminance stddev below this == blank/placeholder
PHASH_MAX_DISTANCE = 5      # <= 5 == same picture

# --- output geometry -------------------------------------------------------
FULL_LONG_EDGE = 1600
THUMB_LONG_EDGE = 320
LQIP_LONG_EDGE = 16

# Quality tiers, best first. Calibration walks down until the projected
# dist/img/ total fits the budget -- quality drops before the cat count does.
QUALITY_TIERS = [
    {"name": "A", "avif": 50, "webp": 80, "thumb": 72},
    {"name": "B", "avif": 46, "webp": 74, "thumb": 70},
    {"name": "C", "avif": 42, "webp": 68, "thumb": 68},
    {"name": "D", "avif": 38, "webp": 62, "thumb": 65},
    {"name": "E", "avif": 34, "webp": 56, "thumb": 62},
]

HAVE_AVIF = features.check("avif")


# ---------------------------------------------------------------------------
# Windows path plumbing + console
# ---------------------------------------------------------------------------

def long_path(p) -> str:
    s = os.fspath(p)
    if os.name != "nt":
        return s
    if not os.path.isabs(s):
        s = os.path.abspath(s)
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s.lstrip("\\")
    return "\\\\?\\" + s


def short_path(p) -> str:
    s = str(p)
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{int(n):,} B" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class Progress:
    """Small dependency-free progress bar."""

    def __init__(self, total: int, label: str, width: int = 34):
        self.total = max(1, total)
        self.label = label
        self.width = width
        self.n = 0
        self.start = time.time()
        self._last = 0.0
        self.draw()

    def step(self, k: int = 1) -> None:
        self.n += k
        now = time.time()
        if now - self._last > 0.08 or self.n >= self.total:
            self._last = now
            self.draw()

    def draw(self) -> None:
        frac = min(1.0, self.n / self.total)
        filled = int(self.width * frac)
        bar = "#" * filled + "." * (self.width - filled)
        elapsed = time.time() - self.start
        rate = self.n / elapsed if elapsed > 0.5 else 0
        eta = (self.total - self.n) / rate if rate > 0 else 0
        eta_s = f"eta {int(eta // 60):d}m{int(eta % 60):02d}s" if rate else "eta --"
        sys.stdout.write(
            f"\r  {self.label:<22} [{bar}] {self.n:>5,}/{self.total:,} "
            f"{frac*100:5.1f}%  {eta_s}   "
        )
        sys.stdout.flush()

    def done(self) -> None:
        self.n = self.total
        self.draw()
        elapsed = time.time() - self.start
        sys.stdout.write(f"\r  {self.label:<22} done in {elapsed:6.1f}s"
                         f"{' ' * 46}\n")
        sys.stdout.flush()


def rule(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_blocks(root: Path) -> list[Path]:
    return sorted((p for p in root.glob("Cats.*") if p.is_dir()), key=lambda p: p.name)


def walk_files(block: Path) -> list[Path]:
    out = []
    for dirpath, _dirs, names in os.walk(long_path(block)):
        for name in names:
            out.append(Path(dirpath) / name)
    return out


# ---------------------------------------------------------------------------
# Stage 1+2 probe: decode, measure, hash
# ---------------------------------------------------------------------------

def load_clean(raw: bytes) -> Image.Image:
    """Decode, honour EXIF orientation, then drop all metadata.

    exif_transpose has to run BEFORE the strip or every phone portrait ends up
    sideways. frombytes() rebuilds the image from raw pixels, so nothing from
    the source header (EXIF GPS, ICC, comments, thumbnails) survives.
    """
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im) or im
        if im.mode not in ("RGB",):
            im = im.convert("RGB")
        else:
            im.load()
        return Image.frombytes("RGB", im.size, im.tobytes())


def laplacian_variance(im: Image.Image) -> float:
    """Sharpness proxy, measured at a fixed scale so it is comparable.

    Laplacian variance scales with resolution, so a 4032px photo would beat an
    800px one on size alone. Normalising to a 512px long edge first makes the
    number mean 'how sharp is this picture' rather than 'how big is it'.
    """
    g = im.convert("L")
    g.thumbnail((512, 512), Image.BILINEAR)
    a = np.asarray(g, dtype=np.float32)
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0
    lap = (
        -4.0 * a[1:-1, 1:-1]
        + a[:-2, 1:-1] + a[2:, 1:-1]
        + a[1:-1, :-2] + a[1:-1, 2:]
    )
    return float(lap.var())


def monochrome_score(im: Image.Image) -> tuple[float, float]:
    """(luminance stddev, mean channel spread) on a 64x64 reduction."""
    small = im.copy()
    small.thumbnail((64, 64), Image.BILINEAR)
    a = np.asarray(small, dtype=np.float32)
    lum = a @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    spread = float((a.max(axis=2) - a.min(axis=2)).mean())
    return float(lum.std()), spread


def probe(path: Path) -> dict:
    rec = {
        "path": short_path(path),
        "ok": False,
        "reason": None,
        "bytes": 0,
        "w": 0, "h": 0,
        "phash": None,
        "sharp": 0.0,
        "lum_std": 0.0,
        "spread": 0.0,
        "src_hash": None,
        "mtime": 0.0,
    }
    lp = long_path(path)
    try:
        st = os.stat(lp)
        rec["bytes"] = st.st_size
        rec["mtime"] = st.st_mtime
        with open(lp, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        rec["reason"] = f"unreadable ({exc.__class__.__name__})"
        return rec

    rec["src_hash"] = hashlib.sha256(raw).hexdigest()

    try:
        im = load_clean(raw)
    except Exception as exc:  # noqa: BLE001 - any decoder error means "not an image"
        rec["reason"] = f"corrupt/not-an-image ({exc.__class__.__name__})"
        return rec

    rec["w"], rec["h"] = im.size
    if rec["w"] < 2 or rec["h"] < 2:
        rec["reason"] = "degenerate size"
        return rec

    rec["lum_std"], rec["spread"] = monochrome_score(im)
    rec["sharp"] = laplacian_variance(im)
    try:
        rec["phash"] = str(imagehash.phash(im, hash_size=8))
    except Exception as exc:  # noqa: BLE001
        rec["reason"] = f"phash failed ({exc.__class__.__name__})"
        return rec

    rec["ok"] = True
    return rec


# ---------------------------------------------------------------------------
# Stage 3: perceptual dedupe
# ---------------------------------------------------------------------------

def hex_to_uint64(h: str) -> int:
    return int(h, 16)


def dedupe(records: list[dict], max_distance: int) -> tuple[list[dict], int, list[tuple[int, int]]]:
    """Cluster by pHash Hamming distance; keep the highest-resolution member.

    The upstream collection was already SHA256-deduped, so byte-identical files
    are long gone. What survives are resizes and recompressions of the same
    photo -- only a perceptual hash catches those.
    """
    n = len(records)
    if n == 0:
        return [], 0, []

    vals = np.array([hex_to_uint64(r["phash"]) for r in records], dtype=np.uint64)

    # Union-find over pairs within the Hamming radius.
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Chunked pairwise popcount. n is a couple of thousand, so the full
    # n^2/2 comparison is a few million ops -- fast in numpy.
    bar = Progress(n, "dedupe scan")
    chunk = 256
    for start in range(0, n, chunk):
        end = min(n, start + chunk)
        block = vals[start:end, None] ^ vals[None, :]          # (c, n) uint64
        # popcount via uint8 view + lookup table
        popc = np.unpackbits(block.view(np.uint8)).reshape(block.shape[0], n, 64).sum(axis=2)
        for i in range(end - start):
            gi = start + i
            close = np.nonzero(popc[i] <= max_distance)[0]
            for gj in close:
                if int(gj) != gi:
                    union(gi, int(gj))
        bar.step(end - start)
    bar.done()

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    keepers = []
    dropped_pairs = []
    for members in clusters.values():
        if len(members) == 1:
            keepers.append(records[members[0]])
            continue
        # Highest resolution wins; file size breaks ties (less recompressed).
        members.sort(key=lambda i: (records[i]["w"] * records[i]["h"], records[i]["bytes"]),
                     reverse=True)
        keepers.append(records[members[0]])
        for loser in members[1:]:
            dropped_pairs.append((members[0], loser))
    removed = n - len(keepers)
    return keepers, removed, dropped_pairs


# ---------------------------------------------------------------------------
# Stage 4: ranking
# ---------------------------------------------------------------------------

def score(rec: dict) -> float:
    """Blend resolution, compression quality and sharpness into one number.

    Orientation is a nudge, not a gate. The source collection is ~73% portrait
    phone photos, so a hard landscape preference would exhaust the landscape
    supply long before the keep target and then backfill with exactly the
    portraits it was demoting -- in arbitrary order. A small bonus keeps the
    set visually varied without letting one axis run the whole ranking.
    """
    px = rec["w"] * rec["h"]
    # Resolution: log-scaled, saturating around 12 MP.
    res = min(1.0, math.log10(max(px, 1) / 4e5) / math.log10(30.0)) if px > 4e5 else 0.0
    res = max(0.0, res)

    # Bytes per pixel: a compression-quality proxy. ~0.5 bpp is generous JPEG,
    # under ~0.08 bpp means it has been squeezed hard somewhere upstream.
    bpp = rec["bytes"] / max(1, px)
    q = min(1.0, bpp / 0.35)

    # Sharpness: Laplacian variance, log-compressed. ~40+ is crisp.
    sharp = min(1.0, math.log10(max(rec["sharp"], 1.0)) / math.log10(300.0))

    ar = rec["w"] / rec["h"]
    if ar >= 1.0:
        orient = 1.0                        # landscape
    elif ar >= 0.9:
        orient = 1.0                        # square-ish
    elif ar >= 0.7:
        orient = 0.88                       # 3:4 phone portrait
    elif ar >= 0.6:
        orient = 0.75
    else:
        orient = 0.6                        # very tall

    # Colour interest: a photo with some chroma spread beats a flat grey one.
    colour = min(1.0, rec["spread"] / 60.0)

    base = 0.34 * res + 0.22 * q + 0.34 * sharp + 0.10 * colour
    return base * (0.82 + 0.18 * orient)


# ---------------------------------------------------------------------------
# Stage 5: encoding
# ---------------------------------------------------------------------------

def fit_long_edge(im: Image.Image, long_edge: int) -> Image.Image:
    w, h = im.size
    scale = long_edge / max(w, h)
    if scale >= 1.0:
        return im.copy()
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    return im.resize((nw, nh), Image.LANCZOS)


def dominant_hex(im: Image.Image) -> str:
    small = im.copy()
    small.thumbnail((80, 80), Image.BILINEAR)
    pal = small.quantize(colors=8, method=Image.MEDIANCUT)
    counts = pal.getcolors() or []
    if not counts:
        return "#808080"
    counts.sort(reverse=True)
    idx = counts[0][1]
    palette = pal.getpalette() or [128, 128, 128]
    r, g, b = palette[idx * 3: idx * 3 + 3]
    return f"#{r:02x}{g:02x}{b:02x}"


def lqip_data_uri(im: Image.Image) -> str:
    tiny = fit_long_edge(im, LQIP_LONG_EDGE)
    buf = io.BytesIO()
    tiny.save(buf, format="WEBP", quality=42, method=6)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/webp;base64,{b64}"


def encode_one(job: tuple[dict, str, dict, bool]) -> dict:
    """Re-encode one keeper into every output format. Returns manifest entry."""
    rec, cat_id, tier, write = job
    lp = long_path(Path(rec["path"]))
    with open(lp, "rb") as fh:
        raw = fh.read()
    im = load_clean(raw)               # metadata already gone at this point
    full = fit_long_edge(im, FULL_LONG_EDGE)
    thumb = fit_long_edge(im, THUMB_LONG_EDGE)

    out = {
        "id": cat_id,
        "w": full.size[0],
        "h": full.size[1],
        "dominant": dominant_hex(full),
        "lqip": lqip_data_uri(full),
        "srcHash": rec["src_hash"],
    }
    total = 0

    def emit(img: Image.Image, rel: str, fmt: str, quality: int) -> int:
        buf = io.BytesIO()
        if fmt == "AVIF":
            img.save(buf, format="AVIF", quality=quality, speed=6)
        else:
            img.save(buf, format="WEBP", quality=quality, method=6)
        data = buf.getvalue()
        if write:
            dest = DIST_IMG / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return len(data)

    if HAVE_AVIF:
        total += emit(full, f"full/{cat_id}.avif", "AVIF", tier["avif"])
    total += emit(full, f"full/{cat_id}.webp", "WEBP", tier["webp"])
    total += emit(thumb, f"thumb/{cat_id}.webp", "WEBP", tier["thumb"])

    out["bytes"] = total
    return out


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def load_cache(use_cache: bool) -> dict:
    if not use_cache or not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("  (cache unreadable, starting fresh)")
        return {}


def save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    tmp.replace(CACHE_FILE)


def cache_key(path: Path) -> str:
    try:
        st = os.stat(long_path(path))
        return f"{short_path(path)}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return f"{short_path(path)}|?|?"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="Phase 1 curation pipeline")
    ap.add_argument("--root", default=str(PROJECT_ROOT))
    ap.add_argument("--keep", type=int, default=800, help="target keep count")
    ap.add_argument("--budget-mb", type=float, default=250.0)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4)))
    ap.add_argument("--calibrate-sample", type=int, default=40)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    t0 = time.time()

    rule("CAT OF THE DAY -- PHASE 1 CURATION")
    print(f"  root        : {root}")
    print(f"  keep target : {args.keep}")
    print(f"  budget      : {args.budget_mb:.0f} MB for dist/img/")
    print(f"  workers     : {args.workers}")
    if HAVE_AVIF:
        print("  AVIF        : native Pillow support -- emitting .avif + .webp")
    else:
        print("  AVIF        : NOT AVAILABLE -- degrading to WebP-only.")
        print("                Install pillow-avif-plugin and rerun to add AVIF.")

    blocks = find_blocks(root)
    if not blocks:
        print("\nNo Cats.* blocks found. Nothing to curate.")
        return 1
    print(f"  blocks      : {', '.join(b.name for b in blocks)}")

    files: list[Path] = []
    for b in blocks:
        files.extend(walk_files(b))
    print(f"  files       : {len(files):,}")

    # ---------------- Stage 1+2: probe (cached) ----------------
    rule("STAGE 1-2  validate + measure")
    cache = load_cache(not args.no_cache)
    hits = [f for f in files if cache_key(f) in cache]
    misses = [f for f in files if cache_key(f) not in cache]
    print(f"  cache hits  : {len(hits):,}   to probe: {len(misses):,}")

    records: list[dict] = [cache[cache_key(f)] for f in hits]
    if misses:
        bar = Progress(len(misses), "probe")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for rec in ex.map(probe, misses):
                records.append(rec)
                cache[f"{rec['path']}|{rec['bytes']}|{int(rec['mtime'])}"] = rec
                bar.step()
        bar.done()
        save_cache(cache)

    records.sort(key=lambda r: r["path"])
    drops = Counter()

    valid = []
    for r in records:
        if not r["ok"]:
            drops["invalid: " + (r["reason"] or "unknown").split("(")[0].strip()] += 1
        else:
            valid.append(r)
    print(f"  decodable   : {len(valid):,} / {len(records):,}")

    def apply_filters(pool: list[dict], short_edge_min: int) -> tuple[list[dict], Counter]:
        d = Counter()
        out = []
        for r in pool:
            if min(r["w"], r["h"]) < short_edge_min:
                d[f"short edge < {short_edge_min}px"] += 1
                continue
            ar = r["w"] / r["h"]
            if ar < ASPECT_MIN or ar > ASPECT_MAX:
                d["aspect ratio outside 0.5-2.0"] += 1
                continue
            if r["lum_std"] < MONO_STD_MIN:
                d["near-monochrome / blank"] += 1
                continue
            out.append(r)
        return out, d

    survivors, fdrops = apply_filters(valid, SHORT_EDGE_MIN)
    drops.update(fdrops)
    print(f"  passed filters at {SHORT_EDGE_MIN}px short edge: {len(survivors):,}")

    # ---------------- Stage 3: perceptual dedupe ----------------
    rule("STAGE 3  perceptual dedupe (pHash, Hamming <= 5)")
    print("  Filenames upstream are SHA256 hex, so byte-identical copies are")
    print("  already gone. What is left are resizes and recompressions.")
    survivors, removed, pairs = dedupe(survivors, PHASH_MAX_DISTANCE)
    drops["perceptual duplicate"] = removed
    print(f"  removed     : {removed:,} duplicate(s)")
    print(f"  unique cats : {len(survivors):,}")

    # ---------------- adaptive keep count ----------------
    rule("STAGE 4  rank + select")
    threshold_used = SHORT_EDGE_MIN
    fallback_note = None
    if len(survivors) < 365:
        print(f"  Only {len(survivors)} survivors -- under a year of cats.")
        print(f"  Rerunning stage 2 with the short edge lowered to {SHORT_EDGE_FALLBACK}px...")
        survivors2, fdrops2 = apply_filters(valid, SHORT_EDGE_FALLBACK)
        survivors2, removed2, _ = dedupe(survivors2, PHASH_MAX_DISTANCE)
        print(f"  at {SHORT_EDGE_FALLBACK}px: {len(survivors2):,} unique cats "
              f"({removed2:,} perceptual dupes removed)")
        survivors = survivors2
        threshold_used = SHORT_EDGE_FALLBACK
        drops = Counter()
        drops.update(fdrops2)
        drops["perceptual duplicate"] = removed2
        if len(survivors) < 365:
            fallback_note = (
                f"Even at {SHORT_EDGE_FALLBACK}px only {len(survivors)} cats survive. "
                "Grab another block from the archive (Cats.00001) and rerun -- "
                "the pipeline globs Cats.* so it will just pick it up."
            )
        else:
            fallback_note = (
                f"The {SHORT_EDGE_FALLBACK}px fallback rescued this to {len(survivors)} cats "
                f"({len(survivors)/365:.1f} years). Another block would let the "
                f"{SHORT_EDGE_MIN}px bar go back up -- worth grabbing if you care about sharpness."
            )
        print(f"\n  {fallback_note}")

    for r in survivors:
        r["score"] = score(r)
    survivors.sort(key=lambda r: r["score"], reverse=True)

    keep_n = min(args.keep, len(survivors))
    keepers = survivors[:keep_n]

    if len(survivors) >= args.keep:
        runway = f"{keep_n} cats = {keep_n/365:.1f} years of daily cats."
    elif len(survivors) >= 365:
        runway = (f"Only {len(survivors)} survivors, so keeping all of them: "
                  f"{len(survivors)} days = {len(survivors)/365:.2f} years of runway "
                  f"before the sequence repeats.")
    else:
        runway = f"{keep_n} cats -- under a year. See the note above."
    print(f"  {runway}")

    ar_mix = Counter()
    for r in keepers:
        a = r["w"] / r["h"]
        ar_mix["landscape" if a > 1.05 else "square" if a >= 0.95 else "portrait"] += 1
    print(f"  orientation mix of keepers: "
          + ", ".join(f"{k} {v}" for k, v in ar_mix.most_common()))

    # ---------------- Stage 5: calibrate then encode ----------------
    rule("STAGE 5  re-encode (calibrating quality against the byte budget)")
    budget_bytes = args.budget_mb * 1024 * 1024
    rng_sample = random.Random(SEED)
    sample = rng_sample.sample(keepers, min(args.calibrate_sample, len(keepers)))

    chosen = QUALITY_TIERS[0]
    projection = 0
    for tier in QUALITY_TIERS:
        bar = Progress(len(sample), f"calibrate tier {tier['name']}")
        total = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            jobs = [(r, "calib", tier, False) for r in sample]
            for res in ex.map(encode_one, jobs):
                total += res["bytes"]
                bar.step()
        bar.done()
        per_cat = total / len(sample)
        projection = per_cat * len(keepers)
        fits = projection <= budget_bytes * 0.94
        print(f"    tier {tier['name']}  avif q{tier['avif']:>2} / webp q{tier['webp']:>2} "
              f"/ thumb q{tier['thumb']:>2}  ->  {human(per_cat)}/cat, "
              f"projected {human(projection)}  {'FITS' if fits else 'over budget'}")
        chosen = tier
        if fits:
            break
    else:
        print("    All tiers projected over budget; using the lowest and reporting actuals.")

    print(f"\n  chosen tier : {chosen['name']} "
          f"(avif q{chosen['avif']}, webp q{chosen['webp']}, thumb q{chosen['thumb']})")

    if DIST_IMG.exists():
        shutil.rmtree(DIST_IMG)
    (DIST_IMG / "full").mkdir(parents=True, exist_ok=True)
    (DIST_IMG / "thumb").mkdir(parents=True, exist_ok=True)

    ids = [f"cat-{i+1:04d}" for i in range(len(keepers))]
    jobs = [(r, cid, chosen, True) for r, cid in zip(keepers, ids)]

    bar = Progress(len(jobs), "encode")
    entries: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(encode_one, jobs):
            entries.append(res)
            bar.step()
    bar.done()

    entries.sort(key=lambda e: e["id"])
    out_bytes = sum(e["bytes"] for e in entries)
    file_count = sum(1 for _ in DIST_IMG.rglob("*") if _.is_file())

    # ---------------- Stage 6: manifest + order ----------------
    rule("STAGE 6  manifest + order")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest = [
        {k: e[k] for k in ("id", "w", "h", "dominant", "lqip", "srcHash")}
        for e in entries
    ]
    (DATA_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8")

    order = [e["id"] for e in entries]
    random.Random(SEED).shuffle(order)
    (DATA_DIR / "order.json").write_text(
        json.dumps({"seed": SEED, "count": len(order), "order": order}, indent=1),
        encoding="utf-8")
    print(f"  data/manifest.json : {len(manifest):,} entries")
    print(f"  data/order.json    : shuffled once with SEED={SEED} "
          f"(stable across rebuilds)")

    # ---------------- Final report ----------------
    rule("FINAL REPORT")
    print(f"  input files                 : {len(records):,}")
    print("  dropped by reason:")
    for reason, n in sorted(drops.items(), key=lambda kv: -kv[1]):
        if n:
            print(f"      {reason:<34} {n:>6,}")
    total_dropped = sum(drops.values())
    print(f"      {'TOTAL DROPPED':<34} {total_dropped:>6,}")
    print(f"  ranked pool (unique cats)   : {len(survivors):,}")
    print(f"  kept                        : {len(keepers):,}")
    print(f"  short-edge threshold used   : {threshold_used}px")
    print()
    print(f"  output files                : {file_count:,}")
    print(f"  output bytes                : {human(out_bytes)}")
    print(f"  budget                      : {human(budget_bytes)}  "
          f"({'UNDER' if out_bytes <= budget_bytes else 'OVER'} by "
          f"{human(abs(budget_bytes - out_bytes))})")
    print(f"  average per cat             : {human(out_bytes / max(1, len(entries)))}")
    print(f"  elapsed                     : {time.time() - t0:.0f}s")
    if fallback_note:
        print(f"\n  NOTE: {fallback_note}")
    print("\n  Nothing in Cats.* was modified. Every output byte was generated here.\n")

    summary = {
        "input_files": len(records),
        "dropped": dict(drops),
        "unique_pool": len(survivors),
        "kept": len(keepers),
        "short_edge_threshold": threshold_used,
        "quality_tier": chosen,
        "output_files": file_count,
        "output_bytes": out_bytes,
        "budget_bytes": int(budget_bytes),
        "avif": HAVE_AVIF,
        "seed": SEED,
    }
    (CACHE_DIR / "curate-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
