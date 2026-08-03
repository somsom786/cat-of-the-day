"""Turn the locally reviewed contact-sheet indices into a picks export.

The contact sheet is deliberately a client-side review aid, but keeping this
small converter in the repo makes a hand-picked run reproducible without ever
putting raw source paths into git.  Picks are keyed by source identity rather
than by positional cat id, so a later source-folder addition cannot silently
point at a different image.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
POOL_PATH = PROJECT_ROOT / "tools" / "review" / "manual-pool.json"
DEFAULT_INDICES = PROJECT_ROOT / "tools" / "review" / "selected-indices.txt"
DEFAULT_OUTPUT = PROJECT_ROOT / "tools" / "review" / "picks.json"


def parse_indices(text: str, limit: int) -> list[int]:
    """Parse comma/whitespace-separated indices and inclusive ranges."""
    values: list[int] = []
    for token in text.replace(",", " ").split():
        if "-" in token:
            left, right = token.split("-", 1)
            start, end = int(left), int(right)
            if start > end:
                start, end = end, start
            values.extend(range(start, end + 1))
        else:
            values.append(int(token))
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value < 1 or value > limit:
            raise ValueError(f"review index {value} is outside 1..{limit}")
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Export manually selected cat review indices")
    ap.add_argument("--indices-file", default=str(DEFAULT_INDICES))
    ap.add_argument("--indices", default=None,
                    help="override the file with comma-separated indices/ranges")
    ap.add_argument("--pool", default=str(POOL_PATH))
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = ap.parse_args()

    pool_path = Path(args.pool).resolve()
    if not pool_path.exists():
        raise SystemExit(f"manual pool not found: {pool_path}")
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    if not isinstance(pool, list) or not pool:
        raise SystemExit("manual pool is empty or malformed")
    by_index = {int(row["reviewIndex"]): row for row in pool}

    if args.indices is not None:
        raw = args.indices
    else:
        indices_path = Path(args.indices_file).resolve()
        if not indices_path.exists():
            raise SystemExit(f"selection file not found: {indices_path}")
        raw = indices_path.read_text(encoding="utf-8")
    indices = parse_indices(raw, len(pool))

    picks = []
    for index in indices:
        row = by_index.get(index)
        if row is None:
            raise SystemExit(f"review index {index} is not in the manual pool")
        if not Path(row["path"]).exists():
            raise SystemExit(f"selected source is missing: {row['path']}")
        picks.append({
            "reviewIndex": index,
            "sourceKey": row["sourceKey"],
            "srcHash": row["srcHash"],
            "path": row["path"],
            "w": row["w"],
            "h": row["h"],
        })

    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"manual": True, "picks": picks}, indent=2), encoding="utf-8")
    print(f"Manual picks: {len(picks):,} / {len(pool):,} reviewed images")
    print(f"Wrote: {out}")
    print("Source images: untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
