# CAT OF THE DAY

A static website that shows one cat per day. The same cat for everyone on
earth, chosen by the date itself. No backend, no database, no state anywhere.

Vanilla HTML/CSS/JS. No React, no Tailwind, no bundler, no npm, no third-party
requests of any kind. The generated site's only runtime dependency is a browser.

---

## How it works

```
daysSinceEpoch = floor(Date.UTC(y, m, d) / 86400000)
catIndex       = ((daysSinceEpoch - LAUNCH_DAY) mod N + N) mod N
catId          = order[catIndex]
```

`LAUNCH_DAY` is written once into `data/site.json` and then reused forever.
`data/order.json` is the manifest shuffled a single time with a hardcoded seed
(`SEED = 19980401`), so the daily sequence is identical across every rebuild and
does not repeat for N days.

**`LAUNCH_DAY` and `SEED` are load-bearing. Changing either one silently
rewrites history** — every existing permalink would start showing a different
cat than the one it showed when someone linked to it.

Today's HTML is generated at build time, not in the browser, so `og:image` is a
real absolute URL to today's cat baked into the served markup. Client-rendered
OG tags do not unfurl on Discord, Bluesky or X — and the unfurl is the entire
distribution mechanism for a site like this.

---

## Layout

```
Cats.00000/            source images -- READ ONLY, never committed, never modified
site/                  hand-written CSS/JS/SVG, copied verbatim into dist/
tools/inventory.py     Phase 0 -- what is actually in the source folders
tools/curate.py        Phase 1 -- validate, filter, dedupe, rank, re-encode
tools/build.py         Phase 2 -- static site generator
data/manifest.json     one entry per cat: id, w, h, dominant, lqip, srcHash
data/order.json        the shuffled daily sequence
data/site.json         LAUNCH_DAY, site URL, guestbook URL
dist/                  the deployable site
.github/workflows/     the daily rebuild
```

---

## First-time setup

```powershell
python -m pip install pillow imagehash numpy
# AVIF: Pillow 11.3+ has native AVIF support and needs nothing extra.
# On older Pillow, add:  python -m pip install pillow-avif-plugin
# Without either, curate.py degrades to WebP-only and says so.
```

### 1. Inventory the source (read-only)

```powershell
python tools/inventory.py
```

Reports file counts, how many files are *actually* decodable images, resolution
and aspect distributions, and the number that matters: how many images have a
short edge of at least 800px.

### 2. Curate

```powershell
python tools/curate.py
```

Validates every file, filters, perceptually dedupes, ranks, and re-encodes the
winners into `dist/img/`. Writes `data/manifest.json` and `data/order.json`.

Resumable: probe results are cached in `tools/.cache/hashes.json` keyed by
path + size + mtime, so a rerun skips the expensive decode/hash pass. Use
`--no-cache` to force a full re-probe.

Useful flags:

| flag | what it does |
|---|---|
| `--keep 1200` | target keep count (default 800) |
| `--budget-mb 250` | byte budget for `dist/img/` |
| `--no-cache` | ignore the hash cache |
| `--workers 8` | thread count |

### 3. Build

```powershell
python tools/build.py
```

Regenerates every HTML page, the RSS feed and the sitemap. Never touches
`dist/img/` — that tree belongs to `curate.py`.

### 4. Preview

```powershell
python -m http.server 8000 --directory dist
```

Then open <http://localhost:8000>. Serve from `dist/`, not the project root —
the site uses root-absolute paths (`/style.css`, `/img/...`).

---

## Before you go live

Edit `data/site.json`:

```json
{
  "launch_date": "2026-08-01",
  "launch_day": 20666,
  "site_url": "https://cats.example.com",
  "guestbook_url": "https://github.com/YOUR-USERNAME/cat-of-the-day/discussions"
}
```

- **`site_url`** — must be the real public origin. `og:image`, the RSS feed and
  `sitemap.xml` all use absolute URLs built from it. `build.py` warns while it
  is still the default.
- **`guestbook_url`** — point it at your repo's GitHub Discussions (enable
  Discussions in repo Settings → Features). If you would rather not use GitHub,
  put a `mailto:you@example.com` here instead. `build.py` warns while it still
  contains `YOUR-USERNAME`.

Then rebuild: `python tools/build.py`.

---

## Deploying to Cloudflare Pages

Free tier, and — the part that matters if this ever has a viral day —
**unmetered bandwidth on static assets**.

### Limits worth knowing

| limit | value | where this site sits |
|---|---|---|
| files per deployment | **20,000** | ~2,412 (800 cats x 3 files + HTML) |
| max size per file | **25 MiB** | largest asset is a few hundred KB |
| builds per month (free) | 500 | one per day |

At 800 cats you are using about 12% of the file budget. You could grow to
roughly 6,600 cats before the 20,000-file ceiling becomes the binding
constraint.

### Route A — connect the Git repo (recommended)

