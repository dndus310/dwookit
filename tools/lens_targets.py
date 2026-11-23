"""dwo-Forge: what the user can pick to follow, read straight from the project files (no editor needed).

    python lens_targets.py --project <dir> [-o targets.json]

Lists
  - Blueprints under Content/ and project plugins' Content/ (a .uasset whose name table holds a Blueprint
    generated class): package path (/Game/... or /<Plugin>/...), kind (blueprint, widget, anim), file, the
    runtime class name (<Name>_C, what DwoStateMonitor watches) and whether `bp add` already tracks it
  - C++ UCLASSes in Source/ and project plugins' Source/: class, runtime class name (prefix A/U dropped),
    parent, header, module
  - the current watch lists: __dwo-forge/bp-track.txt (Lens) and __dwo-forge/sm-watch.txt (DwoStateMonitor)

The dwo-Forge window shows this as a checkable tree; the CLI uses it for `bp pick` and `sm-watch pick`.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HEAD_BYTES = 1 << 20          # the name table sits in the package header; 1 MB covers it for real assets
SKIP_PLUGINS = {"__DwoLensInspector", "__DwoStateMonitor", "__DwoCanvas", "__StateMonitor"}
MARKERS = [(b"ControlRigBlueprintGeneratedClass", "rig"), (b"WidgetBlueprintGeneratedClass", "widget"), (b"AnimBlueprintGeneratedClass", "anim"),
           (b"BlueprintGeneratedClass", "blueprint")]
UCLASS_RE = re.compile(r"UCLASS\s*\((?P<spec>[^)]*)\)\s*class\s+(?:\w+_API\s+)?(?P<name>[AU]\w+)\s*(?:final\s*)?:\s*public\s+(?P<parent>\w+)")


def content_roots(project: Path) -> list[tuple[Path, str]]:
    roots = [(project / "Content", "/Game")]
    plugins = project / "Plugins"
    if plugins.is_dir():
        for up in plugins.rglob("*.uplugin"):
            if any(part in SKIP_PLUGINS for part in up.parts):
                continue
            content = up.parent / "Content"
            if content.is_dir():
                roots.append((content, "/" + up.stem))
    return roots


def blueprint_kind(path: Path) -> str | None:
    """A Blueprint asset's name table holds its own generated class '<Name>_C'. Placed actors, data assets and
    levels that only reference a Blueprint class hold some other '<X>_C' and are skipped."""
    if any(part.startswith("__External") for part in path.parts):
        return None
    try:
        with path.open("rb") as f:
            head = f.read(HEAD_BYTES)
    except OSError:
        return None
    own = f"{path.stem}_C".encode("utf-8")
    if own + bytes(1) not in head:
        return None
    for marker, kind in MARKERS:
        if marker in head:
            return kind
    return None


def read_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def tracked_by(package: str, entries: list[str]) -> bool:
    return any(package == e or (e.endswith("/") and package.startswith(e)) for e in entries)


def scan_blueprints(project: Path, tracked: list[str]) -> list[dict]:
    out = []
    for root, mount in content_roots(project):
        for f in sorted(root.rglob("*.uasset")):
            kind = blueprint_kind(f)
            if not kind:
                continue
            rel = f.relative_to(root).with_suffix("").as_posix()
            package = f"{mount}/{rel}"
            out.append({"package": package, "name": f.stem, "kind": kind,
                        "file": f.relative_to(project).as_posix(), "runtimeClass": f"{f.stem}_C",
                        "tracked": tracked_by(package, tracked)})
    return out


def source_roots(project: Path) -> list[Path]:
    roots = [project / "Source"]
    plugins = project / "Plugins"
    if plugins.is_dir():
        for up in plugins.rglob("*.uplugin"):
            if not any(part in SKIP_PLUGINS for part in up.parts) and (up.parent / "Source").is_dir():
                roots.append(up.parent / "Source")
    return [r for r in roots if r.is_dir()]


def scan_classes(project: Path) -> list[dict]:
    out = []
    for root in source_roots(project):
        for h in sorted(root.rglob("*.h")):
            try:
                text = h.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            if "UCLASS" not in text:
                continue
            module = h.relative_to(root).parts[0] if len(h.relative_to(root).parts) > 1 else ""
            for m in UCLASS_RE.finditer(text):
                name = m.group("name")
                out.append({"name": name, "runtimeClass": name[1:], "parent": m.group("parent"),
                            "actor": name.startswith("A"), "abstract": "Abstract" in m.group("spec"),
                            "file": h.relative_to(project).as_posix(), "module": module})
    return out


def collect(project: Path) -> dict:
    forge = project / "__dwo-forge"
    tracked = read_list(forge / "bp-track.txt")
    watched = [l.split()[0] for l in read_list(forge / "sm-watch.txt")]
    blueprints = scan_blueprints(project, tracked)
    classes = scan_classes(project)
    for item in blueprints + classes:
        item["watched"] = item["runtimeClass"] in watched
    return {"schema": "forge.targets/1", "blueprints": blueprints, "classes": classes,
            "bpTrack": tracked, "smWatch": watched}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True, type=Path)
    ap.add_argument("-o", type=Path)
    args = ap.parse_args(argv)
    data = collect(args.project.resolve())
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.o:
        args.o.write_text(text, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
