"""Small local visual/functional smoke test used by the final handoff."""

from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parent.parent / "qa"


def main() -> int:
    OUT.mkdir(exist_ok=True)
    failures = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 900},
                                device_scale_factor=1)
        external = []
        page.on("request", lambda request: external.append(request.url)
                if "127.0.0.1:8765" not in request.url
                and request.url.startswith(("http://", "https://")) else None)

        page.goto(ROOT + "/", wait_until="networkidle")
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

        page.goto(ROOT + "/archive/", wait_until="networkidle")
        page.screenshot(path=str(OUT / "archive-desktop.png"), full_page=True)
        if page.locator(".grid img").count() < 1:
            failures.append("archive grid")
        if page.locator('.grid img[loading="lazy"]').count() < 1:
            failures.append("archive lazy loading")

        mobile = browser.new_page(viewport={"width": 380, "height": 844},
                                  device_scale_factor=1)
        mobile.goto(ROOT + "/", wait_until="networkidle")
        mobile.screenshot(path=str(OUT / "index-mobile-380.png"), full_page=True)
        if mobile.locator("body").bounding_box()["width"] > 380:
            failures.append("mobile horizontal overflow")

        nav = browser.new_page(viewport={"width": 1200, "height": 900})
        nav.goto(ROOT + "/cat/2026-08-02/", wait_until="networkidle")
        nav.keyboard.press("ArrowLeft")
        nav.wait_for_url("**/cat/2026-08-01/")
        if not nav.url.endswith("/cat/2026-08-01/"):
            failures.append("arrow previous")

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
