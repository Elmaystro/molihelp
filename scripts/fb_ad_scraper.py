#!/usr/bin/env python3
"""
Collect ALL ads of a Facebook Ad Library search (metadata + media), with live progress.

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python fb_ad_scraper.py --query "58 wilaya gros" --country DZ
    python fb_ad_scraper.py --page-id 123456789012345 --status all
    python fb_ad_scraper.py --query gros --max-ads 100        # stop after 100 ads
    python fb_ad_scraper.py --query gros --headed             # watch the browser

Output (inside --out, default ./ads_out):
    all_ads.csv / all_ads.json     one row per ad (id, advertiser, dates, days running, text, ...)
    <library_id>/metadata.json     full details of one ad
    <library_id>/media_1.jpg ...   downloaded creatives

Three phases, each with a progress display:
    1. Scrolling   - loads the results page until no new ads appear (or --max-ads is reached)
    2. Media pass  - re-visits ads whose images/videos had not loaded yet
    3. Saving      - parses each ad, downloads its media, writes the files

Ctrl+C at any point stops gracefully and saves what was collected so far.

Notes:
  * Reads only what the public page shows. No login, no bypassing of any block or captcha.
  * Facebook's HTML is obfuscated, so the script relies on visible text ("Library ID",
    "Started running on", ...). The browser is forced to English for that reason.
  * Automated collection may conflict with Meta's Terms. For heavy or commercial use, prefer
    the official Ad Library API (ad_delivery_start_time / ad_delivery_stop_time).
"""
import argparse
import csv
import json
import mimetypes
import re
import shutil
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

BASE = "https://www.facebook.com/ads/library/"

# --------------------------------------------------------------------------
# Terminal progress helpers
# --------------------------------------------------------------------------
def _width():
    return max(40, shutil.get_terminal_size((110, 20)).columns - 1)


def live(msg):
    """Rewrite the current terminal line (in-place progress)."""
    sys.stdout.write("\r" + msg[: _width()].ljust(_width()))
    sys.stdout.flush()


def endlive():
    sys.stdout.write("\n")
    sys.stdout.flush()


def bar(done, total, width=20):
    filled = int(width * done / total) if total else 0
    return "[" + "#" * filled + "-" * (width - filled) + "]"


# --------------------------------------------------------------------------
# Pure parsing helpers (no browser needed)
# --------------------------------------------------------------------------
DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y")
DATE_PAT = r"(?:[A-Za-z]{3,9}\.? \d{1,2}, \d{4}|\d{1,2} [A-Za-z]{3,9}\.? \d{4})"

CTAS = {
    "shop now", "learn more", "sign up", "download", "install now", "book now", "get offer",
    "contact us", "apply now", "watch more", "subscribe", "order now", "get quote",
    "send message", "call now", "listen now", "see menu", "play game", "use app", "get started",
    "buy now", "donate now", "get directions", "request time", "see more",
}


def parse_date(s):
    s = re.sub(r"\bSept\b", "Sep", s.strip().replace(".", ""))
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_status(text):
    m = re.search(r"^\s*(Active|Inactive)\s*$", text, re.M | re.I)
    return m.group(1).lower() if m else None


def parse_running(text, today=None):
    """Work out start/end date and how many days the ad has been running."""
    today = today or date.today()
    status = parse_status(text)
    out = {
        "status": status, "start_date": None, "end_date": None,
        "days_running": None, "total_active_time": None, "days_source": None,
    }
    rng = re.search(rf"({DATE_PAT})\s*[-\u2013\u2014]\s*({DATE_PAT})", text)
    started = re.search(rf"Started running on\s+({DATE_PAT})", text)

    start = end = None
    if rng:                                   # inactive: "Jan 1, 2026 - Jan 15, 2026"
        start, end = parse_date(rng.group(1)), parse_date(rng.group(2))
        out["days_source"] = "start-end range"
    elif started:                             # active: "Started running on Nov 3, 2026"
        start = parse_date(started.group(1))
        if status != "inactive":
            end = today
            out["days_source"] = "start date -> today"

    if start:
        out["start_date"] = start.isoformat()
    if end:
        out["end_date"] = end.isoformat()
    if start and end:
        out["days_running"] = (end - start).days

    tat = re.search(r"Total active time\s*[:\n]?\s*(\d+)\s*(days?|hrs?|hours?)", text, re.I)
    if tat:
        out["total_active_time"] = f"{tat.group(1)} {tat.group(2)}"
        if out["days_running"] is None and tat.group(2).lower().startswith("day"):
            out["days_running"] = int(tat.group(1))
            out["days_source"] = "Total active time"
    return out


