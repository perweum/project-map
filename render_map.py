#!/usr/bin/env python3
"""Render a project map: map.json -> map.html (self-contained, opens in any browser). With --index (default for
~/agent-maps) it also rebuilds index.html listing all maps in the parent folder.

Run:  python3 -I -B render_map.py <path/to/map.json> [--out map.html] [--png shot.png] [--index]
Exit 1 with a list of problems if the map is invalid (nothing is written then)."""
import argparse, collections, html, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
KINDS = {"input", "script", "agent", "human", "file", "output", "service", "module"}
EDGE_KINDS = {"data", "control", "human", "feedback"}
STATUS = {"idle", "working", "waiting", "done", "failed"}

ap = argparse.ArgumentParser()
ap.add_argument("map"); ap.add_argument("--out"); ap.add_argument("--png"); ap.add_argument("--theme", default="")
ap.add_argument("--index", action="store_true", help="also rebuild index.html of all maps in the parent folder (default for ~/agent-maps)")
a = ap.parse_args()
M = json.load(open(a.map, encoding="utf-8"))

errs, warns = [], []
U, W, H = 20, 220, 80                                   # keep in step with template.html
nodes = M.get("nodes", [])
free = bool(nodes) and all("x" in n and "y" in n for n in nodes)
cols = M.get("columns") or []
FORMAT_VERSION = 1
v = M.get("version", 1)          # maps written before the field existed are version 1
if not isinstance(v, int) or v < 1: errs.append(f"version must be a whole number >= 1, got {v!r}")
elif v > FORMAT_VERSION: errs.append(f"map format version {v} is newer than this renderer understands ({FORMAT_VERSION}): update project-map")
if "version" not in M: warns.append('no "version" field: treated as version 1 (add "version": 1)')
if not M.get("title"): errs.append("title missing")
if not free:
    if any("x" in n or "y" in n for n in nodes): errs.append("give x and y to every node (free layout) or to none (columns)")
    if not cols: errs.append("columns missing (needed when nodes have no x/y)")
ids, seen_rows = set(), set()
for n in nodes:
    i = n.get("id")
    if not i or i in ids: errs.append(f"node id missing or duplicate: {i!r}")
    ids.add(i)
    if n.get("kind") not in KINDS: errs.append(f"{i}: kind {n.get('kind')!r} not in {sorted(KINDS)}")
    if free:
        if not all(isinstance(n.get(k), (int, float)) for k in ("x", "y")): errs.append(f"{i}: x and y must be numbers (grid units of {U} px)")
    else:
        if not isinstance(n.get("col"), int) or not 0 <= n["col"] < len(cols): errs.append(f"{i}: col {n.get('col')!r} outside 0..{len(cols) - 1}")
        if "row" in n:
            if (n.get("col"), n["row"]) in seen_rows: errs.append(f"{i}: col {n.get('col')} row {n['row']} is taken twice")
            seen_rows.add((n.get("col"), n["row"]))
    if n.get("status", "idle") not in STATUS: errs.append(f"{i}: status {n.get('status')!r} not in {sorted(STATUS)}")
    if not n.get("title"): errs.append(f"{i}: title missing")
    if len(n.get("title", "")) > 26: warns.append(f"{i}: title longer than 26 characters, may overflow the box")
    if len(n.get("sub", "")) > 36: warns.append(f"{i}: sub longer than 36 characters, may overflow the box")
if not free:
    rows_given = [("row" in n) for n in nodes]
    if any(rows_given) and not all(rows_given): errs.append("give `row` for every node or for none (then the layout is automatic)")
def box(n): return (n["x"] * U, n["y"] * U, n["x"] * U + W, n["y"] * U + H)
if free and not errs:
    for k, na in enumerate(nodes):
        for nb in nodes[k + 1:]:
            ax, ay, ax2, ay2 = box(na); bx, by, bx2, by2 = box(nb)
            if ax < bx2 + 20 and bx < ax2 + 20 and ay < by2 + 20 and by < ay2 + 20:
                errs.append(f"{na['id']} and {nb['id']} overlap or are closer than one grid unit (boxes are {W // U}x{H // U} units)")
