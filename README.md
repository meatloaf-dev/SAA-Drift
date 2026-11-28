# Geomagnetic Drift Atlas

An interactive 3D globe of Earth's magnetic field: the South Atlantic Anomaly,
field intensity, declination and inclination, isodynamic / isogonic / isoclinic
lines at any zoom, and the drift of magnetic north, 1900–2030. Static site, no
server, no build step for the page itself.

Three magnetic layers, which are easy to confuse:

| layer | contours | run | zero contour |
|---|---|---|---|
| Field intensity (F) | isodynamic, nT at 460 km | — | — |
| Declination (D) | isogonic, degrees | north–south | agonic line |
| Inclination (I) | isoclinic, degrees of dip | **east–west** | dip equator |

The isoclinic family can also be overlaid on either other layer (`◇ Isoclinic
lines`), so isogonic and isoclinic read together as on a paper chart.

Optional overlays: Smithsonian GVP volcanoes, the UVB latitude bounds (solar
geometry only — no cloud, ozone, aerosol or altitude data), the ranked Tier 1–2
sites, and a CDC state mortality choropleth following the epoch slider: heart
disease, cancer, diabetes and Alzheimer's across 1999–2022, and all causes
across 1999–2026.

States are painted as a percentage of that year's national rate rather than an
absolute rate, because the ratio is scale-free and three different sources have
to read as one picture:

| years | source | measure |
|---|---|---|
| 1999–2017 | `bi63-dtpu` | age-adjusted rates by state (CDC froze this series at 2017) |
| 2018–2022 | `3yf8-kanr`, `muzy-jte6` | crude rates from weekly provisional counts (frozen Sept 2023) |
| 2023–2026 | `r8kw-7aab` | all causes only — still updated weekly |

The ratio makes the scales compatible; what it leaves is a level offset per
state, which is measured across the years two sources both cover and divided
out, so the map does not step at the year the source changes. Each year is then
rescaled to put the national level back at 100%, since the hue ramp is centred
there. Measured: a year pair crossing a join moves states by 1.6–4.6pp on
average against 1.6–5.8pp for an ordinary year pair — i.e. the joins are no
longer findable by eye. Raw agreement in the overlap, before that correction,
is r = 0.69–0.92.

Nothing is drawn outside those spans — there is no state breakdown before 1999
— and for heart disease and cancer the legend falls back to the national series,
which reaches back to 1900.

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
| `data/volcano_data.js` | Smithsonian GVP Holocene volcano list (name, position, last eruption) | weekly |
| `data/tier_data.js` | ranked sites from the source document, with the Mag and Dec axes re-scored from the live field | weekly |
| `data/pass_data.js` | the most recent complete day of real Swarm-A 1 Hz measurements over the anomaly (ESA publishes these ~4 days behind real time) | weekly |
| `data/cdc_data.js` | CDC state mortality; the all-cause series chains onto a file CDC republishes weekly | weekly |
| `pole_data.js`, `country_data.js`, `state_data.js`, `cities_data.js`, `zip_data.js` | static reference data | never |

A weekly run makes ~45 API calls (~4 min); a full rebuild ~230 (~20 min).

A weekly run also rebuilds `data/cdc_data.js` (~40 s, no token). US state lines
(interior boundaries, Census via us-atlas) fade in as you zoom past ~88° of
visible arc; rebuild them with `python3 scripts/build_state_lines.py`.

## Local preview

```
docker compose up      # -> http://localhost:8080
```

nginx serving the repo the way GitHub Pages does (gzip, correct MIME types) but
with caching off, so an edit shows on a plain reload. Any static server works —
`python3 -m http.server` is fine; Docker just makes it match production.

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
- The "Site factors" tab explains what each layer measures.
- Ranked-site scores are not transcribed. The two axes the source defines with
  numeric thresholds (Mag, Dec) are recomputed each refresh from the live field
  at surface height, matching the scale its bands use; 9 of 17 sites currently
  differ from the published score. Its own agonic line is a fixed polyline
  matching the line as it stood around 1940 — ~13.6 deg east of today's — so it
  is not used for anything.

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
