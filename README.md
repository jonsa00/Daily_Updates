# Daily Updates

News dashboard (Markets, Technology, World, Montréal) built from RSS feeds,
with a market board of major indices, commodities, rates, FX and crypto.
GitHub Actions rebuilds it hourly and publishes it to GitHub Pages.

- `sources.json` — outlets per section; edit and push to change them
  (`indicators` lists the market board's Yahoo Finance symbols: indices, commodities, rates, FX, crypto)
- `template.html` — page layout and styling
- `build_dashboard.py` — fetches feeds and writes the page (Python stdlib only)

Run locally: `python3 build_dashboard.py` → `dashboard.html`
