# Geomagnetic Drift Atlas

An interactive 3D globe of Earth's magnetic field: the South Atlantic Anomaly,
field intensity and declination, isogonic/isodynamic lines at any zoom, the
drift of magnetic north, 1900–2030. Static site, no server, no build step for
the page itself.

## How it stays current

`scripts/build_data.py` queries [ESA's VirES API](https://vires.services) —
IGRF-14, the live Swarm/CHAOS core-field model, the CHAOS crustal field, and
raw Swarm-A measurements — and writes the result straight into `data/*.js`.
It is stdlib-only: no `requirements.txt`, nothing to `pip install`, no
spherical-harmonic library (VirES does that server-side).

A scheduled GitHub Action (`.github/workflows/refresh-data.yml`) runs it
weekly and commits whatever changed; GitHub Pages redeploys on every push.

The data is split so the weekly refresh is small and the repository stays
lean for years:

| file | contents | rewritten |
|---|---|---|
| `data/field_history.js` | IGRF-14 1900–1995, the 2027–2030 forecast, CHAOS epochs older than three years | only by `--full`, or automatically when VirES publishes a new model version / the year rolls over |
| `data/field_current.js` | the last three CHAOS epochs (the ones a model update can revise) | weekly |
| `data/chaos_fine_data.js` | close-zoom declination, CHAOS core + crustal field, evaluated at refresh time | weekly |
| `data/pass_data.js` | the most recent complete day of real Swarm-A 1 Hz measurements over the anomaly (ESA publishes these ~4 days behind real time) | weekly |
| `pole_data.js`, `country_data.js`, `cities_data.js`, `zip_data.js` | static reference data | never |

A weekly run makes ~45 API calls (~4 min); a full rebuild ~230 (~20 min).

## Accuracy notes

- The grids are geographic (geodetic WGS84) at heights above the ellipsoid;
  VirES works in geocentric coordinates, so every point is converted before
  the call and the returned field vector rotated back. Skipping this is a
  50–350 nT error. Verified against an independent IGRF-14 computation to
  0.0 nT.
- The SAA centre is the field minimum at 460 km, refined to ~0.1° from a local
  0.5° window (not snapped to the 4° grid).
- Secular variation always comes from a single model at ±1 year — never a
  difference across the IGRF/CHAOS join.
- The close-zoom grid uses internal sources only (core + crustal), so it is
  not stamped with one instant's magnetospheric state.

## One-time setup

1. **Repo secret**: Settings → Secrets and variables → Actions → New
   repository secret, name `VIRES_TOKEN`, value the access token from
   [vires.services](https://vires.services) (Account → Access Tokens). Paste
   only the token — a pasted `Authorization: Bearer …` line also works, but
   is not needed.
2. **Pages**: Settings → Pages → Source: *Deploy from a branch* → `main` /
   `(root)`.
3. Trigger the first run: Actions → Refresh geomagnetic data → Run workflow
   (tick *full* the very first time if `data/field_history.js` is missing).

## Local rebuild

```
VIRES_TOKEN=... python3 scripts/build_data.py          # weekly refresh
VIRES_TOKEN=... python3 scripts/build_data.py --full   # everything
```
