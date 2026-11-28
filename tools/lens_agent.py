"""dwookit agent context: tells coding agents (Claude Code, GitHub Copilot) what dwookit already knows about
the project and how to work with it cheaply.

    __dwookit/context/AGENT.md   rewritten by dwookit: how to read the tracked Blueprints (index -> overview ->
                                   parts, cheap subagents for large ones), what to deliver (C++ or an editor
                                   checklist, never .uasset edits), output format, commands, freshness
    .github/instructions/project.instructions.md
                                   the user's own instructions (naming, folders, "no Tick" ...): created once as an
                                   empty shell (applyTo "**"), never overwritten; Copilot reads it by itself
    .github/instructions/dwookit.instructions.md
                                   VS Code Copilot: applyTo "**", tells it to read AGENT.md first
    CLAUDE.local.md                Claude Code: a managed block that imports AGENT.md and the user's instructions
                                   (the file is personal and not committed; a git-tracked one is left alone)
    library selection              instruction files and skills picked from <dwookit>/library for this project
                                   (__dwookit/agent.json; tools/lens_library.py writes them for Claude Code and
                                   GitHub Copilot, the only agents dwookit targets)

    python lens_agent.py write  --project <dir>      AGENT.md + pointer files + the user's empty shell (unless 'off')
    python lens_agent.py remove --project <dir> [--backup <dir>]
                                                     remove them (uninstall, 'agent off'); the user's instructions
                                                     file is copied to --backup first when it has content
    python lens_agent.py status --project <dir>
    python lens_agent.py use    --project <dir> [--instructions a,b|none] [--skills x|none] [--targets claude,copilot|none]
                                                     change the selection and rewrite (omitted options stay as they are)

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
import lens_library  # noqa: E402

BLOCK_BEGIN = "<!-- >>> dwookit managed (removed by __dwookit.bat uninstall) >>> -->"
BLOCK_END = "<!-- <<< dwookit managed <<< -->"
COPILOT_MARK = "<!-- dwookit managed: removed by __dwookit.bat uninstall -->"
CLAUDE_FILE = "CLAUDE.local.md"
COPILOT_FILE = ".github/instructions/dwookit.instructions.md"

USER_FILE = ".github/instructions/project.instructions.md"
USER_MARK = "<!-- dwookit: project instructions"
# What dwo-Forge (the name before dwookit) wrote: recognized so the first write replaces it and uninstall removes it.
LEGACY_BLOCKS = [("<!-- >>> dwo-forge managed (removed by __dwo-forge.bat uninstall) >>> -->", "<!-- <<< dwo-forge managed <<< -->")]
LEGACY_COPILOT = [(".github/instructions/dwo-forge.instructions.md", "<!-- dwo-forge managed: removed by __dwo-forge.bat uninstall -->")]
LEGACY_USER_MARKS = ["<!-- dwo-forge: project instructions"]
BLOCKS = [(BLOCK_BEGIN, BLOCK_END)] + LEGACY_BLOCKS
USER_SHELL = (
    "---\n"
    'applyTo: "**"\n'
    "---\n"
    f"{USER_MARK} (yours: naming, folders, what not to do). Created empty by dwookit and never overwritten; "
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
                      f"`__dwookit.bat bp export` with the editor closed): {', '.join(bp['changed'][:10])}")
    elif bp["tracked"]:
        fresh = f"{len(bp['tracked'])} Blueprint path(s) tracked but not exported yet: `__dwookit.bat bp export`."
    else:
        fresh = "No Blueprint is tracked yet. The user adds them with `__dwookit.bat bp add <path>`."
    has_rules = user_file_has_content(project)

    return f"""\
# dwookit: how to work in this Unreal project

Written by dwookit ({now_utc()}); rewritten after every `bp export` and `analyze`. Do not edit: project rules go
in `{USER_FILE}`{' (it has rules: read it, it wins over this file)' if has_rules else ' (empty so far)'}.
All paths below are relative to the project folder. `__dwookit/` is git-excluded, so search tools may skip it:
open its files by path.

