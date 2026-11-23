"""dwo-Forge agent context: tells coding agents (Claude Code, GitHub Copilot) what dwo-Forge already knows about
the project and how to work with it cheaply.

    __dwo-forge/context/AGENT.md   rewritten by dwo-Forge: how to read the tracked Blueprints (index -> overview ->
                                   parts, cheap subagents for large ones), what to deliver (C++ or an editor
                                   checklist, never .uasset edits), output format, commands, freshness
    .github/instructions/project.instructions.md
                                   the user's own instructions (naming, folders, "no Tick" ...): created once as an
                                   empty shell (applyTo "**"), never overwritten; Copilot reads it by itself
    .github/instructions/dwo-forge.instructions.md
                                   VS Code Copilot: applyTo "**", tells it to read AGENT.md first
    CLAUDE.local.md                Claude Code: a managed block that imports AGENT.md and the user's instructions
                                   (the file is personal and not committed; a git-tracked one is left alone)

    python lens_agent.py write  --project <dir>      AGENT.md + pointer files + the user's empty shell (unless 'off')
    python lens_agent.py remove --project <dir> [--backup <dir>]
                                                     remove them (uninstall, 'agent off'); the user's instructions
                                                     file is copied to --backup first when it has content
    python lens_agent.py status --project <dir>

Standard library only, no network.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lens_bp  # noqa: E402

BLOCK_BEGIN = "<!-- >>> dwo-forge managed (removed by __dwo-forge.bat uninstall) >>> -->"
BLOCK_END = "<!-- <<< dwo-forge managed <<< -->"
COPILOT_MARK = "<!-- dwo-forge managed: removed by __dwo-forge.bat uninstall -->"
CLAUDE_FILE = "CLAUDE.local.md"
COPILOT_FILE = ".github/instructions/dwo-forge.instructions.md"

USER_FILE = ".github/instructions/project.instructions.md"
USER_MARK = "<!-- dwo-forge: project instructions"
USER_SHELL = (
    "---\n"
    'applyTo: "**"\n'
    "---\n"
    f"{USER_MARK} (yours: naming, folders, what not to do). Created empty by dwo-Forge and never overwritten; "
    "uninstall backs it up, then removes it. To keep it in git: git add -f this file. -->\n"
)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ------------------------------------------------------------------------------------------------ AGENT.md

def blueprint_state(forge: Path, project: Path) -> dict:
    tracked = []
    track = forge / "bp-track.txt"
    if track.exists():
        tracked = [l.strip() for l in track.read_text(encoding="utf-8-sig").splitlines()
                   if l.strip() and not l.strip().startswith("#")]
    manifest = lens_bp.load(forge / "blueprints" / "export.json") or {}
    exported = manifest.get("exported", [])
    changed = []
    split = 0
    for e in exported:
        folder = forge / "blueprints" / e["dir"]
        bp = lens_bp.load(folder / f"{e['name']}.json") or {}
        if lens_bp.source_changed(bp, project):
            changed.append(e["name"])
        split += (folder / "parts").is_dir()
    return {"tracked": tracked, "exported": len(exported), "exportedAt": manifest.get("exportedAtUtc", ""),
            "changed": changed, "split": split, "missing": manifest.get("missing", [])}


def latest_analysis(forge: Path) -> str | None:
    runs = forge / "data" / "runs"
    if not runs.is_dir():
        return None
    for run in sorted((d for d in runs.iterdir() if d.is_dir()), reverse=True):
        meta = lens_bp.load(run / "run.json") or {}
        if meta.get("line") == "architecture-report" and (run / "out").is_dir():
            return run.name
    return None


def agent_md(forge: Path, project: Path) -> str:
    bp = blueprint_state(forge, project)
    run = latest_analysis(forge)
    if bp["exported"]:
        fresh = (f"{bp['exported']} tracked Blueprint(s), exported {bp['exportedAt'][:16].replace('T', ' ')} UTC"
                 + (f", {bp['split']} split into parts" if bp["split"] else ""))
        if bp["changed"]:
            fresh += (f". CHANGED since the export (the pseudocode may be stale; ask the user to run "
                      f"`__dwo-forge.bat bp export` with the editor closed): {', '.join(bp['changed'][:10])}")
    elif bp["tracked"]:
        fresh = f"{len(bp['tracked'])} Blueprint path(s) tracked but not exported yet: `__dwo-forge.bat bp export`."
    else:
        fresh = "No Blueprint is tracked yet. The user adds them with `__dwo-forge.bat bp add <path>`."
    has_rules = user_file_has_content(project)

    return f"""\
