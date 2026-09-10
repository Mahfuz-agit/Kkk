"""
Webpage Image Downloader with Popup Handling + Network Interception
---------------------------------------------------------------------
Fetches a webpage using a headless browser, auto-dismisses common
popups, captures EVERY image response seen on the network (not just
<img> tags in the DOM — this also catches background-image CSS,
lazy-loaded CDN images, and JS-injected media), then re-downloads
each one with spoofed Referer/User-Agent headers to bypass hotlink
protection.

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
TIMEOUT_MS = 20000          # max wait for page load
POPUP_WAIT_MS = 3000        # wait for popups to appear before trying to close them
MIN_IMAGE_BYTES = 2048      # skip tiny images (likely icons/trackers)
SCROLL_PASSES = 6           # number of scroll steps to trigger lazy-load
SCROLL_WAIT_MS = 1200       # wait between each scroll
OUTPUT_DIR = "downloads"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# Common close-button / dismiss selectors for popups
POPUP_CLOSE_SELECTORS = [
    "button[aria-label='Close']",
    "button[aria-label='close']",
    "[class*='close-button']",
    "[class*='modal-close']",
    "[class*='popup-close']",
    "[class*='close-icon']",
    "button.close",
    ".close",
    "[data-dismiss='modal']",
    "button:has-text('Accept')",
    "button:has-text('Accept All')",
    "button:has-text('I Agree')",
    "button:has-text('Allow all')",
    "button:has-text('Got it')",
    "#onetrust-accept-btn-handler",
    ".cookie-consent button",
    "button:has-text('No thanks')",
    "button:has-text('Not now')",
    "button:has-text('Maybe later')",
    "a:has-text('No thanks')",
    "button:has-text('Block')",
    "button:has-text('Deny')",
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
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def collect_image_urls(url: str) -> list[str]:
    """
    Collects image URLs two ways:
    1. Network interception - every response with image/* content-type
       (catches CSS background images, CDN-lazy-loaded media, JS-injected images)
    2. DOM scan - <img> tags and inline background-image styles (fallback/extra)
    """
    seen = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            extra_http_headers={"Referer": url},
            viewport={"width": 1366, "height": 900},
        )
        page = await context.new_page()

        # --- network interception: catch every image response ---
        def on_response(response):
            try:
                ctype = response.headers.get("content-type", "")
                if ctype.startswith("image/"):
                    seen[response.url] = True
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
        except PWTimeout:
            print(f"[WARN] Page load timeout: {url}")

        await dismiss_popups(page)

        # multi-pass scroll to trigger lazy-loaded / infinite-scroll images
        for i in range(SCROLL_PASSES):
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(SCROLL_WAIT_MS)
            except Exception:
                break

        # settle time for any trailing network requests
        await page.wait_for_timeout(1500)

        # --- DOM scan as a fallback/extra source ---
        try:
            dom_imgs = await page.eval_on_selector_all(
                "img",
                "(imgs) => imgs.map(img => img.currentSrc || img.src || img.getAttribute('data-src')).filter(Boolean)"
            )
            for src in dom_imgs:
                seen[src] = True
        except Exception:
            pass

        try:
            bg_imgs = await page.evaluate("""
                () => {
                  const urls = [];
                  document.querySelectorAll('*').forEach(el => {
                    const bg = getComputedStyle(el).backgroundImage;
                    const match = bg && bg.match(/url\\(["']?(.*?)["']?\\)/);
                    if (match && match[1]) urls.push(match[1]);
                  });
                  return urls;
                }
            """)
            for src in bg_imgs:
                seen[src] = True
        except Exception:
            pass

        await browser.close()

    return list(seen.keys())


def download_images(url: str, img_urls: list[str]):
    domain = urlparse(url).netloc
    out_dir = os.path.join(OUTPUT_DIR, sanitize_filename(domain))
    os.makedirs(out_dir, exist_ok=True)

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": url,          # spoofed referer to bypass hotlink protection
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    saved = 0

    for img_url in img_urls:
        full_url = urljoin(url, img_url)
        if full_url.startswith("data:"):
            continue

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
            ctype = resp.headers.get("content-type", "")
            ext = "." + ctype.split("/")[-1].split(";")[0] if "/" in ctype else ".jpg"
            ext = ext.replace("jpeg", "jpg")

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
    img_urls = await collect_image_urls(url)
    print(f"Found {len(img_urls)} unique image URLs.")
    download_images(url, img_urls)


if __name__ == "__main__":
    asyncio.run(main())
