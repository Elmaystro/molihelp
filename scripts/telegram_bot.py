#!/usr/bin/env python3
"""
Lightweight Telegram bot — listens for /new and triggers a GitHub Actions run.

The bot itself does NOT scrape or post ads. It only:
  1. Accepts /new <query>  → triggers the GitHub Actions workflow
  2. Replies with a confirmation
  3. The workflow (ad-scraper.yml) handles scraping + Telegram posting via notify_telegram.py

Run locally:
    # Set env vars (or use a .env file with python-dotenv)
    export TELEGRAM_BOT_TOKEN="123:ABC..."
    export GITHUB_TOKEN="ghp_..."
    export GITHUB_REPO="owner/repo"
    python scripts/telegram_bot.py

Required env vars
    TELEGRAM_BOT_TOKEN     from @BotFather
    GITHUB_TOKEN           PAT with  repo + actions:write  scope
    GITHUB_REPO            owner/repo  (e.g. yasser/molihelp)

Optional env vars
    WORKFLOW_FILE          workflow filename  (default: ad-scraper.yml)
    DEFAULT_QUERY          fallback when /new is sent without a query
    DEFAULT_COUNTRY        fallback country code  (default: ALL)
"""

import json
import logging
import os
import re
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Load .env file if present
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
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "ad-scraper.yml")
DEFAULT_QUERY = os.environ.get("DEFAULT_QUERY", "58 wilaya gros")
DEFAULT_COUNTRY = os.environ.get("DEFAULT_COUNTRY", "DZ")
DEFAULT_STATUS = os.environ.get("DEFAULT_STATUS", "active")
DEFAULT_MAX_ADS = os.environ.get("DEFAULT_MAX_ADS", "10")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
def tg(method, **kwargs):
    r = requests.post(f"{API}/{method}", timeout=90, **kwargs)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"TG {method}: {data.get('description', data)}")
    return data["result"]


def reply(chat_id, text, reply_to=None):
    payload = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return tg("sendMessage", json=payload)


# ---------------------------------------------------------------------------
# GitHub Actions trigger
# ---------------------------------------------------------------------------
def trigger_workflow(query, country="DZ", status="active", max_ads="10"):
    """Fire a workflow_dispatch event. Returns True on success."""
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO}"
        f"/actions/workflows/{WORKFLOW_FILE}/dispatches"
    )
    r = requests.post(
        url,
        json={
            "ref": "main",
            "inputs": {
                "query": str(query),
                "country": str(country),
                "status": str(status),
                "max_ads": str(max_ads),
            },
        },
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    if r.status_code == 204:
        return True
    raise RuntimeError(f"GitHub API {r.status_code}: {r.text[:300]}")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def handle_new(chat_id, text, msg_id):
    """Parse /new arguments, trigger workflow, reply."""
    raw = text.replace("/new", "", 1).strip()

    # Support /new query=shoes country=DZ status=active max_ads=10
    kv = dict(re.findall(r"(\w+)\s*=\s*(\S+)", raw))
    if kv:
        query = kv.get("query", DEFAULT_QUERY)
        country = kv.get("country", DEFAULT_COUNTRY)
        status = kv.get("status", DEFAULT_STATUS)
        max_ads = kv.get("max_ads", DEFAULT_MAX_ADS)
    else:
        # Simple form: /new 58 wilaya gros
        query = raw or DEFAULT_QUERY
        country = DEFAULT_COUNTRY
        status = DEFAULT_STATUS
        max_ads = DEFAULT_MAX_ADS

    if not query:
        reply(chat_id,
              "Usage:\n"
              "<code>/new your search query</code>\n"
              "<code>/new query=shoes country=DZ max_ads=10</code>",
              msg_id)
        return

    reply(chat_id,
          f"⏳ <b>Launching scrape…</b>\n\n"
          f"🔎 Query: <code>{query}</code>\n"
          f"🌍 Country: <code>{country}</code>\n"
          f"📊 Status: <code>{status}</code>\n"
          f"📦 Max ads: <code>{max_ads}</code>\n\n"
          f"Triggering GitHub Actions…",
          msg_id)

    try:
        trigger_workflow(query, country, status, max_ads)
        reply(chat_id,
              "✅ <b>Workflow started!</b>\n\n"
              "Scraping usually takes a few minutes.\n"
              "A new thread will appear in this chat when it's done.",
              msg_id)
        log.info("Workflow triggered: query=%r country=%s status=%s max_ads=%s", query, country, status, max_ads)
    except Exception as e:
        reply(chat_id, f"❌ Failed to trigger workflow:\n<code>{e}</code>", msg_id)
        log.error("Workflow trigger failed: %s", e)


def handle_help(chat_id, msg_id):
    reply(chat_id,
          "<b>🤖 MoliHelp Ad Scraper</b>\n\n"
          "<b>Commands:</b>\n"
          "/new <code>query</code> — Scrape ads in Algeria (DZ)\n"
          "/new <code>query=shoes country=DZ max_ads=10</code> — Custom options\n"
          "/help — This message\n\n"
          "<b>Defaults:</b>\n"
          f"• Country: <code>{DEFAULT_COUNTRY}</code>\n"
          f"• Status: <code>{DEFAULT_STATUS}</code>\n"
          f"• Max ads: <code>{DEFAULT_MAX_ADS}</code>\n\n"
          "<b>How it works:</b>\n"
          "1. <code>/new</code> triggers GitHub Actions\n"
          "2. The scraper collects ads from Meta Ad Library\n"
          "3. A new thread appears here with every ad + media\n"
          "4. Media is stored in Telegram — never expires",
          msg_id)


# ---------------------------------------------------------------------------
# Long-polling loop
# ---------------------------------------------------------------------------
def poll():
    offset = 0
    log.info("Bot started — listening for commands…")
    log.info("Repo: %s  Workflow: %s", GITHUB_REPO, WORKFLOW_FILE)

    while True:
        try:
            updates = tg("getUpdates", json={
                "offset": offset,
                "timeout": 30,
                "allowed_updates": ["message"],
            })

            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message")
                if not msg or not msg.get("text"):
                    continue

                chat_id = msg["chat"]["id"]
                msg_id = msg["message_id"]
                text = msg["text"].strip()

                # Strip @botname suffix from commands in groups
                cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

                if cmd == "/new":
                    handle_new(chat_id, text, msg_id)
                elif cmd in ("/help", "/start"):
                    handle_help(chat_id, msg_id)

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as e:
            log.error("Poll error: %s", e)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    missing = []
    if not BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GITHUB_TOKEN:
        missing.append("GITHUB_TOKEN")
    if not GITHUB_REPO:
        missing.append("GITHUB_REPO")
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}\nSee .env.example")
    poll()


if __name__ == "__main__":
    main()
