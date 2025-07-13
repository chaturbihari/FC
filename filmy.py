import os
import json
import logging
import asyncio
import nest_asyncio
from bs4 import BeautifulSoup
from collections import defaultdict
from urllib.parse import urljoin
from pyrogram import Client, idle, utils
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from playwright.async_api import async_playwright
import re

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

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FilmyFlyBot")

# --- Pyrogram Client ---
app = Client("filmyfly-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Movie Tracker ---
def load_filmy():
    if os.path.exists(filmy_FILE):
        with open(filmy_FILE) as f:
            return set(json.load(f))
    return set()

def save_filmy(filmy):
    with open(filmy_FILE, "w") as f:
        json.dump(list(filmy), f, indent=2)

# --- Playwright Helpers ---
async def get_html(page, url):
    try:
        # Block ads and trackers
        async def handle_route(route, request):
            if any(ext in request.url for ext in [".jpg", ".png", ".gif", ".css", ".woff", "googlesyndication", "doubleclick", "adservice", "popads"]):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", handle_route)

        # Catch popup windows (ads)
        page.on("popup", lambda popup: asyncio.create_task(popup.close()))

        # Don't wait for full load — many ad-heavy sites never truly "load"
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")

        # Optional: wait for real content to load (like download buttons)
        # await page.wait_for_selector("a[href*='linkmake']", timeout=5000)

        return await page.content()

    except Exception as e:
        logger.warning(f"Playwright failed to load: {url} - {e}")
        return None

async def get_latest_movie_links(playwright):
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()
    html = await get_html(page, BASE_URL)
    if not html:
        await browser.close()
        return []
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.find_all("div", class_="A10")
    links = [urljoin(BASE_URL, a["href"].strip()) for b in blocks if (a := b.find("a", href=True))]
    await browser.close()
    return list(dict.fromkeys(links))

async def get_quality_links(playwright, movie_url):
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()
    html = await get_html(page, movie_url)
    qlinks = defaultdict(list)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True, string=True):
            text = a.get_text().strip()
            if "download" in text.lower() and "/view/" in a["href"]:
                qual = re.search(r"\{(.+?)\}", text)
                quality = qual.group(1) if qual else "Other"
                full = urljoin(BASE_URL, a["href"])
                qlinks[quality].append(full)
    await browser.close()
    return dict(qlinks)

async def get_intermediate_links(playwright, quality_page_url):
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()
    html = await get_html(page, quality_page_url)
    links = []
    if html:
        soup = BeautifulSoup(html, "html.parser")
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
    await browser.close()
    return links

async def extract_final_links(playwright, cloud_url):
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()
    html = await get_html(page, cloud_url)
    links = []
    if html:
        soup = BeautifulSoup(html, "html.parser")
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
    await browser.close()
    return links

async def get_title_from_intermediate(playwright, url):
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()
    html = await get_html(page, url)
    title = "Untitled"
    if html:
        soup = BeautifulSoup(html, "html.parser")
        t = soup.find("title")
        if t:
            title = t.text.strip()
    await browser.close()
    return title

# --- Telegram Messaging ---
def clean(text):
    return re.sub(r"[\[\]_`*]", "", text)

async def send_quality_message(title, quality, provider, links):
    msg = f"🎬 `{clean(title)}`\n\n"
    msg += f"🔗 **Quality**: `{provider}`\n\n"
    for label, url in links:
        msg += f"• [{clean(label)}]({url})\n"
    msg += "\n🌐 Scraped from [FilmyFly](https://telegram.me/Silent_Bots)"
    try:
        await app.send_message(
            chat_id=CHANNEL_ID,
            text=msg,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
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
    async with async_playwright() as playwright:
        while True:
            try:
                movies = await get_latest_movie_links(playwright)
                new = [m for m in movies if m not in filmy]
                logger.info(f"Found {len(new)} new movies")
                for movie_url in new:
                    logger.info(f"Processing: {movie_url}")
                    try:
                        qlinks = await get_quality_links(playwright, movie_url)
                        for quality, view_urls in qlinks.items():
                            for view_url in view_urls:
                                intermediate_links = await get_intermediate_links(playwright, view_url)
                                for provider, link in intermediate_links:
                                    finals = await extract_final_links(playwright, link)
                                    if not finals:
                                        logger.warning(f"No final links for: {link}")
                                        await asyncio.sleep(2)
                                        finals = await extract_final_links(playwright, link)
                                    if finals:
                                        title = await get_title_from_intermediate(playwright, link)
                                        await send_quality_message(title, quality, provider, finals)
                        filmy.add(movie_url)
                        save_filmy(filmy)
                    except Exception as e:
                        logger.error(f"Error while processing movie: {movie_url} - {e}")
                        await app.send_message(OWNER_ID, f"⚠️ Error on: {movie_url}\n\n{e}")
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
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
