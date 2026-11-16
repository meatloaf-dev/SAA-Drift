"""Regenerates the atlas data from ESA's VirES API (vires.services) -- no local
spherical-harmonic math, no coefficient files, stdlib-only. Requires VIRES_TOKEN.

    python3 scripts/build_data.py          # weekly: only what can change (~4 min)
    python3 scripts/build_data.py --full   # everything, incl. the immutable history (~20 min)

Outputs (data/):
  field_history.js   epochs whose values cannot change between refreshes: IGRF-14
                     1900-1995, the IGRF-14 forecast 2027-2030, and CHAOS epochs
                     older than the last three years. Written by --full only, or
                     automatically when VirES reports a new CHAOS/IGRF source version.
  field_current.js   the last three CHAOS epochs (the ones a model update revises)
  chaos_fine_data.js close-zoom declination, CHAOS core+crustal, evaluated now
  pass_data.js       the most recent full day of real Swarm-A measurements over the SAA

Coordinate frames: grids are geographic (geodetic WGS84) at heights above the
ellipsoid; VirES works in geocentric spherical coordinates, so every point is
converted before the call and the returned NEC vector rotated back. Satellite
positions come back geocentric and are converted the other way for plotting.
"""
import datetime as dt
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

SAT_ALT_KM = 460.0
GROUND_ALT_KM = 0.0
WGS84_A = 6378.137
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)

LAT_STEP, LON_STEP = 4.0, 4.0
LATS = [round(-88 + LAT_STEP * i, 1) for i in range(int(176 / LAT_STEP) + 1)]
LONS = [round(-180 + LON_STEP * i, 1) for i in range(int(360 / LON_STEP))]

THIS_YEAR = dt.datetime.utcnow().year
EPOCHS_IGRF = list(range(1900, 1996, 5))
EPOCHS_MCO = list(range(1997, THIS_YEAR + 1))
EPOCHS_PRED = list(range(THIS_YEAR + 1, 2031))
EPOCHS = EPOCHS_IGRF + EPOCHS_MCO + EPOCHS_PRED
SOURCE_OF = {y: "igrf14" for y in EPOCHS_IGRF}
SOURCE_OF.update({y: "mco_chaos" for y in EPOCHS_MCO})
SOURCE_OF.update({y: "igrf14_predicted" for y in EPOCHS_PRED})
MODEL_OF = {"igrf14": "IGRF", "mco_chaos": "MCO_SHA_2X", "igrf14_predicted": "IGRF"}
# the CHAOS epochs a model update can still revise -- everything else is immutable
CURRENT_EPOCHS = [y for y in EPOCHS_MCO if y >= THIS_YEAR - 2]
HISTORY_EPOCHS = [y for y in EPOCHS if y not in CURRENT_EPOCHS]

FINE_MODEL = "CHAOS-Core,CHAOS-Static"   # internal sources only, no hour-to-hour magnetosphere
SAA_BOX = (-60.0, 10.0, -100.0, 20.0)    # lat min, lat max, lon min, lon max for the satellite passes

MODELS_TEXT = {
    "igrf14": "IGRF-14 (IAGA / NOAA NCEI, public reference model) -- via VirES (vires.services)",
    "mco_chaos": "ESA Swarm MCO_SHA_2X core-field model (CHAOS-Core, degree <=20) -- live via VirES (vires.services), refreshed on a schedule",
    "igrf14_predicted": "IGRF-14 predictive secular-variation segment, 2025-2030 -- a forecast, not a measurement -- via VirES",
}
MODEL_SOURCES = {}  # model id -> list of VirES source files, recorded from responses


# ---------------------------------------------------------------- geodesy
def geod2geoc(lat_deg, h_km):
    la = math.radians(lat_deg)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(la) ** 2)
    x = (n + h_km) * math.cos(la)
    z = (n * (1 - WGS84_E2) + h_km) * math.sin(la)
    return math.degrees(math.atan2(z, x)), math.hypot(x, z) * 1000.0


def geoc2geod(lat_gc_deg, radius_m):
    """Geocentric latitude + radius -> geodetic latitude, height above WGS84 (km)."""
    r = radius_m / 1000.0
    p = r * math.cos(math.radians(lat_gc_deg))
    z = r * math.sin(math.radians(lat_gc_deg))
    lat = math.atan2(z, p * (1 - WGS84_E2))
    for _ in range(5):
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - WGS84_E2 * n / (n + h)))
    return math.degrees(lat), h


