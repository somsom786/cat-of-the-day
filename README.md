# CAT OF THE DAY

A static 1998 fan-shrine homepage that shows the same cat to everybody on
Earth. There is no backend, database, analytics, cookie, framework, bundler,
or runtime request outside the site itself.

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

## Phase 1.5: AgentRouter vision triage

This script uses the separate OpenAI-compatible AgentRouter endpoint at
`https://agentrouter.org/v1`. It does not use the Claude Code connection or
Anthropic's Messages Batch API.

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

If the vision pass is unavailable, the completed local build uses the
deterministic heuristic fallback:

```powershell
python tools/curate.py --keep 800
```

That fallback is explicitly reported; it does not pretend to contain AI
captions or vision scores. A later reviewed run replaces the same manifest
and image tree.

## Build and preview

The encoder always generates new bytes: AVIF and WebP full images at a 1600px
long edge, 320px WebP thumbnails, and stripped-metadata 16px WebP LQIPs. It
calibrates quality before reducing cat count and keeps `dist/img/` below the
250 MB budget when possible.

Build HTML, permalinks, archive, RSS, robots, sitemap, and headers:

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

## Site features

- Win95-style Paint Shop Pro image window and bevelled navigation controls.
- Build-time OG/Twitter tags and image preload for today's cat.
- CSS marquee, fake odometer counter, authored SVG 88x31 buttons, and authored
  under-construction art.
- Caption text from vision scoring under the hero image; fallback `Cat #N.`.
- AVIF/WebP `<picture>`, LQIP backgrounds, lazy archive thumbs, visible focus,
  left/right arrow navigation, reduced-motion support, and 380px layout.
- No third-party requests and no autoplay audio.

## Before going live

Edit `data/site.json` with the real public origin and guestbook destination:

```json
{
  "launch_date": "2026-08-01",
  "launch_day": 20666,
  "site_url": "https://cats.example.com",
  "guestbook_url": "https://github.com/YOUR-USERNAME/cat-of-the-day/discussions"
}
```

Replace both placeholders before rebuilding. `site_url` is used for OG image
URLs, RSS, robots, and the sitemap. The source photographs may have their own
licensing obligations; verify publishing rights before putting a domain on
the site.

## Cloudflare Pages

Cloudflare Pages is the intended host. Its relevant limits are **20,000 files
per deployment** and **25 MiB per file**. The current 800-cat fallback is
2,400 image files and the largest generated file is far below 25 MiB.

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
new day's HTML, and deploys with Cloudflare's Wrangler action if the
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
