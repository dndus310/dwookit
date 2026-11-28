"""DwoLens tracked Blueprints: compressed pseudocode (.md) next to each full export (.json).

The DwoLensInspector commandlet writes, for every Blueprint the user tracks (`__dwookit.bat bp add`):

    <dir>/<package path>/<Name>.json     every graph, node, pin, link, default, variable, component (lens.bp/1)
    <dir>/export.json                    manifest of the last export

This module turns each JSON into <Name>.md: the Blueprint as short, Python-like pseudocode an agent can read
cheaply (even a small model), and writes <dir>/index.md listing every tracked Blueprint. The JSON stays the
source of truth for details the pseudocode leaves out (pin ids, positions, hidden pins).

    python lens_bp.py md --dir <blueprints dir> [--project <project dir>]
    python lens_bp.py one <Name.json> [-o <Name.md>]

Standard library only, no network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "lens.bp/1"
MAX_DEPTH = 8            # nested pure expressions deeper than this are cut
MAX_ARG = 80             # literal defaults longer than this are shortened
SPLIT_LINES = 150        # a Blueprint whose pseudocode is longer is split: overview + parts/<event|fn>.md

LEGEND = (
    "Legend: `on X(...)` event, `fn` function, `await` latent call (Delay ...), `goto Ln` jumps to the line marked "
    "`Ln:` (exec wires that join), `# text` author comment, `(disabled)` node is off. Calls without a target run on "
    "self. Only values the author changed are shown as arguments. Full detail (every pin, id, position): the .json "
    "beside the .md."
)

BINARY_OPS = [
    ("EqualEqual_", "=="), ("NotEqual_", "!="), ("LessEqual_", "<="), ("GreaterEqual_", ">="), ("Less_", "<"),
    ("Greater_", ">"), ("Add_", "+"), ("Subtract_", "-"), ("Multiply_", "*"), ("Divide_", "/"), ("Percent_", "%"),
    ("BooleanAND", "and"), ("BooleanOR", "or"), ("BooleanXOR", "xor"), ("And_", "&"), ("Or_", "|"), ("Xor_", "^"),
]
UNARY_OPS = [("Not_PreBool", "not "), ("Negate", "-"), ("Not_", "~")]


# ----------------------------------------------------------------------------------------------- small helpers

def short(path: str | None) -> str:
    """'/Script/Engine.Actor' -> 'Actor', '/Game/A/BP_X.BP_X_C' -> 'BP_X', 'StandardMacros:ForEachLoop' -> 'ForEachLoop'."""
    if not path:
        return ""
    name = re.split(r"[./:]", path)[-1]
    return name[:-2] if name.endswith("_C") else name


def ident(text: str) -> str:
    """A name usable in pseudocode: 'As BP Player' -> 'AsBP_Player', 'Return Value' -> 'ReturnValue'."""
    text = re.sub(r"[^0-9A-Za-z_]+", " ", text or "").strip()
    parts = text.split()
    if not parts:
        return "v"
    out = parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])
    return out if not out[0].isdigit() else "_" + out


def node_name(node: dict) -> str:
    """A readable name for a node without a function: its title when that is English, else its class.
    A non-English editor localizes titles ('프로퍼티 액세스'), which says nothing in pseudocode."""
    title = (node.get("title") or "").split("\n")[0]
    if title and title.isascii() and re.search(r"[A-Za-z]", title):
        return ident(title)
    cls = node.get("class", "")
    return re.sub(r"^(K2Node_|AnimGraphNode_|EdGraphNode_|MaterialGraphNode_)", "", cls) or ident(title)


def lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def clean_func(name: str) -> str:
    return re.sub(r"^K2_", "", name or "")


def ptype(pin_or_var: dict) -> str:
    return (pin_or_var.get("type") or {}).get("text", "")


def is_exec(pin: dict) -> bool:
    return (pin.get("type") or {}).get("category") == "exec"


def literal(pin: dict) -> str | None:
    """The author-set value of an unlinked input pin, or None when it is the node's own default."""
    obj = pin.get("defaultObject")
    text = pin.get("defaultText")
    value = pin.get("default", "")
    auto = pin.get("autoDefault", "")
    if obj:
        return short(obj)
    if text is not None and text != "":
        return json.dumps(text, ensure_ascii=False)
    if value == auto:
        return None
    return format_value(value, pin)


def format_value(value: str, pin: dict | None = None) -> str:
    cat = ((pin or {}).get("type") or {}).get("category", "")
    if value == "" and cat in ("string", "name", "text"):
        return '""'
    if value == "" and cat in ("int", "int64", "real", "float", "double", "byte"):
        return "0"
    if value == "" and cat == "bool":
        return "false"
    if value == "" and cat in ("object", "class", "softobject", "softclass", "interface"):
        return "None"
    if cat in ("string", "name"):
        return json.dumps(value, ensure_ascii=False)
    if value in ("true", "True"):
        return "true"
    if value in ("false", "False"):
        return "false"
    # Object references: "/Script/Engine.StaticMesh'/Game/M/SM_Door.SM_Door'" or '/Game/M/SM_Door.SM_Door' -> SM_Door
    value = re.sub(r"(?:/Script/\w+\.\w+)?'?\"?/[\w-]+(?:/[\w-]+)*\.(\w+?)(?:_C)?(?::\w+)?\"?'?(?=[,)\s]|$)", r"\1", value)
    # Struct text '(X=0.000000,Y=1.000000,Z=0.000000)' -> '(X=0,Y=1,Z=0)'
    value = re.sub(r"(\d+\.\d*?)0+\b", r"\1", value)
    value = re.sub(r"(\d)\.(?=[,)\s]|$)", r"\1", value)
    if len(value) > MAX_ARG:
        value = value[:MAX_ARG] + "..."
    return value


# ----------------------------------------------------------------------------------------------- graph model

class Graph:
    def __init__(self, data: dict):
        self.data = data
        self.name = data.get("name", "")
        self.kind = data.get("kind", "")
        self.nodes: dict[str, dict] = {}
        for n in data.get("nodes", []):
            self.nodes[n["id"]] = n
            for p in n.get("pins", []):
                p["_node"] = n["id"]

    # pins
    def pins(self, node: dict, direction: str | None = None, exec_: bool | None = None) -> list[dict]:
        out = []
        for p in node.get("pins", []):
            if direction and p.get("dir") != direction:
                continue
            if exec_ is not None and is_exec(p) != exec_:
                continue
            out.append(p)
        return out

    def pin(self, node: dict, name: str, direction: str) -> dict | None:
        for p in node.get("pins", []):
            if p.get("name") == name and p.get("dir") == direction:
                return p
        return None

    def targets(self, pin: dict) -> list[tuple[dict, dict]]:
        """(node, pin) at the other end of the pin's links, looking through reroute (knot) nodes."""
        out = []
        other_dir = "in" if pin.get("dir") == "out" else "out"
        for link in pin.get("links", []):
            node = self.nodes.get(link.get("node"))
            if not node:
                continue
            target = self.pin(node, link.get("pin"), other_dir)
            if not target:
                continue
            if node.get("kind") == "knot":
                through = self.pins(node, direction=pin.get("dir"))
                for t in through:
                    out.extend(self.targets(t))
                continue
            out.append((node, target))
        return out

    def source(self, pin: dict) -> tuple[dict, dict] | None:
        found = self.targets(pin)
        return found[0] if found else None

    def exec_outs(self, node: dict) -> list[dict]:
        return self.pins(node, "out", exec_=True)

    def linked_exec_outs(self, node: dict) -> list[dict]:
        return [p for p in self.exec_outs(node) if self.targets(p)]


# ----------------------------------------------------------------------------------------------- writer

