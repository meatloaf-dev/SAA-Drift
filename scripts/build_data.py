"""Regenerates the field-intensity/declination grids and the close-zoom fine grid
from ESA's VirES API (vires.services) -- no local spherical-harmonic math, no
coefficient files, stdlib-only (urllib + json). Requires VIRES_TOKEN in the
environment. Run from the repo root: python3 scripts/build_data.py
"""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

VIRES_URL = "https://vires.services/ows"
TOKEN = os.environ.get("VIRES_TOKEN")
if not TOKEN:
    sys.exit("VIRES_TOKEN is not set")

EARTH_R_KM = 6371.2
SAT_ALT_KM = 460.0
SAT_RADIUS_M = (EARTH_R_KM + SAT_ALT_KM) * 1000.0
GROUND_RADIUS_M = EARTH_R_KM * 1000.0

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


_last_request_t = [0.0]
_MIN_INTERVAL = 0.4  # seconds between requests -- the run that failed fired ~140 requests back
                      # to back and got a run of 502s; pacing them is both kinder to a shared
                      # public API and likely fixes a self-inflicted overload, not just a fluke


def vires_eval(model_id, timestamps, lats, lons, radii, retries=8):
    wait_for_slot = _MIN_INTERVAL - (time.time() - _last_request_t[0])
    if wait_for_slot > 0:
        time.sleep(wait_for_slot)
    _last_request_t[0] = time.time()
    payload = {"Timestamp": timestamps, "Latitude": lats, "Longitude": lons, "Radius": radii}
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wps:Execute xmlns:wps="http://www.opengis.net/wps/1.0.0" xmlns:ows="http://www.opengis.net/ows/1.1" service="WPS" version="1.0.0">
  <ows:Identifier>vires:eval_model_at_time_and_location</ows:Identifier>
  <wps:DataInputs>
    <wps:Input><ows:Identifier>model_ids</ows:Identifier><wps:Data><wps:LiteralData>{model_id}</wps:LiteralData></wps:Data></wps:Input>
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
            return out[f"B_NEC_{model_id}"]
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            if attempt == retries - 1:
                raise
            # exponential backoff, capped -- VirES has occasional brief 502/503 blips
            # (an actual outage, not a bad request) that can outlast a short retry window
            wait = min(60, 2 ** (attempt + 1))
            print(f"  retry {attempt+1}/{retries-1} after {e}; waiting {wait}s", file=sys.stderr)
            time.sleep(wait)


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def eval_grid(model_id, date_iso, lat_flat, lon_flat, radius_m, chunk_size=4500):
    n = len(lat_flat)
    out = [None] * n
    radii = [radius_m] * chunk_size
    times = [date_iso] * chunk_size
    idx0 = 0
    for lat_c, lon_c in zip(chunks(lat_flat, chunk_size), chunks(lon_flat, chunk_size)):
        m = len(lat_c)
        b_nec = vires_eval(model_id, times[:m], lat_c, lon_c, radii[:m])
        out[idx0:idx0 + m] = b_nec
        idx0 += m
    return out


def f_and_d(b_nec_list):
    F, D = [], []
    for (x, y, z) in b_nec_list:
        F.append(math.sqrt(x * x + y * y + z * z))
        D.append(math.degrees(math.atan2(y, x)))
    return F, D