linked = set()
for e in M.get("edges", []):
    f, t, k = ((e + [None, None])[:2] + [e[2] if len(e) > 2 else "data"]) if isinstance(e, list) else (e.get("from"), e.get("to"), e.get("kind", "data"))
    for x in (f, t):
        if x not in ids: errs.append(f"edge {f}->{t}: unknown node {x!r}")
    if k not in EDGE_KINDS: errs.append(f"edge {f}->{t}: kind {k!r} not in {sorted(EDGE_KINDS)}")
    linked |= {f, t}
for i in ids - linked: warns.append(f"{i}: not connected to anything")
byid = {n.get("id"): n for n in nodes}
for g in M.get("groups", []):
    bad = [x for x in g.get("nodes", []) if x not in ids]
    if bad: errs.append(f"group {g.get('label')!r}: unknown node(s) {bad}"); continue
    if free and not errs:
        bs = [box(byid[x]) for x in g["nodes"]]
        gx, gy, gx2, gy2 = min(b[0] for b in bs) - 20, min(b[1] for b in bs) - 34, max(b[2] for b in bs) + 20, max(b[3] for b in bs) + 20
        inside = [n["id"] for n in nodes if n["id"] not in g["nodes"] and box(n)[0] < gx2 and box(n)[2] > gx and box(n)[1] < gy2 and box(n)[3] > gy]
        if inside: warns.append(f"group {g['label']!r} frame also covers {inside}: move them or add them to the group")
def check_inner(n, path, depth=1):
    """`inner` = a smaller map shown when the box is opened: nodes, edges, groups; x/y optional (auto layout)."""
    inn = n.get("inner")
    if inn is None: return
    where = f"{path} > inner"
    if not isinstance(inn, dict) or not isinstance(inn.get("nodes"), list) or not inn["nodes"]:
        errs.append(f"{where}: needs a non-empty `nodes` list"); return
    if depth > 4: errs.append(f"{where}: nested deeper than 4 levels"); return
    seen = set(); xy = [("x" in m and "y" in m) for m in inn["nodes"]]
    if any(xy) and not all(xy): errs.append(f"{where}: give x/y to every inner node or to none (automatic layout)")
    for m in inn["nodes"]:
        i = m.get("id")
        if not i or i in seen: errs.append(f"{where}: node id missing or duplicate: {i!r}")
        seen.add(i)
        if m.get("kind") not in KINDS: errs.append(f"{where} {i}: kind {m.get('kind')!r} not in {sorted(KINDS)}")
        if not m.get("title"): errs.append(f"{where} {i}: title missing")
        check_inner(m, f"{where} {i}", depth + 1)
    for e in inn.get("edges", []):
        f, t, k = ((e + [None, None])[:2] + [e[2] if len(e) > 2 else "data"]) if isinstance(e, list) else (e.get("from"), e.get("to"), e.get("kind", "data"))
        if f not in seen or t not in seen: errs.append(f"{where}: edge {f}->{t} names an unknown node")
        if k not in EDGE_KINDS: errs.append(f"{where}: edge {f}->{t} kind {k!r} not in {sorted(EDGE_KINDS)}")
    for g in inn.get("groups", []):
        if [x for x in g.get("nodes", []) if x not in seen]: errs.append(f"{where}: group {g.get('label')!r} names an unknown node")
for n in nodes: check_inner(n, n.get("id"))
zcount = collections.Counter(x for g in M.get("groups", []) for x in g.get("nodes", []))
for x, k in zcount.items():
    if k > 1: warns.append(f"{x} is in {k} zones: a box belongs in one zone (the page's Arrange mode keeps it that way)")
live = M.get("live") or {}
for kind in ("sessions", "files", "units", "git", "process"):
    for r in live.get(kind, []):
        if r.get("node") not in ids: errs.append(f"live.{kind}: unknown node {r.get('node')!r}")
for r in live.get("sessions", []):
    try: re.compile(r.get("match", ""))
    except re.error as x: errs.append(f"live.sessions {r.get('node')}: bad regex: {x}")
if errs: sys.exit("map is invalid:\n  " + "\n  ".join(errs))

