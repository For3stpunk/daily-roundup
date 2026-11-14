# The Daily Roundup

A daily publication of the top ~25 articles in each of 14 beats — Graphic Design, Industrial Design,
Programming, Data Science, Data Visualization, Music News, Music Videos, Music Reviews, Book News,
Book Releases, Book Reviews, Movie News, Movie Releases, Movie Reviews — built every morning by
GitHub Actions and published on GitHub Pages with RSS feeds.

## How it works
- `sources.yml` — the publication settings and every source per topic (RSS feeds, Google News
  searches, Hacker News for programming). Edit this to tune what shows up.
- `roundup.py` — fetches everything, keeps articles from the last ~30 hours (a week for slow beats),
  merges duplicates (an article covered by several outlets ranks higher), caps each outlet at 6 per
  beat, puts each article under one beat only, and writes:
  - `data/YYYY-MM-DD.json` — the issue
  - `docs/` — the site: today's issue, archive, per-day issue pages and the feeds
- `.github/workflows/daily.yml` — runs at 13:17 UTC (5:17 AM Pacific in winter) and on demand.

## Feeds (for Feedly)
- `<site>/feed.xml` — everything, one entry per article
- `<site>/issues.xml` — one entry per day containing the whole issue
- `<site>/topics/<beat>.xml` — one feed per beat (e.g. `topics/movie-reviews.xml`)
- `<site>/feeds.html` lists them all.

## One-time setup
1. Push this folder to a **public** GitHub repo (e.g. `daily-roundup`).
2. Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. Set `site_url` in `sources.yml` to `https://<your-username>.github.io/daily-roundup`.
4. **Actions → Daily roundup → Run workflow** to publish the first issue.
5. Check `docs/status.json` after the run: any source marked `error` needs a new URL or removal.

## Run locally
    pip install -r requirements.txt
    python roundup.py            # collect today's issue and build docs/
    python roundup.py --rebuild  # rebuild docs/ from data/ only
