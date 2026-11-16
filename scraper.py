#!/usr/bin/env python3
"""
Gartner job watcher.

Polls Gartner's Workday careers API, keeps tech roles located in India
(including Remote - India) that a fresher can realistically go for, and
sends an alert (Telegram / email / WhatsApp) the moment a new one appears.

Usage:
    python scraper.py                    # normal run (sends alerts, saves state if changed)
    python scraper.py --dry-run          # print what would be sent, don't save state
    python scraper.py --test-alert       # send a test message to every configured channel
    python scraper.py --telegram-chat-id # print your Telegram chat id (after messaging your bot)
"""
from __future__ import annotations

import html
import json
import os
import re
import smtplib
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_FILE = ROOT / "seen_jobs.json"

SITE = CONFIG["site"]
API = f"{SITE['base']}/wday/cxs/{SITE['tenant']}/{SITE['board']}"
PUBLIC = f"{SITE['base']}/en-US/{SITE['board'].lower()}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

DRY_RUN = "--dry-run" in sys.argv


# --------------------------------------------------------------------------- env

def load_dotenv() -> None:
    """Minimal .env loader for local runs (GitHub Actions uses secrets instead)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- http

def http(url: str, payload: dict | None = None, retries: int = 4) -> dict:
    """Workday's bot protection rejects Python's TLS fingerprint (HTTP 429) but
    accepts curl, so all careers-site requests go through the curl binary."""
    cmd = ["curl", "-sS", "--compressed", "--max-time", "30", "-w", "\n%{http_code}",
           "-H", "Accept: application/json", "-H", "Accept-Language: en-US", "-A", UA]
    if payload is not None:
        cmd += ["-H", "Content-Type: application/json", "--data", json.dumps(payload)]
    last = ""
    for attempt in range(retries):
        r = subprocess.run(cmd + [url], capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        if r.returncode == 0 and code == "200":
            return json.loads(body)
        last = f"HTTP {code or '?'} {r.stderr.strip()}"
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Request failed after {retries} tries: {url} ({last})")


# --------------------------------------------------------------------------- matching helpers

def _kw_regex(words: list[str]) -> re.Pattern:
    """Whole-word, case-insensitive match for any of `words` (optional plural 's')."""
    alts = "|".join(re.escape(w.lower()) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alts})s?(?![a-z0-9])", re.I)


INDIA = _kw_regex(CONFIG["india_location_words"])   # whole words: "India" but not "Indiana"
INCLUDE = _kw_regex(CONFIG["title_include"])
EXCLUDE = _kw_regex(CONFIG["title_exclude"])
ENTRY = _kw_regex(CONFIG["entry_level_words"])


# --------------------------------------------------------------------------- fetch

def discover_filters() -> tuple[dict, list[str]]:
    """Find the site's filter ids at run time, so new Indian offices or renamed ids
    don't break anything.

    Returns (india_filter, tech_family_ids).

    The city-level `locations` filter is used rather than `locationCountry`:
    it also catches multi-location postings where India is a secondary location
    (country filter: 28 jobs, city filter: 33 — a strict superset).
    """
    d = http(f"{API}/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    country_ids, location_ids, family_ids = [], [], []
    families = {f.lower() for f in CONFIG["tech_job_families"]}

    def walk(facets):
        for f in facets:
            param = f.get("facetParameter")
            for v in f.get("values", []):
                if "values" in v:          # nested group (locationMainGroup)
                    walk([v])
                    continue
                desc = (v.get("descriptor") or "").strip()
                if param == "locationCountry" and desc.lower() == CONFIG["country"].lower():
                    country_ids.append(v["id"])
                elif param == "locations" and INDIA.search(desc):
                    location_ids.append(v["id"])
                elif param == "jobFamilyGroup" and desc.lower() in families:
                    family_ids.append(v["id"])

    walk(d.get("facets", []))
    if location_ids:
        return {"locations": location_ids}, family_ids
    if country_ids:
        return {"locationCountry": country_ids}, family_ids
    raise RuntimeError("Could not find India location filters on the careers site — site layout may have changed.")


def fetch_all(facets: dict) -> dict[str, dict]:
    jobs: dict[str, dict] = {}
    offset = 0
    while True:
        d = http(f"{API}/jobs", {"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": ""})
        postings = d.get("jobPostings", [])
        for p in postings:
            req_id = (p.get("bulletFields") or [None])[0] or p["externalPath"]
            jobs[req_id] = p
        offset += 20
        if not postings or offset >= d.get("total", 0):
            return jobs


def fetch_india_jobs() -> tuple[dict[str, dict], set[str]]:
    """Returns (all India postings, req ids that are in a tech job category)."""
    india, families = discover_filters()
    jobs = fetch_all(india)
    tech = set(fetch_all({**india, "jobFamilyGroup": families})) if families else set()
    return jobs, tech


def fetch_detail(path: str) -> dict:
    return http(f"{API}{path}").get("jobPostingInfo", {})


# --------------------------------------------------------------------------- filters

def title_ok(title: str, in_tech_family: bool) -> bool:
    """Tech role (by Gartner's own job category OR by title keyword), and not senior."""
    if EXCLUDE.search(title):
        return False
    return in_tech_family or bool(INCLUDE.search(title))


def html_to_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s))


