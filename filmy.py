import os
import json
import logging
import asyncio
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from pyrogram import Client, idle, utils
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from collections import defaultdict
import re
import urllib3
import nest_asyncio
from playwright.async_api import async_playwright
# ── Environment Setup ─────────────────────────────────────────────────────────
nest_asyncio.apply()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_ID = int(os.environ.get("API_ID", "25833520"))
API_HASH = os.environ.get("API_HASH", "7d012a6cbfabc2d0436d7a09d8362af7")
BOT_TOKEN = os.environ.get("FF_BOT_TOKEN","8091169950:AAGNyiZ8vqrqCiPhZcks-Av3lDQy2GIcZuk")
CHANNEL_ID = int(os.environ.get("FF_CHANNEL_ID", "-1002557597877"))
OWNER_ID = int(os.environ.get("FF_OWNER_ID", "921365334"))
BASE_URL    = "https://filmyfly.party/"
filmy_FILE  = "filmy.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36",
    "Referer": "https://linkmake.in/",
    "Accept-Language": "en-US,en;q=0.9",
}

utils.get_peer_type = lambda peer_id: "channel" if str(peer_id).startswith("-100") else "user"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FilmyFlyBot")

# ── Pyrogram Client ──────────────────────────────────────────────────────────
app = Client("filmyfly-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ── File Tracker ──────────────────────────────────────────────────────────────
def load_filmy():
    if os.path.exists(filmy_FILE):
        return set(json.load(open(filmy_FILE)))
    return set()

def save_filmy(filmy):
    with open(filmy_FILE, "w") as f:
        json.dump(list(filmy), f, indent=2)

# ── Safe Request (no redirects, strip meta-refresh) ──────────────────────────
def safe_request(url, retries=2, referer=None):
    """
    Fetch the URL without following redirects and return any HTML response
    with status code < 400, so that challenge or blocked pages still come through.
    Strips out any <meta http-equiv="refresh"> tags to prevent automatic redirects.
    """
    for _ in range(retries):
        try:
            headers = HEADERS.copy()
            if referer:
                headers["Referer"] = referer

            # Do not follow redirects so we capture 3xx/4xx HTML bodies
            r = requests.get(
                url,
                headers=headers,
                timeout=15,
                verify=False,
                allow_redirects=False
            )

            content_type = r.headers.get("Content-Type", "")
            # Accept any HTML response under 400 for debugging
            if r.status_code < 400 and "html" in content_type.lower():
                # Remove meta-refresh tags
                cleaned_html = re.sub(
                    r'<meta[^>]+http-equiv=["\']refresh["\'][^>]*>',
                    "",
                    r.text,
                    flags=re.IGNORECASE
                )
                # Override the ._content so r.text returns cleaned_html
                r._content = cleaned_html.encode("utf-8")
                return r

        except Exception as e:
            logger.warning(f"safe_request failed for {url}: {e}")
        time.sleep(1)

    return None


# ── Scraping Helpers ─────────────────────────────────────────────────────────
def get_latest_movie_links():
    logger.info("Fetching homepage")
    r = safe_request(BASE_URL)
    if not r: return []
    soup = BeautifulSoup(r.text, "html.parser")
    blocks = soup.find_all("div", class_="A10")
    return [
        urljoin(BASE_URL, a["href"].strip())
        for b in blocks if (a := b.find("a", href=True))
    ]

def get_quality_links(movie_url):
    r = safe_request(movie_url)
    if not r: return {}
    soup = BeautifulSoup(r.text, "html.parser")
    q = defaultdict(list)
    for a in soup.find_all("a", href=True, string=True):
        txt = a.get_text().strip()
        if "download" in txt.lower() and "/view/" in a["href"]:
            qual = re.search(r"\{(.+?)\}", txt)
            qname = qual.group(1) if qual else "Other"
            q[qname].append(urljoin(BASE_URL, a["href"]))
    return q

async def get_intermediate_links(view_url: str) -> list[tuple[str, str]]:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1280, "height": 720}
            )
            page = await context.new_page()

            # Go to the link and wait for JS to execute
            await page.goto(view_url, wait_until="domcontentloaded", timeout=20000)

            # Wait up to 5 seconds for expected links to appear
            await page.wait_for_timeout(3000)

            # Prevent opening popups or redirects
            async def on_popup(popup):
                await popup.close()
            page.on("popup", on_popup)

            # Extract links matching the known pattern
            anchors = await page.query_selector_all("a[href*='filesd'], a[href*='filesdl']")
            results = []
            for a in anchors:
                href = await a.get_attribute("href")
                text = (await a.inner_text()).strip()
                if href:
                    results.append((text, href))

            await browser.close()
            return results

    except Exception as e:
        print(f"[WARNING] Playwright error on {view_url}: {e}")
        return []


