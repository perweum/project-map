#!/usr/bin/env python3
"""Live agent board. Serves the project maps in ~/agent-maps on http://127.0.0.1:<port>/ and answers
/<project>/status.json from local facts, described in each map.json under "live":

  sessions  Pi (~/.pi/agent/sessions) and Claude Code (~/.claude/projects) session logs whose folder name contains
            live.match; each session goes to the first rule whose regex matches its title or first prompt
  files     globs: count, newest, age; "alert" = waiting while files exist; "verdict" = read "Verdict:" of the newest;
            "working_if_exists" = working while something matches (e.g. a review worktree)
  units     systemd --user <unit>.service / <unit>.timer: running, last result, next run
  git       local branches of a repo minus "exclude" patterns
  process   pgrep -f pattern

Read-only and local: binds 127.0.0.1, runs only the fixed probes above (never commands from a map), and computes
status only when a page asks. Tool arguments shown in the feed are shortened and secret-looking strings redacted.
Run: python3 -I -B board.py [--root ~/agent-maps] [--port 8765]      one-off: board.py --once <project>
"""
import argparse, collections, datetime, fnmatch, glob, json, os, re, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
SESSION_ROOTS = [("pi", f"{HOME}/.pi/agent/sessions"), ("claude", f"{HOME}/.claude/projects")]
SECRET = re.compile(r"(AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w+|sk-(?:ant-)?[\w\-]{16,}"
                    r"|xox[baprs]-[\w-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Za-z0-9_]*\s*[=:]\s*\S+)", re.I)
RANK = {"failed": 4, "working": 3, "waiting": 2, "done": 1, "idle": 0}

def red(s, n=110):
    s = SECRET.sub("[redacted]", " ".join(str(s).split()))
    return s if len(s) <= n else s[:n - 1] + "…"
def ago(sec):
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 90 else f"{sec // 60} min" if sec < 5400 else f"{sec // 3600} h" if sec < 172800 else f"{sec // 86400} d"
def iso(s):
    try: return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError): return None
def today_start(): return datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()
def X(p): return os.path.expanduser(p)

# ---------------- session logs (parsed incrementally, cached per file) ----------------
def tool_name(n): return re.sub(r"^mcp__.*__", "", n or "")
def arg_of(a):
    if not isinstance(a, dict): return red(a, 80)
    for k in ("description", "command", "file_path", "path", "pattern", "query", "url", "task", "prompt"):
        if a.get(k): return red(a[k], 80)
    return ""
def text_of(c):
    if isinstance(c, str): return c
    if isinstance(c, list): return " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""

class Session:
    def __init__(self, harness, path):
        self.harness, self.path, self.off, self.buf = harness, path, 0, b""
        self.title = self.first = self.model = self.cwd = ""
        self.last = 0.0; self.tools = collections.deque(maxlen=40); self.cost_by_day = collections.Counter(); self.cost_total = 0.0
    def update(self):
        size = os.path.getsize(self.path)
        if size < self.off: self.__init__(self.harness, self.path)
        if size == self.off: return
        with open(self.path, "rb") as f:
            f.seek(self.off); data = self.buf + f.read(); self.off = size
        lines = data.split(b"\n"); self.buf = lines.pop()
        for l in lines:
            try: self.feed(json.loads(l))
            except (ValueError, AttributeError, TypeError): pass
    def feed(self, d):
        t = iso(d.get("timestamp")) or 0
        if t: self.last = max(self.last, t)
        if self.harness == "pi":
            typ = d.get("type")
            if typ == "session": self.cwd = d.get("cwd", "")
            elif typ == "session_info" and d.get("name"): self.title = d["name"]
            elif typ == "message":
                m = d.get("message") or {}
                if m.get("role") == "user" and not self.first: self.first = text_of(m.get("content"))[:3000]
                if m.get("role") == "assistant":
                    self.model = m.get("model") or self.model
                    for c in m.get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "toolCall": self.tools.append((t, tool_name(c.get("name")), arg_of(c.get("arguments"))))
                    cost = ((m.get("usage") or {}).get("cost") or {}).get("total") or 0
                    if cost and t: self.cost_by_day[datetime.date.fromtimestamp(t).isoformat()] += cost
        else:
            typ = d.get("type")
            if d.get("cwd"): self.cwd = d["cwd"]
            if typ == "ai-title": self.title = d.get("aiTitle") or self.title
            elif typ == "cost-state": self.cost_total = d.get("totalCostUSD") or self.cost_total
            elif typ == "user" and not self.first:
                self.first = text_of((d.get("message") or {}).get("content"))[:3000]
            elif typ == "assistant":
                m = d.get("message") or {}
                self.model = m.get("model") or self.model
                for c in m.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use": self.tools.append((t, tool_name(c.get("name")), arg_of(c.get("input"))))
    def label(self): return red(self.title or self.first.split("\n")[0], 60)
    def role_text(self): return self.title + "\n" + self.first[:300]   # skill bodies further down mention other roles