# dwo-Forge: how to work in this Unreal project

Written by dwo-Forge ({now_utc()}); rewritten after every `bp export` and `analyze`. Do not edit: project rules go
in `{USER_FILE}`{' (it has rules: read it, it wins over this file)' if has_rules else ' (empty so far)'}.
All paths below are relative to the project folder. `__dwo-forge/` is git-excluded, so search tools may skip it:
open its files by path.

## What is already known
- Blueprints: {fresh}
  Start at `__dwo-forge/blueprints/index.md`.
- Architecture analysis: {f'latest run `__dwo-forge/data/runs/{run}/out/` (report.html, findings.json)' if run else 'none yet (`__dwo-forge.bat analyze`)'}.

## Reading Blueprints (cheapest first)
`.uasset` files are binary: never open them, never ask for copied T3D text. Use the exported files:
1. `blueprints/index.md`: one row per tracked Blueprint (type, parent, size, parts, changed since export).
2. `<Name>.md`: pseudocode. Small Blueprints are complete in this one file. Large ones are an overview (vars,
   components, defaults, widgets) plus a `## parts` table: one row per event or function with its size,
   the variables it writes and the own functions it calls. Decide from the table which parts matter.
3. `parts/<on_X|fn_X>.md`: one event or function, readable on its own (it lists the vars and components it uses).
4. `<Name>.json`: every node, pin, default and link. Open it only for one exact detail, never to read it all.
Pseudocode: `on X():` event, `fn` function, `await` latent call, `goto Ln` exec wires that join, `# ...` author
comment, `X as T` cast, arguments only where the author changed the default.

## Large or many Blueprints: delegate, then design
When the task spans more than ~300 lines of pseudocode (refactoring, review, "where is X done"):
- Give each relevant part file to a cheap subagent (Claude Code: the Agent tool with a small model such as
  Haiku; other tools: read one part at a time and keep only your notes). Prompt it with:
  "Read <path>. Reply in at most 8 lines, no prose: purpose; state read/written; calls (own and engine);
  duplicated or dead logic; candidates to move into a function, an interface, a component or C++."
- Make the design decisions yourself from those summaries. Do not pull whole graphs into your own context.

## What to deliver
Agents cannot edit Blueprint assets. Deliver one of:
- **C++** (preferred for heavy or repeated logic): a C++ base class, component or BlueprintFunctionLibrary,
  exposed with `UFUNCTION(BlueprintCallable)`, `BlueprintImplementableEvent` or `BlueprintNativeEvent`, plus the
  short editor steps to reparent or call it.
- **Editor checklist** for changes that stay in Blueprint, exactly this format:
  `| # | Blueprint | graph / function | change |` one row per edit, in the order the user applies them
  (e.g. `| 1 | BP_Door | EventGraph / on Open Door | replace the Delay with a Timer by Event (0.3 s) |`).
Output: the change list or the code only. No explanations, background or summaries unless the user asks.

## Checking the work (the user runs these with the editor closed)
- `__dwo-forge.bat compile`: C++ build (editor target).
- `__dwo-forge.bat verify`: compiles every Blueprint, data validation, automation tests.
- `__dwo-forge.bat bp export`: refresh the pseudocode after Blueprint edits.
- `__dwo-forge.bat analyze`: architecture report and quality checks.
Never run these while the Unreal Editor has the project open.
"""


def user_file_has_content(project: Path) -> bool:
    """The user's instructions file says something beyond the shell (front matter, comments, headings)."""
    f = project / USER_FILE
    if not f.exists():
        return False
    text = _strip_comments(f.read_text(encoding="utf-8-sig", errors="replace"))
    if text.startswith("---"):
        text = text.split("---", 2)[-1]
    return any(l.strip() and not l.lstrip().startswith("#") for l in text.splitlines())


