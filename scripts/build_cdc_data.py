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
}
# all-cause is spelled differently in the two weekly files
WEEKLY_ALLCAUSE_COL = {
    "https://data.cdc.gov/resource/3yf8-kanr.json": "allcause",
    "https://data.cdc.gov/resource/muzy-jte6.json": "all_cause",
}
# Still being updated: all-cause deaths by state and week, currently through late 2026. It
# carries no cause breakdown, so it can only extend the all-cause series -- but that is what
# reaches the present. Every jurisdiction reports the same weeks, including the partial
# current year, so a ratio to the national figure stays valid even before the year is over.
API_RECENT = "https://data.cdc.gov/resource/r8kw-7aab.json"
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
    for base, state_col, date_col in API_WEEKLY:
        cols = ",".join(sorted(set(WEEKLY_CAUSE_COLS.values()) | {WEEKLY_ALLCAUSE_COL[base]}))
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
                for key, col in list(WEEKLY_CAUSE_COLS.items()) + [("all", WEEKLY_ALLCAUSE_COL[base])]:
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


def fetch_recent_allcause():
    """All-cause deaths by state and year from the file CDC still updates, plus the number of
    weeks each year contains, so a partial current year can be handled honestly."""
    q = urllib.parse.urlencode({"$select": "state,total_deaths,week_ending_date,`group`",
                                "$limit": 100000})
    with urllib.request.urlopen(f"{API_RECENT}?{q}", timeout=180) as resp:
        rows = json.loads(resp.read())
    deaths, weeks = {}, {}
    for r in rows:
        if r.get("group") != "By Week":
            continue
        st, d, v = r.get("state"), r.get("week_ending_date"), r.get("total_deaths")
        if not st or not d or v in (None, ""):
            continue
        st = MERGE_JURISDICTIONS.get(st, st)
        if st in SKIP_STATES:
            continue
        yr = int(d[:4])
        try:
            deaths.setdefault(st, {}).setdefault(yr, 0)
            deaths[st][yr] += int(float(v))
        except ValueError:
            continue
        weeks.setdefault(yr, set()).add(d)
    return deaths, {y: len(w) for y, w in weeks.items()}


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
        # population-weighted national crude rate, the correct reference for these states
        national = []
        for y in years:
            num = den = 0
            for st in series:
                d = deaths.get(key, {}).get(st, {}).get(y)
                p = state_pop_for(pop, st, y)
                if d and p and series[st].get(y) is not None:
                    num += d
                    den += p
            national.append(round(num / den * 100000, 1) if den else None)
        vals = [v for row in dense.values() for v in row if v is not None]
        if not vals:
            continue
        complete = [st for st, row in dense.items() if all(v is not None for v in row)]
        causes[key] = {"min": round(min(vals), 1), "max": round(max(vals), 1),
                       "states": dense, "national": national,
                       "withheld": len(keep_states) - len(dense)}
        print(f"    {key:11} crude {years[0]}-{years[-1]}, {len(complete)}/{len(dense)} states complete, "
              f"range {causes[key]['min']}-{causes[key]['max']} per 100k")
    return {"years": years, "causes": causes}


def ratio_from_counts(deaths_by_state, years, keep_states, pop):
    """Counts -> percent of the national rate for the same year.

    Counts cannot be compared between states directly -- they scale with population -- so each
    state becomes a rate first and the reference is the true national rate (all deaths over all
    population), not the average of state rates. A uniformly partial year is still valid: every
    jurisdiction contributes the same weeks, so the shortfall divides out of the ratio."""
    out = {}
    for y in years:
        num = den = 0
        for st in keep_states:
            d = deaths_by_state.get(st, {}).get(y)
            p = state_pop_for(pop, st, y)
            if d and p:
                num += d
                den += p
        if not den:
            continue
        national = num / den
        for st in keep_states:
            d = deaths_by_state.get(st, {}).get(y)
            p = state_pop_for(pop, st, y)
            out.setdefault(st, {})[y] = (round(100.0 * (d / p) / national, 1)
                                         if d and p and national else None)
    return out


def state_pop_for(pop, st, year):
    """Population for a state-year, holding the most recent estimate forward. Census publishes
    a year or two behind; population moves under a percent a year and in the same direction
    almost everywhere, so the effect on a ratio-to-national is negligible."""
    by_year = pop.get(st)
    if not by_year:
        return None
    if year in by_year:
        return by_year[year]
    latest = max(by_year)
    return by_year[latest] if year > latest else None


