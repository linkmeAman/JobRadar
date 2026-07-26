# Job Radar — Automated Job Listing Scraper (JobSpy + Telegram)

## 1. Objective

Scheduled script that scrapes new backend/Go/Python job postings (remote + India) using JobSpy, dedupes against previously seen listings, and pushes only new matches to a Telegram chat. Runs unattended as a systemd timer, same pattern as UEM's `uem-gateway` and `uem-topic-router` services.

**Success criteria:** by end of today, a working service that scrapes every 6 hours and sends a Telegram message per new job with title, company, location, salary (if present), and apply link.

## 2. Architecture

```
systemd timer (every 6h)
        │
        ▼
   main.py (orchestrator)
        │
        ├──► scraper.py   (JobSpy wrapper, runs N search configs)
        │
        ├──► dedupe.py    (SQLite: has this job_id been seen?)
        │
        └──► notifier.py  (Telegram Bot API sendMessage for new jobs only)
```

No web server, no queue. Single process, run to completion, exit. State lives in SQLite so restarts don't cause duplicate alerts.

## 3. Directory Structure

```
job-radar/
├── main.py                # entrypoint: run configs -> dedupe -> notify
├── scraper.py             # JobSpy calls, one function per search config
├── dedupe.py              # SQLite read/write, job_id = hash(title+company+site)
├── notifier.py            # Telegram sendMessage wrapper
├── config.yaml            # search terms, locations, filters, telegram creds
├── requirements.txt
├── data/
│   └── jobs.db             # sqlite, gitignored
├── deploy/
│   ├── job-radar.service  # systemd oneshot unit
│   └── job-radar.timer    # systemd timer unit (OnCalendar=*-*-* 0/6:00:00)
├── .env                    # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (gitignored)
└── README.md
```

## 4. Tech Stack

- Python 3.10+ (hard requirement from JobSpy, confirmed on current repo)
- `python-jobspy` — install as `pip install -U python-jobspy` (package name differs from import name: `from jobspy import scrape_jobs`)
- `sqlite3` (stdlib) for dedupe state
- `requests` for Telegram Bot API (no need for `python-telegram-bot` SDK, one endpoint)
- `pyyaml` for config
- systemd timer for scheduling (Ubuntu box, consistent with UEM deployment)

## 5. Filter Config (your stack)

Two real JobSpy constraints changed this from the first draft:

1. **Indeed** only allows one filter group per call: `hours_old` OR (`job_type` + `is_remote`) OR `easy_apply`. Mixing them silently breaks the Indeed leg. Since remote+fulltime matters more than freshness for Indeed, its search drops `hours_old` and leans on `dedupe.py` for novelty instead.
2. **Indeed/Glassdoor need `country_indeed`** as an explicit param (not inferred from `location`). India is supported on Indeed.
3. **LinkedIn's own limitation** is only `hours_old` vs `easy_apply`, so `job_type` + `is_remote` + `hours_old` together is fine there.

`config.yaml`:

```yaml
telegram:
  bot_token: ${TELEGRAM_BOT_TOKEN}
  chat_id: ${TELEGRAM_CHAT_ID}

searches:
  # LinkedIn + Google: hours_old is safe here, keeps results fresh and cuts
  # how many pages get hit (LinkedIn rate limits hard after ~page 10 on one IP)
  - name: backend_li_google
    site_name: [linkedin, google]
    search_term: "backend engineer"
    google_search_term: "backend engineer jobs India remote"
    location: "India"
    is_remote: true
    job_type: fulltime
    hours_old: 24
    results_wanted: 40

  - name: go_developer_li_google
    site_name: [linkedin, google]
    search_term: "Go developer backend"
    google_search_term: "Go backend developer jobs India remote"
    location: "India"
    is_remote: true
    job_type: fulltime
    hours_old: 24
    results_wanted: 30

  - name: python_backend_li_google
    site_name: [linkedin, google]
    search_term: "Python backend engineer FastAPI"
    google_search_term: "Python backend engineer jobs India remote"
    location: "India"
    is_remote: true
    job_type: fulltime
    hours_old: 24
    results_wanted: 30

  - name: ai_llm_li_google
    site_name: [linkedin, google]
    search_term: "LLM AI engineer backend"
    google_search_term: "LLM AI engineer jobs India remote"
    location: "India"
    is_remote: true
    job_type: fulltime
    hours_old: 24
    results_wanted: 30

  # Indeed: no hours_old (conflicts with job_type/is_remote), country_indeed
  # is required, one boolean search_term covers all four role variants
  # (Indeed's own FAQ recommends this OR/quote syntax over separate calls)
  - name: backend_indeed
    site_name: [indeed]
    search_term: 'backend engineer (golang OR python OR "machine learning" OR llm) fulltime remote'
    location: "India"
    country_indeed: "India"
    is_remote: true
    job_type: fulltime
    results_wanted: 60
```

## 6. Component Breakdown

