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

# The age-adjusted series stops at 2017 (CDC froze it). These weekly provisional files carry
# the same four causes by state with matching ICD definitions, 2014-2019 and 2020-2023, so
# together they extend coverage -- but only as raw counts with no age breakdown. They can
# therefore only become CRUDE rates, which is a different measure and gets its own scale and
# its own colour in the page; the two are never blended.
API_WEEKLY = [
    ("https://data.cdc.gov/resource/3yf8-kanr.json", "jurisdiction_of_occurrence", "weekendingdate"),
    ("https://data.cdc.gov/resource/muzy-jte6.json", "jurisdiction_of_occurrence", "week_ending_date"),
]
WEEKLY_CAUSE_COLS = {
    "heart": "diseases_of_heart_i00_i09",
    "cancer": "malignant_neoplasms_c00_c97",
    "diabetes": "diabetes_mellitus_e10_e14",
    "alzheimers": "alzheimer_disease_g30",
}   # all-cause is spelled differently between the two files and is not used here
MAX_SUPPRESSED_WEEKS = 2   # a couple of withheld weeks shifts an annual total trivially
POP_FILES = [
    "https://www2.census.gov/programs-surveys/popest/datasets/2010-2019/national/totals/nst-est2019-alldata.csv",
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/state/totals/NST-EST2024-ALLDATA.csv",
]
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


def fetch_state_population():
    """State population by year from the Census public estimate files (no API key)."""
    import csv, io
    pop = {}
    for url in POP_FILES:
        with urllib.request.urlopen(url, timeout=120) as resp:
            text = resp.read().decode("utf-8-sig", errors="replace")
        for row in csv.DictReader(io.StringIO(text)):
            name = (row.get("NAME") or "").strip()
            if not name or row.get("STATE") in (None, "00") and name == "United States":
                continue
            for col, val in row.items():
                if col and col.startswith("POPESTIMATE") and val:
                    try:
                        pop.setdefault(name, {})[int(col[-4:])] = int(val)
                    except ValueError:
                        pass
    return pop


# NCHS reports these two separately; Census population does not, so they must be recombined
# or New York's deaths are divided by a population that includes eight million New Yorkers
# whose deaths were counted under a different jurisdiction.
MERGE_JURISDICTIONS = {"New York City": "New York"}


def fetch_weekly_annual():
    """Sum weekly provisional counts into state-year totals per cause, and count how many
    weeks were suppressed: NCHS blanks any weekly cell under 10, which for a rare cause in a
    small state can blank nearly the whole year and read as near-zero deaths."""
    deaths, weeks, suppressed = {}, {}, {}
    cols = ",".join(sorted(set(WEEKLY_CAUSE_COLS.values())))
    for base, state_col, date_col in API_WEEKLY:
        offset = 0
        while True:
            q = urllib.parse.urlencode({
                "$select": f"{state_col},mmwryear,mmwrweek,{cols}",
                "$limit": 50000, "$offset": offset,
            })
            with urllib.request.urlopen(f"{base}?{q}", timeout=180) as resp:
                batch = json.loads(resp.read())
            for r in batch:
                st = r.get(state_col)
                if not st or st in SKIP_STATES:
                    continue
                st = MERGE_JURISDICTIONS.get(st, st)
                try:
                    yr = int(r["mmwryear"])
                except (KeyError, TypeError, ValueError):
                    continue
                weeks.setdefault((st, yr), set()).add(r.get("mmwrweek"))
                for key, col in WEEKLY_CAUSE_COLS.items():
                    v = r.get(col)
                    if v in (None, ""):
                        suppressed.setdefault(key, {}).setdefault((st, yr), 0)
                        suppressed[key][(st, yr)] += 1
                        continue
                    try:
                        deaths.setdefault(key, {}).setdefault(st, {}).setdefault(yr, 0)
                        deaths[key][st][yr] += int(float(v))
                    except ValueError:
                        pass
            if len(batch) < 50000:
                break
            offset += 50000
    return deaths, weeks, suppressed


def build_crude(after_year, keep_states):
    """Crude deaths per 100k by state-year, for years the age-adjusted series does not cover."""
    print("fetching weekly provisional counts + Census population")
    deaths, weeks, suppressed = fetch_weekly_annual()
    pop = fetch_state_population()
    years = set()
    out = {}
    for key, by_state in deaths.items():
        series = {}
        for st, by_year in by_state.items():
            # same jurisdictions as the age-adjusted series, so geography does not change
            # shape when the measure switches over at 2018
            if st not in pop or st not in keep_states:
                continue
            for yr, n in by_year.items():
                if yr <= after_year:
                    continue                      # prefer the age-adjusted series where it exists
                if len(weeks.get((st, yr), ())) < 52:
                    continue                      # a partial year would read as a collapse in deaths
                if suppressed.get(key, {}).get((st, yr), 0) > MAX_SUPPRESSED_WEEKS:
                    continue                      # too much of the year is withheld to total honestly
                p = pop[st].get(yr)
                if not p:
                    continue
                series.setdefault(st, {})[yr] = round(n / p * 100000, 1)
                years.add(yr)
        out[key] = series
    years = sorted(years)
    causes = {}
    for key, series in out.items():
        dense = {st: [series[st].get(y) for y in years] for st in sorted(series)}
        vals = [v for row in dense.values() for v in row if v is not None]
        if not vals:
            continue
        complete = [st for st, row in dense.items() if all(v is not None for v in row)]
        causes[key] = {"min": round(min(vals), 1), "max": round(max(vals), 1),
                       "states": dense, "withheld": len(keep_states) - len(dense)}
        print(f"    {key:11} crude {years[0]}-{years[-1]}, {len(complete)}/{len(dense)} states complete, "
              f"range {causes[key]['min']}-{causes[key]['max']} per 100k")
    return {"years": years, "causes": causes}


