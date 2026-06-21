import json
import os
import re
import sys
import time
from datetime import datetime

import google.generativeai as genai
import requests
from bs4 import BeautifulSoup

URL = "https://corporate.ethiopianairlines.com/AboutEthiopian/careers/vacancies"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_postings.json")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = int(os.environ["TELEGRAM_CHAT_ID"])

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
gemini = genai.GenerativeModel("gemini-2.0-flash")

TELEGRAM_MAX_LENGTH = 4096
DETAIL_LENGTH = 600

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
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


def format_with_ai(posting):
    raw = (
        f"Position: {posting['position']}\n"
        f"Category: {posting['category']}\n"
        f"Closing Date: {posting['closing_date']}\n"
        f"Details: {posting.get('raw_details', 'Not available')}"
    )

    prompt = (
        "Format this Ethiopian Airlines job vacancy as a clean, professional Telegram announcement.\n\n"
        "RULES:\n"
        "- Use ONLY these Telegram HTML tags: <b>, <i>, <a href='...'>\n"
        "- No markdown, no asterisks, no backticks, no bullet dashes\n"
        "- Structure: opening emoji + bold title, then sections for Role, Requirements, Deadline\n"
        "- Professional but readable tone\n"
        f"- End with: <a href=\"{URL}\">Apply on Ethiopian Airlines</a>\n"
        "- Return ONLY the message text, nothing else\n\n"
        f"Data:\n{raw}"
    )

    resp = gemini.generate_content(prompt)
    return resp.text.strip()


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