1. Push this repo to GitHub.
2. Cloudflare dashboard → **Workers & Pages** → **Create** → **Pages** →
   **Connect to Git**, pick the repo.
3. Build settings:
   - Framework preset: **None**
   - Build command: *(leave empty)*
   - Build output directory: **`dist`**
4. Save and deploy.

Every push to `main` deploys. The daily workflow commits and pushes, so the
daily rebuild deploys itself with no secrets to manage.

### Route B — deploy with an API token

Add repo secrets `CLOUDFLARE_API_TOKEN` (Pages: Edit permission) and
`CLOUDFLARE_ACCOUNT_ID`. The deploy step in `.github/workflows/daily.yml`
activates automatically when the token is present; without it the step is
skipped and Route A's push-triggered deploy takes over.

### Custom domain

Pages project → **Custom domains** → **Set up a domain** → enter e.g.
`cats.example.com`.

- Domain already on Cloudflare: the DNS record is created for you.
- Domain elsewhere: add the `CNAME` Cloudflare shows you at your registrar.

Certificates are issued automatically. Then set `site_url` in `data/site.json`
to the custom domain and rebuild, or every OG unfurl and RSS item will keep
pointing at the `*.pages.dev` origin.

### Caching

`dist/_headers` ships with the deployment:

```
/img/*   immutable, 1 year   -- filenames are stable, content never changes
/        5 minutes           -- so a new day appears promptly
/cat/*   1 hour              -- permalinks are effectively frozen
```

---

## The daily rebuild

`.github/workflows/daily.yml` runs at `5 0 * * *` UTC plus `workflow_dispatch`.
It reruns `tools/build.py` **only** — never curation, which needs the source
blocks that are deliberately not in this repo — then commits the new day's
permalink and the refreshed `index.html` and deploys.

It uses a `concurrency` group (`daily-cat-build`, `cancel-in-progress: false`)
so a scheduled run and a manual run can never interleave. Both commit to the
same branch, and a race would leave the repo with a half-written day.

---

## Topping up the cat supply

The scripts glob `Cats.*`, so dropping in another block and rerunning just
works — nothing hardcodes `Cats.00000`.

```powershell
# 1. Drop Cats.00001 next to Cats.00000. (.gitignore already excludes it.)
# 2. See what you got:
python tools/inventory.py
# 3. Recurate with a bigger target:
python tools/curate.py --keep 1600
# 4. Rebuild:
python tools/build.py
```

**Recuration reshuffles `data/order.json`.** New cats are interleaved into the
sequence, so days that have already happened will show a different cat than they
did before. If you have been live for a while and care about permalink
stability, keep the old `order.json` and append the new IDs to the end of it
instead of regenerating — the existing prefix stays put and the new cats queue
up behind the ones already scheduled.

Quality is calibrated against the byte budget automatically: `curate.py` encodes
a sample at each quality tier and steps down until the projected `dist/img/`
total fits. **Quality drops before the cat count does.**

---

## What is committed and what is not

`.gitignore` excludes `Cats.*/`, `tools/.cache/`, and `*.7z` / `*.zip` / `*.rar`.
It was written *before* `git init` — the source blocks live inside the project
root, so initialising the repo first would have staged thousands of raw JPEGs.

Committed: `dist/`, `data/`, `site/`, `tools/*.py`, the workflow, this README.

Note that `dist/img/` is about 217 MB across 2,400 files, so the repo is not
small. That is the trade for Cloudflare Pages' Git integration. If you would
rather keep the repo lean, use Route B and add `dist/img/` to `.gitignore`, but
then the daily workflow can no longer deploy on its own — the images would not
exist in the checkout.

---

## Accessibility and taste notes

The 1998 styling is deliberate; the quality floor underneath it is not
negotiable.

- Body text is black on cream, always. Magenta and lime are decorative only —
  never paragraph text, never text on navy.
- Every cat image has real `alt` text.
- `prefers-reduced-motion` kills the marquee, the blink and the construction
  animation. There is also a manual toggle in the footer; the preference is the
  only thing this site ever puts in `localStorage`.
- Visible keyboard focus on every control. Left/right arrow keys move between
  days.
- Readable down to 380px — the panel collapses, the chrome stays.
- No audio. Nothing autoplays.

---

## Licensing, and a word about the cats

Every graphic on this site — the background tile, the favicon, the four 88x31
buttons, the under-construction sign — was authored here as SVG or CSS. No 90s
GIFs were downloaded, no copyrighted characters appear, no recognizable meme
cats were used.

The cat photographs come from your local source blocks, and this repo makes no
claim about their licensing. Every image is fully re-encoded before it ships:
all metadata is stripped, which removes EXIF GPS from strangers' phones and
guarantees you are serving bytes you generated rather than a renamed unknown
binary. **Check that you have the right to publish the photographs before you
point a domain at this.**