def parse_card_text(text):
    """Best-effort split of the card's visible text. raw_text is always kept."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    res = {"advertiser": None, "body_text": None, "cta": None, "domain": None,
           "headline": None, "same_creative_count": None, "multiple_versions": False}

    m = re.search(r"(\d+)\s+ads?\s+use\s+this\s+creative", text, re.I)
    if m:
        res["same_creative_count"] = int(m.group(1))
    res["multiple_versions"] = bool(re.search(r"multiple versions", text, re.I))

    domain_re = r"[A-Z0-9][A-Z0-9.\-]*\.[A-Z]{2,}(/\S*)?"
    if "Sponsored" in lines:
        i = lines.index("Sponsored")
        if i > 0:
            res["advertiser"] = lines[i - 1]
        body, rest, stop = [], [], False
        for l in lines[i + 1:]:
            if not stop and (l.lower() in CTAS or re.fullmatch(domain_re, l)):
                stop = True
            (rest if stop else body).append(l)
        res["body_text"] = "\n".join(body) or None
        for l in rest:
            if l.lower() in CTAS:
                res["cta"] = l
            elif re.fullmatch(domain_re, l) and not res["domain"]:
                res["domain"] = l
            elif not res["headline"]:
                res["headline"] = l
    return res


def unwrap_link(href):
    """l.facebook.com/l.php?u=<real url> -> real url."""
    try:
        p = urlparse(href)
        if p.netloc.endswith("l.facebook.com"):
            u = parse_qs(p.query).get("u")
            if u:
                return unquote(u[0])
    except Exception:
        pass
    return href


def guess_ext(url, content_type, default):
    ct = (content_type or "").split(";")[0].strip().lower()
    fixed = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "video/mp4": ".mp4"}
    if ct in fixed:
        return fixed[ct]
    if ct:
        e = mimetypes.guess_extension(ct)
        if e:
            return e
    m = re.search(r"\.([a-z0-9]{3,4})$", urlparse(url).path, re.I)
    return f".{m.group(1).lower()}" if m else default


def collect_media(data):
    media, seen, avatar = [], set(), None
    for v in data["videos"]:
        if v["src"].startswith("http") and v["src"] not in seen:
            seen.add(v["src"])
            media.append({"type": "video", "url": v["src"]})
        elif v["poster"].startswith("http") and v["poster"] not in seen:
            seen.add(v["poster"])
            media.append({"type": "image", "url": v["poster"],
                          "note": "video thumbnail (video URL was a blob)"})
    for i in data["imgs"]:
        src = i["src"]
        if not src.startswith("http") or src in seen or "emoji" in src or "static.xx.fbcdn" in src:
            continue
        if i["w"] < 120 or i["h"] < 120:
            avatar = avatar or src            # small round image = advertiser avatar
            continue
        seen.add(src)
        media.append({"type": "image", "url": src, "alt": i["alt"]})
    return media, avatar


# --------------------------------------------------------------------------
# In-page JavaScript
# --------------------------------------------------------------------------
# opts = {done: [ids to skip], only: "id" | null}
# Returns {ids: [every Library ID in the DOM, in order], cards: [data for ads not in `done`]}.
# Each ad's card is the largest ancestor of its "Library ID" text that contains no other ad's ID;
# it is tagged data-adl-id so Python can scroll to it / screenshot it later.
COLLECT_JS = r"""
(opts) => {
  const RE = /Library ID:?\s*(\d{8,20})/i;
  const done = new Set(opts.done || []);
  const leaves = [...document.querySelectorAll('div,span')]
    .filter(e => !e.children.length && RE.test(e.textContent));
  const seen = new Set(), items = [];
  for (const e of leaves) {
    const id = e.textContent.match(RE)[1];
    if (seen.has(id)) continue;
    seen.add(id); items.push({id, el: e});
  }
  const cards = [];
  items.forEach((it, i) => {
    if (opts.only ? it.id !== opts.only : done.has(it.id)) return;
    const others = [items[i - 1], items[i + 1]].filter(Boolean).map(x => x.el);
    let card = it.el;
    while (card.parentElement && card.parentElement !== document.body &&
           !others.some(o => card.parentElement.contains(o)))
      card = card.parentElement;
    card.setAttribute('data-adl-id', it.id);
    const imgs = [...card.querySelectorAll('img')].map(im => {
      const r = im.getBoundingClientRect();
      return {src: im.currentSrc || im.src, w: r.width || im.naturalWidth,
              h: r.height || im.naturalHeight, alt: im.alt || ''};
    });
    const videos = [...card.querySelectorAll('video')].map(v => ({
      src: v.currentSrc || v.src || ((v.querySelector('source') || {}).src || ''),
      poster: v.poster || ''}));
    const links = [...card.querySelectorAll('a[href]')]
      .map(a => ({href: a.href, text: (a.innerText || '').trim()}));
    const big = imgs.filter(m => m.w >= 120 && m.h >= 120 && m.src.startsWith('http') &&
                                 !/emoji|static\.xx\.fbcdn/.test(m.src));
    const ready = (big.length > 0 || videos.some(v => v.src.startsWith('http'))) &&
                  videos.every(v => v.src.startsWith('http') || v.poster);
    cards.push({id: it.id, text: card.innerText, imgs, videos, links, ready});
  });
  return {ids: items.map(x => x.id), cards};
}
"""

TOTAL_JS = r"""
() => { const m = (document.body.innerText || '').match(/~?\s*([\d.,]+)\s+results?/i);
        return m ? m[1] : null; }
