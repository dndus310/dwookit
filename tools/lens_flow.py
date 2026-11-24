"""DwoLens game flow: how the project runs, from the top down, as one map of the objects in play.

    map (GameDefaultMap, every level)       -> its GameMode (World Settings) and the Blueprints placed in it
    GameMode                                -> GameState, PlayerState, PlayerController, Pawn, HUD (class defaults)
    PlayerController                        -> the Pawn it possesses, the HUD it owns
    every object                            -> what it spawns (SpawnActor), creates (CreateWidget, ConstructObject),
                                               owns (components), uses (class-typed defaults: EnemyClass ...),
                                               calls, casts to, refers to

Each object carries its role in plain words, its behaviors (from features.json: Jump, Attack ...) and whether it is
a Blueprint or C++. The window draws it as a graph from a chosen root (a map or a GameMode); a click opens the
object's features or its file analysis.

Sources (all from one analyze run): raw/blueprints.json (class defaults, components, the class picked on spawn /
create nodes), out/graph.json (inheritance, calls, casts, references, asset references), out/features.json
(behaviors), Config/DefaultEngine.ini (default map, GameMode, GameInstance) and the project's C++ constructors
(DefaultPawnClass = ..., CreateDefaultSubobject<...>, SpawnActor<...>, ConstructorHelpers::FClassFinder).

    python lens_flow.py --raw <run>/raw --out <run>/out --project <dir>       -> <run>/out/flow.json (lens.flow/1)

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

SCHEMA = "lens.flow/1"

ROLES = [   # engine base class -> role (first match walking up the inheritance)
    ("GameModeBase", "gamemode"), ("GameStateBase", "gamestate"), ("PlayerController", "controller"),
    ("AIController", "ai"), ("PlayerState", "playerstate"), ("Character", "character"), ("Pawn", "pawn"),
    ("HUD", "hud"), ("UserWidget", "widget"), ("GameInstance", "gameinstance"), ("GameplayAbility", "ability"),
    ("GameplayEffect", "effect"), ("AnimInstance", "anim"), ("ActorComponent", "component"),
    ("PlayerCameraManager", "camera"), ("CameraShakeBase", "camera"), ("Actor", "actor"),
    ("WorldSubsystem", "subsystem"), ("GameInstanceSubsystem", "subsystem"), ("LocalPlayerSubsystem", "subsystem"),
    ("PrimaryDataAsset", "data"), ("DataAsset", "data"), ("BlueprintFunctionLibrary", "library"), ("Object", "object"),
]
ROLE_WORDS = {
    "map": "레벨(맵)", "gamemode": "게임 규칙 (GameMode)", "gamestate": "게임 전체 상태 (GameState)",
    "controller": "플레이어 컨트롤러 (입력)", "ai": "AI 컨트롤러", "playerstate": "플레이어별 상태 (PlayerState)",
    "character": "캐릭터", "pawn": "조종 대상 (Pawn)", "hud": "HUD", "widget": "UI 위젯", "gameinstance": "게임 인스턴스 (게임 전체)",
    "ability": "어빌리티 (GAS)", "effect": "게임플레이 이펙트 (GAS)", "anim": "애니메이션 로직", "component": "컴포넌트 (기능 부품)",
    "camera": "카메라", "actor": "월드에 놓이는 액터", "subsystem": "서브시스템", "data": "데이터", "library": "함수 모음", "object": "객체",
}
# GameMode class defaults that wire the player's objects together
GAME_SLOTS = {
    "DefaultPawnClass": "플레이어 폰", "PlayerControllerClass": "플레이어 컨트롤러", "HUDClass": "HUD",
    "GameStateClass": "게임 상태", "PlayerStateClass": "플레이어 상태", "SpectatorClass": "관전자",
    "ReplaySpectatorPlayerControllerClass": "리플레이 컨트롤러", "ServerStatReplicatorClass": "서버 통계",
}
EDGE_RANK = {"slot": 0, "possesses": 1, "spawns": 2, "creates": 2, "has": 3, "places": 3, "uses": 4, "calls": 5,
             "finds": 6, "casts": 7, "refers": 8}
EDGE_WORDS = {"slot": "", "possesses": "조종", "spawns": "생성 (Spawn)", "creates": "만들기", "has": "가짐 (컴포넌트)",
              "places": "배치", "uses": "사용", "calls": "호출", "finds": "찾기", "casts": "형변환해 사용", "refers": "참조"}
NODE_KIND_EDGE = {"K2Node_SpawnActorFromClass": "spawns", "K2Node_SpawnActor": "spawns", "K2Node_CreateWidget": "creates",
                  "K2Node_ConstructObjectFromClass": "creates", "K2Node_AddComponentByClass": "has",
                  "K2Node_AddComponent": "has", "K2Node_GetAllActorsOfClass": "finds",
                  "K2Node_GetAllActorsOfClassMatchingTagQuery": "finds", "K2Node_GetAllActorsWithInterface": "finds"}
PATH_RE = re.compile(r"(/(?:Game|Script|[A-Za-z0-9_]+)/[A-Za-z0-9_/\-\. ]+?)(?=['\")\],]|$)")


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def short(key: str) -> str:
    name = re.split(r"[./:]", key)[-1]
    return name[:-2] if name.endswith("_C") else name


class Model:
    def __init__(self, raw: Path, out: Path, project: Path | None):
        self.project = project
        graph = load(out / "graph.json") or {}
        self.symbols = {s["key"]: s for s in graph.get("symbols", [])}
        self.relations = graph.get("relations", [])
        self.blueprints = (load(raw / "blueprints.json") or {}).get("blueprints", [])
        self.features = load(out / "features.json") or {}
        self.parent: dict[str, str] = {}
        for r in self.relations:
            if r["type"] == "Inherits":
                self.parent.setdefault(r["source"], r["target"])
        # package / object path -> class key
        self.by_path: dict[str, str] = {}
        self.by_name: dict[str, str] = {}
        for key, s in self.symbols.items():
            if s["kind"] in ("blueprint", "class"):
                path = key.split(":", 1)[1]
                self.by_path[path] = key
                self.by_path[path.rsplit(".", 1)[0]] = key if s["kind"] == "blueprint" else self.by_path.get(path.rsplit(".", 1)[0], key)
                if s["kind"] == "blueprint" and s.get("package"):
                    self.by_path[s["package"]] = key
                if not s.get("external"):
                    self.by_name.setdefault(s["name"], key)
                    self.by_name.setdefault(re.sub(r"^[AU](?=[A-Z])", "", s["name"]), key)
        self.edges: dict[tuple[str, str], dict] = {}
        # data assets that decide what is in play (Lyra: experiences, pawn data, ability sets): asset key -> class
        self.data_assets: dict[str, str] = {}
        for key, s in self.symbols.items():
            if s["kind"] != "asset":
                continue
            cls = str(s.get("asset_class", ""))
            name = cls.rsplit(".", 1)[-1]
            if self.project_class(self.by_name.get(name)) or "DataAsset" in name or name in ("DataTable", "PrimaryAssetLabel"):
                self.data_assets[key] = name

    def data_key(self, asset_key: str) -> str | None:
        return "data:" + asset_key.split(":", 1)[1] if asset_key in self.data_assets else None

    def asset_node(self, asset_key: str) -> str | None:
        """An asset as a flow node: its Blueprint class, the data asset, or the map."""
        if asset_key in self.data_assets:
            return self.data_key(asset_key)
        s = self.symbols.get(asset_key, {})
        if str(s.get("asset_class", "")).endswith(".World"):
            return "map:" + asset_key.split(":", 1)[1]
        return self.asset_class_key(asset_key)

    # -- keys
    def project_class(self, key: str | None) -> bool:
        s = self.symbols.get(key or "")
        return bool(s) and s["kind"] in ("blueprint", "class") and not s.get("external")

    def resolve(self, path: str) -> str | None:
        """An object or class path ('/Game/A/BP_X.BP_X_C', '/Game/A/BP_X', '/Script/Mod.Class') -> class key."""
        path = path.strip().strip("'\"")
        path = re.sub(r"\.SKEL_", ".", path)
        for candidate in (path, path.rsplit(".", 1)[0] if "." in path.rsplit("/", 1)[-1] else path):
            if candidate in self.by_path:
                return self.by_path[candidate]
        if path.startswith("/Script/"):
            return self.by_name.get(path.rsplit(".", 1)[-1])
        return None

    def asset_class_key(self, asset_key: str) -> str | None:
        """asset:/Game/A/BP_X -> the Blueprint's class key."""
        return self.by_path.get(asset_key.split(":", 1)[1])

    def role(self, key: str) -> str:
        seen = 0
        k = key
        while k and seen < 40:
            name = short(k)
            for base, role in ROLES:
                if name == base:
                    return role
            k = self.parent.get(k)
            seen += 1
        return "object"

    def chain(self, key: str) -> list[str]:
        out, k = [], self.parent.get(key)
        while k and len(out) < 12:
            out.append(k)
            k = self.parent.get(k)
        return out

    # -- edges
    def add(self, src: str | None, dst: str | None, kind: str, label: str = "", evidence: str = ""):
        if not src or not dst or src == dst:
            return
        if not (self.project_class(dst) or dst.startswith("map:") or dst.startswith("data:")):
            return
        e = self.edges.get((src, dst))
        if e is None or EDGE_RANK[kind] < EDGE_RANK[e["kind"]]:
            labels = (e or {}).get("labels", [])
            self.edges[(src, dst)] = e = {"from": src, "to": dst, "kind": kind, "labels": labels, "evidence": []}
        if label and label not in e["labels"]:
            e["labels"].append(label)
        if evidence and evidence not in e["evidence"] and len(e["evidence"]) < 5:
            e["evidence"].append(evidence)

    def class_defaults(self) -> dict[str, dict[str, str]]:
        """Class key -> {property: class key} for class-typed defaults, inherited down the Blueprint chain."""
        own: dict[str, dict[str, str]] = defaultdict(dict)
        for bp in self.blueprints:
            key = self.resolve(bp.get("generatedClass", ""))
            if not key:
                continue
            for d in bp.get("classDefaults", []):
                value = str(d.get("value", ""))
                # Type'/Path/Obj.Obj': the quoted object comes first, the type only when the object says nothing
                quoted = re.findall(r"'(/[^']+)'", value)
                bare = [m.group(1) for m in PATH_RE.finditer(re.sub(r"'[^']*'", "''", value))]
                found_class = None
                quoted_hit = False
                for path in quoted + ["--types--"] + bare:
                    if path == "--types--":
                        if quoted_hit:
                            break                  # the objects said it; their types add nothing
                        continue
                    data = self.data_key("asset:" + path.split(":", 1)[0].rsplit(".", 1)[0])
                    if data:
                        self.add(key, data, "uses", d["name"], f"{short(key)}.{d['name']}")
                        quoted_hit = True
                        continue
                    target = self.resolve(path.split(":", 1)[0])
                    if target and target != key and self.project_class(target):
                        quoted_hit = quoted_hit or path in quoted
                        if found_class is None:
                            found_class = target
                if found_class:
                    own[key][d["name"]] = found_class
        for key, slots in self.cpp_defaults().items():
            for name, target in slots.items():
                own[key].setdefault(name, target)
        full: dict[str, dict[str, str]] = {}
        for key in self.symbols:
            merged: dict[str, str] = {}
            for k in reversed([key] + self.chain(key)):
                merged.update(own.get(k, {}))
            if merged:
                full[key] = merged
        return full

    def cpp_files(self) -> dict[str, list[Path]]:
        """Project C++ class key -> its .cpp files (next to the header, or a same-named file under Source)."""
        out: dict[str, list[Path]] = {}
        if not self.project:
            return out
        cpps: dict[str, list[Path]] = defaultdict(list)
        for root in [self.project / "Source"] + ([p.parent / "Source" for p in (self.project / "Plugins").rglob("*.uplugin")
                                                  if not p.parent.name.startswith("__")] if (self.project / "Plugins").is_dir() else []):
            if root.is_dir():
                for f in root.rglob("*.cpp"):
                    cpps[f.stem].append(f)
        for key, s in self.symbols.items():
            if s["kind"] == "class" and not s.get("external") and s.get("file"):
                stem = Path(s["file"]).stem
                if cpps.get(stem):
                    out[key] = cpps[stem]
        return out

    def cpp_defaults(self) -> dict[str, dict[str, str]]:
        """C++ constructors: DefaultPawnClass = AX::StaticClass() / = Finder.Class (ConstructorHelpers)."""
        out: dict[str, dict[str, str]] = defaultdict(dict)
        self._cpp_text: dict[str, str] = {}
        for key, files in self.cpp_files().items():
            text = "\n".join(f.read_text(encoding="utf-8-sig", errors="replace") for f in files)
            self._cpp_text[key] = text
            finders = {m.group(1): m.group(2) for m in re.finditer(
                r"ConstructorHelpers::FClassFinder<\s*\w+\s*>\s+(\w+)\s*\(\s*TEXT\(\s*\"([^\"]+)\"", text)}
            for m in re.finditer(r"\b(\w+Class)\s*=\s*([AU]?\w+)::StaticClass\(\)", text):
                target = self.by_name.get(m.group(2)) or self.by_name.get(re.sub(r"^[AU]", "", m.group(2)))
                if target:
                    out[key][m.group(1)] = target
            for m in re.finditer(r"\b(\w+Class)\s*=\s*(\w+)\.Class\b", text):
                path = finders.get(m.group(2))
                target = self.resolve(path) if path else None
                if target:
                    out[key][m.group(1)] = target
        return out

    def scan(self):
        defaults = self.class_defaults()
        # class-typed defaults: GameMode slots and anything else (EnemyClass, WeaponClass ...)
        for key, slots in defaults.items():
            if not self.project_class(key):
                continue
            for name, target in slots.items():
                kind = "slot" if name in GAME_SLOTS and self.role(key) == "gamemode" else "uses"
                self.add(key, target, kind, GAME_SLOTS.get(name, name) if kind == "slot" else name, f"{short(key)}.{name}")
            ctrl = slots.get("PlayerControllerClass")
            pawn = slots.get("DefaultPawnClass")
            hud = slots.get("HUDClass")
            if self.role(key) == "gamemode" and ctrl and pawn:
                self.add(ctrl, pawn, "possesses", "조종 (Possess)", "GameMode가 플레이어 폰을 이 컨트롤러에 붙임")
            if self.role(key) == "gamemode" and ctrl and hud:
                self.add(ctrl, hud, "has", "HUD", "HUD는 플레이어 컨트롤러가 가짐")
        # Blueprint graphs and components
        for bp in self.blueprints:
            key = self.resolve(bp.get("generatedClass", ""))
            if not key:
                continue
            for c in bp.get("components", []):
                target = self.resolve(c.get("classPath", "")) or self.by_name.get(c.get("class", ""))
                self.add(key, target, "has", c.get("name", ""), f"{short(key)}.{c.get('name', '')}")
            for g in bp.get("graphs", []):
                for n in g.get("nodes", []):
                    arg = n.get("classArg")
                    if arg:
                        kind = NODE_KIND_EDGE.get(n.get("class", ""), "uses")
                        self.add(key, self.resolve(arg), kind, n.get("title", "").splitlines()[0][:60], f"{short(key)} · {g.get('name')}")
        # C++: components, spawns, widgets
        for key, text in getattr(self, "_cpp_text", {}).items():
            for m in re.finditer(r"CreateDefaultSubobject<\s*([AU]\w+)\s*>\s*\(\s*TEXT\(\s*\"([^\"]+)\"", text):
                self.add(key, self.by_name.get(m.group(1)), "has", m.group(2), f"{short(key)} 생성자")
            for m in re.finditer(r"SpawnActor<\s*([AU]\w+)\s*>", text):
                self.add(key, self.by_name.get(m.group(1)), "spawns", "SpawnActor", f"{short(key)}.cpp")
            for m in re.finditer(r"CreateWidget<\s*([AU]\w+)\s*>", text):
                self.add(key, self.by_name.get(m.group(1)), "creates", "CreateWidget", f"{short(key)}.cpp")
            for m in re.finditer(r"NewObject<\s*([AU]\w+)\s*>", text):
                self.add(key, self.by_name.get(m.group(1)), "creates", "NewObject", f"{short(key)}.cpp")
        # asset references: into and out of data assets (what data decides), between Blueprints (weakest)
        for r in self.relations:
            if r["type"] not in ("AssetHardRef", "AssetSoftRef"):
                continue
            src, dst = self.asset_node(r["source"]), self.asset_node(r["target"])
            if not src or not dst:
                continue
            ev = r["source"].split(":", 1)[1]
            if dst.startswith("data:") and not src.startswith("map:"):
                self.add(src, dst, "uses", "데이터", ev)
            elif src.startswith("data:"):
                self.add(src, dst, "uses", "데이터가 지정", ev)
            elif src.startswith("map:"):
                if r["type"] == "AssetSoftRef":
                    self.add(src, dst, "uses", "소프트 참조 (World Settings 등)", ev)
            elif self.project_class(src) and self.role(src) == "data":
                self.add(src, dst, "uses", "데이터가 지정", ev)     # data-only Blueprints (Lyra experiences)
            elif self.project_class(src):
                self.add(src, dst, "refers", "에셋 참조", ev)
        # analyzer relations
        for r in self.relations:
            t = r["type"]
            src, dst = r["source"], r["target"]
            if not self.project_class(src):
                continue
            if t == "Calls":
                self.add(src, dst, "calls", "", (r.get("evidence") or [""])[0])
            elif t == "CastsTo":
                self.add(src, dst, "casts", "", (r.get("evidence") or [""])[0])
            elif t in ("RefHard", "RefSoft"):
                self.add(src, dst, "refers", "", (r.get("evidence") or [""])[0])
        self.inherit_edges()

    def inherit_edges(self):
        """A Blueprint does what its project parents (C++ or Blueprint) do: their spawns, components, calls and
        references carry over ('(부모 X)'); calls from a class into its own parents are internal (dropped)."""
        children: dict[str, list[str]] = defaultdict(list)
        for key in self.symbols:
            if self.symbols[key]["kind"] == "blueprint" and self.project_class(key):
                for anc in self.chain(key):
                    if self.project_class(anc):
                        children[anc].append(key)
        for (src, dst), e in list(self.edges.items()):
            if dst in self.chain(src):
                del self.edges[(src, dst)]
                continue
            if e["kind"] in ("slot", "possesses") or (e["kind"] == "uses" and e["labels"] and e["labels"][0] not in ("데이터",)):
                continue                 # class defaults are already merged down the inheritance chain
            for child in children.get(src, []):
                if (child, dst) not in self.edges and dst not in self.chain(child) and dst != child:
                    self.add(child, dst, e["kind"], f"{e['labels'][0] if e['labels'] else ''} (부모 {short(src)})".strip(), short(src))

    def maps(self, ini: dict) -> list[dict]:
        out = []
        for key, s in self.symbols.items():
            if not str(s.get("asset_class", "")).endswith(".World"):
                continue
            path = key.split(":", 1)[1]
            mk = f"map:{path}"
            gamemode = None
            for r in self.relations:
                if r["source"] != key or r["type"] != "AssetHardRef":
                    continue
                data = self.data_key(r["target"])
                if data:
                    self.add(mk, data, "uses", "데이터", path)
                    continue
                target = self.asset_class_key(r["target"])
                if not target or not self.project_class(target):
                    continue
                if self.role(target) == "gamemode":
                    gamemode = target
                    self.add(mk, target, "slot", "게임 모드 (World Settings)", path)
                else:
                    self.add(mk, target, "places", "", path)
            if gamemode is None and ini.get("gamemode"):
                self.add(mk, ini["gamemode"], "slot", "게임 모드 (프로젝트 기본값)", "Config/DefaultEngine.ini")
            out.append({"key": mk, "name": short(path), "path": path, "default": path == ini.get("map")})
        return out

    def behaviors(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for c in self.features.get("classes", []):
            names = [b.get("name") for b in c.get("behaviors", []) if b.get("name")]
            if not names:
                names = [f.get("name") for f in c.get("features", []) if f.get("category") != "public"][:6]
            if names:
                out[c["key"]] = names
        return out


def read_ini(project: Path | None, model: Model) -> dict:
    out = {}
    if not project:
        return out
    ini = project / "Config" / "DefaultEngine.ini"
    try:
        text = ini.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    for name, key in (("GameDefaultMap", "map"), ("GlobalDefaultGameMode", "gamemode"), ("GameInstanceClass", "gameinstance")):
        m = re.search(rf"^\s*{name}\s*=\s*(\S+)", text, re.M)
        if not m:
            continue
        value = m.group(1).strip()
        if key == "map":
            out["map"] = value.rsplit(".", 1)[0] if "." in value.rsplit("/", 1)[-1] else value
        else:
            target = model.resolve(value)
            if target and model.project_class(target):
                out[key] = target
    return out


def tracked_packages(project: Path | None) -> set[str]:
    if not project:
        return set()
    manifest = load(project / "__dwo-forge" / "blueprints" / "export.json") or {}
    return {e.get("package") for e in manifest.get("exported", [])}


def build(raw: Path, out: Path, project: Path | None) -> dict:
    m = Model(raw, out, project)
    ini = read_ini(project, m)
    m.scan()
    maps = m.maps(ini)
    behaviors = m.behaviors()
    tracked = tracked_packages(project)
    # Live Lens: classes that existed while the game ran in the editor (PIE), over all sessions
    runtime = (load(project / "__dwo-forge" / "live" / "runtime.json") or {}).get("classes", {}) if project else {}
    gamemodes = sorted({k for k in m.symbols if m.project_class(k) and m.role(k) == "gamemode"},
                       key=lambda k: (k != ini.get("gamemode"), short(k)))
    # one GameMode per Blueprint/C++ pair is enough as a root: prefer the Blueprint child that is used
    roots = [{"key": mp["key"], "name": mp["name"], "kind": "map", "default": mp["default"]} for mp in
             sorted(maps, key=lambda x: (not x["default"], x["name"]))]
    roots += [{"key": g, "name": short(g), "kind": "gamemode", "default": g == ini.get("gamemode")} for g in gamemodes]
    if ini.get("gameinstance"):
        roots.append({"key": ini["gameinstance"], "name": short(ini["gameinstance"]), "kind": "gameinstance", "default": True})

    used = {k for e in m.edges.values() for k in (e["from"], e["to"])}
    nodes = {}
    for key in sorted(used | set(gamemodes)):
        if key.startswith("data:"):
            cls = m.data_assets.get("asset:" + key.split(":", 1)[1], "")
            nodes[key] = {"name": short(key), "role": "data", "roleWords": f"데이터 ({cls})" if cls else ROLE_WORDS["data"],
                          "source": "data", "path": key.split(":", 1)[1]}
            continue
        if key.startswith("map:"):
            mp = next(x for x in maps if x["key"] == key)
            nodes[key] = {"name": mp["name"], "role": "map", "roleWords": ROLE_WORDS["map"], "source": "map", "path": mp["path"]}
            continue
        s = m.symbols.get(key, {})
        role = m.role(key)
        nodes[key] = {
            "name": short(key), "role": role, "roleWords": ROLE_WORDS.get(role, role),
            "source": "bp" if s.get("kind") == "blueprint" else "cpp",
            "parent": short(m.parent.get(key, "")), "chain": [short(c) for c in m.chain(key)][:6],
            "path": s.get("package") or s.get("file") or "",
            "behaviors": behaviors.get(key, [])[:8],
            "tracked": bool(s.get("package") and s.get("package") in tracked),
        }
        seen = runtime.get(key.split(":", 1)[1])
        if seen:
            nodes[key]["inPlay"] = {"sessions": seen.get("sessions", 0), "maxCount": seen.get("maxCount", 0)}
    edges = sorted(m.edges.values(), key=lambda e: (EDGE_RANK[e["kind"]], e["from"], e["to"]))
    for e in edges:
        e["words"] = EDGE_WORDS[e["kind"]]
    reach = set()
    for r in roots:
        q = deque([r["key"]])
        adj = defaultdict(list)
        for e in edges:
            adj[e["from"]].append(e["to"])
        while q:
            k = q.popleft()
            if k in reach:
                continue
            reach.add(k)
            q.extend(adj.get(k, []))
    for k in list(reach):                      # a C++ base is in play through its Blueprint child
        reach.update(c for c in m.chain(k) if m.project_class(c))
    unreached = sorted(k for k in m.symbols if m.project_class(k) and k not in reach)
    return {"schema": SCHEMA, "defaults": ini, "roots": roots, "nodes": nodes, "edges": edges,
            "unreached": [{"key": k, "name": short(k), "role": m.role(k), "roleWords": ROLE_WORDS.get(m.role(k), ""),
                           "source": "bp" if m.symbols[k]["kind"] == "blueprint" else "cpp"} for k in unreached]}


def reachable(doc: dict, root: str, depth: int = 4, kinds: tuple[str, ...] = ("slot", "possesses", "spawns", "creates", "has", "places", "uses")) -> tuple[list[str], list[dict]]:
    """Objects within depth steps of root along the stronger links; the edges among them."""
    adj = defaultdict(list)
    for e in doc["edges"]:
        if e["kind"] in kinds:
            adj[e["from"]].append(e)
    seen = {root: 0}
    q = deque([root])
    edges = []
    while q:
        k = q.popleft()
        if seen[k] >= depth:
            continue
        for e in adj.get(k, []):
            edges.append(e)
            if e["to"] not in seen:
                seen[e["to"]] = seen[k] + 1
                q.append(e["to"])
    return list(seen), edges


def render_html(doc: dict) -> str:
    """Report section: one diagram per map (map -> GameMode -> player objects -> what they spawn and use)."""
    import html as h
    if not doc.get("roots"):
        return ""
    parts = ["<section><h2>게임 흐름</h2><p class='meta'>맵에서 출발해 게임 모드, 플레이어 객체, 그들이 만들고 쓰는 객체로 내려갑니다. "
             "호출·참조까지 포함한 전체는 dwo-Forge 창의 DwoLens › 게임 흐름에서 봅니다.</p>"]
    for root in [r for r in doc["roots"] if r["kind"] == "map"][:8]:
        keys, edges = reachable(doc, root["key"])
        if len(keys) < 2:
            continue
        ids = {k: f"n{i}" for i, k in enumerate(keys)}
        lines = ["flowchart LR"]
        for k in keys:
            n = doc["nodes"].get(k, {"name": k, "roleWords": ""})
            label = f"{n['name']}<br/><small>{n.get('roleWords', '')}</small>".replace('"', "'")
            lines.append(f'  {ids[k]}["{label}"]')
        for e in edges:
            words = (e["labels"][0] if e["labels"] else e["words"]).replace('"', "'")
            arrow = "-.->" if e["kind"] in ("uses",) else "-->"
            lines.append(f'  {ids[e["from"]]} {arrow}|"{words}"| {ids[e["to"]]}')
        title = f"{root['name']}{' (기본 맵)' if root.get('default') else ''}"
        parts.append(f"<h3>{h.escape(title)}</h3><pre class='mermaid'>{h.escape(chr(10).join(lines))}</pre>")
    parts.append("</section>")
    return "".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--project", type=Path)
    args = ap.parse_args(argv)
    doc = build(args.raw, args.out, args.project)
    (args.out / "flow.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"lens_flow: {len(doc['nodes'])} objects, {len(doc['edges'])} links, {len(doc['roots'])} starting points -> {args.out / 'flow.json'}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
