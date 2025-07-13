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
HEADERS = {"User-Agent": "Mozilla/5.0"}

# --- Logging Setup ---
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
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

async def get_intermediate_links(quality_page_url):
    logger.debug(f"Extracting intermediate links from (playwright): {quality_page_url}")
    links = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(quality_page_url, timeout=30000)
            await page.wait_for_timeout(1000)  # wait for JS to render

            elements = await page.query_selector_all("a, button")
            for el in elements:
                href = await el.get_attribute("href") or await el.get_attribute("data-href")
                label = (await el.inner_text()).strip()
                if not href:
                    onclick = await el.get_attribute("onclick") or ""
                    match = re.search(r"location\.href='([^']+)'", onclick)
                    if match:
                        href = match.group(1)
                if href and label and href.startswith("http") and not any(x in label.lower() for x in ["login", "signup"]):
                    logger.debug(f"Playwright Intermediate Link: {label} => {href}")
                    links.append((label, href))
            await browser.close()
    except Exception as e:
        logger.error(f"❌ Playwright intermediate fetch failed: {e}")
    logger.debug(f"Playwright: Found {len(links)} intermediate links")
    return links


def extract_final_links(cloud_url):
    logger.debug(f"Extracting final download links from: {cloud_url}")
    r = safe_request(cloud_url)
    if not r:
        logger.warning(f"❌ No response from final page: {cloud_url}")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for tag in soup.find_all(["a", "button"]):
        href = tag.get("href") or tag.get("data-href")
        label = tag.get_text(strip=True)
        if href and label and href.startswith("http"):
            logger.debug(f"Final link: {label} => {href}")
            links.append((label, href))
    for form in soup.find_all("form"):
        action = form.get("action")
        label = form.get_text(strip=True)
        if action and action.startswith("http"):
            logger.debug(f"Final form link: {label} => {action}")
            links.append((label, action))
    logger.debug(f"Extracted {len(links)} final links from cloud page")
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
                            intermediate_links = await get_intermediate_links(view_url)
                            for provider, link in intermediate_links:
                                finals = await asyncio.to_thread(extract_final_links, link)
                                if not finals:
                                    logger.warning(f"🔁 Retrying final link for: {link}")
                                    await asyncio.sleep(2)
                                    finals = await asyncio.to_thread(extract_final_links, link)
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