class Writer:
    """Pseudocode for one graph."""

    def __init__(self, graph: Graph, bp: dict):
        self.g = graph
        self.bp = bp
        self.lines: list[str] = []
        self.names: dict[tuple[str, str], str] = {}     # (node id, out pin) -> variable name in pseudocode
        self.used_names: set[str] = set()
        self.emitted: set[str] = set()
        self.labels: dict[str, str] = {}
        self.referenced: set[str] = set()               # pure nodes used inside expressions
        self.comment_boxes = self._comment_boxes()
        self.comment_done: set[int] = set()
        self.goto_used: set[str | None] = set()
        self._find_joins()

    def finish(self) -> list[str]:
        """Drops labels nothing jumps to and numbers the rest L1, L2 ... in reading order."""
        kept = [l for l in self.lines if not (re.fullmatch(r"\s*L\d+:", l) and l.strip()[:-1] not in self.goto_used)]
        order: dict[str, str] = {}
        for l in kept:
            m = re.fullmatch(r"\s*(L\d+):", l)
            if m and m.group(1) not in order:
                order[m.group(1)] = f"L{len(order) + 1}"
        return [re.sub(r"\bL\d+\b", lambda m: order.get(m.group(0), m.group(0)), l) if re.search(r"(^\s*L\d+:|goto L\d+|at L\d+)", l) else l
                for l in kept]

    # -- structure
    def _find_joins(self):
        incoming: dict[str, int] = {}
        for node in self.g.nodes.values():
            for p in self.g.exec_outs(node):
                for target, _ in self.g.targets(p):
                    incoming[target["id"]] = incoming.get(target["id"], 0) + 1
        n = 0
        for node_id, count in incoming.items():
            if count > 1:
                n += 1
                self.labels[node_id] = f"L{n}"

    def _comment_boxes(self) -> list[tuple[float, float, float, float, str]]:
        boxes = []
        for node in self.g.nodes.values():
            if node.get("kind") != "comment":
                continue
            x, y = (node.get("pos") or [0, 0])[:2]
            w, h = (node.get("size") or [0, 0])[:2]
            text = (node.get("title") or "").strip()
            if text:
                boxes.append((x, y, x + w, y + h, text))
        # Smallest box first: the most specific comment wins.
        boxes.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        return boxes

    def _box_comment(self, node: dict) -> str | None:
        x, y = (node.get("pos") or [0, 0])[:2]
        for i, (x0, y0, x1, y1, text) in enumerate(self.comment_boxes):
            if i in self.comment_done:
                continue
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.comment_done.add(i)
                return text
        return None

    def name_for(self, node: dict, pin: dict) -> str:
        key = (node["id"], pin["name"])
        if key in self.names:
            return self.names[key]
        kind = node.get("kind")
        if kind in ("event", "customEvent", "boundEvent", "entry", "tunnel"):
            base = ident(pin.get("label") or pin["name"])
        elif kind == "timeline":
            base = ident(pin["name"])
        elif pin["name"] == "ReturnValue":
            base = lower_first(clean_func(node.get("member") or node_name(node))) or "result"
        else:
            base = lower_first(ident(pin.get("label") or pin["name"]))
        name, i = base, 2
        while name in self.used_names and kind not in ("event", "customEvent", "boundEvent", "entry", "tunnel"):
            name, i = f"{base}{i}", i + 1
        self.used_names.add(name)
        self.names[key] = name
        return name

    # -- expressions
    def expr(self, pin: dict, depth: int = 0) -> str:
        """Value flowing into an input pin."""
        src = self.g.source(pin)
        if not src:
            lit = literal(pin)
            return lit if lit is not None else format_value(pin.get("default", ""), pin)
        node, out = src
        return self.value_of(node, out, depth)

    def value_of(self, node: dict, out: dict, depth: int) -> str:
        if depth > MAX_DEPTH:
            return "…"
        # A split struct output ('Closed Position_X') is a field of the whole value.
        if out.get("splitFrom"):
            parent = self.g.pin(node, out["splitFrom"], "out")
            if parent:
                field = out["name"][len(out["splitFrom"]) + 1:] if out["name"].startswith(out["splitFrom"] + "_") else out["name"]
                return f"{self.value_of(node, parent, depth)}.{field}"
        kind = node.get("kind")
        has_exec = bool(self.g.pins(node, exec_=True))
        if kind == "self":
            return "self"
        if kind in ("event", "customEvent", "boundEvent", "entry", "tunnel"):
            if out["name"] == "OutputDelegate":
                return "@" + self.event_name(node)
            return self.name_for(node, out)
        if has_exec and kind not in ("get",):
            # Output of a node that runs in the exec chain: a named result.
            return self.name_for(node, out)
        self.referenced.add(node["id"])
        return self.pure(node, out, depth + 1)

    def args(self, node: dict, depth: int, skip: tuple[str, ...] = ()) -> list[str]:
        out = []
        for p in self.g.pins(node, "in", exec_=False):
            if p.get("hidden") or p["name"] in skip or p["name"] in ("self", "WorldContextObject", "__WorldContext"):
                continue
            if p.get("split"):
                continue
            src = self.g.source(p)
            if src:
                out.append(self._arg(p, self.value_of(src[0], src[1], depth)))
                continue
            lit = literal(p)
            if lit is not None:
                out.append(self._arg(p, lit))
        return out

    @staticmethod
    def _arg(pin: dict, value: str) -> str:
        return f"{ident(pin.get('label') or pin['name'])}={value}"

    def target_prefix(self, node: dict, depth: int) -> str:
        self_pin = self.g.pin(node, "self", "in")
        if self_pin and self.g.source(self_pin):
            return self.expr(self_pin, depth) + "."
        return ""

    def call_text(self, node: dict, depth: int, out_pin: dict | None = None) -> str:
        member = clean_func(node.get("member") or "")
        if not member:
            member = node_name(node)
        for prefix, op in BINARY_OPS:
            if member.startswith(prefix):
                ins = [p for p in self.g.pins(node, "in", exec_=False) if not p.get("hidden") and p["name"] != "self"]
                if len(ins) >= 2:
                    return "(" + f" {op} ".join(self.expr(p, depth) for p in ins) + ")"
        for prefix, op in UNARY_OPS:
            if member.startswith(prefix):
                ins = [p for p in self.g.pins(node, "in", exec_=False) if not p.get("hidden") and p["name"] != "self"]
                if len(ins) == 1:
                    return op + self.expr(ins[0], depth)
        if member.startswith("Break") and out_pin is not None:
            # BreakVector(V).X -> V.X
            ins = [p for p in self.g.pins(node, "in", exec_=False) if not p.get("hidden") and p["name"] != "self"]
            if len(ins) == 1:
                return f"{self.expr(ins[0], depth)}.{out_pin['name']}"
        if member.startswith("Conv_") or member.startswith("Convert"):
            ins = [p for p in self.g.pins(node, "in", exec_=False) if not p.get("hidden") and p["name"] != "self"]
            if len(ins) == 1:
                return self.expr(ins[0], depth)
        prefix = self.target_prefix(node, depth)
        return f"{prefix}{member}({', '.join(self.args(node, depth))})"

    def pure(self, node: dict, out: dict, depth: int) -> str:
        kind = node.get("kind")
        if kind == "get":
            return self.target_prefix(node, depth) + node.get("member", "")
        if kind == "break":
            ins = self.g.pins(node, "in", exec_=False)
            base = self.expr(ins[0], depth) if ins else "?"
            return f"{base}.{out['name']}"
        if kind == "make":
            return f"{short(node.get('member'))}({', '.join(self.args(node, depth))})"
        if kind == "cast":
            obj = self.g.pin(node, "Object", "in")
            return f"({self.expr(obj, depth) if obj else '?'} as {short(node.get('member'))})"
        if kind == "createDelegate":
            return "@" + (node.get("member") or "?")
        if kind == "macro":
            m = node.get("member", "")
            ins = [p for p in self.g.pins(node, "in", exec_=False) if not p.get("hidden")]
            if m == "IsValid" and ins:
                return f"isValid({self.expr(ins[0], depth)})"
            return f"{m}({', '.join(self.args(node, depth))}).{out['name']}"
        cls = node.get("class", "")
        ins = [p for p in self.g.pins(node, "in", exec_=False) if not p.get("hidden")]
        if cls == "K2Node_GetArrayItem" and len(ins) >= 2:
            return f"{self.expr(ins[0], depth)}[{self.expr(ins[1], depth)}]"
        if cls == "K2Node_Select":
            return f"select({', '.join(self.args(node, depth))})"
        if cls == "K2Node_PropertyAccess":
            return ".".join(node.get("path") or []) or "PropertyAccess()"
        if cls in ("K2Node_EnumEquality", "K2Node_EnumInequality") and len(ins) >= 2:
            op = "==" if cls == "K2Node_EnumEquality" else "!="
            return f"({self.expr(ins[0], depth)} {op} {self.expr(ins[1], depth)})"
        if kind == "call":
            text = self.call_text(node, depth, out)
            if text.endswith(f".{out['name']}") and node.get('member', '').startswith('Break'):
                return text
            outs = [p for p in self.g.pins(node, "out", exec_=False) if not p.get("hidden")]
            if len(outs) > 1 and out["name"] != "ReturnValue":
                return f"{text}.{out['name']}"
            return text
        args = self.args(node, depth)
        return f"{node_name(node)}({', '.join(args)})" + (f".{out['name']}" if len(self.g.pins(node, 'out', exec_=False)) > 1 else "")

    # -- statements
    def emit(self, text: str, indent: int):
        self.lines.append("  " * indent + text)

    def chain(self, out_pin: dict, indent: int):
        for node, in_pin in self.g.targets(out_pin):
            self.statement(node, in_pin, indent)

    def statement(self, node: dict, in_pin: dict, indent: int):
        node_id = node["id"]
        label = self.labels.get(node_id)
        if node_id in self.emitted:
            exec_ins = self.g.pins(node, "in", exec_=True)
            if len(exec_ins) > 1 and in_pin.get("name"):
                # Entered through another input (Timeline Reverse after Play, Gate Close ...): same outputs.
                name = node.get("member") or ident((node.get("title") or "").splitlines()[0])
                self.emit(f"{name}.{in_pin['name']}()  # outputs as at {label or '?'}", indent)
                self.goto_used.add(label)
                return
            self.emit(f"goto {label or '?'}", indent)
            self.goto_used.add(label)
            return
        self.emitted.add(node_id)
        box = self._box_comment(node)
        if box:
            for line in box.splitlines():
                self.emit(f"# {line}", indent)
        if label:
            self.emit(f"{label}:", indent)
        tail = f"  # {node['comment']}" if node.get("comment") else ""
        off = "(disabled) " if node.get("disabled") else ""
        kind = node.get("kind")
        member = node.get("member", "")

        if kind == "branch":
            cond = self.expr(self.g.pin(node, "Condition", "in") or {}, 0)
            t = self.g.pin(node, "then", "out")
            f = self.g.pin(node, "else", "out")
            t_linked = bool(t and self.g.targets(t))
            f_linked = bool(f and self.g.targets(f))
            if t_linked:
                self.emit(f"{off}if {cond}:{tail}", indent)
                self.chain(t, indent + 1)
                if f_linked:
                    self.emit("else:", indent)
                    self.chain(f, indent + 1)
            elif f_linked:
                self.emit(f"{off}if not {cond}:{tail}", indent)
                self.chain(f, indent + 1)
            return

        if kind == "sequence":
            outs = self.g.linked_exec_outs(node)
            if tail:
                self.emit(tail.strip(), indent)
            for p in outs:
                self.chain(p, indent)
            return

        if kind == "switch":
            sel = next((p for p in self.g.pins(node, "in", exec_=False) if not p.get("hidden")), None)
            self.emit(f"{off}switch {self.expr(sel, 0) if sel else '?'}:{tail}", indent)
            for p in self.g.linked_exec_outs(node):
                self.emit(f"case {p.get('label') or p['name']}:", indent + 1)
                self.chain(p, indent + 2)
            return

        if kind == "macro" and self._macro(node, off, tail, indent):
            return

        if kind == "get":
            # Validated get: exec 'then' when the object is valid, 'else' when it is not.
            ok, bad = self.g.pin(node, "then", "out"), self.g.pin(node, "else", "out")
            ok = ok if ok and self.g.targets(ok) else None
            bad = bad if bad and self.g.targets(bad) else None
            cond = f"isValid({self.target_prefix(node, 0)}{member})"
            if ok:
                self.emit(f"{off}if {cond}:{tail}", indent)
                self.chain(ok, indent + 1)
                if bad:
                    self.emit("else:", indent)
                    self.chain(bad, indent + 1)
            elif bad:
                self.emit(f"{off}if not {cond}:{tail}", indent)
                self.chain(bad, indent + 1)
            return

        if kind == "cast":
            obj = self.g.pin(node, "Object", "in")
            out = next((p for p in self.g.pins(node, "out", exec_=False) if p["name"] != "bSuccess"), None)
            name = self.name_for(node, out) if out else "cast"
            self.emit(f"{off}{name} = {self.expr(obj, 0) if obj else '?'} as {short(member)}{tail}", indent)
            ok = self.g.pin(node, "then", "out")
            fail = self.g.pin(node, "CastFailed", "out")
            ok_linked = bool(ok and self.g.targets(ok))
            fail_linked = bool(fail and self.g.targets(fail))
            if fail_linked:
                self.emit(f"if {name}:", indent)
                if ok_linked:
                    self.chain(ok, indent + 1)
                else:
                    self.emit("pass", indent + 1)
                self.emit("else:", indent)
                self.chain(fail, indent + 1)
            elif ok_linked:
                self.emit(f"if {name}:", indent)
                self.chain(ok, indent + 1)
            return

        if kind == "set":
            value_pin = next((p for p in self.g.pins(node, "in", exec_=False) if p["name"] != "self" and not p.get("hidden")), None)
            value = self.expr(value_pin, 0) if value_pin else "?"
            self.emit(f"{off}{self.target_prefix(node, 0)}{member} = {value}{tail}", indent)
            self._continue(node, indent)
            return

        if kind == "result":
            outs = [p for p in self.g.pins(node, "in", exec_=False) if not p.get("hidden")]
            if not outs:
                self.emit(f"{off}return{tail}", indent)
            elif len(outs) == 1 and outs[0]["name"] == "ReturnValue":
                self.emit(f"{off}return {self.expr(outs[0], 0)}{tail}", indent)
            else:
                self.emit(f"{off}return " + ", ".join(f"{p['name']}={self.expr(p, 0)}" for p in outs) + tail, indent)
            return

        if kind in ("addDelegate", "assignDelegate", "removeDelegate", "clearDelegate", "callDelegate"):
            target = self.target_prefix(node, 0)
            if kind == "callDelegate":
                text = f"{target}{member}.broadcast({', '.join(self.args(node, 0, skip=('Delegate',)))})"
            elif kind == "clearDelegate":
                text = f"{target}{member}.clear()"
            else:
                d = self.g.pin(node, "Delegate", "in")
                handler = self.expr(d, 0) if d else "?"
                op = "-=" if kind == "removeDelegate" else "+="
                text = f"{target}{member} {op} {handler}"
            self.emit(f"{off}{text}{tail}", indent)
            self._continue(node, indent)
            return

        if kind == "timeline":
            self.emit(f"{off}{member}.{in_pin['name']}(){tail}", indent)
            self._branches(node, indent, skip=())
            return

        # Plain call (or any other node with exec pins).
        if kind == "call":
            text = self.call_text(node, 0)
        else:
            text = f"{ident(node.get('title', '').splitlines()[0])}({', '.join(self.args(node, 0))})"
            exec_ins = self.g.pins(node, "in", exec_=True)
            if len(exec_ins) > 1:
                text = f"{ident(node.get('title', '').splitlines()[0])}.{in_pin['name']}({', '.join(self.args(node, 0))})"
        outs = [p for p in self.g.pins(node, "out", exec_=False) if self.g.targets(p)]
        if outs:
            names = ", ".join(self.name_for(node, p) for p in outs)
            text = f"{names} = {text}"
        if node.get("latent"):
            text = "await " + text
        self.emit(f"{off}{text}{tail}", indent)
        self._continue(node, indent)

    def _continue(self, node: dict, indent: int):
        """Follow the node's exec outputs: one plain 'then' continues inline, several become 'on <pin>:' blocks."""
        outs = self.g.linked_exec_outs(node)
        if len(outs) == 1 and outs[0]["name"] in ("then", "Then", "execute", "OutputPin", "Completed") and not node.get("latent"):
            self.chain(outs[0], indent)
            return
        if len(outs) == 1 and node.get("latent") and outs[0]["name"] in ("then", "Completed"):
            self.chain(outs[0], indent)
            return
        self._branches(node, indent, skip=())

    def _branches(self, node: dict, indent: int, skip: tuple[str, ...]):
        outs = [p for p in self.g.linked_exec_outs(node) if p["name"] not in skip]
        for p in outs:
            if p["name"] in ("then", "Then"):
                continue
            self.emit(f"on {p.get('label') or p['name']}:", indent)
            self.chain(p, indent + 1)
        for p in outs:
            if p["name"] in ("then", "Then"):
                self.chain(p, indent)

    def _macro(self, node: dict, off: str, tail: str, indent: int) -> bool:
        m = node.get("member", "")
        g = self.g

        def out(name):
            p = g.pin(node, name, "out")
            return p if p and g.targets(p) else None

        def inp(name):
            p = g.pin(node, name, "in")
            return self.expr(p, 0) if p else "?"

        if m in ("ForEachLoop", "ForEachLoopWithBreak", "ReverseForEachLoop"):
            item = g.pin(node, "Array Element", "out")
            index = g.pin(node, "Array Index", "out")
            names = [self.name_for(node, p) for p in (item, index) if p and g.targets(p)]
            self.emit(f"{off}for {', '.join(names) or '_'} in {inp('Array')}:{tail}", indent)
            body = out("LoopBody")
            self.chain(body, indent + 1) if body else self.emit("pass", indent + 1)
            done = out("Completed")
            if done:
                self.chain(done, indent)
            return True
        if m in ("ForLoop", "ForLoopWithBreak"):
            index = g.pin(node, "Index", "out")
            name = self.name_for(node, index) if index and g.targets(index) else "_"
            self.emit(f"{off}for {name} in {inp('FirstIndex')}..{inp('LastIndex')}:{tail}", indent)
            body = out("LoopBody")
            self.chain(body, indent + 1) if body else self.emit("pass", indent + 1)
            done = out("Completed")
            if done:
                self.chain(done, indent)
            return True
        if m == "WhileLoop":
            self.emit(f"{off}while {inp('Condition')}:{tail}", indent)
            body = out("LoopBody")
            self.chain(body, indent + 1) if body else self.emit("pass", indent + 1)
            done = out("Completed")
            if done:
                self.chain(done, indent)
            return True
        if m == "IsValid":
            obj = next((p for p in g.pins(node, "in", exec_=False) if not p.get("hidden")), None)
            ok, bad = out("Is Valid"), out("Is Not Valid")
            cond = f"isValid({self.expr(obj, 0) if obj else '?'})"
            if ok:
                self.emit(f"{off}if {cond}:{tail}", indent)
                self.chain(ok, indent + 1)
                if bad:
                    self.emit("else:", indent)
                    self.chain(bad, indent + 1)
            elif bad:
                self.emit(f"{off}if not {cond}:{tail}", indent)
                self.chain(bad, indent + 1)
            return True
        if m == "DoOnce":
            self.emit(f"{off}do once:{tail}", indent)
            done = out("Completed")
            self.chain(done, indent + 1) if done else self.emit("pass", indent + 1)
            return True
        # Gate, FlipFlop, DoN, MultiGate ...: generic block per output.
        args = self.args(node, 0)
        self.emit(f"{off}{m}({', '.join(args)}){tail}", indent)
        self._branches(node, indent, skip=())
        return True

    # -- entry points
    def event_name(self, node: dict) -> str:
        kind = node.get("kind")
        title = (node.get("title") or "").splitlines()[0]
        if kind == "boundEvent":
            return f"{node.get('component', '')}.{node.get('member', '')}"
        if kind == "customEvent":
            return node.get("member") or title
        if kind == "event":
            name = re.sub(r"^Event\s+", "", title).strip()
            if not name.isascii() and node.get("member"):          # localized editor: '이벤트 BeginPlay'
                return re.sub(r"^Receive(?=[A-Z])", "", node["member"])
            return ident(name) if name else (node.get("member") or "")
        return node_name(node)

    def params(self, node: dict) -> list[dict]:
        return [p for p in self.g.pins(node, "out", exec_=False) if not p.get("hidden") and p["name"] != "OutputDelegate"]

    def entries(self) -> list[dict]:
        """Nodes where execution starts: events, function/macro entries, input actions; ordered top-left first."""
        found = []
        for node in self.g.nodes.values():
            if node.get("kind") == "comment" or node.get("disabled"):
                continue
            ins = self.g.pins(node, "in", exec_=True)
            outs = self.g.exec_outs(node)
            if outs and not any(self.g.targets(p) for p in ins):
                if node.get("kind") in ("event", "customEvent", "boundEvent", "entry", "tunnel") or not ins:
                    found.append(node)
        found.sort(key=lambda n: ((n.get("pos") or [0, 0])[1], (n.get("pos") or [0, 0])[0]))
        return found

    def write_event_graph(self) -> list[str]:
        for node in self.entries():
            kind = node.get("kind")
            outs = self.g.linked_exec_outs(node)
            if not outs and kind in ("event", "boundEvent"):
                continue                                   # placed but empty (ghost) events
            params = ", ".join(self.name_for(node, p) for p in self.params(node))
            flags = node.get("memberFlags") or []
            tag = f"  [{', '.join(f for f in flags if f in ('Server', 'Client', 'Multicast', 'Reliable'))}]" if any(
                f in flags for f in ("Server", "Client", "Multicast")) else ""
            box = self._box_comment(node)
            if box:
                for line in box.splitlines():
                    self.emit(f"# {line}", 0)
            if kind in ("event", "customEvent", "boundEvent"):
                head = f"on {self.event_name(node)}({params}):{tag}"
            else:
                head = f"on {node_name(node)}({params}):"
            if node.get("comment"):
                head += f"  # {node['comment']}"
            self.emit(head, 0)
            self.emitted.add(node["id"])
            if len(outs) == 1 and outs[0]["name"] in ("then", "Then"):
                self.chain(outs[0], 1)
            else:
                for p in outs:
                    self.emit(f"on {p.get('label') or p['name']}:", 1)
                    self.chain(p, 2)
        return self.finish()

    def write_function(self) -> list[str]:
        entry = next((n for n in self.g.nodes.values() if n.get("kind") in ("entry", "tunnel")
                      and self.g.exec_outs(n) and not any(self.g.targets(p) for p in self.g.pins(n, "in", exec_=True))), None)
        if entry is None:
            entry = next((n for n in self.g.nodes.values() if n.get("kind") == "entry"), None)
        if entry is None:
            return self.write_data_graph()
        self.emitted.add(entry["id"])
        for p in self.params(entry):
            self.name_for(entry, p)
        for p in self.g.exec_outs(entry):
            self.chain(p, 1)
        # Pure functions have no exec wires: show the returned value.
        result = next((n for n in self.g.nodes.values() if n.get("kind") == "result"), None)
        if result and result["id"] not in self.emitted and not self.g.exec_outs(entry):
            self.statement(result, {}, 1)
        return self.finish()

    # Graphs without exec wires (anim graph, states, transitions): what flows into the result node.
    DATA_RESULTS = ("AnimGraphNode_Root", "AnimGraphNode_StateResult", "AnimGraphNode_TransitionResult",
                    "AnimGraphNode_CustomTransitionResult", "AnimGraphNode_LinkedAnimGraph")

    def write_data_graph(self) -> list[str]:
        results = [n for n in self.g.nodes.values() if n.get("class") in self.DATA_RESULTS[:4]]
        # a state machine: its states and the transitions between them ('Idle -> Start')
        for node in self.g.nodes.values():
            cls = node.get("class", "")
            if cls == "AnimStateEntryNode":
                for p in self.g.pins(node, "out"):
                    for target, _ in self.g.targets(p):
                        self.emit(f"start -> {(target.get('title') or '').splitlines()[0]}", 1)
            elif cls == "AnimStateTransitionNode":
                self.emit(f"transition {(node.get('title') or '').splitlines()[0]}", 1)
        for node in results:
            self.emitted.add(node["id"])
            for pin in self.g.pins(node, "in", exec_=False):
                if pin.get("hidden"):
                    continue
                if not self.g.source(pin) and literal(pin) is None:
                    continue
                what = "can enter" if node.get("class", "").endswith("TransitionResult") else "pose"
                self.emit(f"{what} = {self.expr(pin, 0)}", 1)
        return self.finish()

    def unused(self) -> list[str]:
        out = []
        for node in self.g.nodes.values():
            if node["id"] in self.emitted or node["id"] in self.referenced:
                continue
            if node.get("kind") in ("comment", "knot", "entry", "result", "tunnel"):
                continue
            if node.get("kind") in ("event",) and not self.g.linked_exec_outs(node):
                continue
            if not self.g.pins(node, exec_=True) and any(self.g.targets(p) for p in self.g.pins(node, "out")):
                continue                                   # pure node feeding something already counted
            out.append((node.get("title") or node.get("class", "")).splitlines()[0])
        return out