def calibrate_onto(target, target_years, anchor, anchor_years):
    """Move one segment's level onto another's, per state, using the years both cover.

    Two segments can count the same deaths differently -- age-adjusted against crude, residence
    against occurrence -- so a state sits at a systematically different percent of national in
    each. The ratio of its two levels across the overlap years is that offset; dividing it out
    makes the segments meet without a step at the join. Only the level moves. The shape within
    a segment -- which state is above its neighbours, and by how much -- is what the map draws,
    and it is untouched. A state with no overlap takes the median offset rather than vanishing."""
    shared = [y for y in target_years if y in anchor_years]
    factors = {}
    for st in set(target) & set(anchor):
        pairs = [(anchor[st][anchor_years.index(y)], target[st][target_years.index(y)])
                 for y in shared]
        pairs = [(a, t) for a, t in pairs if a and t]
        if pairs:
            factors[st] = ((sum(a for a, _ in pairs) / len(pairs))
                           / (sum(t for _, t in pairs) / len(pairs)))
    if not factors:
        return {st: row[:] for st, row in target.items()}, factors
    med = sorted(factors.values())[len(factors) // 2]
    out = {st: [None if v is None else v * factors.get(st, med) for v in row]
           for st, row in target.items()}
    return out, factors


def renormalize_to_national(series, years, pop):
    """Calibration shifts each state by its own factor, so the population-weighted mean drifts
    off the national figure. Rescale each year to put it back on 100%: the hue ramp is centred
    there, and a drifting centre would tint the whole map. Relative spacing is unaffected."""
    for i, y in enumerate(years):
        num = den = 0.0
        for st, row in series.items():
            v, p = row[i], state_pop_for(pop, st, y)
            if v is not None and p:
                num += v * p
                den += p
        if den and num:
            k = 100.0 * den / num
            for row in series.values():
                if row[i] is not None:
                    row[i] *= k


def mean_year_step(series, years):
    """Mean absolute year-over-year change per state, by year pair. A join that needs
    calibration shows up here as one pair far above the rest."""
    steps = {}
    for i in range(len(years) - 1):
        d = [abs(row[i + 1] - row[i]) for row in series.values()
             if row[i] is not None and row[i + 1] is not None]
        if d:
            steps[(years[i], years[i + 1])] = sum(d) / len(d)
    return steps


def build_relative(adjusted, adj_years, crude_all, recent=None, recent_weeks=None, recent_pop=None):
    """One continuous series: each state as a percent of that year's national rate.

    Absolute crude and age-adjusted rates are not comparable -- crude runs higher wherever
    the population is older. Their *ratios to the national figure of the same measure* are,
    because the age-structure effect is largely divided out. The overlap years are measured
    below rather than assumed, and reported alongside the data."""
    def to_ratio(series_by_state, years, national=None):
        """Percent of the national rate. Uses the published national figure where there is one
        (the age-adjusted file carries a United States row); otherwise the population-weighted
        national rate, never the unweighted average of states, which would over-weight Wyoming."""
        out, nat_by_year = {}, {}
        for i, y in enumerate(years):
            n = national[i] if national and i < len(national) else None
            if n is None:
                vals = [row[i] for row in series_by_state.values() if row[i] is not None]
                n = (sum(vals) / len(vals)) if vals else None
            nat_by_year[y] = n
        for st, row in series_by_state.items():
            out[st] = [None if (v is None or not nat_by_year[years[i]])
                       else round(100.0 * v / nat_by_year[years[i]], 1)
                       for i, v in enumerate(row)]
        return out

    causes, all_years = {}, set()
    overlap_report = {}
    for key, adj in adjusted.items():
        adj_ratio = to_ratio(adj["states"], adj_years, adj.get("national"))
        cr = crude_all["causes"].get(key)
        cr_years = crude_all["years"]
        cr_ratio = to_ratio(cr["states"], cr_years, (cr.get("national") if cr else None)) if cr else {}

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

        # all-cause alone has a third source that is still being updated, so its chain runs on
        rec_ratio, rec_years = {}, []
        if key == "all" and recent:
            rec_years = sorted({y for by in recent.values() for y in by})
            keep = set(adj["states"])
            sparse = ratio_from_counts(recent, rec_years, keep, recent_pop or {})
            rec_ratio = {st: [by.get(y) for y in rec_years] for st, by in sparse.items()}

        # Chain the segments onto the age-adjusted one instead of butting them together. The
        # overlap measured above is a real offset, not noise, and splicing at face value puts a
        # step into the animation at the year the source changes -- the most visible thing on
        # screen, and an artefact of the data plumbing rather than anything that happened.
        # Each link anchors on the already-corrected segment before it, so the whole run of
        # years sits on the level the age-adjusted series defines.
        cr_cal, cr_factors = calibrate_onto(cr_ratio, cr_years, adj_ratio, adj_years)
        rec_cal, rec_factors = ({}, {})
        if rec_ratio:
            rec_cal, rec_factors = calibrate_onto(rec_ratio, rec_years, cr_cal, cr_years)

        def splice(crude_seg, recent_seg):
            years_for = sorted(set(adj_years) | set(cr_years) | set(rec_years))
            out = {}
            for st in set(adj_ratio) | set(crude_seg) | set(recent_seg):
                row = []
                for y in years_for:
                    v = None
                    if y in adj_years:
                        v = adj_ratio.get(st, [None] * len(adj_years))[adj_years.index(y)]
                    if v is None and y in cr_years:
                        v = crude_seg.get(st, [None] * len(cr_years))[cr_years.index(y)]
                    if v is None and y in rec_years:
                        v = recent_seg.get(st, [None] * len(rec_years))[rec_years.index(y)]
                    row.append(v)
                out[st] = row
            return out, years_for

        raw, years_for_cause = splice(cr_ratio, rec_ratio)
        merged, _ = splice(cr_cal, rec_cal)
        renormalize_to_national(merged, years_for_cause, recent_pop or {})

        # which file each year came from, so the join pairs can be told from ordinary ones
        src_of = {y: ("adjusted" if y in adj_years else
                      "crude" if y in cr_years else "recent") for y in years_for_cause}
        joins = [(a, b) for a, b in zip(years_for_cause, years_for_cause[1:])
                 if src_of[a] != src_of[b]]
        before, after = mean_year_step(raw, years_for_cause), mean_year_step(merged, years_for_cause)
        ordinary = [v for k, v in after.items() if k not in joins]
        step_report = {
            "joins": {f"{a}-{b}": {"before": round(before.get((a, b), 0), 1),
                                   "after": round(after.get((a, b), 0), 1)} for a, b in joins},
            "ordinary_step_pp": round(sum(ordinary) / len(ordinary), 1) if ordinary else None,
        }
        if key in overlap_report:
            overlap_report[key]["year_step_pp"] = step_report

        merged = {st: [None if v is None else round(v, 1) for v in row]
                  for st, row in merged.items()}
        all_years |= set(years_for_cause)
        vals = [v for row in merged.values() for v in row if v is not None]
        causes[key] = {"label": adj["label"], "states": merged,
                       "min": round(min(vals), 1), "max": round(max(vals), 1),
                       "years": years_for_cause}
        if key == "all" and recent_weeks:
            causes[key]["partial_years"] = {str(y): w for y, w in recent_weeks.items() if w < 52}
    years_sorted = sorted(all_years)
    print("relative (percent of national, blended):")
    for k, c in causes.items():
        o = overlap_report.get(k, {})
        print(f"    {k:11} {c['years'][0]}-{c['years'][-1]}, {len(c['states'])} states, "
              f"{c['min']}-{c['max']}% of national"
              + (f"  | overlap r={o['r']}, mean diff {o['mean_abs_diff_pct']}pp (n={o['n']})" if o else ""))
        for pair, st in (o.get("year_step_pp") or {}).get("joins", {}).items():
            print(f"                join {pair}: step {st['before']}pp -> {st['after']}pp "
                  f"(ordinary year {o['year_step_pp']['ordinary_step_pp']}pp)")
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

    print(f"fetching {API_RECENT} (all-cause, still updating)")
    recent, recent_weeks = fetch_recent_allcause()
    print(f"    all-cause by state {min(recent_weeks)}-{max(recent_weeks)}; "
          f"partial years: {{y: w for y, w in recent_weeks.items() if w < 52}}".replace("{y: w for y, w in recent_weeks.items() if w < 52}",
          str({y: w for y, w in sorted(recent_weeks.items()) if w < 52})))
    relative, overlap = build_relative(out_causes, years, crude_all, recent, recent_weeks,
                                       fetch_state_population())

    doc = {
        "relative": relative,
        "relative_note": ("Each state as a percentage of that year's national rate. The ratio is "
                          "scale-free, so the age-adjusted years and the crude years can share one "
                          "continuous scale even though their absolute rates cannot. Where two "
                          "sources overlap, the later one's level is matched to the earlier across "
                          "the shared years, per state, so the series does not step at the join; "
                          "each year is then rescaled to put the national level back at 100%."),
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
