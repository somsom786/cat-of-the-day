"""Attach cached personality captions/tags to manual picks and the manifest."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PICKS = ROOT / "tools" / "review" / "picks.json"
SCORES = ROOT / "tools" / ".cache" / "scores.json"
MANIFEST = ROOT / "data" / "manifest.json"


def clean_caption(value: object) -> str:
    caption = str(value).translate(str.maketrans({
        "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...",
    }))
    caption = " ".join(caption.strip(" \"'`").split())
    return caption.replace("I'm'll", "I'll").replace("Don't'", "Don't")


def main() -> int:
    picks_doc = json.loads(PICKS.read_text(encoding="utf-8"))
    picks = picks_doc["picks"]
    score_doc = json.loads(SCORES.read_text(encoding="utf-8"))
    scores = score_doc.get("scores", score_doc)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    by_hash = {}
    missing = []
    for pick in picks:
        score = scores.get(pick["sourceKey"])
        if not isinstance(score, dict) or not str(score.get("caption", "")).strip():
            missing.append(pick["sourceKey"])
            continue
        caption = clean_caption(score["caption"])
        tags = [str(tag) for tag in score.get("tags", [])][:3]
        pick["caption"] = caption
        pick["tags"] = tags
        pick["funny"] = int(score.get("funny", 1))
        pick["cuteness"] = int(score.get("cuteness", 1))
        pick["quality"] = int(score.get("quality", 1))
        by_hash[pick["srcHash"]] = (caption, tags)

    if missing:
        raise SystemExit(
            f"Refusing a partial write: {len(missing):,}/{len(picks):,} selected cats lack captions"
        )

    manifest_missing = []
    for entry in manifest:
        values = by_hash.get(entry["srcHash"])
        if not values:
            manifest_missing.append(entry["id"])
            continue
        entry["caption"], entry["tags"] = values
    if manifest_missing:
        raise SystemExit(
            f"Refusing a partial write: {len(manifest_missing):,}/{len(manifest):,} manifest cats lack matching picks"
        )

    PICKS.write_text(json.dumps(picks_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Captions/tags attached: {len(manifest):,}/{len(manifest):,}")
    print("Updated data/manifest.json and ignored tools/review/picks.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
