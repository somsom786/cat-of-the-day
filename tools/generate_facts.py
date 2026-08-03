"""Assemble and validate the fully curated 500-entry daily-fact pool."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from curated_fact_entries import FACTS, LORE, MEMES, MYTHS, RUMORS


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data" / "facts.json"
EXPECTED = Counter({"FACT": 200, "MEME": 100, "MYTH": 75,
                    "LORE": 75, "RUMOR": 50})


def rows(kind: str, entries: list[tuple[str, str]]) -> list[dict]:
    return [{"kind": kind, "text": " ".join(text.split()),
             "note": " ".join(note.split())}
            for text, note in entries]


def validate(items: list[dict]) -> None:
    actual = Counter(item["kind"] for item in items)
    if actual != EXPECTED:
        raise ValueError(f"wrong category mix: {actual}; expected {EXPECTED}")
    seen = set()
    for index, item in enumerate(items, 1):
        text = item["text"].strip()
        note = item.get("note", "").strip()
        key = re.sub(r"[^a-z0-9]", "", text.lower())
        if not text or key in seen:
            raise ValueError(f"empty or duplicate text at row {index}: {text!r}")
        seen.add(key)
        if len(text.split()) > 30:
            raise ValueError(f"text exceeds 30 words at row {index}: {text}")
        if len(note.split()) > 28:
            raise ValueError(f"note exceeds 28 words at row {index}: {note}")
        if item["kind"] == "RUMOR" and not text.lower().startswith("they say"):
            raise ValueError(f"rumor lacks 'They say' framing at row {index}: {text}")
        if item["kind"] == "MYTH" and not note:
            raise ValueError(f"myth lacks a debunk note at row {index}: {text}")


def main() -> int:
    items = (rows("FACT", FACTS) + rows("MYTH", MYTHS)
             + rows("LORE", LORE) + rows("RUMOR", RUMORS)
             + rows("MEME", MEMES))
    validate(items)
    output = []
    for item_id, item in enumerate(items, 1):
        row = {"id": item_id, "kind": item["kind"], "text": item["text"]}
        if item["note"]:
            row["note"] = item["note"]
        output.append(row)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, indent=1, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    print(f"Wrote {len(output)} fully curated entries to {OUTPUT}")
    print("Distribution:", dict(Counter(item["kind"] for item in output)))
    print("Longest text:", max(len(item["text"].split()) for item in output), "words")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
