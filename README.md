# COVID Signal

A personal Washington and Texas COVID-19 activity tracker built from official CDC feeds. It intentionally tracks activity and severity signals rather than reported case counts.

## Update data

Run `./scripts/update.sh`. The updater stores source snapshots under `data/raw`, processed observations in SQLite, and the dashboard payload in `app/public/data/tracker.json`.

## Run locally

From `app`, run `pnpm dev`, then open `http://localhost:3000`.

## Automatic schedule

The included macOS LaunchAgent refreshes every Friday at 12:15 PM local time and once when loaded. Logs go to `outputs/`.

GitHub Actions is the public-data publisher. It refreshes after the Friday CDC release and again Sunday morning for late reports, validates the result, and commits only `public/data/tracker.json`. The hosted Site reads that file directly from GitHub and falls back to its bundled dataset when GitHub cannot be reached.

## Interpretation

The 0–100 activity index is a within-state historical percentile composite: wastewater 40%, emergency visits 25%, hospital admissions 20%, and lag-adjusted death-certificate deaths 15%. Missing signals are excluded and remaining weights are renormalized. It is personal decision support, not an official CDC risk classification or medical advice.