# ----------------------------------------------------------------------------------------------- document

def signature(graph: Graph) -> tuple[str, list[str], dict]:
    entry = next((n for n in graph.nodes.values() if n.get("kind") == "entry"), None)
    if entry is None:
        entry = next((n for n in graph.nodes.values() if n.get("kind") == "tunnel" and graph.exec_outs(n)), None) or {}
    result = next((n for n in graph.nodes.values() if n.get("kind") == "result"), None)
    ins = [f"{p['name']}: {ptype(p)}" for p in entry.get("pins", []) if p.get("dir") == "out" and not is_exec(p) and not p.get("hidden")]
    outs = []
    if result:
        outs = [f"{p['name']}: {ptype(p)}" for p in result.get("pins", []) if p.get("dir") == "in" and not is_exec(p) and not p.get("hidden")]
    text = f"({', '.join(ins)})"
    if outs:
        text += f" -> ({', '.join(outs)})" if len(outs) > 1 else f" -> {outs[0].split(': ', 1)[1] if outs[0].startswith('ReturnValue') else outs[0]}"
    locals_ = [f"{l['name']}: {ptype(l)}" + (f" = {format_value(l['default'])}" if l.get("default") else "") for l in entry.get("locals", [])]
    return text, locals_, entry


def flags_text(entry: dict) -> str:
    keep = [f for f in entry.get("functionFlags", []) if f in ("Pure", "Const", "Private", "Protected", "Static", "Server", "Client",
                                                              "Multicast", "Reliable", "AuthorityOnly")]
    if entry.get("callInEditor"):
        keep.append("CallInEditor")
    return f"  [{', '.join(keep)}]" if keep else ""


