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

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ---------------------------------------------------------------------------
# Section-based extraction
#
# The vacancy page mixes at least 3 different posting layouts (pilot/table
# postings, paragraph-style "VACANCY ANNOUNCEMENT" postings, and Ethiopian
# Skylight Hotel table postings), and each one can include any subset of
# ~8 section headers (AGE LIMIT, LANGUAGE, EMPLOYMENT TYPE, DUTIES &
# RESPONSIBILITIES, etc.) in varying order. Rather than chasing each format
# with its own one-off regex, we locate ALL known headers in the flattened
# text, sort them by position, and slice the content between consecutive
# headers. That way each field stops cleanly at whichever header actually
# comes next, instead of running on into unrelated sections.
#
# Order matters only where one pattern is a prefix of another at the same
# position (e.g. "REGISTRATION DATE & PLACE" vs "REGISTRATION DATE") --
# the more specific/longer pattern must come first so it wins.
# ---------------------------------------------------------------------------
_SECTION_DEFS = [
    ("registration_date_place", r"REGISTRATION\s+DATE\s*(?:&|AND)\s*PLACE\s*:?"),
    ("registration_place",      r"REGISTRATION\s+PLACES?(?:/LOCATIONS)?\s*:?"),
    ("registration_date",       r"REGISTRATION\s+DATE\s*:?"),
    ("employment_type",         r"EMPLOYMENT\s+(?:TYPE|MODALITY)\s*:?"),
    ("qualifications",          r"QUALIF\w*[\w\s&/]*?REQUIREMENT[S]?\s*:?"),
    ("age_limit",               r"AGE\s+LIMIT\s*:?"),
    # "LANGUAGE" as a section header is always followed by "Knowledge of
    # ET..." on this site. Without that anchor, the pattern also matches
    # mid-sentence mentions like "fluency in speaking French language" and
    # truncates qualifications right before the word "language" appears.
    ("language",                r"\bLANGUAGE\s*:?\s*(?=Knowledge of ET)"),
    ("duties",                  r"DUTIES\s*(?:&|AND)?\s*RESPONSIBILITIES\s*:?"),
    ("contact_info",            r"CONTACT/APPLICATION\s+INFORMATION\s*:?"),
    # This boilerplate "bring your documents" paragraph appears right before
    # the final disclaimer NB on almost every local posting. Without this as
    # a boundary, the registration place swallows the whole paragraph.
    ("doc_checklist",           r"Interested applicants must bring"),
    # Word-start anchor so "N.B"/"NB" only matches the actual marker, not an
    # incidental "...n Bachelor..." style adjacency elsewhere in the text.
    ("nb",                      r"\bN\.?\s*B\.?\s*[:\.]?"),
]

_COMBINED = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for name, pattern in _SECTION_DEFS),
    re.IGNORECASE,
)

_DATE_THEN_PLACE = re.compile(r"^(?P<date>.*?\d{4}[,.]?)\s*(?:at\s+)?(?=[A-Z][a-z])(?P<rest>.+)$")

_BOILERPLATE_MARKERS = ("false information", "termination from the process")

_EMPTY_DETAILS = {"reg_date": "", "place": "", "qualifications": "", "age_limit": "", "nb": ""}