## What is already known
- Blueprints: {fresh}
  Start at `__dwookit/blueprints/index.md`.
- Architecture analysis: {f'latest run `__dwookit/data/runs/{run}/out/` (report.html, findings.json)' if run else 'none yet (`__dwookit.bat analyze`)'}.

## Ask before you open (`ctx`, a few lines each, no editor needed)
- `__dwookit.bat ctx bp <Name>`: its files, the part file of each event and function, whether the export is older
  than the asset, how much of it the C++ migration moves and why the rest stays.
- `__dwookit.bat ctx refs <Symbol>`: every place a variable, function, event or dispatcher is read, written,
  called, defined, implemented, bound, cast to or spawned, across all tracked Blueprints, with the part file to
  open (`Health`, `BP_Player.Health`, `BPI_Touch.Primary Thumbstick`). Use it to trace an issue or to find what a
  change affects before you read anything. It covers Blueprints only; search C++ yourself.
- `__dwookit.bat ctx find <text>`: names containing the text. `ctx stale`: what changed since the export.
If `ctx` says CHANGED, the exported files are older than the asset: ask the user to run `__dwookit.bat bp export`
(editor closed; it re-exports only what changed) before relying on them.

## Reading Blueprints (cheapest first)
`.uasset` files are binary: never open them, never ask for copied T3D text. Use the exported files:
1. `blueprints/index.md`: one row per tracked Blueprint (type, parent, size, parts, C++ migration coverage,
   changed since export).
2. `<Name>.md`: pseudocode. Small Blueprints are complete in this one file. Large ones are an overview (vars,
   components, defaults, widgets) plus a `## parts` table: one row per event or function with its size,
   the variables it writes and the own functions it calls. Decide from the table which parts matter.
3. `parts/<on_X|fn_X>.md`: one event or function, readable on its own (it lists the vars and components it uses).
4. `<Name>.json`: every node, pin, default and link. Open it only for one exact detail, never to read it all.
5. `<Name>.explain.json`: the same Blueprint in plain words (composition, behaviors, flows that go into own
   functions, other tracked Blueprints and C++ bodies). When the user asks to explain a file for a person, follow
   `<Name>.explain-prompt.md` (it writes `<Name>.agent.json`, which the dwookit window shows). An agent.json
   carries the fingerprint of the version it explains: if it matches explain.json, reuse it instead of rewriting.
Pseudocode: `on X():` event, `fn` function, `await` latent call, `goto Ln` exec wires that join, `# ...` author
comment, `X as T` cast, arguments only where the author changed the default.

## Large or many Blueprints: delegate, then design
When the task spans more than ~300 lines of pseudocode (refactoring, review, "where is X done"):
- Give each relevant part file to a cheap subagent (Claude Code: the Agent tool with a small model such as
  Haiku; other tools: read one part at a time and keep only your notes). Prompt it with:
  "Read <path>. Reply in at most 8 lines, no prose: purpose; state read/written; calls (own and engine);
  duplicated or dead logic; candidates to move into a function, an interface, a component or C++."
- Make the design decisions yourself from those summaries. Do not pull whole graphs into your own context.

## Moving Blueprint logic to C++: let dwookit do it
dwookit translates a Blueprint into a C++ base class and reparents the Blueprint onto it, checked value by value
and by running both versions (`ctx bp <Name>` shows how much it moves). For a Blueprint it covers, propose that
instead of writing the C++ by hand; the user runs, with the editor closed:
`__dwookit.bat native plan|generate <bp>`, `compile`, `native rebase <bp>`, `native verify <bp>`.
Write C++ by hand only for what `ctx bp` lists as staying, or for new logic.

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
- `__dwookit.bat compile`: C++ build (editor target).
- `__dwookit.bat verify`: compiles every Blueprint, data validation, automation tests.
- `__dwookit.bat bp export`: refresh the pseudocode after Blueprint edits (only changed Blueprints; `--all` for every one).
- `__dwookit.bat analyze`: architecture report and quality checks.
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


def has_block(text: str) -> bool:
    return any(begin in text for begin, _ in BLOCKS)


