import os
import json
import logging
import asyncio
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from pyrogram import Client, idle, utils
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from collections import defaultdict
import re
import time
from playwright.async_api import async_playwright
import nest_asyncio

# --- Environment Setup ---
nest_asyncio.apply()

API_ID = int(os.environ.get("API_ID", "25833520"))
API_HASH = os.environ.get("API_HASH", "7d012a6cbfabc2d0436d7a09d8362af7")
BOT_TOKEN = os.environ.get("FF_BOT_TOKEN", "8091169950:AAGNyiZ8vqrqCiPhZcks-Av3lDQy2GIcZuk")
CHANNEL_ID = int(os.environ.get("FF_CHANNEL_ID", "-1002557597877"))
OWNER_ID = int(os.environ.get("FF_OWNER_ID", "921365334"))
filmy_FILE = "filmy.json"
utils.get_peer_type = lambda peer_id: "channel" if str(peer_id).startswith("-100") else "user"
BASE_URL = "https://filmyfly.party/"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FilmyFlyBot")

# --- Pyrogram Client ---
app = Client("filmyfly-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- filmy Movie Tracker ---
def load_filmy():
    if os.path.exists(filmy_FILE):
        with open(filmy_FILE) as f:
            return set(json.load(f))
    return set()

def save_filmy(filmy):
    with open(filmy_FILE, "w") as f:
        json.dump(list(filmy), f, indent=2)

# --- Basic HTTP Fetcher for initial pages ---
import requests

def safe_request(url, retries=2):
    for _ in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
            if r.status_code == 200:
                return r
        except Exception as e:
            logger.warning(f"Request failed: {e}")
        time.sleep(1)
    return None

# --- Scraper Functions ---
def get_latest_movie_links():
    logger.info("Fetching homepage")
    r = safe_request(BASE_URL)
    if not r: return []
    soup = BeautifulSoup(r.text, "html.parser")
    blocks = soup.find_all("div", class_="A10")
    links = [urljoin(BASE_URL, a["href"].strip()) for b in blocks if (a := b.find("a", href=True))]
    return list(dict.fromkeys(links))

def get_quality_links(movie_url):
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
            qlinks[quality].append(full)
    return dict(qlinks)

def get_intermediate_links_playwright(url):
    links = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=30000)
            page.wait_for_selector("a, button", timeout=10000)
            elements = page.query_selector_all("a, button")

            for el in elements:
                href = el.get_attribute("href") or el.get_attribute("data-href")
                onclick = el.get_attribute("onclick")
                label = el.inner_text().strip()

                # Support onclick-based navigation
                if not href and onclick:
                    m = re.search(r"location\.href='([^']+)'", onclick)
                    if m:
                        href = m.group(1)

                # Only valid, direct, labeled download links
                if (
                    href and href.startswith("http") and
                    label and "download" in label.lower() and
                    all(x not in label.lower() for x in ["login", "signup"])
                ):
                    links.append((label, href))
                    break  # Stop at the first relevant download button
        except Exception as e:
            print("Playwright error:", e)
        finally:
            browser.close()
    return links

get_intermediate_links = extract_links_with_playwright
extract_final_links = extract_links_with_playwright

async def get_title_from_intermediate(url):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(url, timeout=30000)
            title = await page.title()
            await browser.close()
            return title.strip()
    except:
        return "Untitled"

def clean(text):
    return re.sub(r"[\[\]_`*]", "", text)

# --- Telegram Messaging ---
async def send_quality_message(title, quality, provider, links):
    msg = f"\n🎬 `{clean(title)}`\n\n"
    msg += f"🔗 **Quality**: `{provider}`\n\n"
    for label, url in links:
        msg += f"• [{clean(label)}]({url})\n"
    msg += "\n🌐 Scraped from [FilmyFly](https://telegram.me/Silent_Bots)"
    try:
        await app.send_message(chat_id=CHANNEL_ID,
                               text=msg,
                               parse_mode=ParseMode.MARKDOWN,
                               disable_web_page_preview=True)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await send_quality_message(title, quality, provider, links)
    except Exception as e:
        logger.error(f"Send error: {e}")
        await app.send_message(OWNER_ID, f"❌ Send Error for `{title}`\n\n{e}")

# --- Monitor Task ---
async def monitor():
    filmy = load_filmy()
    logger.info(f"Loaded {len(filmy)} filmy entries")
    while True:
        try:
            movies = await asyncio.to_thread(get_latest_movie_links)
            new = [m for m in movies if m not in filmy]
            logger.info(f"Found {len(new)} new movies")
            for movie_url in new:
                logger.info(f"Processing: {movie_url}")
                try:
                    qlinks = await asyncio.to_thread(get_quality_links, movie_url)
                    for quality, view_urls in qlinks.items():
                        for view_url in view_urls:
                            intermediate_links = await get_intermediate_links(view_url)
                            for provider, link in intermediate_links:
                                finals = await extract_final_links(link)
                                if not finals:
                                    await asyncio.sleep(2)
                                    finals = await extract_final_links(link)
                                if finals:
                                    title = await get_title_from_intermediate(link)
                                    await send_quality_message(title, quality, provider, finals)
                    filmy.add(movie_url)
                    save_filmy(filmy)
                except Exception as e:
                    logger.error(f"Error while processing movie: {movie_url} - {e}")
                    await app.send_message(OWNER_ID, f"⚠️ Error on: {movie_url}\n\n{e}")
        except Exception as e:
            logger.exception("Fatal monitor loop error:")
            await app.send_message(OWNER_ID, f"💥 Fatal error in loop:\n\n{e}")
            await asyncio.sleep(30)  

# --- Start Bot ---
async def main():
    await app.start()
    asyncio.create_task(monitor())
    await idle()
    logger.warning("❗ idle() exited — bot is shutting down.")
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
