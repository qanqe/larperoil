import json
import os
import re
import sys
import time
from datetime import datetime

from groq import Groq
import requests
from bs4 import BeautifulSoup

URL = "https://corporate.ethiopianairlines.com/AboutEthiopian/careers/vacancies"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_postings.json")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = int(os.environ["TELEGRAM_CHAT_ID"])
groq_client        = Groq(api_key=os.environ["GROQ_API_KEY"])

TELEGRAM_MAX_LENGTH = 4096
DETAIL_LENGTH       = 600

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def fetch_postings():
    resp = requests.get(URL, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    postings = {}
    links = soup.find_all("a", href=re.compile(r"#collapse(One|two)_\d+"))

    for link in links:
        text = link.get_text(" ", strip=True)
        if "Position" not in text:
            continue

        pos_match   = re.search(r"Position\s*:\s*(.+?)\s*(?:Location\s*:|Closing Date\s*:|Registration Date\s*:|$)", text)
        close_match = re.search(r"Closing Date\s*:\s*(.+)$", text)
        if not pos_match:
            continue

        position = pos_match.group(1).strip()
        closing  = close_match.group(1).strip() if close_match else "Unknown"
        key      = position.lower()
        href     = link.get("href", "")
        category = "International" if "collapseOne" in href else "Local"

        detail_div  = soup.find(id=href.lstrip("#"))
        raw_details = extract_details(detail_div) if detail_div else ""

        postings[key] = {
            "position":     position,
            "closing_date": closing,
            "category":     category,
            "raw_details":  raw_details,
        }

    return postings


def extract_details(detail_div):
    raw = detail_div.get_text(" ", strip=True)
    raw = re.sub(r"\s+", " ", raw).strip()
    if len(raw) <= DETAIL_LENGTH:
        return raw
    return raw[:DETAIL_LENGTH].rsplit(" ", 1)[0] + "..."


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_with_ai(posting):
    raw        = posting.get("raw_details", "")
    cat_emoji  = "🌍" if posting["category"] == "International" else "🇪🇹"

    try:
        prompt = (
            "In exactly 2 sentences, summarize what this job requires. "
            "Plain text only, no formatting, no bullet points.\n\n"
            f"{raw}"
        )
        resp = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
        )
        summary = resp.choices[0].message.content.strip()
    except Exception:
        summary = raw[:200] if raw else "See full listing for details."

    return (
        f"✈️ <b>Ethiopian Airlines — Job Vacancy</b>\n"
        f"\n"
        f"<b>{escape_html(posting['position'])}</b>\n"
        f"\n"
        f"{cat_emoji} <i>{escape_html(posting['category'])}</i>\n"
        f"📅 <b>Deadline:</b> {escape_html(posting['closing_date'])}\n"
        f"\n"
        f"📋 <b>About the Role:</b>\n"
        f"{escape_html(summary)}\n"
        f"\n"
        f'<a href="{URL}">View full listing and apply →</a>'
    )


def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(postings):
    slim = {
        k: {
            "position":     v["position"],
            "closing_date": v["closing_date"],
            "category":     v["category"],
        }
        for k, v in postings.items()
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False, indent=2)


def send_telegram(text, parse_mode="HTML"):
    if len(text) > TELEGRAM_MAX_LENGTH:
        text = text[:TELEGRAM_MAX_LENGTH]
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               parse_mode,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    resp.raise_for_status()


def main():
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Starting vacancy check...")

    try:
        current = fetch_postings()
    except Exception as e:
        print(f"Fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not current:
        print("Parsed 0 postings. Page layout may have changed.")
        sys.exit(1)

    print(f"Found {len(current)} posting(s).")

    first_run = not os.path.exists(STATE_FILE)
    seen      = load_seen()
    new_keys  = list(current.keys()) if first_run else [k for k in current if k not in seen]

    print(f"{'First run' if first_run else 'New'}: {len(new_keys)} to send.")

    if new_keys:
        intl  = sum(1 for k in new_keys if current[k]["category"] == "International")
        local = len(new_keys) - intl

        send_telegram(
            f"✈️ <b>Ethiopian Airlines Vacancy Alert</b>\n"
            f"📅 {datetime.now():%Y-%m-%d}\n"
            f"{'First run — all current vacancies' if first_run else 'New postings this week'}\n"
            f"🌍 International: {intl}  🇪🇹 Local: {local}"
        )

        for i, k in enumerate(new_keys, 1):
            p = current[k]
            print(f"  [{i}/{len(new_keys)}] Formatting: {p['position']}")
            try:
                msg = format_with_ai(p)
                send_telegram(msg)
                print(f"  [{i}/{len(new_keys)}] Sent.")
            except Exception as e:
                print(f"  Failed ({p['position']}): {e}", file=sys.stderr)
            if i < len(new_keys):
                time.sleep(1.5)
    else:
        send_telegram(
            f"✈️ <b>Ethiopian Airlines Vacancy Check</b>\n"
            f"📅 {datetime.now():%Y-%m-%d}\n"
            f"✅ No new postings this week.\n"
            f'<a href="{URL}">View current vacancies</a>'
        )
        print("Nothing new.")

    save_seen(current)
    print("State saved. Done.")


if __name__ == "__main__":
    main()
