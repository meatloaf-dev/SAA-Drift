"""Builds data/cdc_data.js -- age-adjusted death rates by US state and year, for the four
causes the atlas offers as a choropleth. Static historical data: run once, not weekly.

    python3 scripts/build_cdc_data.py

Source: CDC/NCHS "Leading Causes of Death: United States" (data.cdc.gov resource bi63-dtpu),
which is the national mortality file aggregated to state-year-cause. Rates are age-adjusted
to the 2000 US standard population -- without that, a state's rate mostly reports how old its
residents are. Coverage is 1999-2017, the span over which ICD-10 coding is consistent; the
ICD-9 to ICD-10 change in 1999 shifted how several causes are coded, so earlier years are not
comparable and are deliberately not included.
"""
import json
import os
import urllib.parse
import urllib.request

API = "https://data.cdc.gov/resource/bi63-dtpu.json"
# National-only, but age-adjusted back to 1900 -- the one CDC series that spans the atlas's
# whole epoch range. No state breakdown exists publicly before 1999, so for earlier epochs the
# page shows this national figure instead of painting a map it has no data for.
API_LONG = "https://data.cdc.gov/resource/6rkc-nb2q.json"
LONG_CAUSES = {"Heart Disease": "heart", "Cancer": "cancer"}
OUT = "data/cdc_data.js"
# the four the atlas exposes, plus the all-cause baseline to read them against
CAUSES = {
    "Heart disease": "heart",
    "Cancer": "cancer",
    "Diabetes": "diabetes",
    "Alzheimer's disease": "alzheimers",
    "All causes": "all",
}
SKIP_STATES = {"United States"}   # the national row, kept out of the per-state map


def fetch_all():
    rows, offset, limit = [], 0, 50000
    while True:
        q = urllib.parse.urlencode({
            "$select": "year,cause_name,state,aadr",
            "$limit": limit, "$offset": offset,
        })
        with urllib.request.urlopen(f"{API}?{q}", timeout=120) as resp:
            batch = json.loads(resp.read())
        rows.extend(batch)
        if len(batch) < limit:
            return rows
        offset += limit


def fetch_long():
    """National age-adjusted rates 1900-2018, for the causes that series covers."""
    out = {}
    with urllib.request.urlopen(f"{API_LONG}?$limit=5000", timeout=120) as resp:
        for r in json.loads(resp.read()):
            key = LONG_CAUSES.get(r.get("leading_causes"))
            if not key:
                continue
            try:
                out.setdefault(key, {})[int(r["year"])] = float(r["age_adjusted_death_rate"])
            except (TypeError, ValueError, KeyError):
                continue
    return out


def main():
    print(f"fetching {API}")
    rows = fetch_all()
    print(f"  {len(rows)} rows")

    years, states = set(), set()
    data = {}        # key -> {state -> {year -> rate}}
    national = {}    # key -> {year -> rate}
    for r in rows:
        key = CAUSES.get(r.get("cause_name"))
        if not key or "aadr" not in r:
            continue
        year, state = int(r["year"]), r["state"]
        try:
            rate = float(r["aadr"])
        except (TypeError, ValueError):
            continue
        if state in SKIP_STATES:
            national.setdefault(key, {})[year] = rate
            continue
        years.add(year)
        states.add(state)
        data.setdefault(key, {}).setdefault(state, {})[year] = rate

    years = sorted(years)
    states = sorted(states)
    print(f"  {len(states)} states, years {years[0]}-{years[-1]}, causes {sorted(data)}")

    # dense arrays indexed by the year list, so the page can look a year up by position
    out_causes = {}
    for key, by_state in data.items():
        series = {s: [by_state.get(s, {}).get(y) for y in years] for s in states}
        vals = [v for s in series.values() for v in s if v is not None]
        out_causes[key] = {
            "label": next(k for k, v in CAUSES.items() if v == key),
            "min": round(min(vals), 1), "max": round(max(vals), 1),
            "national": [national.get(key, {}).get(y) for y in years],
            "states": {s: [None if v is None else round(v, 1) for v in series[s]] for s in series},
        }
        print(f"    {key:11} range {out_causes[key]['min']}-{out_causes[key]['max']} per 100k")

    print(f"fetching {API_LONG} (national, long series)")
    long_series = fetch_long()
    long_out = {}
    for key, by_year in long_series.items():
        yrs = sorted(by_year)
        long_out[key] = {"years": yrs, "rates": [round(by_year[y], 1) for y in yrs]}
        print(f"    {key:11} national {yrs[0]}-{yrs[-1]}, "
              f"{by_year[yrs[0]]:.0f} -> peak {max(by_year.values()):.0f} -> {by_year[yrs[-1]]:.0f} per 100k")

    doc = {
        "national_long": long_out,
        "national_long_source": "CDC/NCHS, Age-adjusted Death Rates for Selected Major Causes of Death (6rkc-nb2q), 1900-2018, United States total",
        "source": "CDC/NCHS, Leading Causes of Death: United States (bi63-dtpu)",
        "url": "https://data.cdc.gov/NCHS/NCHS-Leading-Causes-of-Death-United-States/bi63-dtpu",
        "measure": "age-adjusted deaths per 100,000, standardised to the 2000 US population",
        "note": ("ICD-10 coding, consistent 1999-2017. Mortality, not prevalence. "
                 "Dementia is not coded separately from Alzheimer's disease in this file."),
        "years": years,
        "causes": out_causes,
    }
    with open(OUT, "w") as f:
        f.write("window.CDC_DATA=")
        json.dump(doc, f, separators=(",", ":"))
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()