def to_geodetic_frame(b_nec, lat_gd, lat_gc):
    d = math.radians(lat_gd - lat_gc)
    n, e, c = b_nec
    return (n * math.cos(d) + c * math.sin(d), e, c * math.cos(d) - n * math.sin(d))


# ---------------------------------------------------------------- VirES
_last_request_t = [0.0]
_MIN_INTERVAL = 0.4


def wps_execute(identifier, inputs, mime, retries=12):
    """POST a WPS Execute; inputs are {name: literal string}. Returns the raw response body."""
    wait_for_slot = _MIN_INTERVAL - (time.time() - _last_request_t[0])
    if wait_for_slot > 0:
        time.sleep(wait_for_slot)
    _last_request_t[0] = time.time()

    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    parts = []
    for k, v in inputs.items():
        if k == "input":
            parts.append(f'<wps:Input><ows:Identifier>input</ows:Identifier><wps:Data><wps:ComplexData mimeType="application/json"><![CDATA[{v}]]></wps:ComplexData></wps:Data></wps:Input>')
        else:
            parts.append(f'<wps:Input><ows:Identifier>{k}</ows:Identifier><wps:Data><wps:LiteralData>{esc(v)}</wps:LiteralData></wps:Data></wps:Input>')
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><wps:Execute xmlns:wps="http://www.opengis.net/wps/1.0.0" '
           f'xmlns:ows="http://www.opengis.net/ows/1.1" service="WPS" version="1.0.0"><ows:Identifier>{identifier}</ows:Identifier>'
           f'<wps:DataInputs>{"".join(parts)}</wps:DataInputs><wps:ResponseForm><wps:RawDataOutput mimeType="{mime}">'
           f'<ows:Identifier>output</ows:Identifier></wps:RawDataOutput></wps:ResponseForm></wps:Execute>')
    req = urllib.request.Request(VIRES_URL, data=xml.encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/xml"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            if attempt == retries - 1:
                raise
            wait = min(60, 2 ** (attempt + 1))
            print(f"  retry {attempt+1}/{retries-1} after {e}; waiting {wait}s", file=sys.stderr)
            time.sleep(wait)


def vires_eval(model_ids, timestamps, lats_gc, lons, radii_m):
    payload = {"Timestamp": timestamps, "Latitude": lats_gc, "Longitude": lons, "Radius": radii_m}
    out = json.loads(wps_execute("vires:eval_model_at_time_and_location",
                                 {"model_ids": model_ids, "input": json.dumps(payload)}, "application/json"))
    for name, info in out.get("__info__", {}).get("models", {}).items():
        MODEL_SOURCES[name] = info.get("sources", [])
    keys = [k for k in out if k.startswith("B_NEC_")]
    summed = [list(v) for v in out[keys[0]]]
    for k in keys[1:]:
        for i, v in enumerate(out[k]):
            summed[i][0] += v[0]; summed[i][1] += v[1]; summed[i][2] += v[2]
    return summed


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def eval_grid(model_ids, date_iso, lat_gd_flat, lon_flat, h_km, chunk_size=4500):
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


# ---------------------------------------------------------------- epochs
def epoch_decimal(y):
    return y + 0.5 if SOURCE_OF[y] == "mco_chaos" else float(y)


def decimal_to_iso(t):
    year = int(t)
    day = int((t - year) * 365.25)
    base = time.mktime((year, 1, 1, 0, 0, 0, 0, 0, 0)) + day * 86400
    return time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(base))


def epoch_date(y):
    return decimal_to_iso(epoch_decimal(y))


VALIDITY = {"IGRF": (1900.0, 2030.0), "MCO_SHA_2X": (1997.12, 2027.08)}


def sv_window(y):
    lo, hi = VALIDITY[MODEL_OF[SOURCE_OF[y]]]
    t = epoch_decimal(y)
    return max(lo, t - 1.0), min(hi, t + 1.0)


def parabolic_min(f_minus, f0, f_plus, step):
    denom = f_minus - 2 * f0 + f_plus
    if denom <= 0:
        return 0.0, f0
    off = step * (f_minus - f_plus) / (2 * denom)
    off = max(-step, min(step, off))
    return off, f0 - (f_minus - f_plus) ** 2 / (8 * denom)


def refine_saa_minimum(model_id, date_iso, lon0, lat0, step=0.5, half=8.0):
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