def _strip_comments(text: str) -> str:
    out, rest = [], text
    while "<!--" in rest:
        before, _, after = rest.partition("<!--")
        out.append(before)
        rest = after.partition("-->")[2]
    out.append(rest)
    return "".join(out)


# ------------------------------------------------------------------------------------------------ pointers

def read_text(path: Path) -> tuple[str, bool, str]:
    """(text, had BOM, newline) so a file the user owns keeps its encoding and line endings."""
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="replace")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), bom, newline


def write_text(path: Path, text: str, bom: bool, newline: str):
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.replace("\n", newline).encode("utf-8"))


def git_tracked(project: Path, rel: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(project), "ls-files", "--error-unmatch", rel],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def without_block(text: str) -> str:
    lines = text.split("\n")
    out, inside = [], False
    for line in lines:
        if line.strip() == BLOCK_BEGIN:
            inside = True
            continue
        if inside:
            if line.strip() == BLOCK_END:
                inside = False
            continue
        out.append(line)
    return "\n".join(out)


def claude_block() -> str:
    return "\n".join([
        BLOCK_BEGIN,
        "This Unreal project is connected to dwo-Forge. Follow these before any task:",
        "@__dwo-forge/context/AGENT.md",
        f"@{USER_FILE}",
        BLOCK_END,
    ])


def copilot_text() -> str:
    return "\n".join([
        "---",
        'applyTo: "**"',
        "---",
        COPILOT_MARK,
        "This Unreal project is connected to dwo-Forge. Before any task, read `__dwo-forge/context/AGENT.md`",
        "(open it by path: the folder is git-excluded, so search may skip it) and follow it: how to read the",
        "exported Blueprint pseudocode, what to deliver and the output format. project.instructions.md wins over it.",
        "",
    ])


def install_pointers(project: Path) -> list[str]:
    notes = []
    claude = project / CLAUDE_FILE
    if git_tracked(project, CLAUDE_FILE):
        notes.append(f"{CLAUDE_FILE} is committed to git: not changed. Add this line yourself if the team wants it: "
                     "@__dwo-forge/context/AGENT.md")
    elif claude.exists():
        text, bom, nl = read_text(claude)
        base = without_block(text).rstrip("\n")
        new = (base + "\n\n" if base.strip() else "") + claude_block() + "\n"
        if new != text:
            write_text(claude, new, bom, nl)
            notes.append(f"{CLAUDE_FILE}: dwo-forge block updated")
    else:
        write_text(claude, claude_block() + "\n", False, "\n")
        notes.append(f"{CLAUDE_FILE}: created (Claude Code reads it automatically)")

    user = project / USER_FILE
    if not user.exists():
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(USER_SHELL, encoding="utf-8", newline="\n")
        notes.append(f"{USER_FILE}: created empty (write your project instructions there)")

    copilot = project / COPILOT_FILE
    if copilot.exists() and COPILOT_MARK not in copilot.read_text(encoding="utf-8-sig", errors="replace"):
        notes.append(f"{COPILOT_FILE} exists and is not dwo-Forge's: left alone")
    elif not copilot.exists() or copilot.read_text(encoding="utf-8") != copilot_text():
        copilot.parent.mkdir(parents=True, exist_ok=True)
        copilot.write_text(copilot_text(), encoding="utf-8", newline="\n")
        notes.append(f"{COPILOT_FILE}: written (VS Code Copilot)")
    return notes


