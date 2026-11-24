#!/usr/bin/env python3
"""
Post scraped ads to a Telegram forum topic (thread).
Runs inside GitHub Actions AFTER fb_ad_scraper.py finishes.

Each run creates a new topic in a supergroup (Topics must be enabled).
Every ad is posted with its media (uploaded to Telegram), and the
returned file_id is saved to telegram_file_ids.json so the media can
be re-sent instantly later without re-uploading.

Required env vars
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID       supergroup ID (negative number, e.g. -100...)

Optional env vars
    AD_QUERY               shown in the topic title
    ADS_DIR                path to the scraper output  (default: ads_out)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Load .env file if present (for local testing)
# ---------------------------------------------------------------------------
def _load_env_file():
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_file):
        env_file = ".env"
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v

_load_env_file()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
ADS_DIR = Path(os.environ.get("ADS_DIR", "ads_out"))
FILE_IDS_PATH = ADS_DIR / "telegram_file_ids.json"

# Telegram limits
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
UPLOAD_LIMIT_MB = 50


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
def tg(method, **kwargs):
    """Call a Telegram Bot API method and return the result."""
    for attempt in range(3):
        try:
            r = requests.post(f"{API}/{method}", timeout=120, **kwargs)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            # Rate-limited — wait and retry
            if r.status_code == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 5)
                print(f"  Rate-limited, waiting {retry_after}s…")
                time.sleep(retry_after)
                continue
            raise RuntimeError(f"Telegram API {method}: {data.get('description', data)}")
        except requests.RequestException as e:
            if attempt < 2:
                time.sleep(3)
                continue
            raise RuntimeError(f"Network error calling {method}: {e}")
    raise RuntimeError(f"Failed {method} after 3 attempts")


def create_topic(title):
    """Create a forum topic and return its message_thread_id."""
    return tg("createForumTopic", json={
        "chat_id": CHAT_ID,
        "name": title[:128],
    })["message_thread_id"]


def send_text(thread_id, text):
    return tg("sendMessage", json={
        "chat_id": CHAT_ID,
        "message_thread_id": thread_id,
        "text": text[:MESSAGE_LIMIT],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def send_photo(thread_id, photo_path, caption):
    """Upload a photo file. Returns the Message object."""
    with open(photo_path, "rb") as f:
        return tg("sendPhoto",
                  data={
                      "chat_id": CHAT_ID,
                      "message_thread_id": thread_id,
                      "caption": caption[:CAPTION_LIMIT],
                      "parse_mode": "HTML",
                  },
                  files={"photo": f})


def send_video(thread_id, video_path, caption):
    """Upload a video file. Returns the Message object."""
    with open(video_path, "rb") as f:
        return tg("sendVideo",
                  data={
                      "chat_id": CHAT_ID,
                      "message_thread_id": thread_id,
                      "caption": caption[:CAPTION_LIMIT],
                      "parse_mode": "HTML",
                  },
                  files={"video": f})


# ---------------------------------------------------------------------------
# Ad formatting
# ---------------------------------------------------------------------------
def format_caption(ad):
    """Build a compact HTML caption for one ad (must stay under 1024 chars)."""
    lines = []

    name = ad.get("advertiser") or "Unknown advertiser"
    lines.append(f"<b>🏷 {_esc(name)}</b>")

    status = (ad.get("status") or "?").capitalize()
    days = ad.get("days_running")
    days_str = f"{days} days" if days is not None else "? days"
    lines.append(f"📊 {status} • {days_str}")

    body = ad.get("body_text") or ""
    if body:
        body = body[:200].replace("\n", " ")
        if len(ad.get("body_text", "")) > 200:
            body += "…"
        lines.append(f"\n📝 {_esc(body)}")

    headline = ad.get("headline")
    cta = ad.get("cta")
    if headline or cta:
        parts = []
        if headline:
            parts.append(_esc(headline[:80]))
        if cta:
            parts.append(f"CTA: {_esc(cta)}")
        lines.append(f"🎯 {' | '.join(parts)}")

    domain = ad.get("domain")
    if domain:
        lines.append(f"🌐 {_esc(domain)}")

    landings = ad.get("landing_urls") or []
    if landings:
        lines.append(f"🔗 {landings[0][:100]}")

    lib_id = ad.get("library_id", "?")
    lines.append(
        f'\n📚 <a href="https://www.facebook.com/ads/library/?id={lib_id}">View in Ad Library</a>'
        f"  •  ID: <code>{lib_id}</code>"
    )

    return "\n".join(lines)


def format_text_message(ad):
    """Longer version for ads without media (up to 4096 chars)."""
    lines = []

    name = ad.get("advertiser") or "Unknown advertiser"
    lines.append(f"<b>🏷 {_esc(name)}</b>")

    status = (ad.get("status") or "?").capitalize()
    days = ad.get("days_running")
    days_str = f"{days} days" if days is not None else "? days"
    lines.append(f"📊 {status} • {days_str}")

    body = ad.get("body_text") or ""
    if body:
        body = body[:600]
        if len(ad.get("body_text", "")) > 600:
            body += "…"
        lines.append(f"\n📝 {_esc(body)}")

    headline = ad.get("headline")
    cta = ad.get("cta")
    if headline:
        lines.append(f"🎯 {_esc(headline)}")
    if cta:
        lines.append(f"🔘 CTA: {_esc(cta)}")

    domain = ad.get("domain")
    if domain:
        lines.append(f"🌐 {_esc(domain)}")

    landings = ad.get("landing_urls") or []
    if landings:
        lines.append(f"🔗 {landings[0][:200]}")

    variants = ad.get("same_creative_count")
    if variants and variants > 1:
        lines.append(f"🔄 {variants} ads use this creative")

    lib_id = ad.get("library_id", "?")
    lines.append(
        f'\n📚 <a href="https://www.facebook.com/ads/library/?id={lib_id}">View in Ad Library</a>'
        f"  •  ID: <code>{lib_id}</code>"
    )

    return "\n".join(lines)


def _esc(s):
    """Escape HTML special chars for Telegram."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------
