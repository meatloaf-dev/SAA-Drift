"""Regenerates the field-intensity/declination grids and the close-zoom fine grid
from ESA's VirES API (vires.services) -- no local spherical-harmonic math, no
coefficient files, stdlib-only (urllib + json). Requires VIRES_TOKEN in the
environment. Run from the repo root: python3 scripts/build_data.py

Coordinate frames: the grids are on geographic (geodetic, WGS84) lat/lon at
heights above the ellipsoid. VirES evaluates models in geocentric spherical
coordinates and returns B in the geocentric NEC frame, so every point is
converted geodetic -> geocentric before the call and the returned vector is
rotated back into the local geodetic frame. Skipping this is a 50-350 nT
latitude-dependent error in F and ~0.1 deg in declination.
"""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

VIRES_URL = "https://vires.services/ows"


def normalize_token(raw):
    """Accept the bare token, or a pasted 'Authorization: Bearer <token>' header line."""
    t = (raw or "").strip().strip("'\"")
    for prefix in ("authorization:", "bearer"):
        if t.lower().startswith(prefix):
            t = t[len(prefix):].strip().strip("'\"")
    return t


TOKEN = normalize_token(os.environ.get("VIRES_TOKEN"))
if not TOKEN:
    sys.exit("VIRES_TOKEN is not set")
if len(TOKEN) < 16 or any(c.isspace() for c in TOKEN):
    sys.exit(f"VIRES_TOKEN does not look like a token (length {len(TOKEN)}) -- paste only the token value")

SAT_ALT_KM = 460.0     # height above the WGS84 ellipsoid (Swarm-like orbit)
GROUND_ALT_KM = 0.0

WGS84_A = 6378.137
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)

LAT_STEP, LON_STEP = 4.0, 4.0
LATS = [round(-88 + LAT_STEP * i, 1) for i in range(int(176 / LAT_STEP) + 1)]
LONS = [round(-180 + LON_STEP * i, 1) for i in range(int(360 / LON_STEP))]

EPOCHS_IGRF = list(range(1900, 1996, 5))
EPOCHS_MCO = list(range(1997, 2027, 1))
EPOCHS_PRED = list(range(2027, 2031, 1))
EPOCHS = EPOCHS_IGRF + EPOCHS_MCO + EPOCHS_PRED
SOURCE_OF = {y: "igrf14" for y in EPOCHS_IGRF}
SOURCE_OF.update({y: "mco_chaos" for y in EPOCHS_MCO})
SOURCE_OF.update({y: "igrf14_predicted" for y in EPOCHS_PRED})
MODEL_OF = {"igrf14": "IGRF", "mco_chaos": "MCO_SHA_2X", "igrf14_predicted": "IGRF"}

# the close-zoom layer: core field + static (crustal/lithospheric) field, internal sources
# only -- deliberately no magnetospheric (MMA) terms, which vary hour to hour and would
# stamp one instant's ring-current state into a map that is labelled by epoch
FINE_MODEL = "CHAOS-Core,CHAOS-Static"


def geod2geoc(lat_deg, h_km):
    """Geodetic latitude + height above WGS84 -> geocentric latitude (deg), radius (m)."""
    la = math.radians(lat_deg)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(la) ** 2)
    x = (n + h_km) * math.cos(la)
    z = (n * (1 - WGS84_E2) + h_km) * math.sin(la)
    return math.degrees(math.atan2(z, x)), math.hypot(x, z) * 1000.0


def to_geodetic_frame(b_nec, lat_gd, lat_gc):
    """Rotate a geocentric-NEC vector into the local geodetic NEC frame (east unchanged)."""
    d = math.radians(lat_gd - lat_gc)
    n, e, c = b_nec
    return (n * math.cos(d) + c * math.sin(d), e, c * math.cos(d) - n * math.sin(d))


_last_request_t = [0.0]
_MIN_INTERVAL = 0.4  # seconds between requests -- be a polite client of a shared public API


