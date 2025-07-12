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
import traceback
from flask import Flask
from threading import Thread

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
BASE_URL = "https://filmyfly.loan/"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FilmyFlyBot")

# --- Pyrogram Client ---
app = Client("filmyfly-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Keep Alive Flask Server ---
flask_app = Flask('')

@flask_app.route('/')
def home():
    return "✅ Bot is alive!"

def run_flask():
    flask_app.run(host='0.0.0.0', port=8080)

def ping_self():
    url = os.environ.get("SELF_PING_URL", "https://your-app-url.onrender.com")  # 🔁 change this
    while True:
        try:
            requests.get(url)
            print("✅ Self-ping sent")
        except Exception as e:
            print(f"⚠️ Self-ping failed: {e}")
        time.sleep(120)

def keep_alive():
    Thread(target=run_flask).start()
    Thread(target=ping_self).start()

# --- File Tracker ---
def load_filmy():
    if os.path.exists(filmy_FILE):
        with open(filmy_FILE) as f:
            return set(json.load(f))
    return set()

def save_filmy(filmy):
    with open(filmy_FILE, "w") as f:
        json.dump(list(filmy), f, indent=2)

# --- Safe Request Wrapper ---
def safe_request(url, retries=2):
    for _ in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, verify=False, timeout=15)
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

def get_intermediate_links(quality_page_url):
    r = safe_request(quality_page_url)
    if not r: return []
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
            logger.info(f"🔗 Intermediate: {link} | Provider: {provider}")
    return links

def extract_final_links(cloud_url):
    r = safe_request(cloud_url)
    if not r: return []
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
            logger.info(f"🔍 Final links: {links}")
    return links

def get_title_from_intermediate(url):
    r = safe_request(url)
    if not r: return "Untitled"
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.find("title")
    return title.text.strip() if title else "Untitled"

def clean(text):
    return re.sub(r"[\[\]_`*]", "", text)

# --- Telegram Messaging ---
async def send_quality_message(title, quality, provider, links):
    if not links:
        logger.warning(f"Skipping empty link list for {title}")
        return

    msg = f"🎬 `{clean(title)}`\n\n"
    msg += f"🔗 **Quality**: `{provider}`\n\n"
    for label, url in links:
        msg += f"• [{clean(label)}]({url})\n"
    msg += "\n🌐 Scraped from [FilmyFly](https://telegram.me/Silent_Bots)"

    try:
        logger.info(f"Sending to channel: {title} | {quality} | {provider}")
        sent_msg = await app.send_message(
            chat_id=CHANNEL_ID,
            text=msg,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        logger.info(f"✅ Sent message ID {sent_msg.id} for {title}")
    except FloodWait as e:
        logger.warning(f"⏳ Flood wait: {e.value}s for {title}")
        await asyncio.sleep(e.value)
        await send_quality_message(title, quality, provider, links)
    except Exception as e:
        logger.error("Send error:\n" + traceback.format_exc())
        await app.send_message(OWNER_ID, f"❌ Send Error for `{title}`\n\n{e}")
# --- Monitor Task ---
async def monitor():
    logger.info("🚀 Monitor started")
    filmy = load_filmy()
    logger.info(f"Loaded {len(filmy)} tracked movies")
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
                            intermediate_links = await asyncio.to_thread(get_intermediate_links, view_url)
                            for provider, link in intermediate_links:
                                finals = await asyncio.to_thread(extract_final_links, link)
                                if not finals:
                                    logger.warning(f"No final links for: {link}")
                                    await asyncio.sleep(2)
                                    finals = await asyncio.to_thread(extract_final_links, link)
                                if finals:
                                    title = await asyncio.to_thread(get_title_from_intermediate, link)
                                    await send_quality_message(title, quality, provider, finals)
                    filmy.add(movie_url)
                    save_filmy(filmy)
                except Exception as e:
                    logger.error("❌ Movie processing error:\n" + traceback.format_exc())
                    await app.send_message(
                        OWNER_ID,
                        f"⚠️ Error while processing:\n`{movie_url}`\n\n{traceback.format_exc()[:4000]}"
                    )
        except Exception as e:
            logger.error("❌ Monitor loop error:\n" + traceback.format_exc())
            await app.send_message(
                OWNER_ID,
                f"🚨 Monitor crashed:\n\n{traceback.format_exc()[:4000]}"
            )
        await asyncio.sleep(300)


# --- Start Bot ---
async def main():
    keep_alive()
    await app.start()
    await app.send_message(CHANNEL_ID, "✅ Bot started and connected!")
    asyncio.create_task(monitor())
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
