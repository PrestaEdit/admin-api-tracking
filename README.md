# PrestaShop Admin API — Endpoint Tracking

A standalone, interactive dashboard that tracks the implementation progress of
PrestaShop's Admin API endpoints (CQRS commands & queries), based on
[PrestaShop/PrestaShop#39630](https://github.com/PrestaShop/PrestaShop/issues/39630).

## Files

- **`admin-api-tracking.html`** — the dashboard. Open it directly in a browser
  (fully static, no network calls). Search, filter by status / type / domain,
  sort domains and table columns.
- **`admin-api-tracking.gen.py`** — the generator. Fetches issue #39630, re-checks
  every referenced `ps_apiresources` pull request live, reconciles statuses
  (merged → Implemented, closed → Missing, open → In Progress), de-duplicates
  source artifacts, and writes the HTML with the current date stamped in.

## Daily auto-refresh

The workflow [`.github/workflows/daily.yml`](.github/workflows/daily.yml) runs
every day at **07:00 UTC (09:00 Europe/Brussels)**, regenerates the dashboard and
commits it if anything changed. You can also trigger it manually from the
**Actions** tab (*Run workflow*).

To view the latest locally:

```bash
git pull
open admin-api-tracking.html      # macOS
```

## Run the generator yourself

Requires `python3` and an authenticated [`gh`](https://cli.github.com/) CLI:

```bash
python3 admin-api-tracking.gen.py [output.html]
```

## Optional: publish via GitHub Pages

Make the repo public, then enable **Settings → Pages → Deploy from branch
(`master` / root)**. The dashboard is then viewable at a URL, auto-updated daily,
with no `git pull` needed.