def vires_eval(model_ids, timestamps, lats_gc, lons, radii_m, retries=12):
    wait_for_slot = _MIN_INTERVAL - (time.time() - _last_request_t[0])
    if wait_for_slot > 0:
        time.sleep(wait_for_slot)
    _last_request_t[0] = time.time()
    payload = {"Timestamp": timestamps, "Latitude": lats_gc, "Longitude": lons, "Radius": radii_m}
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wps:Execute xmlns:wps="http://www.opengis.net/wps/1.0.0" xmlns:ows="http://www.opengis.net/ows/1.1" service="WPS" version="1.0.0">
  <ows:Identifier>vires:eval_model_at_time_and_location</ows:Identifier>
  <wps:DataInputs>
    <wps:Input><ows:Identifier>model_ids</ows:Identifier><wps:Data><wps:LiteralData>{model_ids}</wps:LiteralData></wps:Data></wps:Input>
    <wps:Input><ows:Identifier>input</ows:Identifier><wps:Data><wps:ComplexData mimeType="application/json"><![CDATA[{json.dumps(payload)}]]></wps:ComplexData></wps:Data></wps:Input>
  </wps:DataInputs>
  <wps:ResponseForm><wps:RawDataOutput mimeType="application/json"><ows:Identifier>output</ows:Identifier></wps:RawDataOutput></wps:ResponseForm>
</wps:Execute>"""
    req = urllib.request.Request(
        VIRES_URL, data=xml.encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/xml"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                out = json.loads(resp.read())
            keys = [k for k in out if k.startswith("B_NEC_")]
            # a composite request (e.g. core + static) comes back as one vector per model; sum them
            summed = [list(v) for v in out[keys[0]]]
            for k in keys[1:]:
                for i, v in enumerate(out[k]):
                    summed[i][0] += v[0]; summed[i][1] += v[1]; summed[i][2] += v[2]
            return summed
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            if attempt == retries - 1:
                raise
            wait = min(60, 2 ** (attempt + 1))
            print(f"  retry {attempt+1}/{retries-1} after {e}; waiting {wait}s", file=sys.stderr)
            time.sleep(wait)


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def eval_grid(model_ids, date_iso, lat_gd_flat, lon_flat, h_km, chunk_size=4500):
    """Evaluate at geodetic (lat, lon, h); returns geodetic-frame (N, E, C) per point."""
    out = []
    for lat_c, lon_c in zip(chunks(lat_gd_flat, chunk_size), chunks(lon_flat, chunk_size)):
        conv = [geod2geoc(la, h_km) for la in lat_c]
        b = vires_eval(model_ids, [date_iso] * len(lat_c), [c[0] for c in conv], lon_c, [c[1] for c in conv])
        out.extend(to_geodetic_frame(v, la, c[0]) for v, la, c in zip(b, lat_c, conv))
    return out


def f_and_d(b_nec_list):
    F = [math.sqrt(x * x + y * y + z * z) for (x, y, z) in b_nec_list]
    D = [math.degrees(math.atan2(y, x)) for (x, y, z) in b_nec_list]
    return F, D


def epoch_decimal(y):
    return y + 0.5 if SOURCE_OF[y] == "mco_chaos" else float(y)


def decimal_to_iso(t):
    year = int(t)
    day = int((t - year) * 365.25)
    # epoch dates land on Jan 1 / Jul 1 exactly, but clamped validity bounds can fall anywhere
    base = time.mktime((year, 1, 1, 0, 0, 0, 0, 0, 0)) + day * 86400
    return time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(base))


def epoch_date(y):
    return decimal_to_iso(epoch_decimal(y))


# evaluating outside a model's validity window returns NaN, so the +/-1 yr secular-variation
# samples are clamped to it (bounds kept a few days inside the published limits)
VALIDITY = {"IGRF": (1900.0, 2030.0), "MCO_SHA_2X": (1997.12, 2027.08)}


def sv_window(y):
    """(t_lo, t_hi) in decimal years, one year either side of the epoch, clamped to validity."""
    lo, hi = VALIDITY[MODEL_OF[SOURCE_OF[y]]]
    t = epoch_decimal(y)
    return max(lo, t - 1.0), min(hi, t + 1.0)


def parabolic_min(f_minus, f0, f_plus, step):
    """Sub-grid vertex of the parabola through three samples; returns (offset, value)."""
    denom = f_minus - 2 * f0 + f_plus
    if denom <= 0:
        return 0.0, f0
    off = step * (f_minus - f_plus) / (2 * denom)
    off = max(-step, min(step, off))
    return off, f0 - (f_minus - f_plus) ** 2 / (8 * denom)


def refine_saa_minimum(model_id, date_iso, lon0, lat0, step=0.5, half=8.0):
    """Locate the field minimum to sub-degree precision around a coarse-grid guess.
    Evaluates a local fine window, then parabola-fits the minimum's neighbours."""
    lats = [max(-88.0, min(88.0, lat0 + step * i)) for i in range(int(-half / step), int(half / step) + 1)]
    lons = [((lon0 + step * i + 180) % 360) - 180 for i in range(int(-half / step), int(half / step) + 1)]
    nlon = len(lons)
    lat_flat = [la for la in lats for _ in lons]
    lon_flat = [lo for _ in lats for lo in lons]
    F, _ = f_and_d(eval_grid(model_id, date_iso, lat_flat, lon_flat, SAT_ALT_KM))
    k = min(range(len(F)), key=lambda i: F[i])
    li, lo = divmod(k, nlon)
    lat_c, lon_c, f_c = lats[li], lons[lo], F[k]
    dlat = dlon = 0.0
    f_best = f_c
    if 0 < li < len(lats) - 1:
        dlat, f_lat = parabolic_min(F[k - nlon], f_c, F[k + nlon], step)
        f_best = min(f_best, f_lat)
    if 0 < lo < nlon - 1:
        dlon, f_lon = parabolic_min(F[k - 1], f_c, F[k + 1], step)
        f_best = min(f_best, f_lon)
    return [round(lon_c + dlon, 2), round(lat_c + dlat, 2), round(f_best, 0)]


