"""Encode the hand-picked all-image review set.

Unlike ``curate.py --picks``, this intentionally does not require a source
image to pass the technical short-edge/aspect/sharpness filters.  The human
review is the quality gate here: a blurry, tiny, portrait, or badly timed cat
can be exactly the keeper.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from curate import (  # noqa: E402 - tools/ is the script directory on Windows
    DATA_DIR,
    DIST_IMG,
    HAVE_AVIF,
    QUALITY_TIERS,
    SEED,
    encode_one,
    human,
    setup_console,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PICKS = PROJECT_ROOT / "tools" / "review" / "picks.json"


def load_selected(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc.get("picks", doc) if isinstance(doc, dict) else doc
    if not isinstance(rows, list) or not rows:
        raise ValueError("picks must be an array or an object with a non-empty picks array")
    selected: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each manual pick must be an object")
        path_value = str(row.get("path", ""))
        source_key = str(row.get("sourceKey", ""))
        src_hash = str(row.get("srcHash", ""))
        if not path_value or not Path(path_value).exists():
            raise ValueError(f"selected source is missing: {path_value}")
        identity = source_key or src_hash or path_value
        if identity in seen:
            continue
        seen.add(identity)
        normalized = dict(row)
        normalized["path"] = path_value
        normalized["sourceKey"] = source_key
        normalized["src_hash"] = src_hash
        # encode_one uses the source record's snake-case hash and preserves
        # captions/tags if a future review pass adds them.
        selected.append(normalized)
    return selected


def job_record(row: dict) -> dict:
    rec = dict(row)
    rec["src_hash"] = rec.get("src_hash") or rec.get("srcHash")
    if not rec["src_hash"]:
        raise ValueError(f"manual pick has no srcHash: {rec.get('path')}")
    return rec


def projected_total(records: list[dict], tier: dict, sample_n: int = 32) -> int:
    sample = records[: min(sample_n, len(records))]
    measured = []
    for n, rec in enumerate(sample, start=1):
        measured.append(encode_one((rec, f"sample-{n:04d}", tier, False))["bytes"])
    return round(sum(measured) / max(1, len(measured)) * len(records))


def choose_tier(records: list[dict], budget_bytes: int) -> tuple[dict, int]:
    print("\nCalibrating output quality against the 250 MB deployment budget...")
    for tier in QUALITY_TIERS:
        estimate = projected_total(records, tier)
        print(f"  tier {tier['name']}: projected {human(estimate)} "
              f"(AVIF q{tier['avif']}, WebP q{tier['webp']}, thumb q{tier['thumb']})")
        if estimate <= budget_bytes:
            return tier, estimate
    raise RuntimeError("even the lowest quality tier exceeds the image budget; reduce the pick count")


def clear_generated_images() -> None:
    expected = (PROJECT_ROOT / "dist" / "img").resolve()
    target = DIST_IMG.resolve()
    if target != expected:
        raise RuntimeError(f"refusing to clear unexpected output path: {target}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="Encode all-image manual Cat of the Day picks")
    ap.add_argument("--picks", default=str(DEFAULT_PICKS))
    ap.add_argument("--budget-mb", type=float, default=250.0)
    ap.add_argument("--workers", type=int, default=max(2, os.cpu_count() or 4))
    args = ap.parse_args()

    picks_path = Path(args.picks).resolve()
    if not picks_path.exists():
        print(f"ERROR: picks file does not exist: {picks_path}")
        return 1

    try:
        records = [job_record(r) for r in load_selected(picks_path)]
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: cannot load manual picks: {exc}")
        return 1

    budget_bytes = int(args.budget_mb * 1024 * 1024)
    print("CAT OF THE DAY -- MANUAL PERSONALITY-FIRST ENCODE")
    print(f"  picks       : {len(records):,}")
    print("  gate        : human visual selection only")
    print("  technical   : no resolution/aspect/sharpness/colour filter")
    print(f"  AVIF        : {'enabled' if HAVE_AVIF else 'unavailable; WebP-only'}")
    print(f"  workers     : {args.workers}")

    try:
        tier, estimate = choose_tier(records, budget_bytes)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"  selected    : tier {tier['name']}; projected {human(estimate)}")

    clear_generated_images()
    started = time.time()
    jobs = [(rec, f"cat-{i:04d}", tier, True) for i, rec in enumerate(records, start=1)]
    manifest: list[dict] = []
    errors: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(encode_one, job) for job in jobs]
        for future in futures:
            try:
                manifest.append(future.result())
            except Exception as exc:  # report the source but continue collecting failures
                errors.append(repr(exc))
            done += 1
            if done == 1 or done % 25 == 0 or done == len(futures):
                print(f"\r  encoding    {done:>4,}/{len(futures):,}", end="", flush=True)
    print()

    if errors:
        print(f"ERROR: {len(errors)} image(s) failed to encode")
        for error in errors[:5]:
            print(f"  {error}")
        return 1

    manifest.sort(key=lambda row: row["id"])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    order = [row["id"] for row in manifest]
    random.Random(SEED).shuffle(order)
    (DATA_DIR / "order.json").write_text(
        json.dumps({"seed": SEED, "order": order}, indent=2), encoding="utf-8"
    )

    actual = sum(row.get("bytes", 0) for row in manifest)
    output_files = sum(3 if HAVE_AVIF else 2 for _ in manifest)
    print("\nFINAL MANUAL ENCODE REPORT")
    print(f"  source picks : {len(records):,}")
    print(f"  encoded      : {len(manifest):,}")
    print(f"  output files : {output_files:,}")
    print(f"  output bytes : {actual:,} ({human(actual)})")
    print(f"  budget       : {'PASS' if actual <= budget_bytes else 'FAIL'}")
    print(f"  tier         : {tier['name']}")
    print(f"  elapsed      : {time.time() - started:.0f}s")
    print("  source       : untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