NUM_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10}
NUM = r"(\d{1,2}|" + "|".join(NUM_WORDS) + r")"
YEARS_RE = re.compile(
    rf"\b{NUM}\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*{NUM}\s*\+?\s*)?(?:years?|yrs?)\b",
    re.I,
)
PREFERRED_MARKERS = ["preferred", "nice to have", "nice-to-have", "good to have", "good-to-have",
                     "bonus", "desirable", "desired", "is a plus", "would be a plus", "an advantage",
                     "added advantage", "ideally"]
REQUIRED_MARKERS = ["minimum qualification", "basic qualification", "required", "requirements",
                    "must have", "must-have", "must", "what you'll need", "what you will need",
                    "what you’ll need", "you have", "eligibility"]
COMPANY_WORDS = re.compile(r"\b(gartner|we|our|company|founded|history|decades?)\b", re.I)


def _num(s: str | None) -> int | None:
    if s is None:
        return None
    return int(s) if s.isdigit() else NUM_WORDS.get(s.lower())


def _last_pos(text: str, markers: list[str]) -> int:
    return max((text.rfind(m) for m in markers), default=-1)


def required_experience(text: str) -> dict | None:
    """Work out the experience a posting *requires*.

    - Only 'N years' mentions sitting next to the word 'experience' count.
    - Mentions in a 'preferred / nice to have / bonus' context are set aside, so
      "Minimum: 0-1 years ... Preferred: 3+ years" is read as 0-1, not 3.
    - Company boilerplate ("Gartner has 40+ years of experience...") and anything
      above 10 years is ignored.
    - Among the required mentions, the strictest one (largest lower bound) wins.

    Returns {"min", "max", "quote", "preferred_only"} or None if nothing stated.
    """
    required, preferred = [], []
    low = text.lower()
    for m in YEARS_RE.finditer(text):
        before, after = low[max(0, m.start() - 40): m.start()], low[m.end(): m.end() + 60]
        if not re.search(r"\bexp(erience|\.|\b)", before + " " + after):
            continue
        lo, hi = _num(m.group(1)), _num(m.group(2))
        if lo is None or lo > 10:
            continue
        if COMPANY_WORDS.search(low[max(0, m.start() - 50): m.start()]) and "experience" not in after[:25]:
            continue
        item = {"min": lo, "max": hi,
                "quote": text[max(0, m.start() - 40): m.end() + 50].strip()}
        context = low[max(0, m.start() - 300): m.start()]
        is_pref = (_last_pos(context, PREFERRED_MARKERS) > _last_pos(context, REQUIRED_MARKERS)
                   or any(p in re.split(r"[.;\n]", after)[0][:40]          # same sentence only
                          for p in ("preferred", "is a plus", "a plus", "desirable")))
        (preferred if is_pref else required).append(item)

    if required:
        best = max(required, key=lambda x: x["min"])
        return {**best, "preferred_only": False}
    if preferred:
        best = min(preferred, key=lambda x: x["min"])
        return {**best, "preferred_only": True}
    return None


def classify(title: str, exp: dict | None) -> str | None:
    """Return 'match', 'stretch' or None (skip)."""
    if exp is None or exp["preferred_only"]:
        return "match"
    lo, hi = exp["min"], exp["max"]
    if lo <= CONFIG["max_years"]:
        return "match"
    # e.g. "2-5 years" is a mid-level role even though its lower bound is 2
    if lo > CONFIG["stretch_years"] or (hi is not None and hi > CONFIG["stretch_max_upper"]):
        return None
    return "match" if ENTRY.search(title) else "stretch"


