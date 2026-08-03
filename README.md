# CAT OF THE DAY

A static 1998 fan-shrine homepage that shows the same cat to everybody on
Earth. There is no backend, database, analytics, cookie, framework, bundler,
or runtime request outside the site itself.

Live site: https://cat-of-the-day.pages.dev/

## Source and safety

Raw photographs live in `cat-pictures/`. The pipeline recursively discovers
every file under that folder and treats it as read-only. It does not assume
the names of the extracted blocks, so adding another block and rerunning the
commands works automatically.

`.gitignore` excludes `cat-pictures/`, the curation cache, review picks, and
archives. Before committing, the safety check must show zero staged or tracked
files under `cat-pictures/`.

## Install the local tools

PowerShell:

```powershell
python -m pip install pillow imagehash numpy openai
# Optional AVIF support for older Pillow versions:
python -m pip install pillow-avif-plugin
```

Pillow's native AVIF support is detected at runtime. If it is unavailable,
curation prints a warning and emits WebP-only output; the site still works.

## Phase 0: inventory

This is read-only and safe to rerun while more folders are extracting:

```powershell
python tools/inventory.py
```

It reports folders found, per-folder file counts and bytes, combined size,
extension and actual Pillow format histograms, broken files, resolution and
aspect buckets, the short-edge `>= 800px` count, 20 deterministic random
samples, and unextracted `.7z`, `.zip`, or `.rar` archives. A machine-readable
copy is written to the ignored `tools/.cache/inventory.json`.

## Phase 1: heuristic curation and vision shortlist

The first pass validates content, rejects short/odd/near-monochrome images,
perceptually dedupes with pHash (Hamming distance 5), and ranks by resolution,
compression proxy, sharpness, colour, and orientation. It caches every probe
in `tools/.cache/hashes.json`.

Create the vision pool without encoding finals:

```powershell
python tools/curate.py --no-encode --shortlist 2000
```

This writes the ignored local `data/survivors.json`. It does not modify the
raw source or `dist/img/`.

## Personality-first manual review (current build)

The current published image set was selected by visual inspection, not by
technical image quality. `tools/manual_review.py` makes a contact sheet for
every decodable image under `cat-pictures`—all 5,925 in the current archive—
with no short-edge, aspect-ratio, sharpness, colour, or portrait filter. The
reviewer deliberately keeps the blurry, tiny, badly timed, and geometrically
weird cats that have personality.

The checked-in review page is:

```powershell
python tools/manual_review.py
Start-Process tools/review/index.html
```

The local selection used for the current build is source-keyed and reproducible:

```powershell
python tools/make_picks.py --indices-file tools/review/selected-indices.txt
python tools/manual_encode.py --picks tools/review/picks.json --workers 12
python tools/build.py
```

`manual_encode.py` is intentionally separate from the heuristic encoder. It
re-encodes only the hand-picked sources, strips metadata, writes the manifest
and deterministic order, and checks the 250 MB image budget. The current set
contains 979 manually selected cats and 2,937 generated image files.

## Phase 1.5: personality captions and vision triage

`tools/vision_triage.py` prefers the separate OpenAI-compatible AgentRouter
endpoint at `https://agentrouter.org/v1`. It does not use the Claude Code
connection or Anthropic's Messages Batch API. If AgentRouter is unavailable,
it can use `OPENAI_API_KEY` and the official OpenAI-compatible endpoint.

Set credentials in the current PowerShell session; never put the key in a
file or commit it:

```powershell
$env:AGENTROUTER_API_KEY = "your-key"
# Optional after the model catalog is printed:
$env:VISION_MODEL = "the-exact-catalog-model-id"
```

Run the free model discovery and cost preflight first:

```powershell
python tools/vision_triage.py --preflight
```

The script prints the exact catalog ids and selects a likely cheap vision
model. It estimates cost from 384px-long-edge JPEGs, batches 10 images per
request, and uses conservative token rates. It hard-stops above $25. No paid
request happens until this explicit command is run:

```powershell
python tools/vision_triage.py --confirm-spend
```

The pass uses 6 concurrent workers, retries failed/429 requests with backoff,
and saves each parsed score to `tools/.cache/scores.json` keyed by a hash of
the source path. Re-running never re-scores cached images. It scores
`funny` more heavily than `cuteness` or `quality`, gates quality 1, and
drops non-cats, gore/NSFW, and visible watermarks/text.

The current 979-cat build was captioned locally with the already-installed
Ollama `qwen3.5:9b` vision model. This keeps source photos on the machine and
costs nothing:

```powershell
python tools/local_caption.py --workers 3
python tools/apply_scores.py
```

Both routes write the same ignored score cache. `apply_scores.py` refuses a
partial update, then carries every caption and tag into the committed
manifest and the ignored source-keyed picks file.

After scoring, open the generated local contact sheet:

```powershell
Start-Process tools/review/index.html
```

It is a self-contained static page with the top 400 candidates, captions,
tags, three scores, checkboxes, select-all/select-none controls, and a browser
download for `picks.json`. The generated thumbnails are ignored. The download
is also ignored because it contains local source paths.