if M.get("repo") and os.path.isdir(os.path.expanduser(M["repo"])):    # drift: is the map still true?
    sys.path.insert(0, HERE); import discover
    d = discover.drift(M)
    M["drift"] = {"stale": d["stale"], "new": d["new"][:30], "count": len(d["stale"]) + len(d["new"])}
    stale = set(d["stale"])
    for n in M["nodes"]:
        if n["id"] in stale: n["stale"] = True
    for s in d["stale"]: warns.append(f"drift: {s} points at files that are gone")
    for s in d["new"][:15]: warns.append(f"drift: not on the map yet: {s}")
if a.theme: M["theme"] = a.theme
folder = os.path.dirname(os.path.abspath(a.out or a.map)); parent = os.path.dirname(folder)
home = os.path.expanduser("~"); tl = lambda p: "~" + p[len(home):] if p.startswith(home + "/") else p
cmd = f"python3 {tl(os.path.join(HERE, 'board.py'))}" + ("" if parent == os.path.join(home, "agent-maps") else f" --root {tl(parent)}") + f" --open {os.path.basename(folder)}"
M["_board"] = {"project": os.path.basename(folder), "cmd": cmd}
data = json.dumps(M, ensure_ascii=False).replace("</", "<\\/")
page = open(os.path.join(HERE, "template.html"), encoding="utf-8").read().replace("/*MAP_DATA*/null", data, 1)
out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.map)), "map.html")
open(out, "w", encoding="utf-8").write(page)
print(f"wrote {out}: {len(ids)} nodes, {len(M.get('edges', []))} edges")
for w in warns: print("warning:", w)

# index of every map that sits next to this one (~/agent-maps/<project>/map.html)
root = os.path.dirname(os.path.dirname(os.path.abspath(out)))
items = []
if not (a.index or root == os.path.expanduser("~/agent-maps")): root = None
for d in (sorted(os.listdir(root)) if root else []):
    j = os.path.join(root, d, "map.json")
    if os.path.isfile(j) and os.path.isfile(os.path.join(root, d, "map.html")):
        try: m = json.load(open(j, encoding="utf-8"))
        except ValueError: continue
        items.append(f'<a href="{html.escape(d)}/map.html"><b>{html.escape(m.get("title", d))}</b><span>{html.escape(m.get("subtitle", ""))}</span>'
                     f'<em>{len(m.get("nodes", []))} nodes · {sum(n.get("kind") == "agent" for n in m.get("nodes", []))} agents</em></a>')
if items:
    open(os.path.join(root, "index.html"), "w", encoding="utf-8").write(
        "<!doctype html><meta charset=utf-8><title>Project maps</title><style>body{background:#0a0e15;color:#d8e1f0;font:14px system-ui;"
        "padding:32px;max-width:900px;margin:auto}h1{font-size:18px;letter-spacing:.1em}a{display:block;padding:14px 18px;margin:10px 0;"
        "border:1px solid #2c3b55;border-radius:8px;color:inherit;text-decoration:none;background:#121825}a:hover{border-color:#4da3ff;"
        "box-shadow:0 0 14px #4da3ff55}span{display:block;opacity:.7;font-size:12px;margin-top:3px}em{font-size:11px;color:#5fe08a}</style>"
        "<h1>PROJECT MAPS</h1>" + "".join(items))
    print(f"index: {os.path.join(root, 'index.html')} ({len(items)} maps)")

VW_EST = max([(n.get("x", n.get("col", 0) * 16) * 20 + 220) for n in M["nodes"]] + [1000]) + 80
VH_EST = max([(n.get("y", n.get("row", 0) * 5) * 20 + 80) for n in M["nodes"]] + [400]) + 120
if a.png:
    b = next((shutil.which(x) for x in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome") if shutil.which(x)), None)
    if not b: print("no chromium found: skipped the screenshot")
    else:
        url = "file://" + os.path.abspath(out) + "#clean" + (f",{a.theme}" if a.theme else "")
        wpx = max(1000, min(2400, round(VW_EST * 0.9) + 40)); hpx = round((wpx - 24) * VH_EST / VW_EST) + 150
        subprocess.run([b, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={wpx},{hpx}", "--virtual-time-budget=6000",
                        f"--screenshot={os.path.abspath(a.png)}", url], capture_output=True, timeout=90)
        print(f"screenshot: {a.png}")
