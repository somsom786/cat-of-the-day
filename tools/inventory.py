"""
PHASE 0 -- Inventory.

Walks every extracted block below `cat-pictures` and reports what is actually
in there. Read-only: this script never writes, moves or renames anything inside
the source folder. The only thing it writes is a JSON copy of its own report into
tools/.cache/ (gitignored).

Usage:
    python tools/inventory.py
    python tools/inventory.py --root C:\\some\\other\\project
    python tools/inventory.py --samples 30
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageFile

# Report truncated files as broken rather than silently accepting them.
ImageFile.LOAD_TRUNCATED_IMAGES = False
# These are cat photos, not zip bombs, but keep a sane ceiling.
Image.MAX_IMAGE_PIXELS = 300_000_000

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_SEED = 19980401
ARCHIVE_SUFFIXES = {".7z", ".zip", ".rar", ".tar", ".gz", ".tgz", ".bz2"}

SHORT_EDGE_BUCKETS = [
    ("< 400", 0, 400),
    ("400 - 599", 400, 600),
    ("600 - 799", 600, 800),
    ("800 - 1023", 800, 1024),
    ("1024 - 1439", 1024, 1440),
    ("1440 - 1919", 1440, 1920),
    (">= 1920", 1920, 10**9),
]

ASPECT_BUCKETS = [
    ("< 0.50  ultra-portrait", 0.0, 0.50),
    ("0.50 - 0.74 portrait", 0.50, 0.75),
    ("0.75 - 0.94 tall", 0.75, 0.95),
    ("0.95 - 1.05 square", 0.95, 1.05),
    ("1.06 - 1.40 landscape", 1.05, 1.40),
    ("1.41 - 2.00 wide", 1.40, 2.0001),
    ("> 2.00  ultra-wide", 2.0001, 10**9),
]


# --------------------------------------------------------------------------
# Windows path plumbing
# --------------------------------------------------------------------------

def long_path(p: Path) -> str:
    """Return a string path safe for >260 char paths on Windows."""
    s = os.fspath(p)
    if os.name != "nt":
        return s
    if not os.path.isabs(s):
        s = os.path.abspath(s)
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):  # UNC share
        return "\\\\?\\UNC\\" + s.lstrip("\\")
    return "\\\\?\\" + s


def setup_console() -> None:
    """Non-ASCII filenames must not blow up the console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def short_path(p) -> str:
    """Undo the \\\\?\\ prefix so printed paths look like paths."""
    s = str(p)
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


def display(text) -> str:
    """Best-effort printable form of a possibly exotic filename."""
    enc = (sys.stdout.encoding or "utf-8")
    return short_path(text).encode(enc, errors="replace").decode(enc, errors="replace")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def find_blocks(source: Path) -> list[Path]:
    """Every extracted block below cat-pictures, without hardcoded names."""
    if not source.is_dir():
        return []
    return sorted((p for p in source.iterdir() if p.is_dir()), key=lambda p: p.name)


def walk_files(block: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(long_path(block)):
        for name in filenames:
            out.append(Path(dirpath) / name)
    return out


def find_archives(root: Path) -> list[tuple[Path, int]]:
    skip = {".git", "dist", "node_modules", ".cache", "__pycache__"}
    found: list[tuple[Path, int]] = []
    for dirpath, dirnames, filenames in os.walk(long_path(root)):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in ARCHIVE_SUFFIXES:
                try:
                    found.append((p, p.stat().st_size))
                except OSError:
                    found.append((p, -1))
    return found


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def probe(path: Path) -> dict:
    """Open with Pillow, read real dimensions, then verify() integrity."""
    rec: dict = {
        "path": str(path),
        "ext": path.suffix.lower() or "(none)",
        "bytes": -1,
        "ok": False,
        "format": None,
        "mode": None,
        "w": None,
        "h": None,
        "error": None,
    }
    lp = long_path(path)
    try:
        rec["bytes"] = os.path.getsize(lp)
    except OSError as exc:
        rec["error"] = f"stat: {exc.__class__.__name__}"
        return rec

    try:
        with Image.open(lp) as im:
            rec["format"] = im.format
            rec["mode"] = im.mode
            rec["w"], rec["h"] = im.size
            im.verify()  # decodes far enough to catch truncation/corruption
        rec["ok"] = bool(rec["w"] and rec["h"])
    except Exception as exc:  # noqa: BLE001 - any decoder failure means "not an image"
        msg = str(exc).strip() or exc.__class__.__name__
        rec["error"] = f"{exc.__class__.__name__}: {msg[:120]}"
    return rec


def bucket(value: float, buckets) -> str:
    for label, lo, hi in buckets:
        if lo <= value < hi:
            return label
    return buckets[-1][0]


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n):,} B"
        n /= 1024
    return f"{n:.1f} TB"


