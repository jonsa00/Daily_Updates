# Daily Updates

News dashboard (Technology, World, Montréal) built from RSS feeds.
GitHub Actions rebuilds it hourly and publishes it to GitHub Pages.

- `sources.json` — outlets per section; edit and push to change them
- `template.html` — page layout and styling
- `build_dashboard.py` — fetches feeds and writes the page (Python stdlib only)

Run locally: `python3 build_dashboard.py` → `dashboard.html`