"""


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------
def build_url(args):
    params = {"active_status": args.status, "ad_type": "all",
              "country": args.country, "media_type": "all"}
    if args.page_id:
        params.update({"view_all_page_id": args.page_id, "search_type": "page"})
    else:
        params.update({"q": args.query, "search_type": "keyword_unordered"})
    return BASE + "?" + urlencode(params)


def scroll_and_collect(page, args, total_hint=None):
    """Phase 1: scroll until no new ads appear. Returns (ordered ids, store, done)."""
    store, done, known, ids = {}, set(), set(), []
    idle, rnd = 0, 0
    try:
        while True:
            rnd += 1
            res = page.evaluate(COLLECT_JS, {"done": sorted(done), "only": None})
            for c in res["cards"]:
                store[c["id"]] = c
                if c["ready"]:
                    done.add(c["id"])
            ids = res["ids"]
            new = [i for i in ids if i not in known]
            known.update(ids)
            idle = 0 if new else idle + 1

            total = f" / ~{total_hint}" if total_hint else ""
            live(f"Scrolling  round {rnd:<3} ads found: {len(known)}{total}  "
                 f"(+{len(new)} new)  idle {idle}/{args.patience}")

            if args.max_ads and len(known) >= args.max_ads:
                break
            if idle >= args.patience:
                break
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.keyboard.press("End")
            page.wait_for_timeout(int(args.delay * 1000))
    except KeyboardInterrupt:
        endlive()
        print("Interrupted - continuing with the ads collected so far.")
        return ids, store, done
    endlive()
    if args.max_ads:
        ids = ids[: args.max_ads]
    return ids, store, done


def fill_missing_media(page, ids, store, done):
    """Phase 2: scroll to ads whose media had not loaded and read them again."""
    missing = [i for i in ids if i not in done]
    if not missing:
        print("Media pass: every ad already has its media loaded.")
        return
    try:
        for n, aid in enumerate(missing, 1):
            live(f"Media pass {bar(n, len(missing))} {n}/{len(missing)}  ad {aid}")
            try:
                page.locator(f'[data-adl-id="{aid}"]').first.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(600)
                res = page.evaluate(COLLECT_JS, {"done": [], "only": aid})
                if res["cards"]:
                    store[aid] = res["cards"][0]
                    if res["cards"][0]["ready"]:
                        done.add(aid)
            except Exception:
                pass                           # card gone from DOM: keep what we have
    except KeyboardInterrupt:
        pass
    endlive()
    still = len([i for i in missing if i not in done])
    print(f"Media pass done: {len(missing) - still} recovered, {still} without media (text-only ads or not loaded).")


def build_meta(data, search_url):
    running = parse_running(data["text"])
    fields = parse_card_text(data["text"])
    media, avatar = collect_media(data)

    landing, page_id, page_url = [], None, None
    for l in data["links"]:
        href = unwrap_link(l["href"])
        if "l.facebook.com" in l["href"] and href not in landing:
            landing.append(href)
        m = re.search(r"view_all_page_id=(\d+)", href)
        if m and not page_id:
            page_id = m.group(1)
        if not page_url and re.match(r"https://(www\.)?facebook\.com/[^/?]+/?$", href) and "ads" not in href:
            page_url = href

    return {
        "library_id": data["id"],
        "ad_library_url": f"https://www.facebook.com/ads/library/?id={data['id']}",
        "search_url": search_url,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        **running, **fields,
        "advertiser_page_id": page_id,
        "advertiser_page_url": page_url,
        "advertiser_avatar": avatar,
        "landing_urls": landing,
        "media": media,
        "raw_text": data["text"],
    }


def _short(e):
    return (str(e).strip().splitlines() or ["unknown error"])[0][:120]


def _fetch_api(ctx, url):
    r = ctx.request.get(url, headers={"Referer": "https://www.facebook.com/"}, timeout=60000)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status}")
    return r.body(), r.headers.get("content-type")


def _fetch_browser(dl_page, url):
    """Fallback for images: let Chromium itself load the URL (uses the browser's proxy/certs)."""
    r = dl_page.goto(url, wait_until="load", timeout=60000)
    if r is None or not r.ok:
        raise RuntimeError(f"HTTP {r.status if r else 'no response'}")
    return r.body(), r.headers.get("content-type")


def download_media(ctx, meta, ad_dir, dl_page=None):
    for n, m in enumerate(meta["media"], 1):
        body = ctype = err = None
        try:
            body, ctype = _fetch_api(ctx, m["url"])
        except Exception as e:
            err = _short(e)
            if m["type"] == "image" and dl_page is not None:
                try:
                    body, ctype = _fetch_browser(dl_page, m["url"])
                    err = None
                except Exception as e2:
                    err = f"{err} | browser: {_short(e2)[:60]}"
        if body is None:
            m["error"] = err
            continue
        name = f"media_{n}" + guess_ext(m["url"], ctype, ".mp4" if m["type"] == "video" else ".jpg")
        (ad_dir / name).write_bytes(body)
        m["file"] = name
        m.pop("error", None)


def write_aggregates(out_root, metas):
    if not metas:
        return
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "all_ads.json").write_text(
        json.dumps(metas, indent=2, ensure_ascii=False), encoding="utf-8")
    cols = ["library_id", "status", "start_date", "end_date", "days_running", "advertiser",
            "body_text", "headline", "cta", "domain", "landing_url", "same_creative_count",
            "media_count", "media_files", "ad_library_url"]
    with open(out_root / "all_ads.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for m in metas:
            w.writerow({
                "library_id": m["library_id"], "status": m.get("status"),
                "start_date": m.get("start_date"), "end_date": m.get("end_date"),
                "days_running": m.get("days_running"), "advertiser": m.get("advertiser"),
                "body_text": " ".join((m.get("body_text") or "").split()),
                "headline": m.get("headline"), "cta": m.get("cta"), "domain": m.get("domain"),
                "landing_url": (m.get("landing_urls") or [None])[0],
                "same_creative_count": m.get("same_creative_count"),
                "media_count": len(m.get("media", [])),
                "media_files": ";".join(x["file"] for x in m.get("media", []) if "file" in x),
                "ad_library_url": m["ad_library_url"],
            })


def is_complete(meta, args):
    """An ad counts as done only if every media item was downloaded (or media is off)."""
    return args.no_media or all("file" in m for m in meta.get("media", []))


def save_all(page, ctx, ids, store, args, search_url, out_root):
    """Phase 3: parse, download and write every ad."""
    metas, err_counts = [], Counter()
    total = len(ids)
    dl_page = None if args.no_media else ctx.new_page()
    try:
        for n, aid in enumerate(ids, 1):
            ad_dir = out_root / aid
            meta_file = ad_dir / "metadata.json"
            head = f"{bar(n, total)} {n}/{total}  {aid}"

            old = None
            if meta_file.exists() and not args.refresh:
                old = json.loads(meta_file.read_text(encoding="utf-8"))
                if is_complete(old, args):
                    metas.append(old)
                    live(f"{head}  skipped (already saved)"); endlive()
                    continue
            if aid not in store:
                if old:
                    metas.append(old)
                continue

            live(f"{head}  parsing…")
            meta = build_meta(store[aid], search_url)
            ad_dir.mkdir(parents=True, exist_ok=True)

            if not args.no_media and meta["media"]:
                live(f"{head}  downloading {len(meta['media'])} media…")
                download_media(ctx, meta, ad_dir, dl_page)

            if args.screenshots:
                try:
                    loc = page.locator(f'[data-adl-id="{aid}"]').first
                    loc.scroll_into_view_if_needed(timeout=3000)
                    loc.screenshot(path=str(ad_dir / "card.png"))
                except Exception:
                    pass

            meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            metas.append(meta)

            failed = [] if args.no_media else [m for m in meta["media"] if "file" not in m]
            for m in failed:
                err_counts[(m.get("error") or "unknown")[:80]] += 1
            ok = len(meta["media"]) - len(failed)
            tail = f"  ! {failed[0].get('error', '?')[:40]}" if failed else ""
            days = f"{meta['days_running']}d" if meta["days_running"] is not None else "?d"
            live(f"{head}  {(meta['advertiser'] or '?')[:22]:<22} {meta['status'] or '?':<8} "
                 f"{days:>5}  media {ok}/{len(meta['media'])}{tail}")
            endlive()
    except KeyboardInterrupt:
        endlive()
        print("Interrupted - saving what was processed.")
    finally:
        write_aggregates(out_root, metas)
        if dl_page is not None:
            try:
                dl_page.close()
            except Exception:
                pass
    return metas, err_counts


def run(args):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Install Playwright first:  pip install playwright && playwright install chromium")

    t0 = time.time()
    url = build_url(args)
    out_root = Path(args.out)
    print(f"Opening {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(locale="en-US", viewport={"width": 1400, "height": 1000},
                                 ignore_https_errors=True)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")

        try:
            page.get_by_role(
                "button", name=re.compile(r"decline optional cookies|only allow essential|allow all cookies", re.I)
            ).first.click(timeout=4000)
        except Exception:
            pass

        # NOTE: page.wait_for_function() is blocked by Facebook's Content-Security-Policy
        # (it needs eval), so we poll with page.evaluate(), which is allowed.
        live("Waiting for ads to appear…")
        deadline = time.time() + args.timeout
        found = False
        while time.time() < deadline:
            try:
                if page.evaluate("() => document.body.innerText.includes('Library ID')"):
                    found = True
                    break
            except Exception:
                pass                           # page still navigating
            page.wait_for_timeout(1000)
        endlive()
        if not found:
            body = page.inner_text("body")[:300].replace("\n", " ")
            sys.exit("No ads found. The search may have no results, or Facebook is showing a "
                     f"login/captcha wall (try --headed). Page text starts with: {body!r}")
        page.wait_for_timeout(2500)
        total_hint = page.evaluate(TOTAL_JS)

        ids, store, done = scroll_and_collect(page, args, total_hint)
        print(f"Collected {len(ids)} ads.\n")
        fill_missing_media(page, ids, store, done)
        print()
        metas, err_counts = save_all(page, ctx, ids, store, args, url, out_root)
        browser.close()

    # ---- summary ----
    active = sum(1 for m in metas if m.get("status") == "active")
    n_media = sum(1 for m in metas for x in m.get("media", []) if "file" in x)
    print("\n" + "=" * 60)
    print(f"Done in {time.time() - t0:.0f}s  |  {len(metas)} ads saved to {out_root.resolve()}")
    print(f"  Active: {active}   Other: {len(metas) - active}   Media files: {n_media}   Failed downloads: {sum(err_counts.values())}")
    for reason, cnt in err_counts.most_common(3):
        print(f"    {cnt:>5} x {reason}")
    ranked = sorted((m for m in metas if m.get("days_running") is not None),
                    key=lambda m: m["days_running"], reverse=True)[:5]
    if ranked:
        print("  Longest running:")
        for m in ranked:
            print(f"    {m['days_running']:>4} days  {m['library_id']}  {m.get('advertiser') or '?'}")
    print(f"  Table: {out_root / 'all_ads.csv'}")


def main():
    try:
        sys.stdout.reconfigure(errors="replace")   # Arabic/French names on Windows consoles
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Collect all ads from a Facebook Ad Library search.")
    ap.add_argument("--query", default="", help="keyword(s); several words = AND")
    ap.add_argument("--page-id", help="advertiser Facebook page ID (instead of --query)")
    ap.add_argument("--country", default="ALL", help="country code, e.g. DZ, FR, US, ALL")
    ap.add_argument("--status", default="active", choices=["active", "inactive", "all"])
    ap.add_argument("--out", default="ads_out", help="output folder")
    ap.add_argument("--max-ads", type=int, default=0, help="stop after N ads (0 = all)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds to wait after each scroll")
    ap.add_argument("--patience", type=int, default=4, help="stop after N scrolls with no new ads")
    ap.add_argument("--timeout", type=int, default=45, help="seconds to wait for the first ads")
    ap.add_argument("--no-media", action="store_true", help="metadata only, skip downloads")
    ap.add_argument("--screenshots", action="store_true", help="save card.png for each ad")
    ap.add_argument("--refresh", action="store_true", help="re-process ads already saved")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    args = ap.parse_args()
    if not args.query and not args.page_id:
        ap.error("give --query or --page-id")
    run(args)


if __name__ == "__main__":
    main()