def table(title: str, rows, headers, total_for_pct: int | None = None) -> None:
    print()
    print(title)
    print("-" * max(len(title), 60))
    if not rows:
        print("  (none)")
        return
    body = []
    for row in rows:
        cells = [str(c) for c in row]
        if total_for_pct:
            try:
                pct = 100.0 * float(str(row[1]).replace(",", "")) / total_for_pct
                cells.append(f"{pct:5.1f}%")
            except (ValueError, TypeError, ZeroDivisionError):
                cells.append("")
        body.append(cells)
    heads = list(headers) + (["share"] if total_for_pct else [])
    widths = [
        max(len(heads[i]), max(len(r[i]) for r in body))
        for i in range(len(heads))
    ]
    print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(heads)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in body:
        line = [r[0].ljust(widths[0])]
        line += [r[i].rjust(widths[i]) for i in range(1, len(r))]
        print("  " + "  ".join(line))


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="Phase 0 inventory of cat-pictures blocks")
    ap.add_argument("--root", default=str(PROJECT_ROOT), help="project root")
    ap.add_argument("--source", default=None, help="source folder (default: root/cat-pictures)")
    ap.add_argument("--samples", type=int, default=20, help="random sample count")
    ap.add_argument("--workers", type=int, default=min(16, (os.cpu_count() or 4) * 2))
    args = ap.parse_args()

    root = Path(args.root).resolve()
    source = Path(args.source).resolve() if args.source else root / "cat-pictures"
    blocks = find_blocks(source)

    print("=" * 72)
    print("CAT OF THE DAY -- PHASE 0 INVENTORY (read-only)")
    print("=" * 72)
    print(f"Project root : {root}")
    print(f"Source root  : {source}")
    print(f"Folders found: {len(blocks)}  ->  {', '.join(b.name for b in blocks) or '(none)'}")
    if not source.is_dir():
        print("\nSource folder does not exist yet. Nothing to inventory.")
        return 1

    per_block = []
    all_files: list[Path] = []
    for b in blocks:
        files = walk_files(b)
        per_block.append((b.name, len(files), human(sum(r.stat().st_size for r in files if r.exists()))))
        all_files.extend(files)

    direct_files = [p for p in source.iterdir() if p.is_file()]
    if direct_files:
        per_block.append(("(source root)", len(direct_files),
                          human(sum(p.stat().st_size for p in direct_files))))
        all_files.extend(direct_files)

    print(f"Files on disk: {len(all_files):,}")
    print(f"\nProbing {len(all_files):,} files with Pillow ({args.workers} threads)...")

    records: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for rec in ex.map(probe, all_files):
            records.append(rec)
            done += 1
            if done % 100 == 0 or done == len(all_files):
                pct = 100.0 * done / max(1, len(all_files))
                print(f"\r  {done:,}/{len(all_files):,}  ({pct:5.1f}%)", end="", flush=True)
    print()

    total_bytes = sum(r["bytes"] for r in records if r["bytes"] > 0)
    good = [r for r in records if r["ok"]]
    bad = [r for r in records if not r["ok"]]
    good_bytes = sum(r["bytes"] for r in good if r["bytes"] > 0)

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"  Total files              : {len(records):,}")
    print(f"  Total bytes              : {human(total_bytes)}  ({total_bytes:,} bytes)")
    print(f"  Decodable images         : {len(good):,}  ({100.0*len(good)/max(1,len(records)):.1f}%)")
    print(f"  Broken / not images      : {len(bad):,}")
    print(f"  Mean size (decodable)    : {human(good_bytes / max(1, len(good)))}")

    table(
        "FILES PER BLOCK",
        [(name, f"{n:,}", bytes_) for name, n, bytes_ in per_block],
        ["folder", "files", "bytes"],
        total_for_pct=len(records),
    )

    ext_hist = Counter(r["ext"] for r in records)
    ext_bytes = Counter()
    for r in records:
        if r["bytes"] > 0:
            ext_bytes[r["ext"]] += r["bytes"]
    table(
        "EXTENSION HISTOGRAM (by filename)",
        [(e, f"{n:,}", human(ext_bytes[e])) for e, n in ext_hist.most_common()],
        ["extension", "count", "bytes"],
        total_for_pct=len(records),
    )

    fmt_hist = Counter(r["format"] or "(undecodable)" for r in records)
    table(
        "ACTUAL FORMAT (what Pillow says the bytes are)",
        [(f, f"{n:,}") for f, n in fmt_hist.most_common()],
        ["format", "count"],
        total_for_pct=len(records),
    )

    # Extension lying about content is worth knowing before we trust any glob.
    fmt_ext_ok = {
        "JPEG": {".jpg", ".jpeg", ".jpe", ".jfif"},
        "PNG": {".png"},
        "GIF": {".gif"},
        "WEBP": {".webp"},
        "BMP": {".bmp"},
        "TIFF": {".tif", ".tiff"},
        "AVIF": {".avif"},
    }
    mismatched = [
        r for r in good
        if r["format"] in fmt_ext_ok and r["ext"] not in fmt_ext_ok[r["format"]]
    ]
    print(f"\n  Extension/content mismatches: {len(mismatched):,}")
    for r in mismatched[:5]:
        print(f"    {display(Path(r['path']).name)}  ext={r['ext']} actual={r['format']}")

    mode_hist = Counter(r["mode"] for r in good)
    table(
        "COLOR MODE",
        [(m, f"{n:,}") for m, n in mode_hist.most_common()],
        ["mode", "count"],
        total_for_pct=len(good),
    )

    short_hist = Counter(bucket(min(r["w"], r["h"]), SHORT_EDGE_BUCKETS) for r in good)
    table(
        "RESOLUTION -- SHORT EDGE",
        [(lbl, f"{short_hist.get(lbl, 0):,}") for lbl, _, _ in SHORT_EDGE_BUCKETS],
        ["short edge (px)", "count"],
        total_for_pct=len(good),
    )

    mp_buckets = [
        ("< 0.5 MP", 0, 0.5),
        ("0.5 - 1 MP", 0.5, 1),
        ("1 - 2 MP", 1, 2),
        ("2 - 4 MP", 2, 4),
        ("4 - 8 MP", 4, 8),
        (">= 8 MP", 8, 10**9),
    ]
    mp_hist = Counter(bucket(r["w"] * r["h"] / 1e6, mp_buckets) for r in good)
    table(
        "RESOLUTION -- MEGAPIXELS",
        [(lbl, f"{mp_hist.get(lbl, 0):,}") for lbl, _, _ in mp_buckets],
        ["megapixels", "count"],
        total_for_pct=len(good),
    )

    ar_hist = Counter(bucket(r["w"] / r["h"], ASPECT_BUCKETS) for r in good)
    table(
        "ASPECT RATIO (width / height)",
        [(lbl, f"{ar_hist.get(lbl, 0):,}") for lbl, _, _ in ASPECT_BUCKETS],
        ["ratio", "count"],
        total_for_pct=len(good),
    )

    # The number that decides whether one block is enough content.
    ge800 = [r for r in good if min(r["w"], r["h"]) >= 800]
    ge600 = [r for r in good if min(r["w"], r["h"]) >= 600]
    in_ar = [r for r in ge800 if 0.5 <= r["w"] / r["h"] <= 2.0]
    print()
    print("=" * 72)
    print("THE DECIDING NUMBER")
    print("=" * 72)
    print(f"  Short edge >= 800px                  : {len(ge800):,}")
    print(f"     ...and aspect ratio within 0.5-2.0: {len(in_ar):,}")
    print(f"  Short edge >= 600px (fallback thresh): {len(ge600):,}")
    print(f"  Target keep count                    : 800")
    print(f"  Minimum acceptable (1 year)          : 365")
    headroom = len(in_ar)
    verdict = (
        "COMFORTABLE -- 800 is reachable even after dedupe attrition."
        if headroom >= 1100 else
        "TIGHT -- 800 possible but perceptual dedupe may eat the margin."
        if headroom >= 800 else
        "SHORT OF 800 -- expect the adaptive path in Phase 1."
        if headroom >= 365 else
        "UNDER A YEAR -- Phase 1 will need the 600px fallback or another block."
    )
    print(f"  Verdict (pre-dedupe)                 : {verdict}")

    if bad:
        reason_hist = Counter((r["error"] or "unknown").split(":")[0] for r in bad)
        table(
            "BROKEN / NOT-AN-IMAGE BREAKDOWN",
            [(k, f"{v:,}") for k, v in reason_hist.most_common()],
            ["reason", "count"],
        )
        print("\n  examples:")
        for r in bad[:8]:
            print(f"    {display(Path(r['path']).name):<40} {display(r['error'] or '')}")

    rng = random.Random(SAMPLE_SEED)
    pool = good if good else records
    sample = rng.sample(pool, min(args.samples, len(pool)))
    print()
    print("=" * 72)
    print(f"RANDOM SAMPLE ({len(sample)} files, seed {SAMPLE_SEED})")
    print("=" * 72)
    for r in sample:
        rel = Path(short_path(r["path"]))
        try:
            rel = rel.relative_to(root)
        except ValueError:
            pass
        dims = f"{r['w']}x{r['h']}" if r["ok"] else "UNDECODABLE"
        ar = f"{r['w']/r['h']:.2f}" if r["ok"] else "-"
        print(f"  {dims:>12}  ar={ar:>5}  {human(r['bytes']):>9}  {display(str(rel))}")

    archives = find_archives(source)
    print()
    print("=" * 72)
    print("UNEXTRACTED ARCHIVES")
    print("=" * 72)
    if archives:
        for p, size in archives:
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            print(f"  {human(size):>10}  {display(str(rel))}")
        print(f"\n  {len(archives)} archive(s) found -- extract before curating if they hold cats.")
    else:
        print("  None. Nothing sitting unextracted.")

    cache = root / "tools" / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    report = {
        "root": str(root),
        "blocks": [b.name for b in blocks],
        "files_total": len(records),
        "bytes_total": total_bytes,
        "decodable": len(good),
        "broken": len(bad),
        "short_edge_ge_800": len(ge800),
        "short_edge_ge_800_and_aspect_ok": len(in_ar),
        "short_edge_ge_600": len(ge600),
        "ext_histogram": dict(ext_hist),
        "format_histogram": dict(fmt_hist),
        "short_edge_buckets": {lbl: short_hist.get(lbl, 0) for lbl, _, _ in SHORT_EDGE_BUCKETS},
        "aspect_buckets": {lbl: ar_hist.get(lbl, 0) for lbl, _, _ in ASPECT_BUCKETS},
        "megapixel_buckets": {lbl: mp_hist.get(lbl, 0) for lbl, _, _ in mp_buckets},
        "archives": [str(p) for p, _ in archives],
    }
    out = cache / "inventory.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nMachine-readable copy: {out}")
    print("Nothing inside cat-pictures was modified.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
