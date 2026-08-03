"""Small local visual/functional smoke test used by the final handoff."""

import argparse
import re
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright


OUT = Path(__file__).resolve().parent.parent / "qa"


def goto(page, url: str) -> None:
    """Tolerate the first empty socket probe from Windows preview helpers."""
    for attempt in range(4):
        try:
            page.goto(url, wait_until="networkidle")
            return
        except PlaywrightError:
            if attempt == 3:
                raise
            time.sleep(0.4)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run local Cat of the Day browser smoke tests")
    ap.add_argument("--base", default="http://127.0.0.1:8765",
                    help="preview origin, for example http://127.0.0.1:8766")
    args = ap.parse_args()
    root = args.base.rstrip("/")
    OUT.mkdir(exist_ok=True)
    failures = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 900},
                                device_scale_factor=1)
        external = []
        page.on("request", lambda request: external.append(request.url)
                if not request.url.startswith(root)
                and request.url.startswith(("http://", "https://")) else None)

        goto(page, root + "/")
        page.screenshot(path=str(OUT / "index-desktop.png"), full_page=True)
        if page.locator("h1").inner_text() != "CAT OF THE DAY!":
            failures.append("index h1")
        if page.locator("picture img").count() != 1:
            failures.append("index hero image")
        if not page.locator("picture img").get_attribute("alt"):
            failures.append("hero alt text")
        if page.locator("picture source").count() < 1:
            failures.append("picture sources")
        if page.locator(".viewer-caption").count() != 1:
            failures.append("viewer caption")
        caption = page.locator(".viewer-caption").inner_text().strip()
        if not caption or caption.startswith("Cat #"):
            failures.append("real meme caption")
        if page.locator("picture img").get_attribute("alt") != caption:
            failures.append("caption used as hero alt")
        if page.locator('picture source[type="image/avif"]').count() != 1:
            failures.append("AVIF source")
        if page.locator('picture source[type="image/webp"]').count() != 1:
            failures.append("WebP source")
        if page.locator(".fact-dialog .fact-text").count() != 1:
            failures.append("daily fact dialog")
        if page.locator('head link[rel="alternate"][type="application/rss+xml"]').count() != 1:
            failures.append("RSS autodiscovery")
        if page.locator('.footer a[href="/cats.xml"]').count() != 1:
            failures.append("visible RSS link")
        og_image = page.locator('meta[property="og:image"]').get_attribute("content") or ""
        if not og_image.endswith(".jpg"):
            failures.append("dedicated OG card")
        random_link = page.locator("[data-random-cat]").first
        if random_link.get_attribute("href") == "/archive/":
            failures.append("random cat fallback")
        with page.expect_navigation():
            random_link.click()
        if "/cat/" not in page.url or page.url.endswith("/archive/"):
            failures.append("random cat jump")

        goto(page, root + "/archive/")
        page.screenshot(path=str(OUT / "archive-desktop.png"), full_page=True)
        if page.locator(".grid img").count() < 1:
            failures.append("archive grid")
        if page.locator('.grid img[loading="lazy"]').count() < 1:
            failures.append("archive lazy loading")
        if re.search(r"Cat #\d+\.", page.locator(".grid .cap").first.inner_text()):
            failures.append("archive caption")

        mobile = browser.new_page(viewport={"width": 380, "height": 844},
                                  device_scale_factor=1)
        goto(mobile, root + "/")
        mobile.screenshot(path=str(OUT / "index-mobile-380.png"), full_page=True)
        if mobile.locator("body").bounding_box()["width"] > 380:
            failures.append("mobile horizontal overflow")

        nav = browser.new_page(viewport={"width": 1200, "height": 900})
        goto(nav, root + "/cat/2026-08-02/")
        if "TODAY'S CAT" not in nav.locator("[data-nav-next]").inner_text():
            failures.append("yesterday forward label")
        nav.keyboard.press("ArrowLeft")
        nav.wait_for_url("**/cat/2026-08-01/")
        if not nav.url.endswith("/cat/2026-08-01/"):
            failures.append("arrow previous")
        if "TOMORROW'S CAT" not in nav.locator("[data-nav-next]").inner_text():
            failures.append("older forward label")

        browser.close()

    print(f"screenshots: {OUT}")
    print(f"external requests: {len(external)}")
    if external:
        print("  " + "\n  ".join(external[:10]))
        failures.append("third-party request")
    if failures:
        print("FAIL: " + ", ".join(failures))
        return 1
    print("PASS: local visual and functional smoke checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