def ours_user_file(text: str) -> bool:
    return any(mark in text for mark in [USER_MARK] + LEGACY_USER_MARKS)


def without_block(text: str) -> str:
    lines = text.split("\n")
    out, end = [], None
    for line in lines:
        if end is None:
            match = next((e for b, e in BLOCKS if line.strip() == b), None)
            if match:
                end = match
                continue
            out.append(line)
        elif line.strip() == end:
            end = None
    return "\n".join(out)


def remove_legacy_copilot(project: Path) -> list[str]:
    notes = []
    for name, mark in LEGACY_COPILOT:
        f = project / name
        if f.exists() and not git_tracked(project, name) and mark in f.read_text(encoding="utf-8-sig", errors="replace"):
            f.unlink()
            notes.append(f"{name}: removed (dwo-Forge name)")
    return notes


def claude_block(imports: list[str] | None = None) -> str:
    return "\n".join([
        BLOCK_BEGIN,
        "This Unreal project is connected to dwookit. Follow these before any task:",
        "@__dwookit/context/AGENT.md",
        *(imports or []),
        f"@{USER_FILE}",
        BLOCK_END,
    ])


def copilot_text() -> str:
    return "\n".join([
        "---",
        'applyTo: "**"',
        "---",
        COPILOT_MARK,
        "This Unreal project is connected to dwookit. Before any task, read `__dwookit/context/AGENT.md`",
        "(open it by path: the folder is git-excluded, so search may skip it) and follow it: how to read the",
        "exported Blueprint pseudocode, what to deliver and the output format. project.instructions.md wins over it.",
        "",
    ])


def install_pointers(project: Path, sel: dict | None = None) -> list[str]:
    """Writes the pointer files for the selected agents and the library selection; removes what an agent that is
    no longer selected had."""
    sel = sel or {"targets": list(lens_library.TARGETS), "instructions": [], "skills": []}
    forge = project / "__dwookit"
    targets = sel.get("targets", [])
    lib_notes, imports, installed = lens_library.sync_project(project, forge, sel, lens_library.root_of(),
                                                             lambda rel: git_tracked(project, rel))
    write_installed(forge, installed)
    notes = []
    claude = project / CLAUDE_FILE
    if "claude" not in targets:
        notes += remove_claude_block(project)
    elif git_tracked(project, CLAUDE_FILE):
        notes.append(f"{CLAUDE_FILE} is committed to git: not changed. Add this line yourself if the team wants it: "
                     "@__dwookit/context/AGENT.md")
    elif claude.exists():
        text, bom, nl = read_text(claude)
        base = without_block(text).rstrip("\n")
        new = (base + "\n\n" if base.strip() else "") + claude_block(imports) + "\n"
        if new != text:
            write_text(claude, new, bom, nl)
            notes.append(f"{CLAUDE_FILE}: dwookit block updated")
    else:
        write_text(claude, claude_block(imports) + "\n", False, "\n")
        notes.append(f"{CLAUDE_FILE}: created (Claude Code reads it automatically)")

    user = project / USER_FILE
    if targets and not user.exists():
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(USER_SHELL, encoding="utf-8", newline="\n")
        notes.append(f"{USER_FILE}: created empty (write your project instructions there)")

    copilot = project / COPILOT_FILE
    if "copilot" not in targets:
        if copilot.exists() and COPILOT_MARK in copilot.read_text(encoding="utf-8-sig", errors="replace"):
            copilot.unlink()
            notes.append(f"{COPILOT_FILE}: removed (GitHub Copilot not selected)")
    elif copilot.exists() and COPILOT_MARK not in copilot.read_text(encoding="utf-8-sig", errors="replace"):
        notes.append(f"{COPILOT_FILE} exists and is not dwookit's: left alone")
    elif not copilot.exists() or copilot.read_text(encoding="utf-8") != copilot_text():
        copilot.parent.mkdir(parents=True, exist_ok=True)
        copilot.write_text(copilot_text(), encoding="utf-8", newline="\n")
        notes.append(f"{COPILOT_FILE}: written (VS Code Copilot)")
    for d in (copilot.parent, copilot.parent.parent):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return notes + lib_notes + remove_legacy_copilot(project)


