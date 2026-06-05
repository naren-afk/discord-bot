import discord
import requests
from bs4 import BeautifulSoup
import sqlite3
import asyncio
from datetime import datetime, timedelta
import os
import urllib.parse
import threading
from flask import Flask

# ---------------- ENV ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

RAW_QUERIES = os.getenv(
    "QUERY",
    'site:reddit.com "u/sane_mwm"'
)

QUERIES = [q.strip() for q in RAW_QUERIES.split(";")]

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ---------------- DATABASE ----------------
conn = sqlite3.connect("tracker.db", check_same_thread=False)
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS activity (
    url TEXT PRIMARY KEY,
    first_seen TEXT
)
""")
conn.commit()


def save_link(url):
    try:
        c.execute(
            "INSERT INTO activity VALUES (?, ?)",
            (url, datetime.utcnow().isoformat())
        )
        conn.commit()
        return True
    except:
        return False


def get_all():
    return c.execute(
        "SELECT * FROM activity ORDER BY first_seen DESC"
    ).fetchall()


def get_last_month():
    cutoff = datetime.utcnow() - timedelta(days=30)
    return c.execute(
        "SELECT * FROM activity WHERE first_seen >= ? ORDER BY first_seen DESC",
        (cutoff.isoformat(),)
    ).fetchall()


# ---------------- SCRAPER ----------------
def fetch_links():
    links = set()

    for query in QUERIES:
        url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&num=20"
        res = requests.get(url, headers=HEADERS)

        if res.status_code != 200:
            continue

        soup = BeautifulSoup(res.text, "html.parser")

        for a in soup.select("a"):
            href = a.get("href", "")
            if "/url?q=" in href and "reddit.com" in href:
                clean = href.split("/url?q=")[1].split("&")[0]
                if "/comments/" in clean:
                    links.add(clean)

    return links


# ---------------- DISCORD BOT ----------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    bot.loop.create_task(tracker_loop())


async def tracker_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)

    while True:
        print("Checking...")
        links = fetch_links()

        for link in links:
            if save_link(link):
                await channel.send(f"🆕 New activity found:\n{link}")

        await asyncio.sleep(3600)  # 1 hour


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.content == "!all":
        data = get_all()
        if not data:
            await message.channel.send("No data yet.")
            return

        msg = "**All Activity:**\n"
        for url, ts in data[:20]:
            msg += f"{ts} → {url}\n"

        await message.channel.send(msg)

    elif message.content == "!month":
        data = get_last_month()

        if not data:
            await message.channel.send("No activity in last month.")
            return

        msg = "**Last 30 Days:**\n"
        for url, ts in data[:20]:
            msg += f"{ts} → {url}\n"

        await message.channel.send(msg)

    elif message.content == "!latest":
        data = get_all()[:5]

        msg = "**Latest Activity:**\n"
        for url, ts in data:
            msg += f"{ts} → {url}\n"

        await message.channel.send(msg)


# ---------------- FLASK SERVER ----------------
app = Flask(__name__)


@app.route("/")
def home():
    return "Tracker is running"


def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# ---------------- STARTUP ----------------
def start_bot():
    if not TOKEN:
        raise ValueError("Missing DISCORD_TOKEN")

    bot.run(TOKEN)


if __name__ == "__main__":
    # Start Flask in a separate thread
    threading.Thread(target=run_flask).start()

    # Start Discord bot (blocking)
    start_bot()
