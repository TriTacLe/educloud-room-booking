# ntnu-room-booking

Automates room booking on [tp.educloud.no](https://tp.educloud.no/ntnu/app/rombestilling/) using Playwright. MFA is done manually on first run; session is reused after that.

## Install

```bash
uv sync
uv run playwright install chromium
```

## Setup

Edit `config.yaml` with your NTNU username and preferences (see profiles section inside).

Set Gmail credentials as env vars for notifications (optional):

```bash
export GMAIL_TO=you@gmail.com
export GMAIL_APP_PASSWORD=xxxx
```

## Commands

```bash
uv run koker login                        # authenticate (Feide + MFA, manual)
uv run koker book                         # book rooms per config.yaml
uv run koker book --config config.yaml    # explicit config
uv run koker rooms                        # list available rooms matching config criteria
uv run koker status                       # list active bookings
uv run koker cancel --id <booking-id>
uv run koker cancel --all
```

Session saved to `data/storage_state.json`. Re-run `koker login` when session expires (~2 weeks). If running via systemd timer, you'll get an email alert when it expires.

## Systemd timer (daily auto-booking)

```bash
cp systemd/koker-book.service ~/.config/systemd/user/
cp systemd/koker-book.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now koker-book.timer
```

Uses `config-daily.yaml` (rolling 14-day window). Edit that file separately from `config.yaml`.

## NTNU booking rules

- Max 4 hours per booking
- Max 8 bookings in a rolling 2-week window
- Booking window opens 14 days ahead
- Hours: 08:00-23:00
