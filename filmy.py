import os
import json
import logging
import asyncio
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from pyrogram import Client, idle, utils
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
import nest_asyncio
from collections import defaultdict
import re
import urllib3
import time
from playwright.async_api import async_playwright

# --- Environment Setup ---
nest_asyncio.apply()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_ID = int(os.environ.get("API_ID", "25833520"))
API_HASH = os.environ.get("API_HASH", "7d012a6cbfabc2d0436d7a09d8362af7")
BOT_TOKEN = os.environ.get("FF_BOT_TOKEN", "8091169950:AAGNyiZ8vqrqCiPhZcks-Av3lDQy2GIcZuk")
CHANNEL_ID = int(os.environ.get("FF_CHANNEL_ID", "-1002557597877"))
OWNER_ID = int(os.environ.get("FF_OWNER_ID", "921365334"))
filmy_FILE = "filmy.json"
utils.get_peer_type = lambda peer_id: "channel" if str(peer_id).startswith("-100") else "user"
BASE_URL = "https://filmyfly.party/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36",
    "Referer": "https://linkmake.in/",
    "Accept-Language": "en-US,en;q=0.9",
}

# --- Logging Setup ---
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
# Silence overly verbose Pyrogram internals
logging.getLogger("pyrogram.session.session").setLevel(logging.WARNING)
logging.getLogger("pyrogram.dispatcher").setLevel(logging.INFO)
logging.getLogger("pyrogram.connection.connection").setLevel(logging.WARNING)

logger = logging.getLogger("FilmyFlyBot")

# --- Pyrogram Client ---
app = Client("filmyfly-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- File Tracker ---
def load_filmy():
    if os.path.exists(filmy_FILE):
        with open(filmy_FILE) as f:
            return set(json.load(f))
    return set()

def save_filmy(filmy):
    with open(filmy_FILE, "w") as f:
        json.dump(list(filmy), f, indent=2)

# --- Safe Request ---
def safe_request(url, retries=2):
    logger.debug(f"Requesting URL: {url}")
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, verify=False, timeout=15)
            logger.debug(f"[{url}] Status: {r.status_code}")
            if r.status_code == 200:
                return r
        except Exception as e:
            logger.warning(f"Request attempt {attempt+1} failed: {e}")
        time.sleep(1)
    logger.error(f"❌ Failed to fetch: {url}")
    return None

# --- Scrapers ---
def get_latest_movie_links():
    logger.info("Fetching homepage...")
    r = safe_request(BASE_URL)
    if not r: return []
    soup = BeautifulSoup(r.text, "html.parser")
    blocks = soup.find_all("div", class_="A10")
    links = [urljoin(BASE_URL, a["href"].strip()) for b in blocks if (a := b.find("a", href=True))]
    logger.info(f"Found {len(links)} movie links")
    return list(dict.fromkeys(links))

def get_quality_links(movie_url):
    logger.debug(f"Getting quality links from: {movie_url}")
    r = safe_request(movie_url)
    if not r: return {}
    soup = BeautifulSoup(r.text, "html.parser")
    qlinks = defaultdict(list)
    for a in soup.find_all("a", href=True, string=True):
        text = a.get_text().strip()
        if "download" in text.lower() and "/view/" in a["href"]:
            qual = re.search(r"\{(.+?)\}", text)
            quality = qual.group(1) if qual else "Other"
            full = urljoin(BASE_URL, a["href"])
            logger.debug(f"Quality link: {quality} => {full}")
            qlinks[quality].append(full)
    return dict(qlinks)

def get_intermediate_links(quality_page_url):
    r = safe_request(quality_page_url)
    if not r or "Just a moment" in r.text:
        return None  # force fallback to playwright
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for tag in soup.find_all(["a", "button"]):
        href = tag.get("href") or tag.get("data-href")
        if not href:
            onclick = tag.get("onclick", "")
            m = re.search(r"location\.href='([^']+)'", onclick)
            if m:
                href = m.group(1)
        label = tag.get_text(strip=True)
        if href and label and href.startswith("http") and not any(x in label.lower() for x in ["login", "signup"]):
            links.append((label, href))
    return links

async def get_intermediate_links_playwright(url):
    links = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                logger.warning(f"❌ Playwright page.goto failed: {e}")
                return []

            await page.wait_for_timeout(2000)

            elements = await page.query_selector_all("a, button")
            for el in elements:
                href = await el.get_attribute("href") or await el.get_attribute("data-href")
                label = (await el.inner_text()).strip()
                if not href:
                    onclick = await el.get_attribute("onclick") or ""
                    match = re.search(r"location\\.href=['\"]([^'\"]+)['\"]", onclick)
                    if match:
                        href = match.group(1)
                if href and label and href.startswith("http") and not any(x in label.lower() for x in ["login", "signup"]):
                    links.append((label, href))
    except Exception as e:
        logger.warning(f"Playwright fallback failed: {e}")
    finally:
        try:
            await browser.close()
        except:
            pass
    return links


