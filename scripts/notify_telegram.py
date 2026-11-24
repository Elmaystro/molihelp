#!/usr/bin/env python3
"""
Post scraped ads to a Telegram Channel as commentable posts.
Runs inside GitHub Actions AFTER fb_ad_scraper.py finishes.

Flow:
  1. Posts a summary message to the channel
  2. Posts each ad as an individual channel post (with media if available)
  3. Each post automatically has a "Comments" button (via linked discussion group)
  4. file_ids are saved so media can be re-sent instantly later

Required env vars
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID       Channel ID (negative number, e.g. -100...)
                           The bot must be admin in the channel.

Optional env vars
    AD_QUERY               shown in the summary post
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


def send_text(text, reply_to=None):
    """Post a text message to the channel, optionally as a reply."""
    payload = {
        "chat_id": CHAT_ID,
        "text": text[:MESSAGE_LIMIT],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return tg("sendMessage", json=payload)


def send_photo(photo_path, caption, reply_to=None):
    """Upload a photo to the channel. Returns the Message object."""
    data = {
        "chat_id": CHAT_ID,
        "caption": caption[:CAPTION_LIMIT],
        "parse_mode": "HTML",
    }
    if reply_to:
        data["reply_to_message_id"] = reply_to
    with open(photo_path, "rb") as f:
        return tg("sendPhoto", data=data, files={"photo": f})


def send_video(video_path, caption, reply_to=None):
    """Upload a video to the channel. Returns the Message object."""
    data = {
        "chat_id": CHAT_ID,
        "caption": caption[:CAPTION_LIMIT],
        "parse_mode": "HTML",
    }
    if reply_to:
        data["reply_to_message_id"] = reply_to
    with open(video_path, "rb") as f:
        return tg("sendVideo", data=data, files={"video": f})


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

    # ---- Summary post (the "broadcast") ----
    active = sum(1 for a in ads if a.get("status") == "active")
    summary = (
        f"<b>🔍 New Scrape — {now}</b>\n\n"
        f"🔎 Query: <code>{_esc(query or 'N/A')}</code>\n"
        f"📊 Total: <b>{len(ads)}</b>  •  ✅ Active: <b>{active}</b>  •  ⏸ Inactive: <b>{len(ads) - active}</b>\n\n"
        f"👇 All ads are posted below as replies to this message."
    )
    header_msg = send_text(summary)
    header_id = header_msg["message_id"]
    print(f"✅ Posted header (message_id={header_id})")
    time.sleep(1)

    # ---- Post each ad as a reply to the header ----
    sent = 0
    failed = 0
    for i, ad in enumerate(ads, 1):
        lib_id = ad.get("library_id", "?")
        media_path, media_type = find_local_media(ad)

        try:
            if media_path:
                caption = format_caption(ad)
                if media_type == "video":
                    msg = send_video(media_path, caption, reply_to=header_id)
                else:
                    msg = send_photo(media_path, caption, reply_to=header_id)
                fid = extract_file_id(msg, media_type)
                if fid:
                    file_ids[lib_id] = fid
            else:
                send_text(format_text_message(ad), reply_to=header_id)

            sent += 1
            print(f"  [{i}/{len(ads)}] ✓ {lib_id}")
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(ads)}] ✗ {lib_id}: {e}")

        time.sleep(3.0 if media_path else 2.0)

    # ---- Save file_ids ----
    save_file_ids(file_ids)

    # ---- Final reply under the header ----
    send_text(f"<b>✅ Done — {sent} ads posted, {failed} failed.</b>", reply_to=header_id)

    print(f"\n{'='*50}")
    print(f"Done. {sent} ads sent to channel, {failed} failed.")
    print(f"file_ids saved to {FILE_IDS_PATH}")


if __name__ == "__main__":
    main()
