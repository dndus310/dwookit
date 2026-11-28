"""DwoLens context queries: small answers from the Blueprint export, so a coding agent asks one question instead of
opening every exported file (__dwookit.bat ctx ...).

    ctx bp <Name>        where its files are, which part holds each event / function, whether the export is older
                         than the asset, how much of it the C++ migration moves and why the rest stays
    ctx refs <Symbol>    where a variable, function, event or dispatcher is read, written, called, defined,
                         implemented, bound, cast to or spawned, in every tracked Blueprint ('Health',
                         'BP_Player.Health', 'BPI_Touch.Primary Thumbstick')
    ctx find <text>      Blueprints and symbols whose name contains the text
    ctx stale            Blueprints changed since the export, and AI-written explanations (<Name>.agent.json)
                         that no longer match the Blueprint
    ctx stats            what reading the pseudocode instead of the JSON saves (bytes, and tokens at ~4 bytes each)

Reads __dwookit/blueprints/facts.json and refs.json (written by lens_bp.py md after every bp export). Standard
library only; no network, no LLM.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TOKEN_BYTES = 4          # rough tokens per byte of English-like text; only for comparing sizes


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def changed_now(source: dict | None, project: Path | None) -> bool | None:
    """The asset differs from the one exported (time or size), checked now rather than at export time."""
    if not source or not project or not source.get("file"):
        return None
    f = project / source["file"]
    if not f.exists():
        return True
    try:
        exported = datetime.fromisoformat(source["modifiedUtc"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    now = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
    return abs((now - exported).total_seconds()) > 2 or f.stat().st_size != source.get("bytes")


class Context:
    def __init__(self, root: Path, project: Path | None):
        self.root, self.project = root, project
        facts = load(root / "facts.json") or {}
        refs = load(root / "refs.json") or {}
        if not facts:
            raise SystemExit(f"No Blueprint facts in {root}. Run: __dwookit.bat bp export")
        self.exported = facts.get("exportedAtUtc", "")
        self.bps: dict[str, dict] = facts.get("blueprints", {})
        self.symbols: dict[str, list] = refs.get("symbols", {})

    def rel(self, path: str) -> str:
        return f"__dwookit/blueprints/{path}"

    def find_bp(self, name: str) -> tuple[str, dict]:
        if name in self.bps:
            return name, self.bps[name]
        low = name.lower()
        hits = [n for n in self.bps if n.lower() == low] or [n for n in self.bps if low in n.lower()]
        if len(hits) == 1:
            return hits[0], self.bps[hits[0]]
        if not hits:
            raise SystemExit(f"No tracked Blueprint named {name}. Track it: __dwookit.bat bp add <path>")
        raise SystemExit(f"{name} matches several: {', '.join(sorted(hits)[:12])}")

    def stale_note(self, name: str) -> str:
        if changed_now(self.bps.get(name, {}).get("source"), self.project):
            return (f"CHANGED: {name}.uasset is newer than its export; the files below may be out of date. "
                    "Ask the user to run `__dwookit.bat bp export` (editor closed).")
        return ""

    # -- commands
    def bp(self, name: str) -> list[str]:
        name, f = self.find_bp(name)
        out = []
        note = self.stale_note(name)
        if note:
            out.append(note)
        d = f["dir"]
        b = f.get("bytes") or {}
        out.append(f"{name}  ({f.get('package')})")
        out.append(f"  pseudocode  {self.rel(d)}/{name}.md  ({b.get('md', 0)} bytes; the JSON is {b.get('json', 0)})")
        if f.get("parts"):
            out.append(f"  parts       one per event / function, each readable on its own:")
            for part, file in sorted(f["parts"].items(), key=lambda kv: kv[1]):
                out.append(f"              {part}: {self.rel(d)}/{file}")
        out.append(f"  explained   {self.rel(d)}/{name}.explain.json (rule-based)")
        agent = load(self.root / d / f"{name}.agent.json")
        if agent is not None:
            ok = agent.get("fingerprint") == f.get("fingerprint")
            out.append(f"  AI notes    {self.rel(d)}/{name}.agent.json ({'current' if ok else 'OUT OF DATE: written for another version, rewrite it'})")
        nat = f.get("native")
        if nat and nat.get("error"):
            out.append(f"  C++         {nat['error']}")
        elif nat:
            share = round(100 * nat["migrated"] / nat["nodes"]) if nat["nodes"] else 100
            out.append(f"  C++         the migration moves {nat['migrated']}/{nat['nodes']} nodes ({share}%): "
                       f"__dwookit.bat native plan {f.get('package')}")
            for reason in nat.get("stays", [])[:8]:
                out.append(f"              stays: {reason}")
        defined = sorted({s for s, refs in self.symbols.items() if s.startswith(name + ".")})
        if defined:
            used_elsewhere = [s for s in defined if any(r[0] != name for r in self.symbols[s])]
            out.append(f"  symbols     {len(defined)} of its own; used by other Blueprints: {', '.join(used_elsewhere[:10]) or 'none'}"
                       f"  (ctx refs {name}.<member>)")
        return out

    def matching_symbols(self, query: str) -> list[str]:
        if query in self.symbols:
            return [query]
        low = query.lower()
        exact = [s for s in self.symbols if s.lower() == low]
        if exact:
            return exact
        return sorted(s for s in self.symbols if s.lower().endswith("." + low))

    def refs(self, query: str) -> list[str]:
        hits = self.matching_symbols(query)
        if not hits:
            near = [s for s in self.symbols if query.lower() in s.lower()][:12]
            return [f"No symbol {query}." + (f" Close: {', '.join(near)}" if near else "")]
        out = []
        stale = set()
        for sym in hits:
            refs = self.symbols[sym]
            bps = sorted({r[0] for r in refs})
            out.append(f"{sym}: {len(refs)} use(s) in {len(bps)} Blueprint(s)")
            order = {"define": 0, "implement": 1, "write": 2, "broadcast": 3, "bind": 4, "unbind": 5, "call": 6, "read": 7, "cast": 8, "spawn": 9}
            seen = set()
            for bp, kind, where, file in sorted(refs, key=lambda r: (order.get(r[1], 9), r[0], r[2])):
                key = (bp, kind, where, file)
                if key in seen:
                    continue
                seen.add(key)
                out.append(f"  {kind:<9} {bp:<28} {where or '-':<36} {self.rel(file)}")
                if changed_now(self.bps.get(bp, {}).get("source"), self.project):
                    stale.add(bp)
        if stale:
            out.append(f"CHANGED since the export (may be out of date): {', '.join(sorted(stale))}. "
                       "Ask the user to run `__dwookit.bat bp export` (editor closed).")
        return out

    def find(self, text: str) -> list[str]:
        low = text.lower()
        bps = sorted(n for n in self.bps if low in n.lower())
        syms = sorted(s for s in self.symbols if low in s.lower())
        out = []
        if bps:
            out.append("Blueprints: " + ", ".join(bps[:30]))
        for s in syms[:40]:
            kinds = sorted({r[1] for r in self.symbols[s]})
            out.append(f"  {s}  ({', '.join(kinds)}; {len(self.symbols[s])} use(s))")
        if len(syms) > 40:
            out.append(f"  ... {len(syms) - 40} more: narrow the text")
        return out or [f"Nothing named like {text}."]

    def stale(self) -> list[str]:
        changed = [n for n, f in sorted(self.bps.items()) if changed_now(f.get("source"), self.project)]
        outdated = []
        for n, f in sorted(self.bps.items()):
            agent = load(self.root / f["dir"] / f"{n}.agent.json")
            if agent is not None and agent.get("fingerprint") != f.get("fingerprint"):
                outdated.append(n)
        out = [f"Exported {self.exported}."]
        out.append(f"Changed since the export: {', '.join(changed) if changed else 'none'}"
                   + (". Run `__dwookit.bat bp export` (editor closed); it re-exports only these." if changed else "."))
        out.append(f"AI notes (<Name>.agent.json) out of date: {', '.join(outdated) if outdated else 'none'}"
                   + (". Rewrite them with <Name>.explain-prompt.md." if outdated else "."))
        return out

    def stats(self) -> list[str]:
        js = sum((f.get("bytes") or {}).get("json", 0) for f in self.bps.values())
        md = sum((f.get("bytes") or {}).get("md", 0) for f in self.bps.values())
        parts = sum((f.get("bytes") or {}).get("parts", 0) for f in self.bps.values())
        split = [n for n, f in self.bps.items() if f.get("parts")]
        out = [f"{len(self.bps)} Blueprint(s). Sizes are bytes; tokens are estimates at ~{TOKEN_BYTES} bytes each.",
               f"  JSON (every node and pin)   {js:>10}  ~{js // TOKEN_BYTES} tokens",
               f"  pseudocode (<Name>.md)      {md:>10}  ~{md // TOKEN_BYTES} tokens  ({round(100 * md / js, 1) if js else 0}% of the JSON)"]
        if split:
            out.append(f"  parts of {len(split)} split Blueprint(s) {parts:>6}  (read one part, not all)")
        biggest = sorted(self.bps.items(), key=lambda kv: -(kv[1].get("bytes") or {}).get("json", 0))[:5]
        out.append("Largest exports (JSON -> pseudocode):")
        for n, f in biggest:
            b = f.get("bytes") or {}
            out.append(f"  {n:<32} {b.get('json', 0):>8} -> {b.get('md', 0):>6}"
                       + (f", largest part {b.get('largestPart', 0)}" if f.get("parts") else ""))
        reused = sum(1 for f in self.bps.values() if f.get("reused"))
        out.append(f"Last export reused {reused} unchanged Blueprint(s) without loading them.")
        return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, required=True, help="__dwookit/blueprints")
    ap.add_argument("--project", type=Path, help="project folder (to check assets against the export)")
    ap.add_argument("cmd", choices=["bp", "refs", "find", "stale", "stats"])
    ap.add_argument("arg", nargs="*")
    a = ap.parse_args(argv)
    ctx = Context(a.root, a.project)
    text = " ".join(a.arg).strip()
    if a.cmd in ("bp", "refs", "find") and not text:
        raise SystemExit(f"ctx {a.cmd} needs a name")
    lines = {"bp": lambda: ctx.bp(text), "refs": lambda: ctx.refs(text), "find": lambda: ctx.find(text),
             "stale": ctx.stale, "stats": ctx.stats}[a.cmd]()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")       # Korean and other names on a Windows console
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
