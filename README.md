# Geomagnetic Drift Atlas

An interactive 3D globe of Earth's magnetic field: the South Atlantic Anomaly,
field intensity and declination, isogonic/isodynamic lines at any zoom, the
drift of magnetic north, 1900–2030. Static site, no server, no build step for
the page itself.

## How it stays current

`scripts/build_data.py` queries [ESA's VirES API](https://vires.services) for
IGRF-14 and the live Swarm/CHAOS core-field model, computes field intensity and
declination locally (plain arithmetic, no spherical-harmonic library), and
writes the result straight into `data/*.js`. It's stdlib-only — no
`requirements.txt`, nothing to `pip install`.

A scheduled GitHub Action (`.github/workflows/refresh-data.yml`) runs that
script weekly and commits whatever changed. GitHub Pages redeploys on every
push to `main`, so the site stays current with zero manual steps.

## One-time setup

1. **Repo secret**: Settings → Secrets and variables → Actions → New repository
   secret, name `VIRES_TOKEN`, value a personal access token from
   [vires.services](https://vires.services) (Account → Access Tokens).
2. **Pages**: Settings → Pages → Build and deployment → Source: *Deploy from a
   branch* → Branch: `main` / `(root)`.
3. Done. The page is served at `https://<user>.github.io/<repo>/`; the data
   refreshes on the next scheduled run (or trigger it manually: Actions →
   Refresh geomagnetic data → Run workflow).

## Local rebuild

```
VIRES_TOKEN=... python3 scripts/build_data.py
```

## Layout

```
index.html                   the app
data/                        generated + static reference data
  igrf_data.js                field/declination grid, 1900-2030 (generated)
  chaos_fine_data.js           close-zoom declination, ~now (generated)
  pole_data.js, pass_data.js, country_data.js, cities_data.js, zip_data.js  (static)
scripts/build_data.py        regenerates the two "(generated)" files above
.github/workflows/           scheduled refresh
```