def drop_mirrors(ss):
    """A Pi session that uses Claude through the bridge also leaves a Claude Code log with the same tool calls.
    Keep the Pi one; a Claude log whose recent tool calls mostly match a Pi session's is a mirror (its cost is kept)."""
    pi = {(int(t // 4), n, a[:40]) for s in ss if s.harness == "pi" for t, n, a in s.tools}
    pi |= {(k + 1, n, a) for k, n, a in pi}
    out, mirrors = [], []
    for s in ss:
        recent = list(s.tools)[-10:]
        if s.harness == "claude" and recent and sum((int(t // 4), n, a[:40]) in pi for t, n, a in recent) >= len(recent) / 2: mirrors.append(s)
        else: out.append(s)
    return out, mirrors

SESSIONS, SLOCK = {}, threading.Lock()
def sessions_for(match, days=3):
    out, cutoff = [], time.time() - days * 86400
    with SLOCK:
        for harness, root in SESSION_ROOTS:
            if not os.path.isdir(root): continue
            for d in os.listdir(root):
                if match not in d: continue
                for p in glob.glob(os.path.join(root, d, "*.jsonl")):
                    try:
                        if os.path.getmtime(p) < cutoff: continue
                        s = SESSIONS.setdefault(p, Session(harness, p)); s.update(); out.append(s)
                    except OSError: pass
    return out

def sh(*a, timeout=5):
    try: return subprocess.run(a, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired): return ""

# ---------------- one project's status ----------------
def status(m):
    L, now = m.get("live") or {}, time.time()
    WORK = L.get("working_seconds", 60)
    nodes, feed = {}, []
    def put(nid, st, line, detail=""):
        cur = nodes.get(nid)
        if not cur: nodes[nid] = {"status": st, "line": line, "detail": detail}; return
        if RANK[st] > RANK[cur["status"]]: cur["status"], cur["line"], cur["detail"] = st, line + (" · " + cur["line"] if cur["line"] else ""), detail + ("\n" + cur["detail"] if cur["detail"] else "")
        else: cur["line"] += (" · " if cur["line"] else "") + line; cur["detail"] += ("\n" if cur["detail"] else "") + detail
    totals = {"working": 0, "cost_today": 0.0, "other_active": 0}
    day0, dkey = today_start(), datetime.date.today().isoformat()

    # sessions
    rules = [(r["node"], re.compile(r.get("match", "."), re.I | re.M)) for r in L.get("sessions", [])]
    groups = collections.defaultdict(list)
    found, mirrors = drop_mirrors(sessions_for(L["match"]) if L.get("match") else [])
    for s in mirrors: totals["cost_today"] += s.cost_total if s.last >= day0 else 0
    for s in found:
        node = next((n for n, rx in rules if rx.search(s.role_text())), None)
        totals["cost_today"] += s.cost_by_day.get(dkey, 0) + (s.cost_total if s.harness == "claude" and s.last >= day0 else 0)
        if node: groups[node].append(s)
        elif now - s.last < WORK: totals["other_active"] += 1
        for t, name, arg in s.tools:
            if now - t < 7200: feed.append({"t": t, "node": node or "", "text": f"{name}: {arg}" if arg else name})
    for node, _ in rules:
        ss = sorted(groups.get(node, []), key=lambda s: -s.last)
        detail = "\n".join(f"{'▶' if now - s.last < WORK else '·'} {s.label()} · {s.harness} {s.model or ''} · {ago(now - s.last)} ago" for s in ss[:5])
        working = [s for s in ss if now - s.last < WORK]
        if working:
            totals["working"] += len(working)
            s = working[0]; t = s.tools[-1] if s.tools else None
            put(node, "working", f"{len(working)}× " + (f"{t[1]}: {t[2]}" if t else "thinking") if len(working) > 1 else (f"{t[1]}: {t[2]}" if t else "thinking"), detail)
        elif ss and now - ss[0].last < 1800: put(node, "done", f"idle {ago(now - ss[0].last)} · {ss[0].label()}", detail)
        elif ss: put(node, "idle", f"last run {ago(now - ss[0].last)} ago", detail)
        else: put(node, "idle", "no sessions in 3 days")

    # files
    for r in L.get("files", []):
        hits = [p for p in glob.glob(X(r["glob"])) if os.path.exists(p)]
        lab = r.get("label", "")
        if not hits:
            put(r["node"], "idle", r.get("empty", f"no {lab or 'files'}") if not r.get("alert") else "", ""); continue
        newest = max(hits, key=os.path.getmtime); age = now - os.path.getmtime(newest)
        for p in hits:
            if now - os.path.getmtime(p) < 86400: feed.append({"t": os.path.getmtime(p), "node": r["node"], "text": f"{os.path.basename(p)} updated"})
        name = os.path.basename(newest)
        if r.get("working_if_exists"): put(r["node"], "working", f"{lab or name} open {ago(age)}"); continue
        if r.get("verdict"):
            v = re.search(r"Verdict:\s*\**\s*([A-Z][A-Z ]+[A-Z])", open(newest, errors="replace").read(4000))
            v = v.group(1) if v else "no verdict"
            put(r["node"], "working" if age < WORK else "done" if v == "APPROVE" else "waiting", f"{name}: {v} ({ago(age)})"); continue
        st = "working" if age < WORK else "waiting" if r.get("alert") else "idle"
        put(r["node"], st, f"{len(hits)} · newest {name} {ago(age)} ago" if len(hits) > 1 else f"{lab or name} {ago(age)} ago")

    # systemd units
    timers = {}
    try:
        for t in json.loads(sh("systemctl", "--user", "list-timers", "--all", "-o", "json") or "[]"): timers[t["unit"]] = t
    except ValueError: pass
    for r in L.get("units", []):
        u = r["unit"]; props = dict(l.split("=", 1) for l in sh("systemctl", "--user", "show", f"{u}.service", "-p", "ActiveState",
                                                                  "-p", "Result", "-p", "ExecMainExitTimestamp", "--timestamp=unix").splitlines() if "=" in l)
        t = timers.get(f"{u}.timer", {})
        nxt = f"next {datetime.datetime.fromtimestamp(t['next'] / 1e6):%H:%M}" if t.get("next") else "no timer"
        lt = t.get("last") and t["last"] / 1e6
        if lt: feed.append({"t": lt, "node": r["node"], "text": f"{u} ran ({props.get('Result', '?')})"})
        if props.get("ActiveState") in ("active", "activating", "reloading"): put(r["node"], "working", f"running · {nxt}")
        elif props.get("Result") not in ("success", "", None): put(r["node"], "failed", f"last run {props.get('Result')} · {nxt}")
        else: put(r["node"], "idle", f"{nxt} · last {ago(now - lt) + ' ago' if lt else 'never'}")

    # git branches
    for r in L.get("git", []):
        bs = [b for b in sh("git", "-C", X(r["repo"]), "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
              if not any(fnmatch.fnmatch(b, x) for x in r.get("exclude", ["main"]))]
        put(r["node"], "waiting" if bs else "idle", f"{len(bs)} open branch(es): {', '.join(bs)[:60]}" if bs else "no open branches", "\n".join(bs))

    # processes
    for r in L.get("process", []):
        run = bool(sh("pgrep", "-f", r["pattern"]).strip())
        put(r["node"], "working" if run else "idle", r.get("running", "running") if run else r.get("stopped", "not running"))

    if m.get("repo"):                                       # drift, at most every 5 minutes per map
        key = m["repo"]; hit = DRIFT.get(key)
        if not hit or now - hit[0] > 300:
            try:
                sys.path.insert(0, HERE); import discover
                d = discover.drift(m); hit = (now, {"stale": d["stale"], "new": d["new"][:30], "count": len(d["stale"]) + len(d["new"])})
            except Exception as e: hit = (now, {"count": 0, "error": str(e)})
            DRIFT[key] = hit
        totals["drift"] = hit[1]
    feed.sort(key=lambda f: -f["t"])
    totals["cost_today"] = round(totals["cost_today"], 2)
    return {"time": now, "nodes": nodes, "feed": feed[:40], "totals": totals}

# ---------------- server ----------------
CACHE, CLOCK, DRIFT = {}, threading.Lock(), {}
def project_status(root, proj):
    path = os.path.join(root, proj, "map.json")
    with CLOCK:
        hit = CACHE.get(proj)
        if hit and time.time() - hit[0] < 2.5: return hit[1]
    data = json.dumps(status(json.load(open(path, encoding="utf-8")))).encode()
    with CLOCK: CACHE[proj] = (time.time(), data)
    return data

def rerender_if_stale(root, proj):
    j, h = os.path.join(root, proj, "map.json"), os.path.join(root, proj, "map.html")
    tpl = os.path.join(HERE, "template.html")
    if os.path.exists(j) and (not os.path.exists(h) or os.path.getmtime(h) < max(os.path.getmtime(j), os.path.getmtime(tpl))):
        subprocess.run([sys.executable, "-I", "-B", os.path.join(HERE, "render_map.py"), j, "--index"], capture_output=True, timeout=60)

def handler(root):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def send(self, code, body, ctype):
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            parts = [p for p in unquote(urlparse(self.path).path).split("/") if p]
            if any(p.startswith(".") for p in parts): return self.send(404, b"not found", "text/plain")
            try:
                if not parts: return self.send(200, open(os.path.join(root, "index.html"), "rb").read(), "text/html; charset=utf-8")
                proj = parts[0]
                if not os.path.isfile(os.path.join(root, proj, "map.json")): return self.send(404, b"no such map", "text/plain")
                if len(parts) == 1: self.send_response(302); self.send_header("Location", f"/{proj}/map.html"); self.end_headers(); return
                if parts[1:] == ["status.json"]: return self.send(200, project_status(root, proj), "application/json")
                if parts[1:] == ["map.html"]:
                    rerender_if_stale(root, proj)
                    return self.send(200, open(os.path.join(root, proj, "map.html"), "rb").read(), "text/html; charset=utf-8")
                if parts[1:] == ["map.json"]: return self.send(200, open(os.path.join(root, proj, "map.json"), "rb").read(), "application/json")
            except Exception as e:   # keep serving; show the problem to the page
                return self.send(500, f"board error: {e}".encode(), "text/plain")
            self.send(404, b"not found", "text/plain")
    return H

def running(port):
    import socket
    with socket.socket() as c:
        c.settimeout(0.5)
        return c.connect_ex(("127.0.0.1", port)) == 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Live agent board for the maps in ~/agent-maps")
    ap.add_argument("--root", default="~/agent-maps"); ap.add_argument("--port", type=int, default=8765); ap.add_argument("--once")
    ap.add_argument("--open", nargs="?", const="", metavar="PROJECT",
                    help="open the board in your browser (starts it first unless it already runs); optionally straight to one map")
    a = ap.parse_args(); root = X(a.root)
    if a.once: print(json.dumps(status(json.load(open(os.path.join(root, a.once, "map.json")))), indent=1)); sys.exit()
    url = f"http://127.0.0.1:{a.port}/" + (f"{a.open}/" if a.open else "")
    if a.open is not None and running(a.port):
        import webbrowser; webbrowser.open(url); print(f"board already running: opened {url}"); sys.exit()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), handler(root))
    print(f"agent board on http://127.0.0.1:{a.port}/ (maps in {root}); Ctrl+C stops it", flush=True)
    if a.open is not None:
        import webbrowser; threading.Timer(0.3, webbrowser.open, [url]).start()
    srv.serve_forever()
