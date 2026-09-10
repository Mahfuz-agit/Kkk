"""
Webpage Image Downloader with Popup Handling
---------------------------------------------
Fetches a webpage using a headless browser, auto-dismisses common
popups (cookie banners, login/signup modals, newsletter popups,
notification prompts, age gates, exit-intent overlays), then
downloads all images found on the page.

Usage:
    python download_images.py https://example.com

Output:
    Images saved to ./downloads/<domain>/
"""

import asyncio
import os
import sys
import re
import hashlib
from urllib.parse import urljoin, urlparse

import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout


# ---------- CONFIG ----------
TIMEOUT_MS = 15000          # max wait for page load
POPUP_WAIT_MS = 3000        # wait for popups to appear before trying to close them
MIN_IMAGE_BYTES = 2048      # skip tiny images (likely icons/trackers)
OUTPUT_DIR = "downloads"

# Common close-button / dismiss selectors for popups
POPUP_CLOSE_SELECTORS = [
    # generic close buttons
    "button[aria-label='Close']",
    "button[aria-label='close']",
    "[class*='close-button']",
    "[class*='modal-close']",
    "[class*='popup-close']",
    "[class*='close-icon']",
    "button.close",
    ".close",
    "[data-dismiss='modal']",

    # cookie consent
    "button:has-text('Accept')",
    "button:has-text('Accept All')",
    "button:has-text('I Agree')",
    "button:has-text('Allow all')",
    "button:has-text('Got it')",
    "#onetrust-accept-btn-handler",
    ".cookie-consent button",

    # newsletter / signup popups
    "button:has-text('No thanks')",
    "button:has-text('Not now')",
    "button:has-text('Maybe later')",
    "a:has-text('No thanks')",

    # notification prompts (best effort, browser-level ones can't be clicked)
    "button:has-text('Block')",
    "button:has-text('Deny')",

    # age verification
    "button:has-text('I am over 18')",
    "button:has-text('Enter')",
]


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\-.]", "_", name)
    return name[:150] if len(name) > 150 else name


async def dismiss_popups(page):
    """Try each known popup-close selector; ignore failures."""
    await page.wait_for_timeout(POPUP_WAIT_MS)
    for selector in POPUP_CLOSE_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=500):
                await locator.click(timeout=1000)
                await page.wait_for_timeout(300)
        except Exception:
            continue
    # fallback: press Escape to close any remaining modal/overlay
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def get_image_urls(url: str) -> list[str]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        try:
            await page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
        except PWTimeout:
            print(f"[WARN] Page load timeout: {url}")

        await dismiss_popups(page)

        # scroll to trigger lazy-loaded images
        try:
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
            await page.wait_for_timeout(1500)
        except Exception:
            pass

        img_urls = await page.eval_on_selector_all(
            "img",
            """(imgs) => imgs.map(img => img.currentSrc || img.src || img.getAttribute('data-src')).filter(Boolean)"""
        )

        await browser.close()
        return list(dict.fromkeys(img_urls))  # dedupe, preserve order


def download_images(url: str, img_urls: list[str]):
    domain = urlparse(url).netloc
    out_dir = os.path.join(OUTPUT_DIR, sanitize_filename(domain))
    os.makedirs(out_dir, exist_ok=True)

    headers = {"User-Agent": "Mozilla/5.0"}
    saved = 0

    for img_url in img_urls:
        full_url = urljoin(url, img_url)
        try:
            resp = requests.get(full_url, headers=headers, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            print(f"[SKIP] {full_url} -> {e}")
            continue

        if len(resp.content) < MIN_IMAGE_BYTES:
            continue

        ext = os.path.splitext(urlparse(full_url).path)[1]
        if not ext or len(ext) > 5:
            ext = ".jpg"

        name = hashlib.sha1(full_url.encode()).hexdigest()[:16] + ext
        path = os.path.join(out_dir, name)

        with open(path, "wb") as f:
            f.write(resp.content)
        saved += 1
        print(f"[SAVED] {full_url} -> {path}")

    print(f"\nDone. {saved}/{len(img_urls)} images saved to {out_dir}")


async def main():
    if len(sys.argv) < 2:
        print("Usage: python download_images.py <url>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"Fetching: {url}")
    img_urls = await get_image_urls(url)
    print(f"Found {len(img_urls)} image references.")
    download_images(url, img_urls)


if __name__ == "__main__":
    asyncio.run(main())