def _truncate_clean(text, limit):
    """Truncate at a word boundary instead of slicing mid-word/mid-sentence."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(" .,;:") + "…"


def extract_structured_details(detail_div):
    if not detail_div:
        return dict(_EMPTY_DETAILS)

    raw = detail_div.get_text(" ", strip=True)
    raw = re.sub(r"\s+", " ", raw).strip()

    raw_matches = list(_COMBINED.finditer(raw))

    # "Age limit" sometimes appears as its own bold header (e.g. "AGE LIMIT:
    # 18-35...") and sometimes as a clause glued onto the NB sentence (e.g.
    # "N.B: Age limit; 18-35..."). In the second case it isn't a real section
    # boundary -- drop it so that text stays part of the NB content instead
    # of being split into an orphan fragment.
    matches = []
    for m in raw_matches:
        if (
            m.lastgroup == "age_limit"
            and matches
            and matches[-1].lastgroup == "nb"
            and (m.start() - matches[-1].end()) <= 15
        ):
            continue
        matches.append(m)

    sections = {}  # name -> list of content strings, in order of appearance
    for i, m in enumerate(matches):
        name = m.lastgroup
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections.setdefault(name, []).append(raw[start:end].strip())

    qualifications = _truncate_clean(sections.get("qualifications", [""])[0], 600)
    age_limit = _truncate_clean(sections.get("age_limit", [""])[0], 120)

    # First NB that isn't just the generic legal disclaimer repeated on
    # almost every posting -- that one carries no useful info for a reader.
    nb = ""
    for candidate in sections.get("nb", []):
        if not any(marker in candidate.lower() for marker in _BOILERPLATE_MARKERS):
            nb = _truncate_clean(candidate, 200)
            break

    reg_date = ""
    place = ""
    if "registration_date_place" in sections:
        section_text = sections["registration_date_place"][0]
        section_text = re.sub(r"^From\s+", "", section_text, flags=re.IGNORECASE).strip()
        at_split = re.split(r"\s+at\s+", section_text, maxsplit=1, flags=re.IGNORECASE)
        if len(at_split) > 1:
            reg_date = at_split[0].strip()
            place = _truncate_clean(at_split[1], 150)
        else:
            # No "at" connector -- date and place were likely separate
            # bullet items (e.g. "June 4, 2026 – June 10, 2026 Ethiopian
            # Airlines HQ..."). Split right after the last year in the date.
            dp_match = _DATE_THEN_PLACE.match(section_text)
            if dp_match:
                reg_date = dp_match.group("date").strip()
                place = _truncate_clean(dp_match.group("rest"), 150)
            else:
                reg_date = section_text.strip()
    else:
        if "registration_date" in sections:
            reg_date = re.sub(
                r"^From\s+", "", sections["registration_date"][0], flags=re.IGNORECASE
            ).strip()
        if "registration_place" in sections:
            place = _truncate_clean(sections["registration_place"][0], 150)

    return {
        "reg_date":       reg_date,
        "place":          place,
        "qualifications": qualifications,
        "age_limit":      age_limit,
        "nb":             nb,
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

        detail_div = soup.find(id=href.lstrip("#"))
        details    = extract_structured_details(detail_div)

        postings[key] = {
            "position":     position,
            "closing_date": closing,
            "category":     category,
            **details,
        }

    return postings


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_with_ai(posting):
    cat_emoji = "🌍" if posting["category"] == "International" else "🇪🇹"
    quals     = posting.get("qualifications", "")

    bullets = []
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{
                "role": "user",
                "content": (
                    "Extract the key qualifications for this job as 3-5 short bullet "
                    "points (education, experience, key skills). Each bullet must be "
                    "under 12 words. Return ONLY the bullets, one per line, each "
                    "starting with '-'. No intro, no closing remarks, no extra "
                    "commentary.\n\n"
                    f"{quals}"
                )
            }],
            max_tokens=150,
        )
        summary_raw = resp.choices[0].message.content.strip()
        bullets = [
            re.sub(r"^[-•*]\s*", "", line).strip()
            for line in summary_raw.splitlines()
            if line.strip()
        ]
        bullets = [_truncate_clean(b, 100) for b in bullets if b][:5]
    except Exception:
        bullets = []

    if not bullets:
        # Fallback: split the raw qualifications into short fragments instead
        # of dumping the whole passage in one block.
        bullets = [s.strip() for s in re.split(r"(?<=[.;])\s+", quals) if s.strip()][:4]
        bullets = bullets or ["See full listing for details."]

    lines = [
        "✈️ <b>Ethiopian Airlines — Job Vacancy</b>",
        "",
        f"<b>{escape_html(posting['position'])}</b>",
        f"{cat_emoji} <i>{escape_html(posting['category'])}</i>",
        "",
    ]

    if posting.get("reg_date"):
        lines.append(f"📅 <b>Registration Period:</b> {escape_html(posting['reg_date'])}")
    elif posting.get("closing_date"):
        lines.append(f"📅 <b>Closing Date:</b> {escape_html(posting['closing_date'])}")

    if posting.get("place"):
        lines.append(f"📍 <b>Place of Registration:</b> {escape_html(posting['place'])}")

    if posting.get("age_limit"):
        lines.append(f"🎯 <b>Age Limit:</b> {escape_html(posting['age_limit'])}")

    lines += [
        "",
        "📋 <b>Requirements:</b>",
    ]
    for bullet in bullets:
        lines.append(f"• {escape_html(bullet)}")

    if posting.get("nb"):
        lines += [
            "",
            f"⚠️ <b>NB:</b> {escape_html(posting['nb'])}",
        ]

    lines += [
        "",
        f'<a href="{URL}">View full listing and apply →</a>',
    ]

    return "\n".join(lines)


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