def var_line(v: dict) -> str:
    text = f"{v['name']}: {ptype(v)}"
    if v.get("default") not in (None, ""):
        text += f" = {format_value(v['default'], v)}"
    tags = list(v.get("flags", []))
    if v.get("repNotify"):
        tags = [t for t in tags if t != "Replicated"] + [f"RepNotify={v['repNotify']}"]
    if v.get("category"):
        tags.append(f"cat={v['category']}")
    if tags:
        text += f"  [{', '.join(tags)}]"
    if v.get("tooltip"):
        text += f"  # {v['tooltip']}"
    return text


def changed_text(items: list[dict]) -> str:
    return ", ".join(f"{c['name']}={format_value(c['value'])}" for c in items)


def render_head(bp: dict) -> list[str]:
    """Everything before the graphs: type, vars, components, defaults, timelines, widgets, dispatchers."""
    out: list[str] = []
    chain = [short(c) for c in bp.get("parentChain", [])]
    kind = bp.get("assetClass", "Blueprint")
    out.append(f"# {bp.get('name')}")
    out.append(f"{kind} : {' < '.join(chain) or short(bp.get('parent'))}  ·  {bp.get('package')}")
    if bp.get("interfaces"):
        out.append("implements: " + ", ".join(short(i) for i in bp["interfaces"]))
    if bp.get("status") and bp["status"] not in ("BS_UpToDate", "BS_UpToDateWithWarnings"):
        out.append(f"compile status: {bp['status']}")
    if bp.get("description"):
        out.append(f"> {bp['description']}")
    out.append("")

    if bp.get("variables"):
        out.append("## vars")
        out += [var_line(v) for v in bp["variables"]]
        out.append("")

    comps = bp.get("components") or []
    inherited = bp.get("inheritedComponents") or []
    if comps or inherited:
        out.append("## components")
        out += [component_line(c) for c in comps]
        out += [component_line(c, inherited=True) for c in inherited]
        out.append("")

    if bp.get("classDefaults"):
        out.append("## defaults")
        out.append(changed_text(bp["classDefaults"]))
        out.append("")

    if bp.get("timelines"):
        out.append("## timelines")
        for t in bp["timelines"]:
            tags = [f"{t.get('length', 0):g}s"] + (["loop"] if t.get("loop") else []) + (["autoplay"] if t.get("autoPlay") else [])
            out.append(f"{t['name']} [{', '.join(tags)}] tracks: {', '.join(t.get('tracks', [])) or '-'}")
        out.append("")

    if bp.get("widgets"):
        out.append("## widgets")
        children: dict[str, list[dict]] = {}
        for w in bp["widgets"]:
            children.setdefault(w.get("parent", ""), []).append(w)

        def walk(parent: str, depth: int):
            for w in children.get(parent, []):
                out.append("  " * depth + f"{w['name']}: {w['class']}" + (" (var)" if w.get("variable") else ""))
                walk(w["name"], depth + 1)
        walk("", 0)
        for b in bp.get("bindings", []):
            out.append(f"bind {b['widget']}.{b['property']} <- {b.get('function') or b.get('sourceProperty', '')}")
        if bp.get("animations"):
            out.append("animations: " + ", ".join(bp["animations"]))
        out.append("")

    dispatchers = [Graph(g) for g in bp.get("graphs", []) if g.get("kind") == "dispatcher"]
    if dispatchers:
        out.append("## dispatchers")
        for g in dispatchers:
            sig, _, _ = signature(g)
            out.append(f"{g.name}{sig}")
        out.append("")
    return out