async def extract_final_links_playwright(cloud_url: str) -> list[tuple[str, str]]:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1280, "height": 720}
            )
            page = await context.new_page()

            await page.goto(cloud_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)

            # optional: close any popup
            page.on("popup", lambda popup: asyncio.create_task(popup.close()))

            # Collect any links from .container or general anchors
            container = await page.query_selector("div.container")
            anchors = await container.query_selector_all("a") if container else await page.query_selector_all("a")

            results = []
            for a in anchors:
                href = await a.get_attribute("href")
                text = (await a.inner_text()).strip()
                if href and href.startswith("http") and text:
                    results.append((text, href))

            logger.info(f"[PW extract_final_links] Found {len(results)} links at {cloud_url}")
            for lbl, link in results:
                logger.info(f"→ {lbl}: {link}")

            await browser.close()
            return results

    except Exception as e:
        logger.warning(f"[PW extract_final_links] Error at {cloud_url}: {e}")
        return []


def get_title_from_intermediate(url):
    r = safe_request(url)
    if not r: return "Untitled"
    t = BeautifulSoup(r.text, "html.parser").find("title")
    return t.text.strip() if t else "Untitled"

def clean(txt):
    return re.sub(r"[\[\]_`*]", "", txt)

# ── Telegram Messaging ────────────────────────────────────────────────────────
async def send_quality_message(title, quality, provider, links):
    text = f"🎬 `{clean(title)}`\n\n🔗 **Quality**: `{provider}`\n\n"
    for lbl, u in links:
        text += f"• [{clean(lbl)}]({u})\n"
    text += "\n🌐 Scraped from [FilmyFly](https://telegram.me/Silent_Bots)"
    try:
        await app.send_message(CHANNEL_ID, text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        logger.info(f"📨 Sent: {title} | {provider}")
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await send_quality_message(title, quality, provider, links)
    except Exception as e:
        logger.error(f"❌ Send error: {e}")
        await app.send_message(OWNER_ID, f"❌ Send Error for `{title}`\n\n{e}")

# ── Monitor Loop ─────────────────────────────────────────────────────────────
async def monitor():
    seen = load_filmy()
    logger.info(f"Loaded {len(seen)} previous entries")
    while True:
        try:
            movies = await asyncio.to_thread(get_latest_movie_links)
            new = [m for m in movies if m not in seen]
            logger.info(f"Found {len(new)} new movies")
            for murl in new:
                logger.info(f"Processing: {murl}")
                qlinks = await asyncio.to_thread(get_quality_links, murl)
                for quality, views in qlinks.items():
                    for vurl in views:
                        ilinks = await get_intermediate_links(vurl)
                        if not ilinks:
                            logger.warning(f"⚠️ No intermediate links for: {vurl}")
                        
                            # — save whatever HTML we did get, and send it to the OWNER_ID —
                            r = await asyncio.to_thread(safe_request, vurl)
                            if r:
                                fn = f"raw_page_{int(time.time())}.html"
                                with open(fn, "w", encoding="utf-8") as f:
                                    f.write(r.text)
                        
                                try:
                                    await app.send_document(
                                        OWNER_ID,
                                        fn,
                                        caption=f"⚠️ No intermediate links for:\n{vurl}"
                                    )
                                except Exception as exc:
                                    logger.error(f"❌ Could not send HTML doc to owner: {exc}")
                                finally:
                                    os.remove(fn)
                        
                            # skip this view_url and move on
                            continue

                        for provider, il in ilinks:
                            finals = await asyncio.to_thread(extract_final_links, il)
                            if not finals:
                                logger.warning(f"No final links for: {il}")
                                await asyncio.sleep(2)
                                finals = await extract_final_links_playwright(il)
                            if finals:
                                title = await asyncio.to_thread(get_title_from_intermediate, il)
                                await send_quality_message(title, quality, provider, finals)
                seen.add(murl)
                save_filmy(seen)

        except Exception as E:
            logger.error(f"Monitor loop error: {E}")
            await app.send_message(OWNER_ID, f"🚨 Monitor loop crashed:\n\n{E}")

        await asyncio.sleep(300)

# ── Entry Point ───────────────────────────────────────────────────────────────
async def main():
    await app.start()
    await app.send_message(CHANNEL_ID, "✅ FilmyFly Bot Started!")
    asyncio.create_task(monitor())
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