def build_relative(adjusted, adj_years, crude_all):
    """One continuous series: each state as a percent of that year's national rate.

    Absolute crude and age-adjusted rates are not comparable -- crude runs higher wherever
    the population is older. Their *ratios to the national figure of the same measure* are,
    because the age-structure effect is largely divided out. The overlap years are measured
    below rather than assumed, and reported alongside the data."""
    def to_ratio(series_by_state, years):
        out, nat_by_year = {}, {}
        for i, y in enumerate(years):
            vals = [row[i] for row in series_by_state.values() if row[i] is not None]
            nat_by_year[y] = (sum(vals) / len(vals)) if vals else None
        for st, row in series_by_state.items():
            out[st] = [None if (v is None or not nat_by_year[years[i]])
                       else round(100.0 * v / nat_by_year[years[i]], 1)
                       for i, v in enumerate(row)]
        return out

    causes, all_years = {}, set()
    overlap_report = {}
    for key, adj in adjusted.items():
        if key == "all":
            continue
        adj_ratio = to_ratio(adj["states"], adj_years)
        cr = crude_all["causes"].get(key)
        cr_years = crude_all["years"]
        cr_ratio = to_ratio(cr["states"], cr_years) if cr else {}

        # measure agreement where both exist, instead of trusting that they agree
        pairs = []
        for st in set(adj_ratio) & set(cr_ratio):
            for y in set(adj_years) & set(cr_years):
                a = adj_ratio[st][adj_years.index(y)]
                c = cr_ratio[st][cr_years.index(y)]
                if a is not None and c is not None:
                    pairs.append((a, c))
        if pairs:
            n = len(pairs)
            ma = sum(a for a, _ in pairs) / n
            mc = sum(c for _, c in pairs) / n
            va = sum((a - ma) ** 2 for a, _ in pairs) ** 0.5
            vc = sum((c - mc) ** 2 for _, c in pairs) ** 0.5
            r = sum((a - ma) * (c - mc) for a, c in pairs) / (va * vc) if va and vc else 0
            mad = sum(abs(a - c) for a, c in pairs) / n
            overlap_report[key] = {"n": n, "r": round(r, 3), "mean_abs_diff_pct": round(mad, 1)}

        merged, years_for_cause = {}, sorted(set(adj_years) | set(cr_years))
        for st in set(adj_ratio) | set(cr_ratio):
            row = []
            for y in years_for_cause:
                v = None
                if y in adj_years:
                    v = adj_ratio.get(st, [None] * len(adj_years))[adj_years.index(y)]
                if v is None and y in cr_years:
                    v = cr_ratio.get(st, [None] * len(cr_years))[cr_years.index(y)]
                row.append(v)
            merged[st] = row
        all_years |= set(years_for_cause)
        vals = [v for row in merged.values() for v in row if v is not None]
        causes[key] = {"label": adj["label"], "states": merged,
                       "min": round(min(vals), 1), "max": round(max(vals), 1),
                       "years": years_for_cause}
    years_sorted = sorted(all_years)
    print("relative (percent of national, blended):")
    for k, c in causes.items():
        o = overlap_report.get(k, {})
        print(f"    {k:11} {c['years'][0]}-{c['years'][-1]}, {len(c['states'])} states, "
              f"{c['min']}-{c['max']}% of national"
              + (f"  | overlap r={o['r']}, mean diff {o['mean_abs_diff_pct']}pp (n={o['n']})" if o else ""))
    return {"years": years_sorted, "causes": causes}, overlap_report


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

    crude_all = build_crude(after_year=0, keep_states=set(states))
    crude = build_crude(after_year=years[-1], keep_states=set(states))

    relative, overlap = build_relative(out_causes, years, crude_all)

    doc = {
        "relative": relative,
        "relative_note": ("Each state as a percentage of that year's national rate. The ratio is "
                          "scale-free, so the age-adjusted years and the crude years can share one "
                          "continuous scale even though their absolute rates cannot."),
        "relative_validation": overlap,
        "crude": crude,
        "crude_source": ("CDC/NCHS weekly provisional counts by state (3yf8-kanr 2014-2019, "
                         "muzy-jte6 2020-2023) over Census state population estimates. Crude, "
                         "NOT age-adjusted, and by jurisdiction of occurrence rather than residence; "
                         "a separate measure from the age-adjusted series, never blended with it."),
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