def component_line(c: dict, inherited: bool = False) -> str:
    text = f"{c['name']}: {c['class']}"
    if inherited:
        text += " (inherited)"
    elif c.get("root"):
        text += " (root)"
    elif c.get("parent"):
        text += f" < {c['parent']}" + (f"@{c['socket']}" if c.get("socket") else "")
    if c.get("changed"):
        text += f"  {{{changed_text(c['changed'])}}}"
    return text


LABEL_LINE = re.compile(r"^\s*(L\d+):$")
LABEL_USE = re.compile(r"(?:goto|at) (L\d+)\b")


def renumber_labels(lines: list[str]) -> list[str]:
    """L-labels numbered L1, L2 ... in reading order (a part cut out of a graph gets its own numbering)."""
    order: dict[str, str] = {}
    for line in lines:
        for m in [LABEL_LINE.match(line)] + list(LABEL_USE.finditer(line)):
            if m and m.group(1) not in order:
                order[m.group(1)] = f"L{len(order) + 1}"
    if not order:
        return lines
    return [re.sub(r"\bL\d+\b", lambda m: order.get(m.group(0), m.group(0)), l)
            if LABEL_LINE.match(l) or LABEL_USE.search(l) else l for l in lines]


def split_events(body: list[str]) -> list[list[str]]:
    """Cuts an event graph's pseudocode into one block per event. Author comments right above an event go with
    it. Events joined by a goto (exec wires that meet, like Open Door / Close Door driving one timeline) stay
    together, so every block reads on its own."""
    segs: list[list[str]] = []
    pending: list[str] = []
    for line in body:
        if line.startswith("# "):
            pending.append(line)
        elif line.startswith("on ") or not segs:
            segs.append(pending + [line])
            pending = []
        else:
            segs[-1] += pending + [line]
            pending = []
    if pending:
        if segs:
            segs[-1] += pending
        else:
            segs.append(pending)

    parent = list(range(len(segs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    defined = {}
    for i, seg in enumerate(segs):
        for line in seg:
            m = LABEL_LINE.match(line)
            if m:
                defined[m.group(1)] = i
    for i, seg in enumerate(segs):
        for line in seg:
            for m in LABEL_USE.finditer(line):
                j = defined.get(m.group(1))
                if j is not None:
                    parent[find(i)] = find(j)
    groups: dict[int, list[str]] = {}
    for i, seg in enumerate(segs):
        groups.setdefault(find(i), []).extend(seg)
    return [renumber_labels(lines) for lines in groups.values()]


def graph_parts(bp: dict) -> tuple[list[dict], list[str]]:
    """Every readable unit of the Blueprint in reading order: the events of each event graph (see split_events),
    then functions, interface functions, macros and collapsed graphs. A part has kind (event, fn, macro,
    collapsed), name, events, head (the '## ...' lines), body (its text in the whole document; for an event
    graph the whole graph on the first part, None on the others) and lines (its own text)."""
    parts: list[dict] = []
    unused_total: list[str] = []
    graphs = [Graph(g) for g in bp.get("graphs", [])]
    for g in graphs:
        if g.kind != "event":
            continue
        w = Writer(g, bp)
        body = w.write_event_graph()
        unused_total += w.unused()
        if not body:
            continue
        for i, block in enumerate(split_events(body)):
            events = [m.group(1) for m in (re.match(r"on (.+?)\(", l) for l in block) if m]
            parts.append({"kind": "event", "graph": g.name, "name": events[0] if events else g.name, "events": events,
                          "head": [f"## graph {g.name}"], "body": body if i == 0 else None, "lines": block})

    for g in graphs:
        if g.kind not in ("function", "interface", "macro", "sub"):
            continue
        owner = (g.data.get("owner") or "").splitlines()[0] if g.data.get("owner") else ""
        if owner and owner != g.name:
            g.name = f"{g.name} {owner}" if g.name in ("Transition", "EventGraph") else g.name
        sig, locals_, entry = signature(g)
        w = Writer(g, bp)
        body = w.write_function() or ["  pass"]
        unused_total += w.unused()
        label = {"function": "fn", "interface": "fn", "macro": "macro", "sub": "collapsed"}[g.kind]
        head = f"## {label} {g.name}{sig}{flags_text(entry)}"
        if g.kind == "interface" and g.data.get("interface"):
            head += f"  (interface {short(g.data['interface'])})"
        heads = [head]
        if entry.get("tooltip"):
            heads.append(f"# {entry['tooltip']}")
        if locals_:
            heads.append("local " + ", ".join(locals_))
        parts.append({"kind": label, "graph": g.name, "name": g.name, "events": [], "head": heads, "body": body,
                      "lines": body})
    return parts, unused_total


def unused_lines(unused: list[str]) -> list[str]:
    if not unused:
        return []
    shown = ", ".join(unused[:12]) + (f" (+{len(unused) - 12})" if len(unused) > 12 else "")
    return [f"## unconnected nodes ({len(unused)})", shown, ""]


def render(bp: dict) -> str:
    """The whole Blueprint as one pseudocode document."""
    out = render_head(bp)
    parts, unused = graph_parts(bp)
    for part in parts:
        if part["body"] is None:
            continue                                   # later events of an event graph: printed with the first
        out += part["head"] + part["body"] + [""]
    out += unused_lines(unused)
    out.append(f"<!-- {LEGEND} -->")
    return "\n".join(out).rstrip() + "\n"


def find_names(text: str, names: list[str]) -> list[str]:
    """Names (they may contain spaces: 'Door Size') that occur in text as whole words. Longest first, and a
    found name is blanked out, so 'Door' is not found inside 'Door Size'."""
    found = []
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        pat = re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])")
        if pat.search(text):
            found.append(name)
            text = pat.sub("\x00", text)
    return found


def part_facts(bp: dict, part: dict, parts: list[dict]) -> dict:
    """What a part touches, for the overview table and the part file's header: variables used and written,
    components used, the Blueprint's own functions and events it calls."""
    body = part["lines"]
    var_names = [v["name"] for v in bp.get("variables", [])]
    comp_names = [c["name"] for c in (bp.get("components") or []) + (bp.get("inheritedComponents") or [])]
    writes = []
    for line in body:
        m = re.match(r"^\s*(?:\(disabled\) )?(.+?) = ", line)
        if m and m.group(1) in var_names and m.group(1) not in writes:
            writes.append(m.group(1))
    own = set(part["events"]) | {part["name"]}
    callable_ = [p["name"] for p in parts if p["kind"] in ("fn", "macro")] + [e for p in parts for e in p["events"]]
    calls_text = "\n".join(l for l in body if not l.startswith("on "))
    calls = [n for n in dict.fromkeys(callable_)
             if n not in own and re.search(r"(?<![\w.])" + re.escape(n) + r"\(", calls_text)]
    # Names of calls, events, timelines and author comments are not variable uses ('Open Door()' is not 'Door').
    text = "\n".join(re.sub(r"(^\s*#.*$|  # .*$)", "", l) for l in body)
    for name in sorted(set(callable_) | {t["name"] for t in bp.get("timelines", [])}, key=len, reverse=True):
        text = re.sub(r"(?<![\w])" + re.escape(name) + r"(?=\(|\.)", "\x00", text) if name not in var_names else text
    used = set(find_names(text, var_names + comp_names))
    return {"writes": writes, "calls": calls,
            "vars": [n for n in var_names if n in used], "components": [n for n in comp_names if n in used]}


def part_file_name(part: dict, taken: set[str]) -> str:
    stem = f"{part['kind'].replace('event', 'on')}_{re.sub(r'[^0-9A-Za-z_]+', '_', part['name']).strip('_') or 'part'}"
    name, n = stem, 2
    while name.lower() in taken:                       # Windows paths ignore case
        name, n = f"{stem}_{n}", n + 1
    taken.add(name.lower())
    return f"{name}.md"


def part_title(part: dict) -> str:
    if part["kind"] == "event":
        return ", ".join(f"on {e}" for e in part["events"]) or part["name"]
    if part.get("members"):
        return f"{part['kind']} " + ", ".join(part["members"])
    return part["head"][0][3:].strip()


def render_part(bp: dict, part: dict, facts: dict) -> str:
    """One part as a file that reads on its own: which Blueprint, the variables and components it uses (with
    types), what it writes and calls, then its pseudocode."""
    vars_ = {v["name"]: v for v in bp.get("variables", [])}
    comps = {c["name"]: c for c in (bp.get("components") or []) + (bp.get("inheritedComponents") or [])}
    chain = [short(c) for c in bp.get("parentChain", [])]
    title = part_title(part) if part["kind"] == "event" else f"{part['kind']} {part['name']}"
    out = [f"# {bp.get('name')} > {title}",
           f"{bp.get('assetClass', 'Blueprint')} : {' < '.join(chain) or short(bp.get('parent'))}  ·  {bp.get('package')}"
           f"  ·  overview: ../{bp.get('name')}.md", ""]
    if facts["vars"]:
        out.append("vars used: " + "; ".join(var_line(vars_[n]) for n in facts["vars"]))
    if facts["components"]:
        out.append("components used: " + "; ".join(f"{n}: {comps[n]['class']}" for n in facts["components"]))
    if facts["writes"]:
        out.append("writes: " + ", ".join(facts["writes"]))
    if facts["calls"]:
        out.append("calls own: " + ", ".join(facts["calls"]))
    if out[-1]:
        out.append("")
    out += part["head"] + part["lines"] + ["", f"<!-- {LEGEND} -->"]
    return "\n".join(out).rstrip() + "\n"


PACK_LINES = 30          # small events of one graph share a part file up to this many lines


def shift_labels(lines: list[str], offset: int) -> list[str]:
    return [re.sub(r"\bL(\d+)\b", lambda m: f"L{int(m.group(1)) + offset}", l)
            if LABEL_LINE.match(l) or LABEL_USE.search(l) else l for l in lines]


def pack_events(parts: list[dict], limit: int = PACK_LINES) -> list[dict]:
    """Neighbouring small events of the same graph go into one part (input bindings and one-line handlers are
    not worth a file each). Labels stay distinct, then are renumbered."""
    out: list[dict] = []
    for part in parts:
        prev = out[-1] if out else None
        tiny = lambda x: x["kind"] == "fn" and len(x["lines"]) <= 3 and (not x.get("members") or len(x["lines"]) <= limit)
        if (prev and prev["kind"] == part["kind"] and len(prev["lines"]) + len(part["lines"]) + len(part.get("head", [])) <= limit
                and (part["kind"] in ("collapsed", "macro") or (tiny(part) and (tiny(prev) or prev.get("members"))))):
            # small state / transition / collapsed graphs: one file with each graph's own heading
            merged = dict(prev)
            if not prev.get("members"):
                merged["lines"] = prev["head"] + prev["lines"]
                merged["members"] = [prev["name"]]
                merged["head"] = []
            merged["members"] = merged["members"] + [part["name"]]
            merged["lines"] = merged["lines"] + part["head"] + part["lines"]
            out[-1] = merged
        elif (prev and part["kind"] == "event" and prev["kind"] == "event" and prev["graph"] == part["graph"]
                and len(prev["lines"]) + len(part["lines"]) <= limit):
            used = len({m.group(1) for l in prev["lines"] for m in [LABEL_LINE.match(l)] if m})
            merged = dict(prev)
            merged["events"] = prev["events"] + part["events"]
            merged["lines"] = renumber_labels(prev["lines"] + shift_labels(part["lines"], used + 1000))
            out[-1] = merged
        else:
            out.append(part)
    return out


def render_split(bp: dict, pack_lines: int = PACK_LINES, where: dict | None = None) -> tuple[str, dict[str, str]]:
    """A Blueprint too long to read at once: <Name>.md becomes the overview (everything but the graphs, and a
    table of parts) and every event and function goes to parts/<file>.md. Returns (overview, {file: text}).
    where: filled with {event or function name: part file}."""
    out = render_head(bp)
    parts, unused = graph_parts(bp)
    parts = pack_events(parts, pack_lines)
    files: dict[str, str] = {}
    taken: set[str] = set()
    out += ["## parts",
            "Each event and function is a file in parts/ that reads on its own. Open only the ones the task needs.",
            "",
            "| part | lines | writes | calls own |",
            "|---|---|---|---|"]
    for part in parts:
        facts = part_facts(bp, part, parts)
        fname = part_file_name(part, taken)
        files[fname] = render_part(bp, part, facts)
        if where is not None:
            for key in (part.get("events") or []) + (part.get("members") or []) + [part["name"]]:
                where.setdefault(key, fname)
        out.append(f"| [{part_title(part).replace('|', '/')}](parts/{fname}) | {len(part['lines'])} | "
                   f"{', '.join(facts['writes']) or '-'} | {', '.join(facts['calls']) or '-'} |")
    out.append("")
    out += unused_lines(unused)
    out.append(f"<!-- {LEGEND} -->")
    return "\n".join(out).rstrip() + "\n", files


# ----------------------------------------------------------------------------------------------- folder

def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def source_changed(bp: dict, project: Path | None) -> bool | None:
    src = bp.get("source") or {}
    if not project or not src.get("file"):
        return None
    f = project / src["file"]
    if not f.exists():
        return True
    try:
        exported = datetime.fromisoformat(src["modifiedUtc"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    now = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
    return abs((now - exported).total_seconds()) > 2 or f.stat().st_size != src.get("bytes")


# ----------------------------------------------------------------------------------------------- facts and references
# Written next to index.md for `ctx` (lens_ctx.py) and the agent guide, so an agent asks one small question instead
# of opening every export:
#   facts.json (lens.bpfacts/1)  per Blueprint: fingerprint, changed since export, sizes (bytes of json, md, parts),
#                                C++ migration coverage and why nodes stay, the part file of each event / function
#   refs.json  (lens.refs/1)     symbol -> where it is read, written, called, defined, bound, cast to or spawned

FACTS = "lens.bpfacts/1"
REFS = "lens.refs/1"
VOLATILE_KEYS = ("exportedAtUtc", "source", "status")


def fingerprint(bp: dict) -> str:
    """The Blueprint's content, without when it was exported or the file's time: equal fingerprints mean the
    exported graphs, variables and defaults are the same (an explanation written for one fits the other)."""
    data = {k: v for k, v in bp.items() if k not in VOLATILE_KEYS}
    return hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


EVENT_CLASSES = ("K2Node_Event", "K2Node_CustomEvent", "K2Node_ComponentBoundEvent", "K2Node_InputAction", "K2Node_InputKey",
                 "K2Node_EnhancedInputAction", "K2Node_InputAxisEvent", "K2Node_InputAxisKeyEvent", "K2Node_InputTouch",
                 "K2Node_WidgetAnimationEvent", "K2Node_InputVectorAxisEvent")


def node_owners(bp: dict) -> dict[str, tuple[str, str]]:
    """node id -> (kind, name) of the event or function it belongs to: a function graph's nodes are that
    function's; an event graph's node belongs to the first event whose flow reaches it (exec forward, data back)."""
    owners: dict[str, tuple[str, str]] = {}
    for g in bp.get("graphs", []):
        nodes = {n["id"]: n for n in g.get("nodes", [])}
        if g.get("kind") != "event":
            for nid in nodes:
                owners[nid] = ("fn", g.get("name", ""))
            continue
        feeds: dict[str, list[str]] = {}          # node -> nodes whose outputs it reads
        for n in nodes.values():
            for pin in n.get("pins", []):
                if pin.get("dir") == "out":
                    for link in pin.get("links", []):
                        if not is_exec(pin):
                            feeds.setdefault(link["node"], []).append(n["id"])
        for root in (n for n in nodes.values() if n.get("class") in EVENT_CLASSES):
            name = root.get("member") or (root.get("title") or "").splitlines()[0]
            if root.get("class") == "K2Node_ComponentBoundEvent" and root.get("component"):
                name = f"{root['component']}.{name}"
            stack = [root["id"]]
            while stack:
                nid = stack.pop()
                if nid in owners or nid not in nodes:
                    continue
                owners[nid] = ("on", name)
                n = nodes[nid]
                stack += feeds.get(nid, [])
                for pin in n.get("pins", []):
                    if pin.get("dir") == "out" and is_exec(pin):
                        stack += [l["node"] for l in pin.get("links", [])]
    return owners


def symbol_of(bp: dict, owner: str | None, member: str) -> str:
    o = short((owner or "").replace(".SKEL_", "."))
    return f"{o or bp.get('name')}.{member}"


def node_refs(bp: dict) -> list[tuple[str, str, str]]:
    """(symbol, kind, node id) for what each node reads, writes, calls, defines, binds, casts to or spawns."""
    out = []
    for g in bp.get("graphs", []):
        for n in g.get("nodes", []):
            cls, member, owner = n.get("class", ""), n.get("member") or "", n.get("memberOwner")
            if cls == "K2Node_VariableGet" and member:
                out.append((symbol_of(bp, owner, member), "read", n["id"]))
            elif cls == "K2Node_VariableSet" and member:
                out.append((symbol_of(bp, owner, member), "write", n["id"]))
            elif cls in ("K2Node_CallFunction", "K2Node_CallArrayFunction", "K2Node_Message", "K2Node_CallParentFunction",
                         "K2Node_CommutativeAssociativeBinaryOperator", "K2Node_PromotableOperator") and member:
                out.append((symbol_of(bp, owner, member), "call", n["id"]))
            elif cls == "K2Node_CustomEvent" and member:
                out.append((symbol_of(bp, None, member), "define", n["id"]))
            elif cls == "K2Node_Event" and member:
                # an override or interface event: filed under what it implements, next to its callers
                out.append((symbol_of(bp, owner, member), "implement", n["id"]))
            elif cls == "K2Node_FunctionEntry" and g.get("kind") == "interface" and g.get("interface"):
                out.append((symbol_of(bp, g["interface"], g.get("name", "")), "implement", n["id"]))
            elif cls == "K2Node_FunctionEntry":
                out.append((f"{bp.get('name')}.{g.get('name')}", "define", n["id"]))
            elif cls == "K2Node_CallDelegate" and member:
                out.append((symbol_of(bp, owner, member), "broadcast", n["id"]))
            elif cls in ("K2Node_AddDelegate", "K2Node_RemoveDelegate", "K2Node_ClearDelegate") and member:
                out.append((symbol_of(bp, owner, member), "bind" if cls == "K2Node_AddDelegate" else "unbind", n["id"]))
            elif cls == "K2Node_ComponentBoundEvent" and member:
                out.append((f"{bp.get('name')}.{n.get('component', '')}.{member}", "bind", n["id"]))
            elif cls in ("K2Node_DynamicCast", "K2Node_ClassDynamicCast") and member:
                out.append((short(member), "cast", n["id"]))
            elif cls in ("K2Node_SpawnActorFromClass", "K2Node_GenericCreateObject", "K2Node_ConstructObjectFromClass", "K2Node_CreateWidget"):
                target = n.get("spawnClass") or next((p.get("defaultObject") for p in n.get("pins", []) if p.get("name") == "Class"), "")
                if target:
                    out.append((short(target.replace(".SKEL_", ".")), "spawn", n["id"]))
    return out


def native_coverage(bp: dict) -> dict | None:
    """What the C++ migration (lens_native) could move today: nodes, migrated, and why the rest stays."""
    if "native" not in bp:
        return None
    try:
        import lens_native
        plan = lens_native.Plan(json.loads(json.dumps(bp)), module="Game").build()
    except BaseException as e:                       # a Blueprint the planner refuses (already rebased ...)
        return {"error": str(e)[:200]}
    items = list(plan.funcs.values()) + list(plan.events) + list(getattr(plan, "timelines", {}).values())
    used, stays, ghosts = set(), [], set()
    for it in items:
        if it.migrated:
            used |= set(it.extra.get("nodes", []))
        elif it.extra.get("empty"):
            ghosts.add(it.extra.get("node"))            # an event with nothing wired to it: nothing to move
        elif it.reason:
            stays.append(f"{it.bp}: {it.reason}")
    graph_nodes = {n["id"] for g in bp.get("graphs", []) if g.get("kind") in ("event", "function", "interface") for n in g.get("nodes", [])
                   if n.get("class") != "EdGraphNode_Comment" and n["id"] not in ghosts}
    return {"nodes": len(graph_nodes), "migrated": len(graph_nodes & used), "stays": stays[:20]}


def write_folder(root: Path, project: Path | None, split_lines: int = SPLIT_LINES) -> int:
    manifest = load(root / "export.json")
    if not manifest:
        print(f"lens_bp: no export.json in {root}", file=sys.stderr)
        return 1
    keep = {e["dir"] for e in manifest.get("exported", [])}

    # Remove folders of Blueprints that are no longer tracked (only folders this tool wrote: they hold a lens.bp json).
    for js in sorted(root.rglob("*.json")):
        if js.name == "export.json":
            continue
        rel = js.parent.relative_to(root).as_posix()
        if rel in keep:
            continue
        data = load(js)
        if isinstance(data, dict) and data.get("schema") == SCHEMA:
            shutil.rmtree(js.parent, ignore_errors=True)
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()

    rows = []
    facts, refs = {}, {}
    for entry in manifest.get("exported", []):
        folder = root / entry["dir"]
        js = folder / f"{entry['name']}.json"
        bp = load(js)
        if not bp:
            continue
        md = render(bp)
        whole_lines = md.count("\n")
        parts_dir = folder / "parts"
        shutil.rmtree(parts_dir, ignore_errors=True)
        part_files: dict[str, str] = {}
        where: dict[str, str] = {}
        if whole_lines > split_lines:
            md, part_files = render_split(bp, where=where)
            parts_dir.mkdir()
            for fname, text in part_files.items():
                (parts_dir / fname).write_text(text, encoding="utf-8")
        (folder / f"{entry['name']}.md").write_text(md, encoding="utf-8")
        md_file = f"{entry['dir']}/{entry['name']}.md"
        owners = node_owners(bp)
        for symbol, kind, nid in node_refs(bp):
            okind, oname = owners.get(nid, ("", ""))
            part = where.get(oname)
            refs.setdefault(symbol, []).append([entry["name"], kind, f"{okind} {oname}".strip(),
                                                f"{entry['dir']}/parts/{part}" if part else md_file])
        part_bytes = [len(t.encode("utf-8")) for t in part_files.values()]
        facts[entry["name"]] = {
            "dir": entry["dir"], "package": entry.get("package"), "fingerprint": fingerprint(bp), "source": bp.get("source"),
            "changed": source_changed(bp, project),
            "bytes": {"json": js.stat().st_size, "md": len(md.encode("utf-8")), "parts": sum(part_bytes),
                      "largestPart": max(part_bytes, default=0)},
            "parts": {k: f"parts/{v}" for k, v in where.items()},
            "native": native_coverage(bp),
            "reused": bool(entry.get("reused")),
        }
        graphs = bp.get("graphs", [])
        rows.append({
            "name": entry["name"], "dir": entry["dir"], "parent": short(bp.get("parent")), "class": bp.get("assetClass"),
            "vars": len(bp.get("variables", [])), "fns": sum(1 for g in graphs if g.get("kind") in ("function", "interface")),
            "nodes": sum(len(g.get("nodes", [])) for g in graphs), "mdLines": whole_lines, "parts": len(part_files),
            "changed": source_changed(bp, project),
        })

    lines = [
        "# Tracked Blueprints",
        "",
        f"Exported {manifest.get('exportedAtUtc', '')} (UE {manifest.get('engineVersion', '')}). Each folder has "
        "`<Name>.md` (pseudocode, read this first) and `<Name>.json` (every node, pin and link). A Blueprint longer "
        f"than {split_lines} lines is split: `<Name>.md` is its overview with a table of parts, and every event and "
        "function is a file in `parts/` that reads on its own.",
        "Refresh: `__dwookit.bat bp export` (editor closed). Track more: `__dwookit.bat bp add <path>`.",
        "",
        "| Blueprint | type : parent | vars | functions | nodes | pseudocode lines | parts | C++ migration | |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda r: r["dir"]):
        note = "changed since export" if r["changed"] else ""
        nat = (facts.get(r["name"]) or {}).get("native") or {}
        cpp = "-" if not nat else ("rebased or refused" if nat.get("error") else f"{nat['migrated']}/{nat['nodes']} nodes")
        lines.append(f"| [{r['name']}]({r['dir']}/{r['name']}.md) | {r['class']} : {r['parent']} | {r['vars']} | {r['fns']} | "
                     f"{r['nodes']} | {r['mdLines']} | {r['parts'] or '-'} | {cpp} | {note} |")
    if manifest.get("missing"):
        lines += ["", "Not found (no Blueprint at this path): " + ", ".join(manifest["missing"])]
    if manifest.get("failed"):
        lines += ["", "Could not export: " + ", ".join(manifest["failed"])]
    lines += ["", LEGEND, ""]
    (root / "index.md").write_text("\n".join(lines), encoding="utf-8")
    stamp = manifest.get("exportedAtUtc", "")
    (root / "facts.json").write_text(json.dumps({"schema": FACTS, "exportedAtUtc": stamp, "blueprints": facts},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    (root / "refs.json").write_text(json.dumps({"schema": REFS, "exportedAtUtc": stamp,
                                               "symbols": {k: refs[k] for k in sorted(refs)}}, ensure_ascii=False),
                                    encoding="utf-8")
    print(f"lens_bp: {len(rows)} Blueprint(s) -> {root}")
    return 0


def print_status(root: Path, project: Path | None) -> int:
    manifest = load(root / "export.json") or {}
    exported = manifest.get("exported", [])
    print(f"exported {manifest.get('exportedAtUtc', '?')}: {len(exported)} Blueprint(s) in {root}")
    stale = 0
    for e in exported:
        bp = load(root / e["dir"] / f"{e['name']}.json") or {}
        changed = source_changed(bp, project)
        stale += bool(changed)
        print(f"  {e['package']}{'  (changed since export)' if changed else ''}")
    for m in manifest.get("missing", []):
        print(f"  {m}  (no Blueprint found)")
    if stale:
        print(f"{stale} changed since the export: run bp export again.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("md", help="write <Name>.md for every exported Blueprint and index.md")
    a.add_argument("--dir", required=True, type=Path)
    a.add_argument("--project", type=Path)
    a.add_argument("--split-lines", type=int, default=SPLIT_LINES,
                   help=f"split Blueprints longer than this into parts/ (default {SPLIT_LINES})")
    b = sub.add_parser("one", help="render one exported JSON")
    b.add_argument("json", type=Path)
    b.add_argument("-o", type=Path)
    c = sub.add_parser("status", help="exported Blueprints and whether their .uasset changed since")
    c.add_argument("--dir", required=True, type=Path)
    c.add_argument("--project", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "md":
        code = write_folder(args.dir, args.project, args.split_lines)
        if code == 0:
            import lens_explain                       # the same Blueprints explained for people (HTML, window)
            code = lens_explain.write_all(args.dir, args.project)
        return code
    if args.cmd == "status":
        return print_status(args.dir, args.project)
    bp = load(args.json)
    if not bp:
        print(f"lens_bp: cannot read {args.json}", file=sys.stderr)
        return 1
    text = render(bp)
    if args.o:
        args.o.write_text(text, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