Encode the reviewed selection (all picks are used unless `--keep` is supplied):

```powershell
python tools/curate.py --picks tools/review/picks.json
```

If you choose not to use the vision pass, the manual review path above is the
preferred fallback. The older deterministic heuristic fallback is still
available for an unattended run:

```powershell
python tools/curate.py --keep 800
```

That fallback is explicitly reported; it does not pretend to contain vision
captions. A later reviewed run replaces the same manifest and image tree.

## Build and preview

The encoder always generates new bytes: AVIF and WebP full images at a 1600px
long edge, 320px WebP thumbnails, and stripped-metadata 16px WebP LQIPs. It
calibrates quality before reducing cat count and keeps `dist/img/` below the
250 MB budget when possible.

Build HTML, permalinks, 1200x630 social cards, archive, RSS, robots, sitemap,
and headers:

```powershell
python tools/build.py
python -m http.server 8000 --directory dist
```

Open <http://localhost:8000>. Serve `dist/`, not the project root, because the
generated site intentionally uses root-absolute asset paths.

The daily choice is deterministic:

```text
daysSinceEpoch = floor(Date.UTC(y, m, d) / 86400000)
catIndex       = ((daysSinceEpoch - LAUNCH_DAY) mod N + N) mod N
```

`LAUNCH_DAY` is persisted in `data/site.json`. `data/order.json` is shuffled
once using `SEED = 19980401`, so a sequence never repeats until all N cats have
appeared. Recuration changes the sequence; if historical permalink stability
matters, preserve the old order prefix and append new ids.

Daily facts live in `data/facts.json`. The 500-entry pool is independently
shuffled with `SEED_FACTS = 20250401` and a separate day offset, so cat/fact
pairings do not lock together. Each archive permalink receives its own
date-correct fact, and facts repeat only after all 500 have appeared.

The checked-in pool is the deployable source of truth. To deliberately
replace it with a fresh, category-balanced local draft, run
`python tools/generate_facts.py`; all 500 source entries are hand-curated in
`tools/curated_fact_entries.py`. The assembler validates the exact
200/100/75/75/50 mix and refuses long, duplicate, empty, undebunked, or
unframed entries before writing the file.

## Site features

- Win95-style Paint Shop Pro image window and bevelled navigation controls.
- Dedicated build-time 1200x630 JPEG OG/Twitter cards with the cat and its
  meme caption, plus an image preload for today's cat.
- CSS marquee, fake odometer counter, authored SVG 88x31 buttons, and authored
  under-construction art.
- Image-specific meme captions under the hero, in alt text, and in archive
  tiles; fallback `Cat #N.` is retained only as a corruption guard.
- An independently rotating Win95-style daily fact dialog, fact-powered
  marquee, and the same date-correct fact in each RSS item.
- AVIF/WebP `<picture>`, LQIP backgrounds, lazy archive thumbs, visible focus,
  left/right arrow navigation, reduced-motion support, and 380px layout.
- No third-party requests and no autoplay audio.

## Site identity

Edit `data/site.json` with the real public origin and guestbook destination:

```json
{
  "launch_date": "2026-08-01",
  "launch_day": 20666,
  "site_url": "https://cat-of-the-day.pages.dev",
  "guestbook_url": "https://github.com/somsom786/cat-of-the-day/discussions"
}
```

Update these values after attaching a custom domain. `site_url` is used for OG
image URLs, RSS, robots, and the sitemap. The source photographs may have
their own licensing obligations; verify publishing rights before publication.

## Cloudflare Pages

Cloudflare Pages is the intended host. Its relevant limits are **20,000 files
per deployment** and **25 MiB per file**. The current manual set is 979 cats,
2,937 image files, and 2,951 files total including pages; it is comfortably
inside those limits and the largest generated file is far below 25 MiB.

### Connect the repository

1. Push this repository to GitHub.
2. Cloudflare dashboard → Workers & Pages → Create → Pages → Connect to Git.
3. Choose this repository, use framework preset **None**, leave build command
   empty, and set output directory to `dist`.
4. Deploy.

### Custom domain

In the Pages project choose **Custom domains → Set up a domain**, then follow
Cloudflare's DNS instructions. Once the domain exists, update `site_url` in
`data/site.json` and rebuild so OG and RSS links point to the real origin.

`dist/_headers` gives images one year of immutable caching, the root five
minutes, and permalinks one hour.

## Daily workflow

`.github/workflows/daily.yml` runs at `5 0 * * *` UTC and supports
`workflow_dispatch`. It only runs `tools/build.py`, never curation, commits a
new day's HTML and OG card, and deploys with Cloudflare's Wrangler action if the
`CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` secrets exist. A
`daily-cat-build` concurrency group prevents overlapping runs.

## Git safety check

Before a commit, use:

```powershell
git add -A
Write-Host "staged source files:" ((git diff --cached --name-only | Select-String '^cat-pictures[\\/]' | Measure-Object).Count)
Write-Host "tracked source files:" ((git ls-files | Select-String '^cat-pictures[\\/]' | Measure-Object).Count)
git status
```

Both counts must be zero. The raw `cat-pictures/` tree must remain untouched.
