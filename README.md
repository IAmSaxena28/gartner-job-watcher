# Gartner Job Watcher

Checks Gartner's careers site every 15 minutes and sends a Telegram alert when a
tech role a fresher can apply for opens in India, including Remote - India.
Each alert has the job link, the Req ID and a referral message ready to paste
on LinkedIn.

## How it decides

1. **Location**: only India postings (any city plus Remote - India). It finds the
   site's location filters automatically, so a new Indian office is picked up
   without a code change.
2. **Title**: the job must be a tech role. That means it's in one of Gartner's
   own job categories (*Technology*, *Data Analytics*) **or** its title has a tech
   keyword (software, engineer, data, systems analyst, technical support, MTS,
   trainee, and so on). Titles with a seniority word (Senior, Lead, Principal,
   Director, Manager, Architect, and so on) are dropped.
3. **Experience**: the description is scanned for phrases like "2-4 years of
   experience" that appear next to the word *experience*.
   - Numbers in a *preferred / nice to have / a plus* context are ignored, so
     "Minimum 0-1 yrs … Preferred 3+ yrs" is read as 0–1.
   - Company boilerplate ("Gartner has 40 years of…") and anything over 10 years is ignored.
   - The strictest **required** number decides what happens:
     - 0–1 years, no number, or only "preferred" numbers → 🚨 **NEW**
     - 2 years with an upper limit of at most 4 (e.g. "2-4 years") → ⚠️ **STRETCH**,
       still worth a referral ask. Intern, Associate or Graduate titles in this band → 🚨 **NEW**
     - anything higher, e.g. "2-5 years" or "3+ years" → skipped
   - Each alert quotes the sentence it got the number from, so you can check it.
4. **De-duplication**: each Req ID alerts once. If a posting disappears and comes
   back more than 3 days later, it alerts again as 🔁 **REOPENED**.

You can change all of these rules in `config.json`, for example `max_years`,
`stretch_years`, `stretch_max_upper` and the keyword lists. Your referral message
also lives there: edit `profile_intro` (who you are) and `referral_template`.

**Reliability**
- Each alert channel (Telegram, email, WhatsApp) is tried separately. If one fails, the others still send.
- If every channel fails, the job is **not** marked as seen, so the next run tries again.
- If the site changes and the script starts failing, you get **one** ❌ error
  message, not one every 15 minutes, and a ✅ message when it recovers.
- `seen_jobs.json` is only committed when it actually changes, which is at most
  about once a day plus whenever a new job appears. Before pushing, the workflow
  rebases so it never conflicts with your own pushes.

## How do I know it's working?

Every morning at about **8 AM IST** the bot sends a **☀️ Daily check-in**. It shows:
- how many checks ran in the last 24 hours
- how many India postings are open right now
- which matching roles are still open
- which tech roles it **skipped**, and why

**Rule: if the check-in doesn't arrive by about 9 AM, something is broken.** No
check-in is the alarm. You don't have to watch anything else.

## Setup

### 1. Telegram bot (2 minutes)
1. In Telegram, message **@BotFather** → `/newbot` → choose a name. Copy the token.
2. Open your new bot and press **Start**.
3. Locally: `cp .env.example .env`, paste the token into `TELEGRAM_BOT_TOKEN`, then run
   `python3 scraper.py --telegram-chat-id` and copy the printed ID into `TELEGRAM_CHAT_ID`.
4. `python3 scraper.py --test-alert` → you should get a message.

### 2. Run it on GitHub
1. Push this folder to a GitHub repo.
2. Go to **Settings → Secrets and variables → Actions** and add `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID`.
3. Go to the **Actions** tab → *Check Gartner jobs* → **Run workflow** to trigger the first run.

Public repos get unlimited free Actions minutes. If you make the repo **private**,
change the cron schedule in `.github/workflows/check-jobs.yml` to `*/30`.

## What actually starts the checks

GitHub's built-in schedule (`cron` in the workflow) turned out to be unreliable for
this account: it ran once in 12 hours. So **cron-job.org** starts the workflow every
15 minutes through GitHub's API, using a key that can only start Actions in this repo
and expires on 2027-11-17. The GitHub schedule stays switched on as a backup.

- Pause alerts: switch the job off on cron-job.org.
- Before 2027-11-17: make a new key and paste it into the cron-job.org job's
  `Authorization` header.

## Commands

| Command | What it does |
|---|---|
| `python3 scraper.py` | Normal check: sends alerts and saves `seen_jobs.json` |
| `python3 scraper.py --dry-run` | Prints alerts to the terminal and saves nothing |
| `python3 scraper.py --test-alert` | Sends a test message to every configured channel |
| `python3 scraper.py --telegram-chat-id` | Prints your Telegram chat ID |

To get alerts again for jobs it has already seen, delete `seen_jobs.json`.

## Optional extra channels
- **Email**: set `SMTP_USER`, `SMTP_PASSWORD` and `EMAIL_TO`. For Gmail, use an
  [App password](https://myaccount.google.com/apppasswords), not your normal password.
- **WhatsApp**: set `CALLMEBOT_PHONE` and `CALLMEBOT_APIKEY` (see
  [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/)). It's free
  but sometimes slow, so keep Telegram as your main channel.