def find_local_media(ad):
    """Return (Path, type) for the first downloaded media file, or (None, None)."""
    for m in ad.get("media", []):
        fname = m.get("file")
        if fname:
            path = ADS_DIR / ad["library_id"] / fname
            if path.exists():
                size_mb = path.stat().st_size / (1024 * 1024)
                if size_mb <= UPLOAD_LIMIT_MB:
                    return path, m["type"]
    return None, None


def extract_file_id(msg, media_type):
    """Pull the file_id from a Telegram Message object."""
    if media_type == "video" and msg.get("video"):
        return {"type": "video", "file_id": msg["video"]["file_id"]}
    if msg.get("photo"):
        # photo is an array of sizes; take the largest
        return {"type": "photo", "file_id": msg["photo"][-1]["file_id"]}
    return None


# ---------------------------------------------------------------------------
# File-ID persistence
# ---------------------------------------------------------------------------
def load_file_ids():
    if FILE_IDS_PATH.exists():
        return json.loads(FILE_IDS_PATH.read_text(encoding="utf-8"))
    return {}


def save_file_ids(fids):
    ADS_DIR.mkdir(parents=True, exist_ok=True)
    FILE_IDS_PATH.write_text(
        json.dumps(fids, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set.")
    if not CHAT_ID:
        sys.exit("TELEGRAM_CHAT_ID is not set.")

    ads_file = ADS_DIR / "all_ads.json"
    if not ads_file.exists():
        sys.exit(f"{ads_file} not found — nothing to send.")

    ads = json.loads(ads_file.read_text(encoding="utf-8"))
    if not ads:
        sys.exit("all_ads.json is empty — nothing to send.")

    file_ids = load_file_ids()
    query = os.environ.get("AD_QUERY", "")
    now = datetime.now(timezone.utc).strftime("%b %d, %Y  %H:%M UTC")

    # ---- Create forum topic ----
    active = sum(1 for a in ads if a.get("status") == "active")
    title = f"🔍 {now} • {len(ads)} ads"
    if query:
        title += f' • "{query[:40]}"'

    thread_id = create_topic(title)
    print(f"✅ Created topic: {title}  (thread_id={thread_id})")

    # ---- Summary message ----
    summary = (
        f"<b>📋 Scrape Summary</b>\n\n"
        f"🔎 Query: <code>{_esc(query or 'N/A')}</code>\n"
        f"📊 Total ads: <b>{len(ads)}</b>\n"
        f"✅ Active: <b>{active}</b>\n"
        f"⏸ Inactive: <b>{len(ads) - active}</b>\n"
        f"🕐 Scraped at: {now}"
    )
    send_text(thread_id, summary)
    time.sleep(0.5)

    # ---- Post each ad ----
    sent = 0
    failed = 0
    for i, ad in enumerate(ads, 1):
        lib_id = ad.get("library_id", "?")
        media_path, media_type = find_local_media(ad)

        try:
            if media_path:
                caption = format_caption(ad)
                if media_type == "video":
                    msg = send_video(thread_id, media_path, caption)
                else:
                    msg = send_photo(thread_id, media_path, caption)
                # Store the file_id
                fid = extract_file_id(msg, media_type)
                if fid:
                    file_ids[lib_id] = fid
            else:
                # No local media — send as text
                send_text(thread_id, format_text_message(ad))

            sent += 1
            print(f"  [{i}/{len(ads)}] ✓ {lib_id}")
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(ads)}] ✗ {lib_id}: {e}")

        # Respect Telegram rate limits (~20 msg/min to groups)
        time.sleep(3.0 if media_path else 1.5)

    # ---- Save file_ids ----
    save_file_ids(file_ids)

    # ---- Final summary in thread ----
    send_text(thread_id, f"<b>✅ Done — {sent} ads posted, {failed} failed.</b>")

    print(f"\n{'='*50}")
    print(f"Done. {sent} ads sent, {failed} failed.")
    print(f"file_ids saved to {FILE_IDS_PATH}")


if __name__ == "__main__":
    main()