def build_main_grid():
    lat_flat = [la for la in LATS for _ in LONS]
    lon_flat = [lo for _ in LATS for lo in LONS]
    n = len(lat_flat)
    print(f"main grid: {n} points x {len(EPOCHS)} epochs")

    F_by_epoch, D_by_epoch, D0_by_epoch, saa_center = {}, {}, {}, {}
    for y in EPOCHS:
        source = SOURCE_OF[y]
        model_id = MODEL_OF[source]
        date_iso = epoch_date(y)
        t0 = time.time()
        b_sat = eval_grid(model_id, date_iso, lat_flat, lon_flat, SAT_ALT_KM)
        b_gnd = eval_grid(model_id, date_iso, lat_flat, lon_flat, GROUND_ALT_KM)
        F, D = f_and_d(b_sat)
        _, D0 = f_and_d(b_gnd)
        F_by_epoch[y] = [round(v, 0) for v in F]
        D_by_epoch[y] = [round(v, 2) for v in D]
        D0_by_epoch[y] = [round(v, 2) for v in D0]
        k = min(range(n), key=lambda i: F[i])
        saa_center[str(y)] = refine_saa_minimum(model_id, date_iso, lon_flat[k], lat_flat[k])
        print(f"  {y} ({source}, {model_id}) {time.time()-t0:.1f}s  "
              f"coarse Fmin={F[k]:.0f} @ {lon_flat[k]},{lat_flat[k]}  refined -> {saa_center[str(y)]}")

    # secular variation dF/dt (nT/yr) at the satellite altitude, always from a single model:
    # a centred difference across neighbouring epochs is used only when both neighbours are
    # one year away and from the same model (then it is exactly the model at y+-1); otherwise
    # the model is evaluated at y+-1 directly, so no estimate straddles the IGRF/CHAOS joins
    # or averages over a 10-year window
    sv_by_epoch = {}
    for i, y in enumerate(EPOCHS):
        src = SOURCE_OF[y]
        prev_ok = i > 0 and EPOCHS[i - 1] == y - 1 and SOURCE_OF[EPOCHS[i - 1]] == src
        next_ok = i < len(EPOCHS) - 1 and EPOCHS[i + 1] == y + 1 and SOURCE_OF[EPOCHS[i + 1]] == src
        if prev_ok and next_ok:
            F_prev, F_next = F_by_epoch[EPOCHS[i - 1]], F_by_epoch[EPOCHS[i + 1]]
            dt = 2.0
        else:
            model_id = MODEL_OF[src]
            t_lo, t_hi = sv_window(y)
            F_prev, _ = f_and_d(eval_grid(model_id, decimal_to_iso(t_lo), lat_flat, lon_flat, SAT_ALT_KM))
            F_next, _ = f_and_d(eval_grid(model_id, decimal_to_iso(t_hi), lat_flat, lon_flat, SAT_ALT_KM))
            dt = t_hi - t_lo
            print(f"  dF/dt {y}: evaluated {model_id} at {t_lo:.2f} and {t_hi:.2f}")
        sv = [(b - a) / dt for a, b in zip(F_prev, F_next)]
        if any(math.isnan(v) for v in sv) or any(math.isnan(v) for v in F_by_epoch[y]):
            sys.exit(f"NaN in results for {y} -- a model was evaluated outside its validity window")
        sv_by_epoch[y] = [round(v, 2) for v in sv]

    return {
        "meta": {
            "lat_step": LAT_STEP, "lon_step": LON_STEP,
            "lats": LATS, "lons": LONS,
            "sat_alt_km": SAT_ALT_KM, "epochs": EPOCHS,
            "frame": "geodetic WGS84 lat/lon; heights above the ellipsoid; NEC components in the local geodetic frame",
            "source_of": {str(y): SOURCE_OF[y] for y in EPOCHS},
            "models": {
                "igrf14": "IGRF-14 (IAGA / NOAA NCEI, public reference model) -- via VirES (vires.services)",
                "mco_chaos": "ESA Swarm MCO_SHA_2X core-field model (CHAOS-Core, degree <=20) -- live via VirES (vires.services), refreshed on a schedule",
                "igrf14_predicted": "IGRF-14 predictive secular-variation segment, 2025-2030 -- a forecast, not a measurement -- via VirES",
            },
            "saa_center": saa_center,
            "saa_center_note": "field minimum at 460 km, refined to ~0.1 deg from a 0.5 deg local window; [lon, lat, F nT]",
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "F": {str(y): F_by_epoch[y] for y in EPOCHS},
        "D": {str(y): D_by_epoch[y] for y in EPOCHS},
        "D0": {str(y): D0_by_epoch[y] for y in EPOCHS},
        "dFdt": {str(y): sv_by_epoch[y] for y in EPOCHS},
    }


def build_fine_grid(fine_step=0.5):
    n_lat = int(176 / fine_step) + 1
    n_lon = int(360 / fine_step)
    lats = [round(-88 + fine_step * i, 2) for i in range(n_lat)]
    lons = [round(-180 + fine_step * i, 2) for i in range(n_lon)]
    lat_flat = [la for la in lats for _ in lons]
    lon_flat = [lo for _ in lats for lo in lons]
    print(f"fine grid: {len(lat_flat)} points ({FINE_MODEL}, now, ground level)")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t0 = time.time()
    _, D = f_and_d(eval_grid(FINE_MODEL, now, lat_flat, lon_flat, GROUND_ALT_KM, chunk_size=8000))
    print(f"  done in {time.time()-t0:.1f}s")
    return {
        "meta": {
            "lat_step": fine_step, "lon_step": fine_step,
            "lats": lats, "lons": lons,
            "epoch": now,
            "frame": "geodetic WGS84, at the ellipsoid surface",
            "model": "CHAOS core field + CHAOS static (crustal) field to degree 185, internal sources only -- live via VirES",
        },
        "D": [round(v, 2) for v in D],  # only declination is ever read by the client
    }


def write_js(path, var_name, data):
    with open(path, "w") as f:
        f.write(f"window.{var_name}=")
        json.dump(data, f, separators=(",", ":"))
    print(f"wrote {path} ({os.path.getsize(path)} bytes)")


if __name__ == "__main__":
    main = build_main_grid()
    write_js("data/igrf_data.js", "IGRF_DATA", main)
    fine = build_fine_grid()
    write_js("data/chaos_fine_data.js", "CHAOS_FINE_DATA", fine)
