"""Builds data/state_data.js -- interior US state boundaries for reference, plus per-state
polygon rings used to fill the CDC choropleth. Static reference data: run once, not weekly.

    python3 scripts/build_state_lines.py

Source is the Census-derived us-atlas TopoJSON (the companion to the world-atlas file the
country outlines come from). Only arcs shared by two state polygons are kept, so the national
outline and coastline are left out -- those are already drawn from country_data.js and would
otherwise be doubled. TopoJSON is decoded here rather than with a library, to keep the
toolchain stdlib-only.
"""
import json
import math
import os
import urllib.request

SRC = "https://cdn.jsdelivr.net/npm/us-atlas@3/states-10m.json"
OUT = "data/state_data.js"
TOLERANCE_DEG = 0.01    # borders: ~1 km, well under a screen pixel at full zoom
FILL_TOLERANCE_DEG = 0.04   # fills only need to be right to about a texture pixel


def decode_arcs(topo):
    """TopoJSON arcs are quantised delta pairs; undo the deltas and apply the transform."""
    scale = topo["transform"]["scale"]
    translate = topo["transform"]["translate"]
    out = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append([x * scale[0] + translate[0], y * scale[1] + translate[1]])
        out.append(pts)
    return out


def iter_arc_indices(geometry):
    """Yield every arc index used by a Polygon/MultiPolygon geometry."""
    t = geometry.get("type")
    if t == "Polygon":
        rings = geometry["arcs"]
    elif t == "MultiPolygon":
        rings = [r for poly in geometry["arcs"] for r in poly]
    else:
        return
    for ring in rings:
        for a in ring:
            yield (~a if a < 0 else a)


def ring_points(arc_indices, arcs):
    """Stitch a TopoJSON ring: negative index means traverse that arc backwards."""
    pts = []
    for a in arc_indices:
        arc = arcs[~a][::-1] if a < 0 else arcs[a]
        pts.extend(arc[1:] if pts else arc)   # drop the duplicated join point
    return pts


def state_rings(geometry, arcs):
    t = geometry.get("type")
    if t == "Polygon":
        rings = geometry["arcs"]
    elif t == "MultiPolygon":
        rings = [r for poly in geometry["arcs"] for r in poly]
    else:
        return []
    return [ring_points(r, arcs) for r in rings]


def simplify(points, tol):
    """Iterative Douglas-Peucker (recursion would blow the stack on the long arcs)."""
    n = len(points)
    if n < 3:
        return points[:]
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi <= lo + 1:
            continue
        ax, ay = points[lo]
        bx, by = points[hi]
        dx, dy = bx - ax, by - ay
        denom = math.hypot(dx, dy)
        best_i, best_d = -1, -1.0
        for i in range(lo + 1, hi):
            px, py = points[i]
            if denom == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / denom
            if d > best_d:
                best_i, best_d = i, d
        if best_d > tol:
            keep[best_i] = True
            stack.append((lo, best_i))
            stack.append((best_i, hi))
    return [points[i] for i in range(n) if keep[i]]


def main():
    print(f"fetching {SRC}")
    with urllib.request.urlopen(SRC, timeout=120) as resp:
        topo = json.loads(resp.read())
    arcs = decode_arcs(topo)
    states = topo["objects"]["states"]["geometries"]
    print(f"  {len(states)} state geometries, {len(arcs)} arcs")

    # an arc bordering two states is interior; one used once is coast or national border
    use_count = {}
    for g in states:
        for a in set(iter_arc_indices(g)):   # set(): a state touching its own arc twice still counts once
            use_count[a] = use_count.get(a, 0) + 1
    interior = sorted(a for a, n in use_count.items() if n >= 2)
    print(f"  {len(interior)} interior arcs ({len(use_count)-len(interior)} exterior, left to country_data)")

    raw_pts = 0
    lines = []
    for a in interior:
        pts = [[round(x, 4), round(y, 4)] for x, y in arcs[a]]
        raw_pts += len(pts)
        s = simplify(pts, TOLERANCE_DEG)
        if len(s) >= 2:
            lines.append(s)
    kept = sum(len(p) for p in lines)
    print(f"  simplified {raw_pts} -> {kept} points at {TOLERANCE_DEG} deg")

    # per-state polygon rings, for filling the choropleth. Keyed by the Census state name,
    # which is what the CDC mortality file uses, so the two join without a lookup table.
    polygons, poly_pts, dropped = {}, 0, 0
    for g in states:
        name = (g.get("properties") or {}).get("name")
        if not name:
            continue
        rings = []
        for ring in state_rings(g, arcs):
            s_ring = simplify([[round(x, 4), round(y, 4)] for x, y in ring], FILL_TOLERANCE_DEG)
            if len(s_ring) >= 4:          # a ring needs at least a triangle plus the closing point
                rings.append(s_ring)
                poly_pts += len(s_ring)
            else:
                dropped += 1
        if rings:
            polygons[name] = rings
    print(f"  {len(polygons)} states with fill polygons, {poly_pts} points "
          f"({dropped} slivers below the fill tolerance dropped)")

    doc = {
        "source": "US Census Bureau cartographic boundaries via us-atlas (states-10m), public domain",
        "url": "https://github.com/topojson/us-atlas",
        "note": "interior state boundaries only; national outline and coastline come from country_data.js",
        "borders": lines,
        "polygons": polygons,
    }
    with open(OUT, "w") as f:
        f.write("window.STATE_DATA=")
        json.dump(doc, f, separators=(",", ":"))
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()