def remove_claude_block(project: Path) -> list[str]:
    claude = project / CLAUDE_FILE
    if not claude.exists() or git_tracked(project, CLAUDE_FILE):
        return []
    text, bom, nl = read_text(claude)
    if not has_block(text):
        return []
    rest = without_block(text).strip("\n")
    if rest.strip():
        write_text(claude, rest + "\n", bom, nl)
        return [f"{CLAUDE_FILE}: dwookit block removed (your own lines kept)"]
    claude.unlink()
    return [f"{CLAUDE_FILE}: removed"]


def write_installed(forge: Path, paths: list[str]):
    """Paths written outside __dwookit for the library selection: the git exclude block and uninstall read it."""
    f = forge / "context" / "installed.json"
    if paths:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"paths": sorted(set(paths))}, indent=1) + "\n", encoding="utf-8")
    elif f.exists():
        f.unlink()


def remove_pointers(project: Path, backup: Path | None = None, with_user_file: bool = True) -> list[str]:
    """Removes what install_pointers wrote. The user's instructions file only goes on uninstall (with_user_file),
    after a copy to backup when it has content; 'agent off' keeps it."""
    notes = []
    user = project / USER_FILE
    if with_user_file and user.exists() and not git_tracked(project, USER_FILE) and ours_user_file(user.read_text(encoding="utf-8-sig", errors="replace")):
        if user_file_has_content(project) and backup is not None:
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(user, backup / user.name)
            notes.append(f"{USER_FILE}: backed up to {backup / user.name}")
        user.unlink()
        notes.append(f"{USER_FILE}: removed")
    notes += remove_claude_block(project)
    notes += lens_library.remove_from_project(project, lambda rel: git_tracked(project, rel))
    write_installed(project / "__dwookit", [])
    copilot = project / COPILOT_FILE
    if copilot.exists() and COPILOT_MARK in copilot.read_text(encoding="utf-8-sig", errors="replace"):
        copilot.unlink()
        notes.append(f"{COPILOT_FILE}: removed")
    notes += remove_legacy_copilot(project)
    for d in (copilot.parent, copilot.parent.parent):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return notes


# ------------------------------------------------------------------------------------------------ commands

def settings_path(forge: Path) -> Path:
    return forge / "agent.json"


def load_settings(forge: Path) -> dict:
    return lens_bp.load(settings_path(forge)) or {}


def save_settings(forge: Path, settings: dict):
    settings_path(forge).write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pointers_enabled(forge: Path) -> bool:
    return load_settings(forge).get("pointers", True)


def set_pointers_enabled(forge: Path, on: bool):
    save_settings(forge, {**load_settings(forge), "pointers": on})


def selection(forge: Path) -> dict:
    """The project's library selection. A project without one (just connected, or connected before the library
    existed) gets the library defaults, saved so later changes to the defaults do not move it."""
    s = load_settings(forge)
    if "instructions" not in s:
        lib = lens_library.list_items(lens_library.root_of())
        s.update(instructions=lib["defaults"]["instructions"], skills=lib["defaults"]["skills"], targets=lib["targets"])
        save_settings(forge, s)
    return {"targets": [t for t in s.get("targets", lens_library.TARGETS) if t in lens_library.TARGETS],
            "instructions": list(s.get("instructions") or []), "skills": list(s.get("skills") or [])}


def cmd_write(project: Path) -> int:
    forge = project / "__dwookit"
    if not forge.is_dir():
        print(f"lens_agent: {forge} does not exist (project not connected)", file=sys.stderr)
        return 1
    ctx = forge / "context"
    ctx.mkdir(exist_ok=True)
    (ctx / "AGENT.md").write_text(agent_md(forge, project), encoding="utf-8", newline="\n")
    sel = selection(forge)
    if pointers_enabled(forge) and sel["targets"]:
        notes = install_pointers(project, sel)
    else:
        notes = remove_pointers(project, with_user_file=False)
    for note in notes:
        print(f"agent: {note}")
    return 0


