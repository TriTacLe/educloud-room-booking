# ntnu-room-booking

Automates room booking on [tp.educloud.no](https://tp.educloud.no/ntnu/app/rombestilling/) using Playwright. MFA is done manually on first run; session is reused after that.

## Install

```bash
uv sync
uv run playwright install chromium
```

## Setup

```bash
cp config.example.yaml config.yaml
# edit config.yaml
```

## Commands

### Login

```bash
uv run koker login
```

Opens Chromium. Complete Feide + Microsoft Authenticator which user has to do manually. Session saved to `data/storage_state.json`. Re-run when session expires.

### Book

```bash
uv run koker book --config config.yaml
```

Searches for available rooms matching your config, picks best fit, confirms booking. If no room at configured time, steps forward in 15-min increments up to `search_cutoff_end`, then tries the next day, up to `max_days_ahead`.

### Status

```bash
uv run koker status          # list active bookings
uv run koker status --verbose  # with debug output
```

### Cancel

```bash
uv run koker cancel --id <booking-id>
uv run koker cancel --all
```

## NTNU booking rules

- Max 4 hours per booking
- Max 8 bookings in a rolling 2-week window
- Booking window opens 14 days ahead (near midnight)
- Hours: 08:00-23:00
