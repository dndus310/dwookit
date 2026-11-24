"""dwookit library: instruction files and skills kept in the dwookit folder, applied to connected projects.

Only Claude Code and GitHub Copilot are targets. The library lives in <dwookit>/library (git-ignored, so the files
users upload stay out of the dwookit repository):

    library/instructions/<name>.md        an instruction file (Markdown; front matter optional)
    library/skills/<name>/SKILL.md        a skill: SKILL.md (front matter name + description) and its other files
    library/library.json                  {"defaults": {"instructions": [...], "skills": [...]}, "targets": [...]}

Upload (add) checks what the agents require: a skill name is lowercase letters, digits and hyphens (64 at most) and
matches its folder; SKILL.md gets name and description front matter when they are missing. Defaults are what a
project gets when it is connected without an explicit choice; a new item is added to the defaults.

What a connected project gets for its selection (__dwookit/agent.json: instructions, skills, targets):

    Claude Code      CLAUDE.local.md block imports __dwookit/context/instructions/<name>.md
                     .claude/skills/<name>/ (a copy, with a .dwookit-managed marker)
    GitHub Copilot   .github/instructions/dwookit-<name>.instructions.md (applyTo from the file, else "**")
                     .github/skills/<name>/ (a copy, with a .dwookit-managed marker)

A folder or file that exists without dwookit's marker is never touched. Everything written outside __dwookit is listed
in __dwookit/context/installed.json, which the git exclude block and uninstall read.

    python lens_library.py list [--json]
    python lens_library.py add-instruction <file.md> [--name N]
    python lens_library.py add-skill <folder | SKILL.md | skill.zip> [--name N]
    python lens_library.py remove instruction|skill <name>
    python lens_library.py defaults [--instructions a,b|none] [--skills x|none] [--targets claude,copilot|none]

Standard library only, no network.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

SYSTEM = Path(__file__).resolve().parent.parent
TARGETS = ("claude", "copilot")
TARGET_NAMES = {"claude": "Claude Code", "copilot": "GitHub Copilot"}
MANAGED = ".dwookit-managed"
COPILOT_PREFIX = "dwookit-"
INSTRUCTION_MARK = "<!-- dwookit managed: removed by __dwookit.bat uninstall -->"
MAX_INSTRUCTION = 256 * 1024
MAX_SKILL_FILES = 200
MAX_SKILL_BYTES = 20 * 1024 * 1024
SKIP = {".git", "__pycache__", ".DS_Store", "Thumbs.db", MANAGED}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
YAML_SPECIAL = r"[:#'\"{}\[\],&*!|>%@`]"


class LibraryError(Exception):
    pass


# ------------------------------------------------------------------------------------------------ text helpers

def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:64].strip("-")
    return s or "item"


def read_md(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n")


def split_front_matter(text: str) -> tuple[list[str], str]:
    """(front matter lines without the --- fences, body). No front matter: ([], text)."""
    if not text.startswith("---\n"):
        return [], text
    end = text.find("\n---", 3)
    if end < 0:
        return [], text
    after = text.find("\n", end + 4)
    body = "" if after < 0 else text[after + 1:]
    return text[4:end].split("\n"), body.lstrip("\n")


def fm_value(lines: list[str], key: str) -> str:
    for line in lines:
        m = re.match(rf"^{re.escape(key)}\s*:\s*(.*)$", line)
        if m:
            v = m.group(1).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            return v
    return ""


def fm_set(lines: list[str], key: str, value: str) -> list[str]:
    needs_quotes = bool(re.search(YAML_SPECIAL, value)) or value != value.strip()
    line = f"{key}: {json.dumps(value, ensure_ascii=False) if needs_quotes else value}"
    out, done = [], False
    for l in lines:
        if re.match(rf"^{re.escape(key)}\s*:", l):
            if not done:
                out.append(line)
                done = True
            continue
        out.append(l)
    return out if done else [line] + out


def first_sentence(body: str) -> str:
    for line in body.split("\n"):
        t = line.strip().lstrip("#").strip()
        if t and not t.startswith(("<!--", "```", "---")):
            return t[:200]
    return ""


def heading(body: str) -> str:
    for line in body.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# ------------------------------------------------------------------------------------------------ library

def root_of(system: Path | None = None) -> Path:
    """<dwookit>/library; DWOOKIT_LIBRARY points somewhere else (tests, a shared team folder)."""
    if system is None and os.environ.get("DWOOKIT_LIBRARY"):
        return Path(os.environ["DWOOKIT_LIBRARY"])
    return (system or SYSTEM) / "library"


def load_settings(root: Path) -> dict:
    try:
        s = json.loads((root / "library.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        s = {}
    d = s.get("defaults") or {}
    return {"defaults": {"instructions": list(d.get("instructions") or []), "skills": list(d.get("skills") or [])},
            "targets": [t for t in (s.get("targets") if s.get("targets") is not None else TARGETS) if t in TARGETS]}


def save_settings(root: Path, s: dict):
    root.mkdir(parents=True, exist_ok=True)
    (root / "library.json").write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def list_items(root: Path) -> dict:
    items = {"instructions": [], "skills": []}
    for f in sorted((root / "instructions").glob("*.md")) if (root / "instructions").is_dir() else []:
        fm, body = split_front_matter(read_md(f))
        items["instructions"].append({"name": f.stem, "title": fm_value(fm, "title") or heading(body) or f.stem,
                                      "description": fm_value(fm, "description") or first_sentence(body),
                                      "applyTo": fm_value(fm, "applyTo"), "bytes": f.stat().st_size})
    for d in sorted(p for p in (root / "skills").iterdir() if p.is_dir()) if (root / "skills").is_dir() else []:
        f = d / "SKILL.md"
        if not f.exists():
            continue
        fm, body = split_front_matter(read_md(f))
        files = [p for p in d.rglob("*") if p.is_file()]
        items["skills"].append({"name": d.name, "description": fm_value(fm, "description") or first_sentence(body),
                                "files": len(files), "bytes": sum(p.stat().st_size for p in files)})
    s = load_settings(root)
    names = {k: {i["name"] for i in items[k]} for k in items}
    items["defaults"] = {k: [n for n in s["defaults"][k] if n in names[k]] for k in ("instructions", "skills")}
    items["targets"] = s["targets"]
    return items


def _add_default(root: Path, kind: str, name: str):
    s = load_settings(root)
    if name not in s["defaults"][kind]:
        s["defaults"][kind].append(name)
        save_settings(root, s)


def add_instruction(root: Path, src: Path, name: str | None = None) -> tuple[str, list[str]]:
    if not src.is_file():
        raise LibraryError(f"{src}: not a file")
    if src.suffix.lower() not in (".md", ".txt", ".markdown"):
        raise LibraryError(f"{src.name}: an instruction file is Markdown (.md) or text (.txt)")
    if src.stat().st_size > MAX_INSTRUCTION:
        raise LibraryError(f"{src.name}: larger than {MAX_INSTRUCTION // 1024} KB; agents read the whole file every time")
    text = read_md(src)
    fm, body = split_front_matter(text)
    if not body.strip():
        raise LibraryError(f"{src.name}: empty")
    stem = re.sub(r"\.instructions$", "", src.stem, flags=re.I)
    n = slug(name or fm_value(fm, "name") or stem)
    dest = root / "instructions" / f"{n}.md"
    notes = [f"instruction '{n}': {'replaced' if dest.exists() else 'added'}"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    _add_default(root, "instructions", n)
    return n, notes


def _find_skill_dir(src: Path, tmp: Path) -> Path:
    if src.is_file() and src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            for info in z.infolist():
                p = Path(info.filename)
                if p.is_absolute() or ".." in p.parts or ":" in info.filename:
                    raise LibraryError(f"{src.name}: unsafe path inside the zip ({info.filename})")
            total = sum(i.file_size for i in z.infolist())
            if total > MAX_SKILL_BYTES:
                raise LibraryError(f"{src.name}: more than {MAX_SKILL_BYTES // (1024 * 1024)} MB unpacked")
            z.extractall(tmp)
        src = tmp
    if src.is_file():
        if src.name.lower() != "skill.md":
            raise LibraryError(f"{src.name}: a skill is a folder with SKILL.md, that SKILL.md, or a .zip of the folder")
        return src.parent
    if not src.is_dir():
        raise LibraryError(f"{src}: not found")
    if (src / "SKILL.md").is_file():
        return src
    found = sorted((p.parent for p in src.rglob("SKILL.md")), key=lambda p: len(p.parts))
    if not found:
        raise LibraryError(f"{src.name}: no SKILL.md inside")
    if len(found) > 1 and len(found[0].parts) == len(found[1].parts):
        raise LibraryError(f"{src.name}: holds several skills ({', '.join(p.name for p in found[:4])}); add them one by one")
    return found[0]


def add_skill(root: Path, src: Path, name: str | None = None) -> tuple[str, list[str]]:
    notes = []
    with tempfile.TemporaryDirectory(prefix="dwookit-skill-") as t:
        tmp = Path(t)
        skill = _find_skill_dir(src, tmp / "zip")
        files = [p for p in skill.rglob("*") if p.is_file() and not (set(p.relative_to(skill).parts) & SKIP)]
        if len(files) > MAX_SKILL_FILES:
            raise LibraryError(f"{skill.name}: {len(files)} files (at most {MAX_SKILL_FILES})")
        if sum(p.stat().st_size for p in files) > MAX_SKILL_BYTES:
            raise LibraryError(f"{skill.name}: more than {MAX_SKILL_BYTES // (1024 * 1024)} MB")
        fm, body = split_front_matter(read_md(skill / "SKILL.md"))
        wanted = name or fm_value(fm, "name") or (skill.name if skill != tmp / "zip" else src.stem)
        n = slug(wanted)
        if n != wanted:
            notes.append(f"name '{wanted}' -> '{n}' (lowercase letters, digits and hyphens; the folder name must match)")
        desc = fm_value(fm, "description")
        if not desc:
            desc = first_sentence(body) or n
            notes.append("description was missing: taken from the first line (both agents need it to pick the skill)")
        fm = fm_set(fm_set(fm, "description", desc[:1024]), "name", n)
        stage = tmp / "stage" / n
        for p in files:
            rel = p.relative_to(skill)
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, stage / rel)
        (stage / "SKILL.md").write_text("---\n" + "\n".join(fm) + "\n---\n\n" + body.lstrip("\n"), encoding="utf-8", newline="\n")
        dest = root / "skills" / n
        notes.insert(0, f"skill '{n}': {'replaced' if dest.exists() else 'added'} ({len(files)} file(s))")
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, dest)
    _add_default(root, "skills", n)
    return n, notes


def remove_item(root: Path, kind: str, name: str) -> list[str]:
    key = "instructions" if kind.startswith("instruction") else "skills"
    path = root / "instructions" / f"{name}.md" if key == "instructions" else root / "skills" / name
    if not path.exists():
        raise LibraryError(f"{kind} '{name}' is not in the library")
    shutil.rmtree(path) if path.is_dir() else path.unlink()
    s = load_settings(root)
    s["defaults"][key] = [n for n in s["defaults"][key] if n != name]
    save_settings(root, s)
    return [f"{kind} '{name}' removed from the library (projects keep their copy until their selection changes)"]


def parse_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    v = value.strip()
    return [] if v.lower() in ("", "none", "-") else [x.strip() for x in v.split(",") if x.strip()]


def set_defaults(root: Path, instructions=None, skills=None, targets=None) -> list[str]:
    s = load_settings(root)
    if instructions is not None:
        s["defaults"]["instructions"] = instructions
    if skills is not None:
        s["defaults"]["skills"] = skills
    if targets is not None:
        s["targets"] = [t for t in targets if t in TARGETS]
    save_settings(root, s)
    return [f"defaults: instructions {', '.join(s['defaults']['instructions']) or 'none'}; skills "
            f"{', '.join(s['defaults']['skills']) or 'none'}; agents {', '.join(TARGET_NAMES[t] for t in s['targets']) or 'none'}"]


# ------------------------------------------------------------------------------------------------ projects

def instruction_copy(forge: Path, name: str) -> Path:
    return forge / "context" / "instructions" / f"{name}.md"


def copilot_instruction(project: Path, name: str) -> Path:
    return project / ".github" / "instructions" / f"{COPILOT_PREFIX}{name}.instructions.md"


def skill_dirs(project: Path) -> dict[str, Path]:
    return {"claude": project / ".claude" / "skills", "copilot": project / ".github" / "skills"}


def _is_ours_dir(d: Path) -> bool:
    return (d / MANAGED).exists()


def _is_ours_file(f: Path) -> bool:
    try:
        return INSTRUCTION_MARK in f.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False


def _remove_empty(*dirs: Path):
    for d in dirs:
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def sync_project(project: Path, forge: Path, sel: dict, root: Path, tracked) -> tuple[list[str], list[str], list[str]]:
    """Makes the project match the selection. Returns (notes, Claude import lines, paths written outside __dwookit).
    tracked(rel) says whether git tracks a path: such files are never changed."""
    notes, imports, installed = [], [], []
    targets = [t for t in sel.get("targets", []) if t in TARGETS]
    names_i = list(dict.fromkeys(sel.get("instructions", [])))
    names_s = list(dict.fromkeys(sel.get("skills", [])))

    # instructions: a copy under __dwookit/context/instructions (Claude imports it), a Copilot file per instruction
    cdir = forge / "context" / "instructions"
    for f in cdir.glob("*.md") if cdir.is_dir() else []:
        if f.stem not in names_i:
            f.unlink()
    for n in names_i:
        src = root / "instructions" / f"{n}.md"
        copy = instruction_copy(forge, n)
        if src.exists():
            fm, body = split_front_matter(read_md(src))
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_text(body.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
        elif not copy.exists():
            notes.append(f"instruction '{n}' is not in the library: skipped")
            continue
        if "claude" in targets:
            imports.append(f"@__dwookit/context/instructions/{n}.md")
    gi = project / ".github" / "instructions"
    for f in gi.glob(f"{COPILOT_PREFIX}*.instructions.md") if gi.is_dir() else []:
        n = f.name[len(COPILOT_PREFIX):-len(".instructions.md")]
        if _is_ours_file(f) and ("copilot" not in targets or n not in names_i) and not tracked(f.relative_to(project).as_posix()):
            f.unlink()
            notes.append(f"{f.relative_to(project).as_posix()}: removed")
    if "copilot" in targets:
        for n in names_i:
            copy = instruction_copy(forge, n)
            if not copy.exists():
                continue
            src = root / "instructions" / f"{n}.md"
            fm = split_front_matter(read_md(src))[0] if src.exists() else []
            dest = copilot_instruction(project, n)
            rel = dest.relative_to(project).as_posix()
            if dest.exists() and (not _is_ours_file(dest) or tracked(rel)):
                notes.append(f"{rel} exists and is not dwookit's: left alone")
                continue
            head = ["---", f"applyTo: {json.dumps(fm_value(fm, 'applyTo') or '**')}"]
            if fm_value(fm, "description"):
                head.append(f"description: {json.dumps(fm_value(fm, 'description'), ensure_ascii=False)}")
            text = "\n".join(head + ["---", INSTRUCTION_MARK, ""]) + read_md(copy)
            if not dest.exists() or read_md(dest) != text:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8", newline="\n")
                notes.append(f"{rel}: written")
            installed.append(rel)

    # skills: a copy per agent folder, marked so only dwookit's copies are ever replaced or removed
    for t, base in skill_dirs(project).items():
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if d.is_dir() and _is_ours_dir(d) and (t not in targets or d.name not in names_s):
                shutil.rmtree(d, ignore_errors=True)
                notes.append(f"{d.relative_to(project).as_posix()}: removed")
        if t not in targets:
            continue
        for n in names_s:
            src = root / "skills" / n
            dest = base / n
            rel = dest.relative_to(project).as_posix()
            if not src.is_dir():
                if dest.is_dir() and _is_ours_dir(dest):
                    installed.append(rel + "/")
                else:
                    notes.append(f"skill '{n}' is not in the library: skipped")
                continue
            if dest.exists() and (not _is_ours_dir(dest) or tracked(rel)):
                notes.append(f"{rel} exists and is not dwookit's: left alone")
                continue
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            (dest / MANAGED).write_text("Copied by dwookit from its library; removed by __dwookit.bat uninstall.\n", encoding="utf-8")
            installed.append(rel + "/")
        if names_s:
            notes.append(f"{TARGET_NAMES[t]} skills: {', '.join(names_s)}")
    _remove_empty(project / ".claude" / "skills", project / ".claude", project / ".github" / "skills")
    return notes, imports, installed


def remove_from_project(project: Path, tracked) -> list[str]:
    """Removes every dwookit copy (Copilot instruction files, skill folders) whatever the selection was."""
    notes = []
    gi = project / ".github" / "instructions"
    for f in gi.glob(f"{COPILOT_PREFIX}*.instructions.md") if gi.is_dir() else []:
        if _is_ours_file(f) and not tracked(f.relative_to(project).as_posix()):
            f.unlink()
            notes.append(f"{f.relative_to(project).as_posix()}: removed")
    for base in skill_dirs(project).values():
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if d.is_dir() and _is_ours_dir(d):
                shutil.rmtree(d, ignore_errors=True)
                notes.append(f"{d.relative_to(project).as_posix()}: removed")
    _remove_empty(project / ".claude" / "skills", project / ".claude", project / ".github" / "skills")
    return notes


# ------------------------------------------------------------------------------------------------ command line

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--system", type=Path, help="dwookit folder (default: the one this file is in)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    for name in ("add-instruction", "add-skill"):
        p = sub.add_parser(name)
        p.add_argument("path", type=Path)
        p.add_argument("--name")
    p = sub.add_parser("remove")
    p.add_argument("kind", choices=["instruction", "skill"])
    p.add_argument("name")
    p = sub.add_parser("defaults")
    for k in ("instructions", "skills", "targets"):
        p.add_argument(f"--{k}")
    args = ap.parse_args(argv)
    root = root_of(args.system)
    try:
        if args.cmd == "list":
            items = list_items(root)
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=1))
                return 0
            print(f"library: {root}")
            print(f"agents : {', '.join(TARGET_NAMES[t] for t in items['targets']) or 'none'}")
            for kind in ("instructions", "skills"):
                print(f"{kind}: {len(items[kind])}")
                for i in items[kind]:
                    mark = "*" if i["name"] in items["defaults"][kind] else " "
                    print(f"  {mark} {i['name']:<28} {i['description'][:90]}")
            print("  (* applied by default when a project is connected)")
            return 0
        if args.cmd == "add-instruction":
            _, notes = add_instruction(root, args.path, args.name)
        elif args.cmd == "add-skill":
            _, notes = add_skill(root, args.path, args.name)
        elif args.cmd == "remove":
            notes = remove_item(root, args.kind, args.name)
        else:
            notes = set_defaults(root, parse_list(args.instructions), parse_list(args.skills), parse_list(args.targets))
    except (LibraryError, OSError, zipfile.BadZipFile) as e:
        print(f"library: ERROR: {e}", file=sys.stderr)
        return 1
    for n in notes:
        print(f"library: {n}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
