# Job Radar

Job Radar runs the configured JobSpy searches, records seen listings in
SQLite, and sends a Telegram message for each newly discovered job. It is
intended to run to completion under a systemd timer every 30 minutes.

Before deduplication, each listing is scored against the configured resume.
Only jobs at or above the configured score are alerted, with the score and
matching skills included in the Telegram message.

The 30-minute runner is designed to reduce provider pressure: it rotates two
role searches per run, always includes Indeed, isolates providers, and stores
rate-limit cooldowns across process restarts.

## Runtime flow

```text
systemd timer (00 and 30 each hour)
  -> validate Telegram and resume files
  -> select the next rotating search batch
  -> skip providers with an active persisted cooldown
  -> scrape each provider independently
  -> filter and rank jobs against the resume
  -> deduplicate and persist notification payloads
  -> send the strongest pending alerts
  -> mark each alert delivered only after Telegram accepts it
```

## Setup

Use Python 3.10 or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U -r requirements.txt
```

## Resume matching

`config.yaml` watches both supplied resume files. Replace either file whenever
you update your resume. Job Radar hashes the PDFs at the start of every run and
automatically rebuilds its cached skills and role profile when either has
changed. No manual refresh is needed.

The default profile is tuned to the current resume: backend and platform
engineering roles using Python, Go, FastAPI, Django, AWS, Docker, MySQL, Redis,
Linux, and microservices. Adjust `resume.paths` if you move either file.

## Configuration

The important controls are in `config.yaml`:

- `scraping.searches_per_run`: rotating non-Indeed searches selected each run.
  The default `2` covers all four role searches once per hour.
- `scraping.cooldown_minutes`: first provider cooldown after HTTP 429.
- `scraping.max_cooldown_minutes`: maximum exponential cooldown.
- `matching.minimum_score`: minimum resume score required for an alert.
- `matching.max_alerts_per_run`: maximum Telegram messages in one run.
- `matching.maximum_required_years`: rejects jobs requiring more experience.
- `matching.allowed_countries`: allowed countries for non-remote jobs.
- `matching.seniority_penalty_terms`: lowers stretch-role scores.
- `matching.excluded_title_terms`: rejects clearly unrelated job titles.
- `searches[].always_run`: excludes a search from rotation. Indeed uses this.

With the defaults, a provider returning repeated 429 responses is paused for
120, 240, 480, and then at most 720 minutes. Other providers continue running
every 30 minutes. A successful request clears that provider's cooldown.

The rotating LinkedIn/Google searches use a six-hour freshness window and
request at most 20 results each. Since every role search runs once per hour,
this leaves a generous overlap without repeatedly paging through a full day of
listings. Indeed requests 30 results and relies on SQLite deduplication because
its remote/full-time filter cannot be combined with `hours_old`.

Create `.env` in the project directory with the Telegram bot credentials:

```bash
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Run it manually from the project directory after exporting the variables:

```bash
set -a
. ./.env
set +a
python main.py
```

The first successful scrape stores job IDs and notification payloads in
`data/jobs.db`. A listing is marked notified only after Telegram accepts its
message. If Telegram is unavailable or a run stops midway, unsent listings
remain pending and are retried on the next run.

Pending alerts are ordered by resume match score. At most 10 are sent per run;
the remainder stay pending for later runs.

SQLite contains three internal tables:

- `seen_jobs`: deduplication identity, notification payload, and delivery time
- `provider_state`: provider health, result count, 429 streak, and cooldown
- `runtime_state`: the round-robin search cursor

`data/resume_profile.json` is a generated cache of the current PDF hashes,
detected roles, and detected skills. It is rebuilt automatically and is not
committed.

Telegram rate limits and temporary server/network failures are retried up to
three times using Telegram's `Retry-After` delay when it is supplied.

Scraping is isolated per provider. If LinkedIn, Google, or Indeed returns a
429, that provider is skipped for the rest of the current run without losing
results from the other providers. The next 30-minute run tries that provider
again; Job Radar does not retry a blocked scraper immediately.

Each completed run logs these counts:

- `scraped`: listings returned by all available providers
- `matched`: listings meeting the resume match threshold
- `new`: matched listings not previously stored
- `pending`: notifications not yet confirmed by Telegram
- `queued`: pending notifications selected for this run
- `deferred`: pending notifications retained for a later run
- `sent`: notifications accepted by Telegram during this run

It also logs `selected_searches`, per-site result counts, active cooldown
expiry timestamps, and Telegram retry delays.

## Tests

Run the automated checks without making real scrape or Telegram requests:

```bash
python -m unittest discover -s tests -v
```

## Systemd installation

On the Ubuntu host, place this project at `/opt/job-radar` and create its
virtual environment there. Ensure `/opt/job-radar/.env` contains the two
Telegram variables above, then install the included units:

```bash
sudo cp deploy/job-radar.service /etc/systemd/system/
sudo cp deploy/job-radar.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now job-radar.timer
```

The included timer uses:

```ini
OnCalendar=*-*-* *:00/30:00
Persistent=true
```

This runs at minute `00` and `30` of every hour. `Persistent=true` causes one
catch-up run after the machine restarts if a scheduled run was missed.

If the project remains in WSL at `/mnt/d/Radar`, update the installed service
so all three paths point there:

```ini
WorkingDirectory=/mnt/d/Radar
EnvironmentFile=/mnt/d/Radar/.env
ExecStart=/mnt/d/Radar/.venv/bin/python main.py
```

After changing either systemd unit, reload and restart the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl restart job-radar.timer
systemctl list-timers job-radar.timer
```

Check the schedule and a run with:

```bash
systemctl list-timers job-radar.timer
sudo systemctl start job-radar.service
journalctl -u job-radar.service
```

## Troubleshooting

- `skipped=cooldown`: the provider previously returned 429. This is expected;
  the persisted expiry time is included in the log.
- `blocked=429`: a new provider rate limit was recorded. Other providers
  continue, and the blocked provider is not retried immediately.
- `matched=0`: scraping worked, but no listing passed resume, seniority,
  country, and title filters. Lower `matching.minimum_score` cautiously.
- `pending>queued`: the alert cap is working; deferred alerts remain safe in
  SQLite.
- Telegram 401 means an invalid or revoked token. Telegram 403 usually means
  the chat ID is wrong, the bot is blocked, or `/start` was not sent.

Keep `.env`, `data/jobs.db`, and `data/resume_profile.json` private. They are
excluded by `.gitignore`.