def remove_pointers(project: Path, backup: Path | None = None, with_user_file: bool = True) -> list[str]:
    """Removes what install_pointers wrote. The user's instructions file only goes on uninstall (with_user_file),
    after a copy to backup when it has content; 'agent off' keeps it."""
    notes = []
    user = project / USER_FILE
    if with_user_file and user.exists() and not git_tracked(project, USER_FILE) and USER_MARK in user.read_text(encoding="utf-8-sig", errors="replace"):
        if user_file_has_content(project) and backup is not None:
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(user, backup / user.name)
            notes.append(f"{USER_FILE}: backed up to {backup / user.name}")
        user.unlink()
        notes.append(f"{USER_FILE}: removed")
    claude = project / CLAUDE_FILE
    if claude.exists() and not git_tracked(project, CLAUDE_FILE):
        text, bom, nl = read_text(claude)
        if BLOCK_BEGIN in text:
            rest = without_block(text).strip("\n")
            if rest.strip():
                write_text(claude, rest + "\n", bom, nl)
                notes.append(f"{CLAUDE_FILE}: dwo-forge block removed (your own lines kept)")
            else:
                claude.unlink()
                notes.append(f"{CLAUDE_FILE}: removed")
    copilot = project / COPILOT_FILE
    if copilot.exists() and COPILOT_MARK in copilot.read_text(encoding="utf-8-sig", errors="replace"):
        copilot.unlink()
        notes.append(f"{COPILOT_FILE}: removed")
    for d in (copilot.parent, copilot.parent.parent):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return notes


# ------------------------------------------------------------------------------------------------ commands

def settings_path(forge: Path) -> Path:
    return forge / "agent.json"


def pointers_enabled(forge: Path) -> bool:
    return (lens_bp.load(settings_path(forge)) or {}).get("pointers", True)


def set_pointers_enabled(forge: Path, on: bool):
    settings_path(forge).write_text(json.dumps({"pointers": on}, indent=2) + "\n", encoding="utf-8")


def cmd_write(project: Path) -> int:
    forge = project / "__dwo-forge"
    if not forge.is_dir():
        print(f"lens_agent: {forge} does not exist (project not connected)", file=sys.stderr)
        return 1
    ctx = forge / "context"
    ctx.mkdir(exist_ok=True)
    (ctx / "AGENT.md").write_text(agent_md(forge, project), encoding="utf-8", newline="\n")
    if pointers_enabled(forge):
        for note in install_pointers(project):
            print(f"agent: {note}")
    return 0


def cmd_remove(project: Path, backup: Path | None = None) -> int:
    for note in remove_pointers(project, backup):
        print(f"agent: {note}")
    return 0


def cmd_status(project: Path) -> int:
    forge = project / "__dwo-forge"
    agent = forge / "context" / "AGENT.md"
    print(f"AGENT.md        : {'written' if agent.exists() else 'missing (run: __dwo-forge.bat agent)'}")
    user = project / USER_FILE
    print(f"instructions    : {USER_FILE} "
          + ("(has content)" if user_file_has_content(project) else "(empty)" if user.exists() else "missing"))
    claude = project / CLAUDE_FILE
    if git_tracked(project, CLAUDE_FILE):
        state = "committed to git, not changed by dwo-Forge"
    elif claude.exists() and BLOCK_BEGIN in claude.read_text(encoding="utf-8-sig", errors="replace"):
        state = "has the dwo-forge block"
    else:
        state = "no dwo-forge block"
    print(f"Claude Code     : {CLAUDE_FILE} {state}")
    copilot = project / COPILOT_FILE
    if copilot.exists():
        ours = COPILOT_MARK in copilot.read_text(encoding="utf-8-sig", errors="replace")
        print(f"Copilot         : {COPILOT_FILE} {'written by dwo-Forge' if ours else 'exists, not dwo-Forge'}")
    else:
        print(f"Copilot         : {COPILOT_FILE} missing")
    print(f"pointer files   : {'on' if pointers_enabled(forge) else 'off (__dwo-forge.bat agent on)'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("write", "remove", "status", "on", "off"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True, type=Path)
        p.add_argument("--backup", type=Path)
    args = ap.parse_args(argv)
    project = args.project.resolve()
    if args.cmd == "write":
        return cmd_write(project)
    if args.cmd == "remove":
        return cmd_remove(project, args.backup)
    if args.cmd == "on":
        set_pointers_enabled(project / "__dwo-forge", True)
        return cmd_write(project)
    if args.cmd == "off":
        set_pointers_enabled(project / "__dwo-forge", False)
        for note in remove_pointers(project, with_user_file=False):
            print(f"agent: {note}")
        return 0
    return cmd_status(project)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