def cmd_use(project: Path, instructions, skills, targets) -> int:
    forge = project / "__dwookit"
    if not forge.is_dir():
        print(f"lens_agent: {forge} does not exist (project not connected)", file=sys.stderr)
        return 1
    selection(forge)
    s = load_settings(forge)
    lib = lens_library.list_items(lens_library.root_of())
    for key, value in (("instructions", instructions), ("skills", skills)):
        if value is not None:
            known = {i["name"] for i in lib[key]}
            for n in value:
                if n not in known:
                    print(f"agent: '{n}' is not in the library ({key}); added anyway, skipped until it is", file=sys.stderr)
            s[key] = value
    if targets is not None:
        bad = [t for t in targets if t not in lens_library.TARGETS]
        if bad:
            print(f"lens_agent: unknown agent(s) {', '.join(bad)}: use claude, copilot", file=sys.stderr)
            return 1
        s["targets"] = targets
    save_settings(forge, s)
    sel = selection(forge)
    print(f"agent: agents {', '.join(lens_library.TARGET_NAMES[t] for t in sel['targets']) or 'none'}; "
          f"instructions {', '.join(sel['instructions']) or 'none'}; skills {', '.join(sel['skills']) or 'none'}")
    return cmd_write(project)


def cmd_remove(project: Path, backup: Path | None = None) -> int:
    for note in remove_pointers(project, backup):
        print(f"agent: {note}")
    return 0


def cmd_status(project: Path) -> int:
    forge = project / "__dwookit"
    agent = forge / "context" / "AGENT.md"
    print(f"AGENT.md        : {'written' if agent.exists() else 'missing (run: __dwookit.bat agent)'}")
    user = project / USER_FILE
    print(f"instructions    : {USER_FILE} "
          + ("(has content)" if user_file_has_content(project) else "(empty)" if user.exists() else "missing"))
    claude = project / CLAUDE_FILE
    if git_tracked(project, CLAUDE_FILE):
        state = "committed to git, not changed by dwookit"
    elif claude.exists() and BLOCK_BEGIN in claude.read_text(encoding="utf-8-sig", errors="replace"):
        state = "has the dwookit block"
    else:
        state = "no dwookit block"
    print(f"Claude Code     : {CLAUDE_FILE} {state}")
    copilot = project / COPILOT_FILE
    if copilot.exists():
        ours = COPILOT_MARK in copilot.read_text(encoding="utf-8-sig", errors="replace")
        print(f"Copilot         : {COPILOT_FILE} {'written by dwookit' if ours else 'exists, not dwookit'}")
    else:
        print(f"Copilot         : {COPILOT_FILE} missing")
    print(f"pointer files   : {'on' if pointers_enabled(forge) else 'off (__dwookit.bat agent on)'}")
    if forge.is_dir():
        sel = selection(forge)
        print(f"agents          : {', '.join(lens_library.TARGET_NAMES[t] for t in sel['targets']) or 'none'}")
        print(f"library         : instructions {', '.join(sel['instructions']) or 'none'}; skills {', '.join(sel['skills']) or 'none'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("write", "remove", "status", "on", "off", "use"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True, type=Path)
        p.add_argument("--backup", type=Path)
        if name == "use":
            for k in ("instructions", "skills", "targets"):
                p.add_argument(f"--{k}")
    args = ap.parse_args(argv)
    project = args.project.resolve()
    if args.cmd == "use":
        pl = lens_library.parse_list
        return cmd_use(project, pl(args.instructions), pl(args.skills), pl(args.targets))
    if args.cmd == "write":
        return cmd_write(project)
    if args.cmd == "remove":
        return cmd_remove(project, args.backup)
    if args.cmd == "on":
        set_pointers_enabled(project / "__dwookit", True)
        return cmd_write(project)
    if args.cmd == "off":
        set_pointers_enabled(project / "__dwookit", False)
        for note in remove_pointers(project, with_user_file=False):
            print(f"agent: {note}")
        return 0
    return cmd_status(project)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
