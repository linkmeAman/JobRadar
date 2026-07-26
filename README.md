# Job Radar

Job Radar runs the configured JobSpy searches, records seen listings in
SQLite, and sends a Telegram message for each newly discovered job. It is
intended to run to completion under a systemd timer every six hours.

## Setup

Use Python 3.10 or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U -r requirements.txt
```

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

Check the schedule and a run with:

```bash
systemctl list-timers job-radar.timer
sudo systemctl start job-radar.service
journalctl -u job-radar.service
```