async def extract_final_links_playwright(cloud_url):
    links = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)
            page = await context.new_page()
            logger.debug(f"Playwright: Navigating to {cloud_url}")
            resp = await page.goto(cloud_url, wait_until='networkidle', timeout=45000)

            # Wait until we're not on Cloudflare anymore
            if "cloudflare.com" in page.url:
                logger.warning(f"Still stuck on Cloudflare after navigation: {page.url}")
                await browser.close()
                return []

            await page.wait_for_timeout(1500)
            elements = await page.query_selector_all("a, button, form")
            for el in elements:
                href = await el.get_attribute("href") or await el.get_attribute("data-href") or await el.get_attribute("action")
                label = (await el.inner_text()).strip()
                if href and label and href.startswith("http") and not any(x in label.lower() for x in ["login", "signup"]):
                    links.append((label, href))

            await browser.close()
    except Exception as e:
        logger.warning(f"Playwright final-link fallback failed: {e}")
    return links



def extract_final_links(cloud_url):
    logger.debug(f"Requesting URL: {cloud_url}")
    r = safe_request(cloud_url)
    if not r:
        logger.warning(f"❌ Failed to fetch: {cloud_url}")
        return []

    if "cloudflare.com" in r.url:
        logger.warning(f"🚫 Stuck at Cloudflare challenge: {r.url}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    links = []

    for tag in soup.find_all(["a", "button"]):
        href = tag.get("href") or tag.get("data-href")
        label = tag.get_text(strip=True)
        if href and label and href.startswith("http"):
            links.append((label, href))

    for form in soup.find_all("form"):
        action = form.get("action")
        label = form.get_text(strip=True)
        if action and action.startswith("http"):
            links.append((label, action))

    logger.debug(f"[{cloud_url}] Extracted {len(links)} final links")
    return links


def get_title_from_intermediate(url):
    logger.debug(f"Extracting title from: {url}")
    r = safe_request(url)
    if not r: return "Untitled"
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.find("title")
    return title.text.strip() if title else "Untitled"

def clean(text):
    return re.sub(r"[\[\]_`*]", "", text)

# --- Telegram Messaging ---
async def send_quality_message(title, quality, provider, links):
    logger.info(f"Sending: {title} | {quality} | {provider} | {len(links)} links")
    msg = f"🎬 `{clean(title)}`\n\n"
    msg += f"🔗 **Quality**: `{provider}`\n\n"
    for label, url in links:
        msg += f"• [{clean(label)}]({url})\n"
    msg += "\n🌐 Scraped from [FilmyFly](https://telegram.me/Silent_Bots)"
    try:
        await app.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except FloodWait as e:
        logger.warning(f"FloodWait: Sleeping {e.value}s")
        await asyncio.sleep(e.value)
        await send_quality_message(title, quality, provider, links)
    except Exception as e:
        logger.error(f"Send error: {e}")
        await app.send_message(OWNER_ID, f"❌ Send Error for `{title}`\n\n{e}")

# --- Monitor Task ---
async def monitor():
    filmy = load_filmy()
    logger.info(f"📦 Loaded {len(filmy)} previous movie entries")
    while True:
        try:
            logger.info("📥 Fetching latest movie links...")
            movies = await asyncio.to_thread(get_latest_movie_links)
            new = [m for m in movies if m not in filmy]
            logger.info(f"🆕 Found {len(new)} new movies")
            for movie_url in new:
                logger.info(f"➡️ Processing: {movie_url}")
                try:
                    qlinks = await asyncio.to_thread(get_quality_links, movie_url)
                    for quality, view_urls in qlinks.items():
                        for view_url in view_urls:
                            intermediate_links = await asyncio.to_thread(get_intermediate_links, view_url)
                            if intermediate_links is None or len(intermediate_links) == 0:
                                logger.info(f"Fallback to Playwright: {view_url}")
                                intermediate_links = await get_intermediate_links_playwright(view_url)
                            for provider, link in intermediate_links:
                                finals = await asyncio.to_thread(extract_final_links, link)
                                finals = await asyncio.to_thread(extract_final_links, link)
                                if not finals:
                                    logger.warning(f"🌐 Falling back to Playwright for final: {link}")
                                    finals = await extract_final_links_playwright(link)
                                if finals:
                                    title = await asyncio.to_thread(get_title_from_intermediate, link)
                                    await send_quality_message(title, quality, provider, finals)
                                else:
                                    logger.error(f"❌ No final links for {link}")
                    filmy.add(movie_url)
                    save_filmy(filmy)
                    logger.info(f"✅ Done: {movie_url}")
                except Exception as e:
                    logger.exception(f"⚠️ Error while processing movie: {movie_url}")
                    await app.send_message(OWNER_ID, f"⚠️ Error on: {movie_url}\n\n{e}")
        except Exception as e:
            logger.exception("🚨 Monitor loop crashed")
            await app.send_message(OWNER_ID, f"🚨 Monitor loop crashed:\n\n{e}")
        await asyncio.sleep(300)

# --- Start Bot ---
async def main():
    await app.start()
    asyncio.create_task(monitor())
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