def build_epochs(epochs, known_F):
    """Evaluate the given epochs. known_F supplies F for neighbouring epochs built elsewhere
    (so a centred secular-variation difference can be used across the history/current split)."""
    lat_flat = [la for la in LATS for _ in LONS]
    lon_flat = [lo for _ in LATS for lo in LONS]
    n = len(lat_flat)
    print(f"grid: {n} points x {len(epochs)} epochs {epochs[0]}..{epochs[-1]}")
    F_by, D_by, D0_by, saa = {}, {}, {}, {}
    for y in epochs:
        source = SOURCE_OF[y]
        model_id = MODEL_OF[source]
        date_iso = epoch_date(y)
        t0 = time.time()
        b_sat = eval_grid(model_id, date_iso, lat_flat, lon_flat, SAT_ALT_KM)
        b_gnd = eval_grid(model_id, date_iso, lat_flat, lon_flat, GROUND_ALT_KM)
        F, D = f_and_d(b_sat)
        _, D0 = f_and_d(b_gnd)
        F_by[y] = [round(v, 0) for v in F]
        D_by[y] = [round(v, 2) for v in D]
        D0_by[y] = [round(v, 2) for v in D0]
        k = min(range(n), key=lambda i: F[i])
        saa[str(y)] = refine_saa_minimum(model_id, date_iso, lon_flat[k], lat_flat[k])
        print(f"  {y} ({source}) {time.time()-t0:.1f}s  SAA {saa[str(y)]}")

    all_F = dict(known_F); all_F.update(F_by)
    sv_by = {}
    for y in epochs:
        src = SOURCE_OF[y]
        prev_ok = (y - 1) in all_F and SOURCE_OF.get(y - 1) == src
        next_ok = (y + 1) in all_F and SOURCE_OF.get(y + 1) == src
        if prev_ok and next_ok:
            F_prev, F_next, dt_y = all_F[y - 1], all_F[y + 1], 2.0
        else:
            model_id = MODEL_OF[src]
            t_lo, t_hi = sv_window(y)
            F_prev, _ = f_and_d(eval_grid(model_id, decimal_to_iso(t_lo), lat_flat, lon_flat, SAT_ALT_KM))
            F_next, _ = f_and_d(eval_grid(model_id, decimal_to_iso(t_hi), lat_flat, lon_flat, SAT_ALT_KM))
            dt_y = t_hi - t_lo
            print(f"  dF/dt {y}: evaluated {model_id} at {t_lo:.2f} and {t_hi:.2f}")
        sv = [(b - a) / dt_y for a, b in zip(F_prev, F_next)]
        if any(math.isnan(v) for v in sv) or any(math.isnan(v) for v in F_by[y]):
            sys.exit(f"NaN in results for {y} -- a model was evaluated outside its validity window")
        sv_by[y] = [round(v, 2) for v in sv]

    return {
        "meta": {
            "lat_step": LAT_STEP, "lon_step": LON_STEP, "lats": LATS, "lons": LONS,
            "sat_alt_km": SAT_ALT_KM, "epochs": epochs,
            "frame": "geodetic WGS84 lat/lon; heights above the ellipsoid; NEC components in the local geodetic frame",
            "source_of": {str(y): SOURCE_OF[y] for y in epochs},
            "models": MODELS_TEXT,
            "saa_center": saa,
            "saa_center_note": "field minimum at 460 km, refined to ~0.1 deg from a 0.5 deg local window; [lon, lat, F nT]",
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "F": {str(y): F_by[y] for y in epochs},
        "D": {str(y): D_by[y] for y in epochs},
        "D0": {str(y): D0_by[y] for y in epochs},
        "dFdt": {str(y): sv_by[y] for y in epochs},
    }


# ---------------------------------------------------------------- fine grid
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
        "meta": {"lat_step": fine_step, "lon_step": fine_step, "lats": lats, "lons": lons, "epoch": now,
                 "frame": "geodetic WGS84, at the ellipsoid surface",
                 "model": "CHAOS core field + CHAOS static (crustal) field to degree 185, internal sources only -- live via VirES"},
        "D": [round(v, 2) for v in D],
    }