**scraper.py**
- Loops over `searches` in config, calls `jobspy.scrape_jobs(**search_config)` per entry
- Catches per site failures independently (LinkedIn rate limits fast, don't let it kill the Indeed/Google runs)
- A 429 from any site means that site blocked you for the run, back off, don't retry immediately (JobSpy's own FAQ: wait between scrapes or rotate proxies, don't hammer)
- Returns a merged pandas DataFrame

**dedupe.py**
- Table: `seen_jobs(job_id TEXT PRIMARY KEY, title TEXT, company TEXT, site TEXT, first_seen_at TIMESTAMP)`
- `job_id = sha256(title + company + site)` since JobSpy's own job IDs aren't always stable across runs
- `filter_new(df) -> df_new` : anti-join against `seen_jobs`, then inserts the new rows
- Carries more weight now since the Indeed leg has no `hours_old` filter and will re-scrape older postings every run

**notifier.py**
- One Telegram message per new job (not batched as a single wall of text)
- Field names come straight off JobSpy's DataFrame columns: `title`, `company`, `city`, `state`, `job_url`, `min_amount`, `max_amount`, `currency`, `is_remote`
- Message format:
  ```
  🆕 {title}
  🏢 {company} | 📍 {city}, {state}  (or "Remote" if is_remote)
  💰 {min_amount}-{max_amount} {currency}   (omit line if both amounts are null)
  🔗 {job_url}
  ```
- Sleep ~0.3s between sends to stay under Telegram's rate limit

**main.py**
- `df = scraper.run_all(config.searches)`
- `new = dedupe.filter_new(df)`
- `notifier.send_all(new)`
- Log counts (scraped / new / sent) to stdout, captured by journald via systemd

## 7. Telegram Setup (one time, ~10 min)

1. Message `@BotFather` on Telegram, `/newbot`, get `TELEGRAM_BOT_TOKEN`
2. Message your new bot once (anything), then hit `https://api.telegram.org/bot<TOKEN>/getUpdates` to read your `chat_id`
3. Store both in `.env`, never commit

## 8. Systemd Wiring (matches UEM pattern)

`job-radar.service`:
```ini
[Unit]
Description=Job Radar scrape and notify run
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/job-radar
EnvironmentFile=/opt/job-radar/.env
ExecStart=/opt/job-radar/.venv/bin/python main.py
```

`job-radar.timer`:
```ini
[Unit]
Description=Run Job Radar every 6 hours

[Timer]
OnCalendar=*-*-* 0/6:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

`sudo systemctl enable --now job-radar.timer`

## 9. Build Order for Today (roughly 3 to 4 hours)

1. `pip install -U python-jobspy pyyaml requests` in a venv, confirm Python is 3.10+ (`python3 --version`) (15 min)
2. `scraper.py` + test one search config manually, confirm the Indeed leg (no `hours_old`) and the LinkedIn/Google leg both return data (45 min)
3. `dedupe.py` + SQLite schema, test insert/filter logic with fake data (45 min)
4. Telegram bot setup + `notifier.py`, send a test message (30 min)
5. Wire `main.py`, run end to end manually (30 min)
6. Systemd service and timer, verify with `systemctl status` and `journalctl -u job-radar` (30 min)

## 10. Known Risks

- **Indeed vs LinkedIn filter conflicts are real and silent.** Indeed accepts only one of `hours_old` / (`job_type`+`is_remote`) / `easy_apply` per call, JobSpy won't error, it'll just drop or ignore the conflicting filter. Config above already accounts for this, don't add `hours_old` back into the Indeed entry later without re-checking.
- **429 responses mean you're blocked for that site, not that something's broken.** JobSpy's own guidance is to wait between scrapes and/or rotate proxies, not to retry immediately.
- LinkedIn is the most rate limited site in JobSpy, expect occasional empty returns on a single IP, especially past page 10
- Indeed is currently the least rate limited site, safe to lean on it more heavily
- Google jobs filtering is controlled entirely by `google_search_term`, keep it phrased like a real search query (copy what Google's own jobs search box suggests), not structured like the other params
- LinkedIn's `easy_apply` filter is documented as currently non functional, don't build logic around it

## 11. Future Enhancements (not today)

- Swap flat Telegram messages for a daily digest option
- Feed new listings through your NL2SQL/LangGraph stack for relevance scoring instead of keyword match alone
- Add Google Sheet as a secondary sink for historical tracking and filtering
- Auto tailor resume bullets per listing using your existing achievement bank
- Add proxy rotation if Indeed or LinkedIn start blocking your IP outright

## 12. Ready to Paste Agent Prompt

```
Build "Job Radar": a Python 3.10+ job scraping automation using the
python-jobspy library (pip install -U python-jobspy, import as
`from jobspy import scrape_jobs`). Follow the structure and config in
PROJECT.md exactly (scraper.py, dedupe.py, notifier.py, main.py,
config.yaml, systemd units under deploy/).

Requirements:
- scraper.py must iterate over `searches` in config.yaml and call
  jobspy.scrape_jobs per entry, catching exceptions per search so one
  failing site/search does not kill the run. Do not add hours_old to
  any search entry that also has job_type + is_remote AND includes
  "indeed" in site_name, that combination is invalid for Indeed.
  Entries targeting Indeed must include country_indeed.
- dedupe.py uses SQLite at data/jobs.db, job_id = sha256(title+company+site),
  exposes filter_new(df) -> DataFrame of only unseen rows, and persists new
  rows as seen.
- notifier.py sends one Telegram message per new job via the Bot API
  (requests, no SDK), using DataFrame columns title, company, city,
  state, job_url, min_amount, max_amount, currency, is_remote per the
  format in PROJECT.md section 6, with a 0.3s delay between sends.
- main.py orchestrates: scrape -> dedupe -> notify -> log counts to stdout.
- Read TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment variables.
- Include requirements.txt and a README with setup + systemd install steps.

Scope lock: build only what's in PROJECT.md. No web UI, no queue, no Google
Sheet integration in this pass. Ask before adding anything not listed here.
```
