"""Live Lens, the processing side: turns what the editor collected during a session into the cache.

The DwoLensInspector editor plugin (DwoLensLive.cpp) writes, per editor run, __dwo-forge/live/sessions/<id>/:
events.jsonl (what happened), raw/ and saves/NNNN/ (DwoLens raw data, full or only changed packages) and
session.json. After the editor closed, forge.ps1 live-ingest calls this module:

    merge       raw of the session -> __dwo-forge/live/base/ (the complete, current raw data). A full collection
                replaces it; a partial one replaces only the Blueprints it collected and drops removed packages
    accumulate  after the analysis of the new run: what changed against the previous live run (classes, flow
                links, session activity) -> out/changes.json and live/history.jsonl; classes seen in play
                -> live/runtime.json; retention by the 'keep' setting:
                    latest    only the newest live run is kept
                    history   every run's results and the change log are kept, raw data only for the newest
                    snapshot  every run is kept complete (raw data too)

The cache is disposable: 'forge.ps1 live rebuild' drops live/base and the next session collects everything.

    python lens_live.py merge --session <dir> --base <dir>
    python lens_live.py accumulate --project <dir> --run <run dir> --session <dir> --keep history
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

RAW_FILES = ("meta.json", "reflection.json", "blueprints.json", "assets.json")


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def save(path: Path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def events(session: Path) -> list[dict]:
    out = []
    try:
        for line in (session / "events.jsonl").read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def collected_dirs(session: Path) -> list[Path]:
    """Save-time exports in order, then the exit collection."""
    dirs = sorted(p for p in (session / "saves").glob("*") if (p / "meta.json").exists()) if (session / "saves").is_dir() else []
    if (session / "raw" / "meta.json").exists():
        dirs.append(session / "raw")
    return dirs


def bp_package(entry: dict) -> str:
    return entry.get("package") or entry.get("asset", "").split(".")[0]


def class_package(entry: dict) -> str:
    return entry.get("package") or entry.get("path", "").split(".")[0]


def merge_one(base: Path, raw: Path, removed: set[str]) -> str:
    meta = load(raw / "meta.json", {})
    if not meta.get("partial"):
        base.mkdir(parents=True, exist_ok=True)
        for name in RAW_FILES:
            shutil.copy2(raw / name, base / name)
        return "full"
    if not (base / "meta.json").exists():
        raise SystemExit("lens_live: a partial collection needs a full one first (live rebuild)")
    packages = set(meta.get("packages", [])) | removed
    bps = load(base / "blueprints.json", {"blueprints": []})
    new_bps = load(raw / "blueprints.json", {"blueprints": []})["blueprints"]
    bps["blueprints"] = [b for b in bps["blueprints"] if bp_package(b) not in packages] + new_bps
    save(base / "blueprints.json", bps)
    refl = load(base / "reflection.json", {"native": [], "blueprint": []})
    new_refl = load(raw / "reflection.json", {"native": [], "blueprint": []})
    refl["native"] = new_refl.get("native", refl.get("native", []))          # always complete
    refl["blueprint"] = [c for c in refl.get("blueprint", []) if class_package(c) not in packages] + new_refl.get("blueprint", [])
    save(base / "reflection.json", refl)
    if not meta.get("assetsSkipped"):
        shutil.copy2(raw / "assets.json", base / "assets.json")
    elif removed:
        assets = load(base / "assets.json", {"assets": []})
        assets["assets"] = [a for a in assets["assets"] if a.get("package") not in removed]
        save(base / "assets.json", assets)
    base_meta = load(base / "meta.json", {})
    base_meta.update({k: v for k, v in meta.items() if k not in ("partial", "packages", "assetsSkipped")})
    base_meta["partial"] = False
    save(base / "meta.json", base_meta)
    return f"partial ({len(meta.get('packages', []))})"


def cmd_merge(session: Path, base: Path) -> int:
    summary = load(session / "session.json", {})
    removed = set(summary.get("removed", []))
    dirs = collected_dirs(session)
    if not dirs:
        print("lens_live: nothing collected in this session")
        return 3
    for d in dirs:
        print(f"lens_live: merged {d.name}: {merge_one(base, d, removed)}")
    return 0


# ---------------------------------------------------------------------------------------------- accumulate

def live_runs(runs_dir: Path) -> list[Path]:
    out = []
    for run in sorted(runs_dir.glob("run-*")):
        meta = load(run / "run.json", {})
        if meta.get("source") == "live":
            out.append(run)
    return out


def project_classes(run: Path) -> set[str]:
    g = load(run / "out" / "graph.json", {})
    return {s["key"] for s in g.get("symbols", []) if s.get("kind") in ("blueprint", "class") and not s.get("external")}


def flow_edges(run: Path) -> set[tuple[str, str, str]]:
    f = load(run / "out" / "flow.json", {})
    return {(e["from"], e["to"], e["kind"]) for e in f.get("edges", [])}


def short(key: str) -> str:
    name = key.replace(":", "/").rsplit("/", 1)[-1].split(".")[-1]
    return name[:-2] if name.endswith("_C") else name


def session_activity(session: Path) -> dict:
    ev = events(session)
    count = lambda t: sum(1 for e in ev if e.get("type") == t)
    saved = sorted({e.get("package") for e in ev if e.get("type") == "asset_saved"})
    opened = sorted({e.get("package") for e in ev if e.get("type") == "asset_opened"})
    in_play: dict[str, int] = {}
    for e in ev:
        if e.get("type") == "pie_end":
            for cls, n in (e.get("classes") or {}).items():
                in_play[cls] = max(in_play.get(cls, 0), int(n))
    errors = [e for e in ev if e.get("type") == "log" and e.get("level") == "error"]
    bp_errors = sorted({e.get("package") for e in ev if e.get("type") == "bp_compiled" and e.get("status") == "error"})
    return {"saved": saved, "opened": opened, "removed": sorted({e.get("package") for e in ev if e.get("type") == "asset_removed"}),
            "pieRuns": count("pie_end"), "inPlay": in_play, "logErrors": len(errors),
            "logWarnings": sum(1 for e in ev if e.get("type") == "log" and e.get("level") == "warning"),
            "firstErrors": [f"{e.get('category')}: {e.get('message', '')[:200]}" for e in errors[:10]],
            "blueprintErrors": bp_errors, "mapsOpened": sorted({e.get("map") for e in ev if e.get("type") == "map_opened"}),
            "reloads": count("cpp_reloaded")}


def update_runtime(live: Path, activity: dict, session_id: str):
    """Classes that existed while the game ran in the editor, accumulated over sessions (for the game flow)."""
    path = live / "runtime.json"
    doc = load(path, {"schema": "lens.runtime/1", "classes": {}})
    for cls, n in activity["inPlay"].items():
        c = doc["classes"].setdefault(cls, {"sessions": 0, "maxCount": 0})
        c["sessions"] += 1
        c["maxCount"] = max(c["maxCount"], n)
        c["lastSession"] = session_id
    save(path, doc)


def cmd_accumulate(project: Path, run: Path, session: Path, keep: str) -> int:
    forge = project / "__dwo-forge"
    live = forge / "live"
    runs = live_runs(forge / "data" / "runs")
    previous = [r for r in runs if r != run]
    prev = previous[-1] if previous else None
    now_classes, now_edges = project_classes(run), flow_edges(run)
    before_classes = project_classes(prev) if prev else set()
    before_edges = flow_edges(prev) if prev else set()
    activity = session_activity(session)
    summary = load(session / "session.json", {})
    changes = {
        "schema": "lens.changes/1", "session": summary.get("id", session.name), "run": run.name,
        "previousRun": prev.name if prev else None, "ended": summary.get("ended"), "collected": summary.get("collected"),
        "exitSeconds": summary.get("exitSeconds"),
        "classesAdded": sorted(short(k) for k in now_classes - before_classes) if prev else [],
        "classesRemoved": sorted(short(k) for k in before_classes - now_classes) if prev else [],
        "flowAdded": [f"{short(a)} -{k}-> {short(b)}" for a, b, k in sorted(now_edges - before_edges)][:200] if prev else [],
        "flowRemoved": [f"{short(a)} -{k}-> {short(b)}" for a, b, k in sorted(before_edges - now_edges)][:200] if prev else [],
        "activity": activity,
    }
    save(run / "out" / "changes.json", changes)
    update_runtime(live, activity, changes["session"])
    line = {k: v for k, v in changes.items() if k != "activity"}
    line.update({"saved": len(activity["saved"]), "opened": len(activity["opened"]), "pieRuns": activity["pieRuns"],
                 "logErrors": activity["logErrors"], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if keep != "latest":
        with (live / "history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    # retention
    for old in previous:
        if keep == "latest":
            shutil.rmtree(old, ignore_errors=True)
        elif keep == "history":
            shutil.rmtree(old / "raw", ignore_errors=True)
    summary["ingested"] = run.name
    save(session / "session.json", summary)
    print(f"lens_live: {run.name}: +{len(changes['classesAdded'])}/-{len(changes['classesRemoved'])} classes, "
          f"+{len(changes['flowAdded'])}/-{len(changes['flowRemoved'])} flow links, keep={keep}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("merge")
    m.add_argument("--session", required=True, type=Path)
    m.add_argument("--base", required=True, type=Path)
    a = sub.add_parser("accumulate")
    a.add_argument("--project", required=True, type=Path)
    a.add_argument("--run", required=True, type=Path)
    a.add_argument("--session", required=True, type=Path)
    a.add_argument("--keep", choices=("latest", "history", "snapshot"), default="history")
    args = ap.parse_args(argv)
    if args.cmd == "merge":
        return cmd_merge(args.session, args.base)
    return cmd_accumulate(args.project, args.run, args.session, args.keep)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