def build_main_grid():
    lat_flat, lon_flat = [], []
    for la in LATS:
        for lo in LONS:
            lat_flat.append(la)
            lon_flat.append(lo)
    n = len(lat_flat)
    print(f"main grid: {n} points x {len(EPOCHS)} epochs")

    F_by_epoch, D_by_epoch, D0_by_epoch = {}, {}, {}
    for y in EPOCHS:
        source = SOURCE_OF[y]
        model_id = MODEL_OF[source]
        date_iso = f"{y}-07-01T00:00:00Z" if source == "mco_chaos" else f"{y}-01-01T00:00:00Z"
        t0 = time.time()
        b_sat = eval_grid(model_id, date_iso, lat_flat, lon_flat, SAT_RADIUS_M)
        b_gnd = eval_grid(model_id, date_iso, lat_flat, lon_flat, GROUND_RADIUS_M)
        F, D = f_and_d(b_sat)
        _, D0 = f_and_d(b_gnd)
        F_by_epoch[y] = [round(v, 0) for v in F]
        D_by_epoch[y] = [round(v, 2) for v in D]
        D0_by_epoch[y] = [round(v, 2) for v in D0]
        idx_min = min(range(n), key=lambda i: F[i])
        print(f"  {y} ({source}, {model_id}) {time.time()-t0:.1f}s  "
              f"Fmin={F[idx_min]:.0f} @ {lon_flat[idx_min]},{lat_flat[idx_min]}")

    sv_by_epoch = {}
    for i, y in enumerate(EPOCHS):
        F_this = F_by_epoch[y]
        if i == 0:
            F_next = F_by_epoch[EPOCHS[i + 1]]
            dt_y = EPOCHS[i + 1] - y
            sv = [(b - a) / dt_y for a, b in zip(F_this, F_next)]
        elif i == len(EPOCHS) - 1:
            F_prev = F_by_epoch[EPOCHS[i - 1]]
            dt_y = y - EPOCHS[i - 1]
            sv = [(a - b) / dt_y for a, b in zip(F_prev, F_this)]
        else:
            F_next = F_by_epoch[EPOCHS[i + 1]]
            F_prev = F_by_epoch[EPOCHS[i - 1]]
            dt_y = EPOCHS[i + 1] - EPOCHS[i - 1]
            sv = [(b - a) / dt_y for a, b in zip(F_prev, F_next)]
        sv_by_epoch[y] = [round(v, 2) for v in sv]

    saa_center = {}
    for y in EPOCHS:
        F = F_by_epoch[y]
        idx_min = min(range(n), key=lambda i: F[i])
        saa_center[str(y)] = [round(lon_flat[idx_min], 1), round(lat_flat[idx_min], 1), round(F[idx_min], 0)]

    out = {
        "meta": {
            "lat_step": LAT_STEP, "lon_step": LON_STEP,
            "lats": LATS, "lons": LONS,
            "sat_alt_km": SAT_ALT_KM, "epochs": EPOCHS,
            "source_of": {str(y): SOURCE_OF[y] for y in EPOCHS},
            "models": {
                "igrf14": "IGRF-14 (IAGA / NOAA NCEI, public reference model) -- via VirES (vires.services)",
                "mco_chaos": "ESA Swarm MCO_SHA_2X core-field model (CHAOS-Core, degree <=20) -- live via VirES (vires.services), refreshed on a schedule",
                "igrf14_predicted": "IGRF-14 predictive secular-variation segment, 2025-2030 -- a forecast, not a measurement -- via VirES",
            },
            "saa_center": saa_center,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "F": {str(y): F_by_epoch[y] for y in EPOCHS},
        "D": {str(y): D_by_epoch[y] for y in EPOCHS},
        "D0": {str(y): D0_by_epoch[y] for y in EPOCHS},
        "dFdt": {str(y): sv_by_epoch[y] for y in EPOCHS},
    }
    return out


def build_fine_grid(fine_step=0.5):
    n_lat = int(176 / fine_step) + 1
    n_lon = int(360 / fine_step)
    lats = [round(-88 + fine_step * i, 2) for i in range(n_lat)]
    lons = [round(-180 + fine_step * i, 2) for i in range(n_lon)]
    lat_flat, lon_flat = [], []
    for la in lats:
        for lo in lons:
            lat_flat.append(la)
            lon_flat.append(lo)
    n = len(lat_flat)
    print(f"fine grid: {n} points (CHAOS, now)")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t0 = time.time()
    b_gnd = eval_grid("CHAOS", now, lat_flat, lon_flat, GROUND_RADIUS_M, chunk_size=8000)
    _, D = f_and_d(b_gnd)  # only declination is ever read by the client -- F is dead weight, skip it
    print(f"  done in {time.time()-t0:.1f}s")
    return {
        "meta": {
            "lat_step": fine_step, "lon_step": fine_step,
            "lats": lats, "lons": lons,
            "epoch": now,
            "model": "CHAOS (core + static crustal field to degree 185 + magnetosphere) -- live via VirES",
        },
        "D": [round(v, 2) for v in D],
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