def describe_exp(exp: dict | None) -> str:
    if exp is None:
        return "not stated"
    rng = f"{exp['min']}–{exp['max']} yrs" if exp["max"] is not None else f"{exp['min']}+ yrs"
    return rng + (" (preferred, not required)" if exp["preferred_only"] else "")


# --------------------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"jobs": {}, "failures": 0}


def dump(state: dict) -> str:
    return json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def save_state(state: dict) -> None:
    if DRY_RUN:
        return
    STATE_FILE.write_text(dump(state))


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def days_between(a: str, b: str) -> int:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days


# --------------------------------------------------------------------------- notify

def http_post_form(url: str, fields: dict) -> None:
    data = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
        r.read()


def _telegram(subject: str, rich: str, plain: str) -> None:
    http_post_form(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage", {
        "chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": rich, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })


def _email(subject: str, rich: str, plain: str) -> None:
    user = os.environ["SMTP_USER"]
    msg = MIMEText(plain)
    msg["Subject"], msg["From"], msg["To"] = subject, user, os.getenv("EMAIL_TO") or user
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), 465, timeout=30) as s:
        s.login(user, os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def _whatsapp(subject: str, rich: str, plain: str) -> None:
    q = urllib.parse.urlencode({"phone": os.environ["CALLMEBOT_PHONE"], "text": plain[:1500],
                                "apikey": os.environ["CALLMEBOT_APIKEY"]})
    urllib.request.urlopen(f"https://api.callmebot.com/whatsapp.php?{q}", timeout=30).read()


CHANNELS = [
    ("Telegram", ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"), _telegram),
    ("Email", ("SMTP_USER", "SMTP_PASSWORD"), _email),
    ("WhatsApp", ("CALLMEBOT_PHONE", "CALLMEBOT_APIKEY"), _whatsapp),
]


def notify(subject: str, rich: str, plain: str) -> bool:
    """Send on every configured channel independently. One channel failing never
    stops the others. Returns True if at least one delivery succeeded."""
    configured = [(n, fn) for n, keys, fn in CHANNELS if all(os.getenv(k) for k in keys)]
    if not configured and not DRY_RUN and os.getenv("GITHUB_ACTIONS"):
        print("  !! No alert channel configured (add TELEGRAM_* secrets).", file=sys.stderr)
        return False                         # nobody would see it → don't mark the job as seen
    if DRY_RUN or not configured:
        print("\n" + "=" * 70 + f"\n[ALERT] {subject}\n" + plain + "\n" + "=" * 70)
        return True
    delivered = False
    for name, fn in configured:
        try:
            fn(subject, rich, plain)
            delivered = True
        except Exception as e:  # noqa: BLE001
            print(f"  !! {name} delivery failed: {e}", file=sys.stderr)
    return delivered


class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def format_alert(job: dict, kind: str, exp: dict | None, reopened: bool) -> tuple[str, str, str]:
    title, url, req, loc = job["title"], job["url"], job["req_id"], job["location"]
    deadline = job.get("deadline") or ""
    tag = "🔁 REOPENED" if reopened else ("🚨 NEW" if kind == "match" else "⚠️ STRETCH")
    note = "" if kind == "match" else "Asks a bit more experience than you have — still worth a referral ask."
    quote = f"“…{exp['quote']}…”" if exp else ""

    referral = CONFIG["referral_template"].format_map(SafeDict(
        title=title, req=req, location=loc, url=url, intro=CONFIG["profile_intro"]))

    subject = f"{tag} Gartner: {title} ({loc})"
    plain = (f"{tag} — {title}\n"
             f"📍 {loc}\n🆔 Req {req}\n🧭 Experience: {describe_exp(exp)}\n"
             + (f"   {quote}\n" if quote else "")
             + (f"⏳ {deadline}\n" if deadline else "")
             + (f"{note}\n" if note else "")
             + f"🔗 {url}\n\nReferral message to copy:\n{referral}")
    e = html.escape
    rich = (f"<b>{e(tag)} — {e(title)}</b>\n"
            f"📍 {e(loc)}\n🆔 Req <code>{e(req)}</code>\n🧭 Experience: {e(describe_exp(exp))}\n"
            + (f"<i>{e(quote)}</i>\n" if quote else "")
            + (f"⏳ {e(deadline)}\n" if deadline else "")
            + (f"<i>{e(note)}</i>\n" if note else "")
            + f"🔗 <a href=\"{e(url)}\">Open posting</a>\n\n"
            f"<b>Referral message (tap to copy):</b>\n<code>{e(referral)}</code>")
    return subject, rich, plain


# --------------------------------------------------------------------------- main

def run() -> None:
    state = load_state()
    before = dump(state)
    seen: dict = state.setdefault("jobs", {})
    now = today()

    listings, tech_ids = fetch_india_jobs()
    print(f"Fetched {len(listings)} India postings ({len(tech_ids)} in tech categories)")

    alerts, undelivered = 0, 0
    for req_id, p in listings.items():
        prev = seen.get(req_id)
        reopened = bool(prev and days_between(prev["last_seen"], now) > CONFIG["reopen_after_days"])
        if prev and not reopened:
            prev["last_seen"] = now          # date only → at most one change per job per day
            continue

        record = {"title": p["title"], "first_seen": prev["first_seen"] if prev else now,
                  "last_seen": now, "decision": "skip:title"}

        if not title_ok(p["title"], req_id in tech_ids):
            seen[req_id] = record
            continue

        info = fetch_detail(p["externalPath"])
        exp = required_experience(html_to_text(info.get("jobDescription", "")))
        kind = classify(p["title"], exp)
        record["decision"] = kind or f"skip:experience({describe_exp(exp)})"
        if not kind:
            seen[req_id] = record
            continue

        job = {
            "title": p["title"],
            "req_id": req_id,
            "location": info.get("location") or p.get("locationsText", ""),
            "url": info.get("externalUrl") or f"{PUBLIC}{p['externalPath']}",
            "deadline": info.get("jobPostingEndDateAsText") or info.get("timeLeftToApply") or "",
        }
        if notify(*format_alert(job, kind, exp, reopened)):
            seen[req_id] = record            # only remember it once you've actually been told
            alerts += 1
            print(f"  -> alerted [{kind}] {req_id} {p['title']} ({describe_exp(exp)})")
        else:
            undelivered += 1                 # not saved → retried on the next run

    # forget jobs that have been gone for a long time to keep the state file small
    for rid in [r for r, v in seen.items() if days_between(v["last_seen"], now) > 90]:
        del seen[rid]

    if state.get("failures", 0) > 0:
        notify("✅ Gartner watcher recovered", "✅ Gartner job watcher is working again.",
               "Gartner job watcher is working again.")
        state["failures"] = 0

    if dump(state) != before:
        save_state(state)
        print("State changed (dry run: not saved)." if DRY_RUN else "State updated.")
    else:
        print("No changes.")
    print(f"Done. {alerts} alert(s) sent.")
    if undelivered:
        raise RuntimeError(f"{undelivered} alert(s) could not be delivered on any channel; will retry next run.")


def main() -> None:
    load_dotenv()
    if "--telegram-chat-id" in sys.argv:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            sys.exit("Put TELEGRAM_BOT_TOKEN in .env first.")
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30) as r:
            updates = json.loads(r.read().decode()).get("result", [])
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
                 for u in updates if "message" in u}
        if not chats:
            sys.exit("No messages found. Open your bot in Telegram, press Start / send 'hi', then rerun.")
        for cid, name in chats.items():
            print(f"TELEGRAM_CHAT_ID={cid}   ({name})")
        return
    if "--test-alert" in sys.argv:
        ok = notify("✅ Gartner watcher test", "✅ <b>Gartner job watcher</b> is connected. You'll get alerts here.",
                    "Gartner job watcher is connected. You'll get alerts here.")
        print("Test alert sent." if ok else "Test alert FAILED on every channel.")
        return
    try:
        run()
    except Exception as e:  # noqa: BLE001
        state = load_state()
        # alert + write state only on the first failure in a row, so an outage
        # neither spams you nor creates a commit every 15 minutes
        if not state.get("failures"):
            msg = f"Gartner job watcher failed: {e}"
            notify("❌ Gartner watcher error", f"❌ {html.escape(msg)}", msg)
            state["failures"] = 1
            save_state(state)
        raise


if __name__ == "__main__":
    main()
