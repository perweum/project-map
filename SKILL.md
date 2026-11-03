---
name: project-map
description: >
  Create or refresh a visual architecture map of a project: its inputs and data sources, scripts and timers, app
  code, agents and humans, the files they hand over, and its outputs. A script discovers the parts; you tidy the
  draft and render an interactive page (pan, zoom, hover details) under ~/agent-maps/<project>/. Also checks whether
  an existing map is out of date. Use when asked to map, draw, visualise or explain how a project and its agents fit
  together, or to update a map. Triggers: project map, architecture drawing, agent map, draw the setup, node map.
---

# Project map

The scripts sit next to this file; below, `<skill>` means this skill's folder. They need only Python 3.10+.
The map is data (`map.json`); the look comes from the shared renderer. Never invent parts: the discovery script
finds facts, you add names, grouping and descriptions. If unsure whether something exists, leave it out and say so.

## Where things go
- `~/agent-maps/<project-folder-name>/map.json` and `map.html`, outside the repo (nothing gets published).
  Only put a map in a repository if the user asks for that.
- Existing map? **Update** it (step 1b); keep ids, positions, zones and wording the user tuned.

## 1a. New map: discover
`python3 -I -B <skill>/discover.py <repo> -o ~/agent-maps/<project>/map.json`
It finds scripts, package/Makefile commands, CI workflows, timers, app modules and their imports, docker-compose
services, agent skills that mention the project and the folders they hand work through, data folders, fetched hosts,
and generated outputs, with links between them and a column layout. Each box has `source` (where it came from).

## 1b. Existing map: drift and update
- `python3 -I -B <skill>/discover.py <repo> --check <map.json>`: lists boxes whose files are gone (STALE) and parts not on the map (NEW).
- `python3 -I -B <skill>/discover.py <repo> --update <map.json>`: adds NEW parts (marked `"new": true`, placed under boxes of
  the same kind) and marks STALE boxes; it never moves or deletes anything. Then tidy those boxes (step 2) and remove `new`.
- Parts the user does not want on the map: `--baseline <map.json>` records everything currently left out in `ignore`.

## 2. Tidy the draft (this is your job)
Read the instruction files (`AGENTS.md`, `CLAUDE.md`, `README*`, `CONTRIBUTING*`) and the skills involved, then edit map.json:
- **Names and text:** `title` <= 22 characters, `sub` <= 36 (what it does), `detail` 1-2 sentences for the hover panel,
  `model` for agents if their settings or skill say which model they use.
- **Merge and drop:** merge minor scripts into one box ("update.py + gen_*.py"), drop noise (one-off research scripts,
  links that are not real). Aim for at most ~30 boxes. Keep every `source` (merge the lists) so drift keeps working.
- **Add what a script cannot see:** humans and what they do (approve, record, release), external inputs named only in docs,
  the hand-over files between agents. Give added boxes a `source` when they correspond to a file.
- **Zones** (`groups`): tidy blocks such as Sources, Timers & CI, Generators/Build, App code, Change pipeline, Shipped.
- **Layout:** `x`, `y` are grid units of 20 px; a box is 11 x 4 units. Stack with pitch 6, leave ~7-9 units between zones
  so wires have corridors, and read left to right as the flow. Place a box level with what it talks to most.

Format reference:
```json
{"version": 1, "title": "…", "subtitle": "what it is · updated <date>", "repo": "~/path/to/repo",
 "nodes": [{"id": "gens", "x": 18, "y": 10, "kind": "script", "title": "update.py + gen_*.py", "sub": "regenerate data",
            "detail": "…", "source": ["tools/update.py", "tools/gen_*.py"]}],
 "edges": [["gens", "data", "data"]],
 "groups": [{"label": "Generators", "nodes": ["gens"], "kind": "script"}],
 "ignore": [], "live": {}}
```
**Inner maps:** discover.py gives merged boxes and app folders an `inner` map (shown when the box is opened). Keep them
when you tidy; you may rename their boxes or drop noise inside. To give a hand-made box detail, add
`"inner": {"nodes": [...], "edges": [...]}` (x/y optional; `"outside": true` for boxes from the level above).
`version` is the map format (currently 1); keep it, and never raise it by hand.
Node kinds: `input`, `script`, `service` (CI, timers, containers), `module` (app code), `agent` (a model, costs tokens),
`human`, `file` (hand-over file or folder), `output`. Edge kinds: `data`, `control` (starts/calls), `human`, `feedback`.

## 3. Render and look at it
`python3 -I -B <skill>/render_map.py ~/agent-maps/<project>/map.json --png /tmp/<project>-map.png`
Fix every error it prints; read the warnings (unconnected boxes, frames covering boxes, drift). Open the PNG and check
that no text overflows, wires do not bunch up (spread zones apart) and the flow reads left to right. Re-render after changes.
The page pans by dragging and zooms with pinch, Ctrl+scroll or its buttons, so it also works on a phone.

## 4. Live board (optional; ask the user before adding it to a new project)
`python3 -I -B <skill>/board.py --open` serves every map in ~/agent-maps on http://127.0.0.1:8765/ and colours boxes from local
facts. discover.py drafts a `live` section; check and adjust it. Only these read-only probes exist:
```json
"live": {
  "match": "<text in the agent session folder names, usually the repo folder name>", "working_seconds": 60,
  "sessions": [{"node": "impl", "match": "implementation sub-agent|<project>-implementer"}],
  "files":    [{"node": "reviews", "glob": "~/loop/reviews/*.md", "verdict": true},
               {"node": "flags", "glob": "~/loop/PENDING*", "alert": true},
               {"node": "gate", "glob": "/tmp/review-*", "working_if_exists": true, "label": "review worktree"}],
  "units":    [{"node": "check", "unit": "<systemd user unit name>"}],
  "git":      [{"node": "pr", "repo": "~/repo", "exclude": ["main", "t3/*"]}],
  "process":  [{"node": "app", "pattern": "<pgrep -f pattern>", "running": "app running", "stopped": "app closed"}]}
```
The board also serves Arrange mode: the user can drag boxes and edit zones on the page and press Save, which rewrites
only positions and `groups` in map.json. Re-read map.json before editing it, so you do not overwrite their layout.
Session rules match a session's name plus the first 300 characters of its first prompt; the first matching rule wins.
Verify with `python3 -I -B <skill>/board.py --once <project>`: every agent box should show its own sessions.

Never print environment variables or secrets while discovering (`env`, `printenv`, `.env` files): they end up in logs.

## 5. Reply
Give the path to `map.html` (and the board URL if it runs), a few lines on what the map shows, and anything you left
out because you could not confirm it.