# ---------------------------------------------------------------- satellite passes
def build_passes(days_back=14, sampling="PT10S", gap_s=120):
    """Most recent full UTC day of Swarm-A 1 Hz MAGA_LR_1B measurements inside the SAA box,
    split into passes at gaps in the time series. Positions converted to geodetic for plotting."""
    end = dt.datetime.utcnow().replace(microsecond=0)
    begin = end - dt.timedelta(days=days_back)
    lat0, lat1, lon0, lon1 = SAA_BOX
    csv = wps_execute("vires:fetch_filtered_data", {
        "collection_ids": json.dumps({"MAGA_LR_1B": ["SW_OPER_MAGA_LR_1B"]}),
        "begin_time": begin.isoformat() + "Z", "end_time": end.isoformat() + "Z",
        "sampling_step": sampling,
        "filters": f"Latitude >= {lat0} AND Latitude <= {lat1} AND Longitude >= {lon0} AND Longitude <= {lon1}",
        "variables": "Latitude,Longitude,Radius,F",
    }, "text/csv").decode()
    lines = csv.strip().splitlines()   # CRLF-safe: a bare split("\n") leaves "\r" on the last column
    cols = [c.strip() for c in lines[0].split(",")]
    ci = {c: i for i, c in enumerate(cols)}
    rows = [[v.strip() for v in l.split(",")] for l in lines[1:] if l.strip()]
    if not rows:
        sys.exit("no Swarm-A measurements returned for the last two weeks")
    # last UTC day that has data ending at/after 23:00 -> a complete day, not a partial one
    days = sorted({r[ci["Timestamp"]][:10] for r in rows})
    day = days[-1]
    if max(r[ci["Timestamp"]] for r in rows if r[ci["Timestamp"]].startswith(day))[11:13] < "23" and len(days) > 1:
        day = days[-2]
    passes, cur, prev_t = [], [], None
    for r in rows:
        ts = r[ci["Timestamp"]]
        if not ts.startswith(day):
            continue
        t = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        if prev_t is not None and (t - prev_t).total_seconds() > gap_s:
            passes.append(cur); cur = []
        lat_gd, _ = geoc2geod(float(r[ci["Latitude"]]), float(r[ci["Radius"]]))
        cur.append([ts[:19], round(lat_gd, 3), round(float(r[ci["Longitude"]]), 3), round(float(r[ci["F"]]), 1)])
        prev_t = t
    if cur:
        passes.append(cur)
    passes = [p for p in passes if len(p) >= 6]
    print(f"satellite passes: {day}, {len(passes)} passes, {sum(len(p) for p in passes)} points (latest data day available: {days[-1]})")
    return {"satellite": "Swarm-A", "date": day, "product": "SW_OPER_MAGA_LR_1B", "sampling": sampling,
            "box": {"lat": [lat0, lat1], "lon": [lon0, lon1]}, "frame": "geodetic WGS84",
            "fetched": end.isoformat() + "Z", "passes": passes}


# ---------------------------------------------------------------- io
def write_js(path, var_name, data):
    with open(path, "w") as f:
        f.write(f"window.{var_name}=")
        json.dump(data, f, separators=(",", ":"))
    print(f"wrote {path} ({os.path.getsize(path)} bytes)")


def read_js(path):
    try:
        t = open(path).read()
        return json.loads(t[t.index("{"):t.rindex("}") + 1])
    except (OSError, ValueError):
        return None


def current_model_versions():
    """Cheap probe: one point per model records the VirES source files in MODEL_SOURCES."""
    for model_id in ("IGRF", "MCO_SHA_2X"):
        vires_eval(model_id, ["2020-01-01T00:00:00Z"], [0.0], [0.0], [6371200.0])
    return {m: MODEL_SOURCES.get(m, []) for m in ("IGRF", "MCO_SHA_2X")}


if __name__ == "__main__":
    full = "--full" in sys.argv
    history = read_js("data/field_history.js")
    versions = current_model_versions()
    if history and not full:
        if history["meta"].get("epochs") != HISTORY_EPOCHS:
            print("history epoch set changed (new year) -> full rebuild"); full = True
        elif history["meta"].get("model_versions") != versions:
            print("VirES reports a new model version -> full rebuild"); full = True
    elif not history:
        full = True

    if full:
        hist = build_epochs(HISTORY_EPOCHS, {})
        hist["meta"]["model_versions"] = versions
        write_js("data/field_history.js", "FIELD_HISTORY", hist)
        history = hist

    known_F = {int(y): v for y, v in history["F"].items()}
    cur = build_epochs(CURRENT_EPOCHS, known_F)
    cur["meta"]["model_versions"] = versions
    write_js("data/field_current.js", "FIELD_CURRENT", cur)
    write_js("data/chaos_fine_data.js", "CHAOS_FINE_DATA", build_fine_grid())
    write_js("data/pass_data.js", "PASS_DATA", build_passes())
