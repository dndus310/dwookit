"""DwoLens native migration: Blueprint -> C++ base class, from a Blueprint export (lens.bp/1 with "native").

The generated class becomes the Blueprint's new parent (the rebase step in the DwoLensInspector commandlet), so the
asset, its references, level instances and everything that was not migrated keep working. Rules:

  exact or not at all    a function or event is migrated only when every node in it translates to C++ with the
                         same meaning; one unknown node keeps the whole function in the Blueprint (with the reason)
  same names             variables and components keep their Blueprint names, so references, level instances and
                         saved values map 1:1; a name that is not a C++ identifier gets a recorded rename
  values stay in the BP  component templates, asset references and variable defaults are kept as overrides on the
                         Blueprint (rebase copies them and verifies them); C++ initializers only mirror simple values
  ubergraph semantics    event graph temporaries live in members, as in the Blueprint's persistent frame

    python lens_native.py plan     <Name.json> [--module M] [--class-name N]           what would migrate, and why not
    python lens_native.py generate <Name.json> --out <dir> [--module M] [--class-name N] [--api API]
                                   -> <dir>/<Class>.h, <Class>.cpp, DwoNativeLiterals.h, <Name>.native.json, report

Standard library only, no network.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "dwookit.native/1"
CPP_KEYWORDS = {"alignas", "alignof", "and", "asm", "auto", "bool", "break", "case", "catch", "char", "class", "const",
                "constexpr", "continue", "default", "delete", "do", "double", "else", "enum", "explicit", "export",
                "extern", "false", "float", "for", "friend", "goto", "if", "inline", "int", "long", "mutable",
                "namespace", "new", "noexcept", "not", "nullptr", "operator", "or", "private", "protected", "public",
                "register", "return", "short", "signed", "sizeof", "static", "struct", "switch", "template", "this",
                "throw", "true", "try", "typedef", "typename", "union", "unsigned", "using", "virtual", "void",
                "volatile", "while", "Super", "ThisClass"}
STANDARD_MACROS = "/Engine/EditorBlueprintResources/StandardMacros"

# Blueprint event -> native override: (owner class, event) -> (C++ signature, call Super first?, super call)
# Order follows the engine: the Blueprint event runs inside Super, so the body goes after Super where Super calls
# the event last, before it where Super calls it first (EndPlay).
OVERRIDES = {
    ("Actor", "ReceiveBeginPlay"): ("void BeginPlay()", "after", "Super::BeginPlay();", []),
    ("Actor", "ReceiveTick"): ("void Tick(float DeltaSeconds)", "after", "Super::Tick(DeltaSeconds);", ["DeltaSeconds"]),
    ("Actor", "ReceiveEndPlay"): ("void EndPlay(const EEndPlayReason::Type EndPlayReason)", "before", "Super::EndPlay(EndPlayReason);", ["EndPlayReason"]),
    ("Actor", "ReceiveDestroyed"): ("void Destroyed()", "before", "Super::Destroyed();", []),
    ("Actor", "ReceiveActorBeginOverlap"): ("void NotifyActorBeginOverlap(AActor* OtherActor)", "after", "Super::NotifyActorBeginOverlap(OtherActor);", ["OtherActor"]),
    ("Actor", "ReceiveActorEndOverlap"): ("void NotifyActorEndOverlap(AActor* OtherActor)", "after", "Super::NotifyActorEndOverlap(OtherActor);", ["OtherActor"]),
    ("Pawn", "ReceivePossessed"): ("void PossessedBy(AController* NewController)", "after", "Super::PossessedBy(NewController);", ["NewController"]),
    ("Pawn", "ReceiveUnpossessed"): ("void UnPossessed()", "custom", "AController* OldController = Controller;\n\tSuper::UnPossessed();", ["OldController"]),
    ("ActorComponent", "ReceiveBeginPlay"): ("void BeginPlay()", "after", "Super::BeginPlay();", []),
    ("ActorComponent", "ReceiveTick"): ("void TickComponent(float DeltaTime, ELevelTick TickType, FActorComponentTickFunction* ThisTickFunction)", "after",
                                        "Super::TickComponent(DeltaTime, TickType, ThisTickFunction);", ["DeltaTime"]),
    ("ActorComponent", "ReceiveEndPlay"): ("void EndPlay(const EEndPlayReason::Type EndPlayReason)", "before", "Super::EndPlay(EndPlayReason);", ["EndPlayReason"]),
    ("UserWidget", "Construct"): ("void NativeConstruct()", "after", "Super::NativeConstruct();", []),
    ("UserWidget", "Destruct"): ("void NativeDestruct()", "before", "Super::NativeDestruct();", []),
    ("UserWidget", "PreConstruct"): ("void NativePreConstruct()", "after", "Super::NativePreConstruct();", ["IsDesignTime()"]),
    ("UserWidget", "Tick"): ("void NativeTick(const FGeometry& MyGeometry, float InDeltaTime)", "after", "Super::NativeTick(MyGeometry, InDeltaTime);", ["MyGeometry", "InDeltaTime"]),
    ("UserWidget", "OnInitialized"): ("void NativeOnInitialized()", "after", "Super::NativeOnInitialized();", []),
    ("AnimInstance", "BlueprintUpdateAnimation"): ("void NativeUpdateAnimation(float DeltaSeconds)", "after", "Super::NativeUpdateAnimation(DeltaSeconds);", ["DeltaSeconds"]),
    ("AnimInstance", "BlueprintInitializeAnimation"): ("void NativeInitializeAnimation()", "after", "Super::NativeInitializeAnimation();", []),
    ("AnimInstance", "BlueprintBeginPlay"): ("void NativeBeginPlay()", "after", "Super::NativeBeginPlay();", []),
}
# Where a native class hierarchy decides which override row applies.
OVERRIDE_BASES = ["Pawn", "Actor", "ActorComponent", "UserWidget", "AnimInstance"]

# Private or protected engine members Blueprints read directly (AllowPrivateAccess): the public C++ getter.
PRIVATE_GETTERS = {
    ("Character", "CharacterMovement"): "GetCharacterMovement()", ("Character", "Mesh"): "GetMesh()",
    ("Character", "CapsuleComponent"): "GetCapsuleComponent()", ("Actor", "RootComponent"): "GetRootComponent()",
    ("Actor", "Instigator"): "GetInstigator()", ("SceneComponent", "RelativeLocation"): "GetRelativeLocation()",
    ("SceneComponent", "RelativeRotation"): "GetRelativeRotation()", ("SceneComponent", "RelativeScale3D"): "GetRelativeScale3D()",
    ("Pawn", "Controller"): "GetController()", ("Pawn", "PlayerState"): "GetPlayerState()",
    ("Controller", "Pawn"): "GetPawn()", ("PlayerController", "PlayerCameraManager"): "PlayerCameraManager",
}

# Core types the export may describe without an include (CoreMinimal has them).
CORE_STRUCTS = {"Vector", "Vector2D", "Vector4", "Rotator", "Quat", "Transform", "LinearColor", "Color", "IntPoint",
                "IntVector", "Box", "Box2D", "Plane", "Matrix", "Guid", "DateTime", "Timespan", "Vector3f", "Rotator3f"}


class Unsupported(Exception):
    def __init__(self, reason: str, node: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.node = node


# ------------------------------------------------------------------------------------------------ names and text

def default_of(t: str) -> str:
    """A value-initialized default in C++ (what a Blueprint pin has when the node did not run)."""
    return "nullptr" if t.endswith("*") else f"{t}{{}}"


def ident(name: str) -> str:
    """A C++ identifier for a Blueprint name: 'Initial Touch Location' -> 'InitialTouchLocation'."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name not in CPP_KEYWORDS:
        return name
    parts = re.split(r"[^0-9A-Za-z_]+", name)
    out = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not out or out[0].isdigit():
        out = "V" + out
    return out + ("_" if out in CPP_KEYWORDS else "")


def short(path: str | None) -> str:
    if not path:
        return ""
    n = re.split(r"[./:]", path)[-1]
    return n[:-2] if n.endswith("_C") else n


def cpp_string(text: str) -> str:
    return 'TEXT("' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "") + '")'


def number(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def fmt_real(v: float, single: bool = False) -> str:
    s = repr(float(v))
    if "e" not in s and "." not in s and "inf" not in s:
        s += ".0"
    return s + ("f" if single else "")


def strip_skel(path: str) -> str:
    """'/Game/A/SKEL_BP_X_C' and '/Game/A/BP_X.BP_X_C' name the same class."""
    return re.sub(r"\.SKEL_", ".", path or "")


# ------------------------------------------------------------------------------------------------ model

@dataclass
class Item:
    bp: str
    cpp: str
    migrated: bool = False
    reason: str = ""
    extra: dict = field(default_factory=dict)


class Export:
    def __init__(self, doc: dict):
        if doc.get("schema") != "lens.bp/1":
            raise SystemExit("not a lens.bp/1 Blueprint export")
        if "native" not in doc:
            raise SystemExit("this export has no 'native' section: export again with the current DwoLens (__dwookit.bat bp export)")
        self.doc = doc
        self.name = doc["name"]
        self.native = doc["native"]
        self.types = self.native.get("types", {})
        self.enums = self.native.get("enums", {})
        self.functions = self.native.get("functions", {})
        self.parent_members = set(self.native.get("parentMembers", []))
        self.generated = strip_skel(doc.get("generatedClass", ""))
        self.graphs = [g for g in doc.get("graphs", [])]
        self.nodes: dict[str, dict] = {}
        self.node_graph: dict[str, dict] = {}
        for g in self.graphs:
            for n in g.get("nodes", []):
                self.nodes[n["id"]] = n
                self.node_graph[n["id"]] = g
                n["_pins"] = {p["name"]: p for p in n.get("pins", [])}

    def is_self_class(self, path: str) -> bool:
        p = strip_skel(path)
        return not p or p == self.generated or short(p) == self.name

    def type_info(self, path: str) -> dict:
        return self.types.get(path) or {}

    def native_parent(self) -> str:
        chain = self.doc.get("parentChain") or [self.doc.get("parent", "")]
        for p in chain:
            if self.type_info(p).get("native"):
                return p
        return chain[-1] if chain else ""

    def chain_names(self) -> list[str]:
        """Short names of the native parent and its ancestors as far as the export knows (for override lookup)."""
        out = [short(p) for p in self.doc.get("parentChain", [])]
        return out


# ------------------------------------------------------------------------------------------------ types

class Types:
    def __init__(self, ex: Export, self_cpp: str):
        self.ex = ex
        self.self_cpp = self_cpp
        self.includes: set[str] = set()       # for the .cpp
        self.header_includes: set[str] = set()
        self.forward: set[str] = set()        # classes forward-declared in the header
        self.modules: set[str] = set()        # engine/plugin modules the code uses (Build.cs dependencies)

    def _class(self, path: str, header: bool) -> str:
        if self.ex.is_self_class(path):
            return self.self_cpp
        t = self.ex.type_info(path)
        if not t:
            raise Unsupported(f"type {short(path)} is not described in the export")
        if not t.get("native"):
            raise Unsupported(f"{short(path)} is a Blueprint class (migrate it first, or keep this code in Blueprint)")
        cpp = t["cpp"]
        if t.get("include"):
            self.includes.add(t["include"])
        if t.get("module"):
            self.modules.add(t["module"])
        if header:
            self.forward.add(cpp)
        return cpp

    def _struct(self, path: str, header: bool) -> str:
        t = self.ex.type_info(path)
        name = short(path)
        if not t:
            if name in CORE_STRUCTS:
                return "F" + name
            raise Unsupported(f"struct {name} is not described in the export")
        if not t.get("native"):
            raise Unsupported(f"{name} is a Blueprint struct (user-defined structs have no C++ type)")
        if t.get("include") and name not in CORE_STRUCTS:
            (self.header_includes if header else self.includes).add(t["include"])
        if t.get("module"):
            self.modules.add(t["module"])
        return t["cpp"]

    def _enum(self, path: str, header: bool, as_property: bool) -> str:
        e = self.ex.enums.get(path)
        if not e:
            raise Unsupported(f"enum {short(path)} is not described in the export")
        if not e.get("native"):
            raise Unsupported(f"{short(path)} is a Blueprint enum (user-defined enums have no C++ type)")
        if e.get("include"):
            (self.header_includes if header else self.includes).add(e["include"])
        cpp = e["cpp"]
        if e.get("form") != "enumClass" and as_property:
            return f"TEnumAsByte<{cpp}>"
        return cpp

    def terminal(self, category: str, sub: str, obj: str, header: bool, as_property: bool) -> str:
        if category == "bool":
            return "bool"
        if category == "int":
            return "int32"
        if category == "int64":
            return "int64"
        if category in ("real", "float", "double"):
            return "float" if sub == "float" else "double"
        if category == "byte":
            return self._enum(obj, header, as_property) if obj else "uint8"
        if category == "enum":
            return self._enum(obj, header, as_property)
        if category == "name":
            return "FName"
        if category == "string":
            return "FString"
        if category == "text":
            return "FText"
        if category == "struct":
            return self._struct(obj, header)
        if category == "object":
            if sub == "self":
                return (f"TObjectPtr<{self.self_cpp}>" if as_property else f"{self.self_cpp}*")
            c = self._class(obj, header) if obj else "UObject"
            return f"TObjectPtr<{c}>" if as_property else f"{c}*"
        if category == "class":
            return f"TSubclassOf<{self._class(obj, header) if obj else 'UObject'}>"
        if category == "softobject":
            return f"TSoftObjectPtr<{self._class(obj, header) if obj else 'UObject'}>"
        if category == "softclass":
            return f"TSoftClassPtr<{self._class(obj, header) if obj else 'UObject'}>"
        if category == "interface":
            return f"TScriptInterface<{self._class(obj, header)}>"
        raise Unsupported(f"pin type {category} has no direct C++ equivalent here")

    def of(self, t: dict, header: bool = False, as_property: bool = False) -> str:
        base = self.terminal(t.get("category", ""), t.get("subCategory", ""), t.get("object", ""), header, as_property)
        c = t.get("container")
        if c == "array":
            return f"TArray<{base}>"
        if c == "set":
            return f"TSet<{base}>"
        if c == "map":
            value = self.terminal(t.get("valueCategory", ""), "", t.get("valueObject", ""), header, as_property)
            return f"TMap<{base}, {value}>"
        return base

    def is_object(self, t: dict) -> bool:
        return t.get("category") in ("object", "class", "interface") and not t.get("container")


# ------------------------------------------------------------------------------------------------ literals

class Literals:
    """Pin defaults and variable defaults as C++ expressions, parsed the way the Blueprint compiler parses them."""

    def __init__(self, types: Types, plan):
        self.types = types
        self.plan = plan
        self.uses_helpers = False

    def pin(self, pin: dict) -> str:
        t = pin.get("type", {})
        if pin.get("defaultObject"):
            return self.object_literal(t, pin["defaultObject"])
        if t.get("category") == "text":
            return self.text(pin.get("defaultText", pin.get("default", "")))
        return self.value(t, pin.get("default", pin.get("autoDefault", "")))

    def object_literal(self, t: dict, path: str) -> str:
        """An asset or class picked in a pin: a hidden UPROPERTY the rebase fills, so the reference stays hard and
        cooked (a path string in C++ would not be)."""
        if path.startswith("/Script/") and t.get("category") == "class":
            return f"{self.types._class(path, False)}::StaticClass()"
        return self.plan.literal_property(t, path)

    def text(self, value: str) -> str:
        v = (value or "").strip()
        if re.match(r'^(NSLOCTEXT|LOCTEXT|INVTEXT|LOCTABLE)\(', v):
            return v
        return "FText::GetEmpty()" if not v else f"FText::FromString({cpp_string(v)})"

    def value(self, t: dict, text: str) -> str:
        cat = t.get("category", "")
        text = "" if text is None else str(text)
        if t.get("container"):
            if text.strip() in ("", "()"):
                return f"{self.types.of(t)}()"
            raise Unsupported("a container literal with values")
        if cat == "bool":
            return "true" if text.strip().lower() == "true" else "false"
        if cat in ("int", "int64"):
            v = number(text) if text.strip() else 0
            if v is None:
                raise Unsupported(f"integer literal '{text}'")
            return str(int(v))
        if cat in ("real", "float", "double"):
            v = number(text) if text.strip() else 0.0
            if v is None:
                raise Unsupported(f"number literal '{text}'")
            return fmt_real(v, t.get("subCategory") == "float")
        if cat == "byte" and not t.get("object"):
            v = number(text) if text.strip() else 0
            return f"(uint8){int(v or 0)}"
        if cat in ("byte", "enum"):
            return self.enum(t.get("object", ""), text)
        if cat == "name":
            return "NAME_None" if text in ("", "None") else f"FName({cpp_string(text)})"
        if cat == "string":
            return f"FString({cpp_string(text)})"
        if cat == "text":
            return self.text(text)
        if cat == "struct":
            return self.struct(t, text)
        if cat in ("object", "interface", "softobject", "softclass", "class"):
            if text.strip() in ("", "None", "nullptr"):
                return "nullptr" if cat in ("object", "class") else f"{self.types.of(t)}()"
            return self.object_literal(t, text.strip().strip('"'))
        raise Unsupported(f"literal of type {cat}")

    def enum(self, path: str, text: str) -> str:
        e = self.types.ex.enums.get(path)
        self.types._enum(path, False, False)
        values = e.get("values", {})
        key = text.split("::")[-1] if text else ""
        if not key:
            first = next(iter(values.values()), None)
            if first is None:
                raise Unsupported(f"empty enum {short(path)}")
            cpp = first
        elif key in values:
            cpp = values[key]
        elif text in values.values():
            cpp = text
        else:
            raise Unsupported(f"enum value '{text}' of {short(path)}")
        if e.get("form") == "regular" or "::" in cpp:
            return cpp
        return f"{e['cpp']}::{cpp}"

    def struct(self, t: dict, text: str) -> str:
        cpp = self.types.of(t)
        name = short(t.get("object", ""))
        s = text.strip()
        if s in ("", "()"):
            return f"{cpp}()" if name not in ("Vector", "Rotator", "Vector2D", "Transform", "Quat", "LinearColor") else {
                "Vector": "FVector::ZeroVector", "Rotator": "FRotator::ZeroRotator", "Vector2D": "FVector2D::ZeroVector",
                "Transform": "FTransform::Identity", "Quat": "FQuat::Identity", "LinearColor": "FLinearColor(ForceInit)"}[name]
        nums = [number(x) for x in s.split(",")] if "=" not in s else []
        if name in ("Vector", "Vector3f", "Rotator") and len(nums) == 3 and all(v is not None for v in nums):
            single = name == "Vector3f"
            return f"{cpp}({', '.join(fmt_real(v, single) for v in nums)})"
        fields = dict(re.findall(r"(\w+)=([-+0-9.eE]+)", s)) if s.startswith("(") else {}
        order = {"Vector": "XYZ", "Vector2D": "XY", "Rotator": ["Pitch", "Yaw", "Roll"], "LinearColor": "RGBA", "Vector4": "XYZW"}
        if name in order and fields and set(fields) == set(order[name]):
            return f"{cpp}({', '.join(fmt_real(float(fields[k])) for k in order[name])})"
        # anything else: the engine's own parser at startup, exactly as the Blueprint compiler does it
        self.uses_helpers = True
        fn = {"Vector": "DwoNative::ParseVector", "Rotator": "DwoNative::ParseRotator", "Transform": "DwoNative::ParseTransform"}.get(name)
        if fn:
            return f"{fn}({cpp_string(s)})"
        return f"DwoNative::ImportStruct<{cpp}>({cpp_string(s)})"


# ------------------------------------------------------------------------------------------------ function translation

class Fn:
    """Translates one function graph or one event (its exec flow) into C++ statements."""

    def __init__(self, plan, graph: dict, root: dict, mode: str, params: dict[str, str]):
        self.plan = plan
        self.ex: Export = plan.ex
        self.types: Types = plan.types
        self.lit: Literals = plan.literals
        self.graph = graph
        self.root = root
        self.mode = mode                  # "function" | "event"
        self.params = params              # (node id, pin name) of the entry/event outputs -> C++ expression
        self.lines: list[str] = []
        self.locals: dict[str, tuple[str, str]] = {}   # name -> (type, init)
        self.members: dict[str, tuple[str, bool]] = {}  # event-graph temporaries -> (type, is UPROPERTY)
        self.incoming = self._incoming()
        self.labels: dict[str, str] = {}
        self.emitted: set[str] = set()
        self.used_nodes: set[str] = {root["id"]}
        self.loop_vars: dict[tuple[str, str], str] = {}
        self.depth = 1
        self.result_outs: list[tuple[str, str]] = []    # (pin name, C++ out param)
        self.return_type = "void"
        self.pure_done: set[str] = set()     # pure nodes with out params already run for the current statement
        self.guarded: str | None = None      # target local the current call statement must check

    # -- graph helpers
    def _incoming(self) -> dict[str, int]:
        count: dict[str, int] = {}
        for n in self.graph["nodes"]:
            for p in n.get("pins", []):
                if p["dir"] == "out" and p["type"].get("category") == "exec":
                    for l in p.get("links", []):
                        count[l["node"]] = count.get(l["node"], 0) + 1
        return count

    def node(self, node_id: str) -> dict:
        n = self.ex.nodes.get(node_id)
        if not n:
            raise Unsupported("a link points to a node the export does not have")
        return n

    def say(self, text: str):
        self.lines.append("\t" * self.depth + text)

    def exec_target(self, node: dict, pin_name: str) -> dict | None:
        p = node["_pins"].get(pin_name)
        if not p or not p.get("links"):
            return None
        if len(p["links"]) > 1:
            raise Unsupported("one exec output wired to several inputs", node)
        n = self.node(p["links"][0]["node"])
        while n["class"] == "K2Node_Knot":       # reroute: part of the flow it carries
            self.used_nodes.add(n["id"])
            n = self.node(self.exec_out_links(n, "OutputPin"))
        return n

    def exec_out_links(self, node, pin):
        p = node["_pins"].get(pin)
        if not p or not p.get("links"):
            raise Unsupported("a reroute node that leads nowhere", node)
        return p["links"][0]["node"]

    # -- data
    def input(self, node: dict, pin_name: str) -> str:
        p = node["_pins"].get(pin_name)
        if p is None:
            raise Unsupported(f"pin '{pin_name}' missing", node)
        if p.get("split"):
            return self.make_split(node, p)
        links = p.get("links") or []
        if links:
            src = self.node(links[0]["node"])
            return self.value(src, links[0]["pin"])
        if pin_name == "self" or p.get("type", {}).get("subCategory") == "self":
            return "this"
        return self.lit.pin(p)

    def make_split(self, node: dict, parent: dict) -> str:
        t = self.types.of(parent["type"])
        parts = []
        for sub in node.get("pins", []):
            if sub.get("splitFrom") == parent["name"] and sub["dir"] == "in":
                member = sub["name"][len(parent["name"]) + 1:]
                parts.append(f"V.{member} = {self.input(node, sub['name'])};")
        return f"[&]() {{ {t} V; {' '.join(parts)} return V; }}()"

    def value(self, node: dict, pin_name: str) -> str:
        """Value of an output pin."""
        self.used_nodes.add(node["id"])
        pin = node["_pins"].get(pin_name)
        if pin is None:
            raise Unsupported(f"output pin '{pin_name}' missing", node)
        if pin.get("splitFrom"):
            parent = pin["splitFrom"]
            return f"({self.value(node, parent)}).{pin_name[len(parent) + 1:]}"
        key = (node["id"], pin_name)
        if key in self.params:
            return self.params[key]
        if key in self.loop_vars:
            return self.loop_vars[key]
        cls, kind = node["class"], node.get("kind")
        if node.get("disabled"):
            raise Unsupported("a disabled node feeds data", node)
        if cls == "K2Node_Knot":
            return self.input(node, "InputPin")
        if cls == "K2Node_Self":
            return "this"
        if cls == "K2Node_VariableGet":
            return self.var_get(node)
        if cls == "K2Node_VariableSet" and pin["dir"] == "out" and pin["type"].get("category") != "exec":
            return self.plan.local_names.get(node["member"]) or self.plan.var_cpp(node["member"], node)
        if node.get("pure") and cls in ("K2Node_CallFunction", "K2Node_CommutativeAssociativeBinaryOperator", "K2Node_PromotableOperator"):
            return self.call_expr(node, pin_name, pure=True)
        if cls == "K2Node_DynamicCast" and node.get("pure"):
            target = self.types._class(node["member"], False)
            if pin_name == "bSuccess":
                return f"(Cast<{target}>({self.input(node, 'Object')}) != nullptr)"
            return f"Cast<{target}>({self.input(node, 'Object')})"
        if cls == "K2Node_BreakStruct":
            return f"({self.input(node, self.struct_input(node))}).{pin_name}"
        if cls == "K2Node_MakeStruct":
            t = self.types.of(pin["type"])
            sets = [f"V.{p['name']} = {self.input(node, p['name'])};" for p in node["pins"] if p["dir"] == "in"]
            return f"[&]() {{ {t} V; {' '.join(sets)} return V; }}()"
        if cls == "K2Node_Select":
            return self.select(node)
        if cls == "K2Node_FormatText":
            return self.format_text(node)
        if cls == "K2Node_GetArrayItem":
            return f"({self.input(node, 'Array')})[{self.input(node, 'Dimension 1') if 'Dimension 1' in node['_pins'] else self.input(node, 'Index')}]"
        # an impure node's output: the temporary it wrote when it ran
        name = self.temp_name(node, pin_name)
        if name not in self.locals and name not in self.members:
            raise Unsupported("reads the output of a node that does not run before it", node)
        return name

    def struct_input(self, node):
        return next(p["name"] for p in node["pins"] if p["dir"] == "in")

    def temp_name(self, node: dict, pin_name: str) -> str:
        return f"Tmp_{node['id'][:8]}_{ident(pin_name)}"

    def declare_temp(self, node: dict, pin: dict) -> str:
        name = self.temp_name(node, pin["name"])
        t = self.types.of(pin["type"], header=self.mode == "event", as_property=self.mode == "event" and self.types.is_object(pin["type"]))
        if self.mode == "event":
            self.members[name] = (t, self.types.is_object(pin["type"]))
        else:
            self.locals[name] = (self.types.of(pin["type"]), "")
        return name

    def var_get(self, node: dict) -> str:
        owner = node.get("memberOwner", "")
        target = node["_pins"].get("self")
        local = self.plan.local_names.get(node.get("member", ""))
        if local and not (target and target.get("links")):
            return local
        if self.ex.is_self_class(owner) and not (target and target.get("links")):
            return self.plan.var_cpp(node["member"], node)
        # another object's variable
        obj = self.input(node, "self") if target else "this"
        if not self.ex.type_info(owner).get("native") and not self.ex.is_self_class(owner):
            raise Unsupported(f"reads a variable of the Blueprint class {short(owner)}", node)
        getter = PRIVATE_GETTERS.get((short(owner), node["member"]))
        if node.get("memberAccess") in ("protected", "private") and getter:
            if obj == "this":
                return getter
            pin = next((p for p in node["pins"] if p["dir"] == "out"), None)
            default = f"{self.types.of(pin['type'])}{{}}" if pin else "nullptr"
            t = self.types.of(pin["type"]) if pin else "UObject*"
            ot = self.types.of(target["type"]) if target and target["type"].get("object") else "UObject*"
            return f"[&]() -> {t} {{ {ot} O = {obj}; if (O) {{ return O->{getter}; }} return {default_of(t)}; }}()"
        if node.get("memberAccess") == "private" or (node.get("memberAccess") == "protected" and (obj != "this" or not self.plan.inherits(owner))):
            raise Unsupported(f"{node['member']} is {node.get('memberAccess')} in C++", node)
        if node.get("memberAccessor"):
            raise Unsupported(f"{node['member']} has a Blueprint getter/setter", node)
        member = node.get("memberName") or node["member"]
        if obj == "this":
            return member
        pin = next((p for p in node["pins"] if p["dir"] == "out"), None)
        default = f"{self.types.of(pin['type'])}{{}}" if pin else "{}"
        t = self.types.of(pin["type"]) if pin else "UObject*"
        ot = self.types.of(target["type"]) if target and target["type"].get("object") else "UObject*"
        return f"[&]() -> {t} {{ {ot} O = {obj}; if (O) {{ return O->{member}; }} return {default_of(t)}; }}()"

    def native_fn(self, node: dict) -> dict:
        key = f"{node.get('memberOwner', '')}:{node.get('member', '')}"
        f = self.ex.functions.get(key)
        if not f:
            if self.ex.is_self_class(node.get("memberOwner", "")):
                return {}
            raise Unsupported(f"no signature for {short(node.get('memberOwner'))}.{node.get('member')}", node)
        if f.get("customThunk"):
            raise Unsupported(f"{node['member']} is a Blueprint-only (CustomThunk) function", node)
        if f.get("internalOnly"):
            raise Unsupported(f"{node['member']} is internal to Blueprint (BlueprintInternalUseOnly)", node)
        if f.get("latent"):
            raise Unsupported(f"{node['member']} is latent (Delay-like); latent actions stay in Blueprint", node)
        if not f.get("native"):
            raise Unsupported(f"{short(node.get('memberOwner'))}.{node['member']} is a Blueprint function", node)
        return f

    def call_expr(self, node: dict, want_pin: str | None, pure: bool) -> str:
        """The call; for a pure call with out params, the call is run into temporaries first (pre-statements)."""
        owner = node.get("memberOwner", "")
        if self.ex.is_self_class(owner):
            return self.self_call(node, want_pin, pure)
        f = self.native_fn(node)
        owner_cpp = self.types._class(f["owner"], False)
        flags = set(f.get("flags", []))
        args, outs = [], {}
        ret = None
        for prm in f["params"]:
            name = prm["name"]
            if prm.get("return"):
                ret = prm
                continue
            if prm.get("out"):
                pin = node["_pins"].get(name)
                temp = f"Out_{node['id'][:8]}_{ident(name)}"
                self.locals.setdefault(temp, (self._param_value_type(prm), ""))
                outs[name] = temp
                args.append(temp)
                continue
            if f.get("worldContext") == name:
                args.append("this")
                continue
            pin = node["_pins"].get(name)
            if pin is None:
                if prm.get("cppDefault") is not None:
                    break                     # trailing C++ defaults: stop, the rest take their defaults
                raise Unsupported(f"parameter {name} has no pin", node)
            if prm.get("ref") and not prm.get("const"):
                args.append(self.lvalue(node, name))
            else:
                args.append(self.input(node, name))
        guard = None
        if "Static" in flags:
            call = f"{owner_cpp}::{f['name']}({', '.join(args)})"
        else:
            target_pin = node["_pins"].get("self")
            if target_pin and target_pin.get("links"):
                obj = self.input(node, "self")
                if f.get("access") != "public":
                    raise Unsupported(f"{f['name']} is {f.get('access')} in C++", node)
                if obj != "this" and pure and not outs:
                    # inline, so a loop condition re-reads the target each time, as the Blueprint does
                    t = self._param_value_type(ret) if ret else "void"
                    pin = node["_pins"]["self"]
                    ot = self.types.of(pin["type"]) if pin["type"].get("object") else "UObject*"
                    body = f"return O->{f['name']}({', '.join(args)});" if ret else f"O->{f['name']}({', '.join(args)});"
                    tail = f" return {default_of(t)};" if ret else ""
                    return f"[&]() -> {t} {{ {ot} O = {obj}; if (O) {{ {body} }}{tail} }}()"
                if obj != "this":
                    guard = self.guard_target(node, obj)
                    obj = guard
                call = f"{obj}->{f['name']}({', '.join(args)})"
            else:
                if not self.plan.inherits(f["owner"]):
                    raise Unsupported(f"{f['name']} is called on self but self is not a {short(f['owner'])}", node)
                if f.get("access") == "private":
                    raise Unsupported(f"{f['name']} is private in C++", node)
                call = f"{f['name']}({', '.join(args)})"
        if guard and not pure:
            self.guarded = guard                     # the statement wraps the call in if (Target)
        if outs:
            if not pure:
                return call, outs, ret
            # a pure call with out params: run it once per statement, read its outputs
            temp = f"Ret_{node['id'][:8]}"
            if node["id"] not in self.pure_done:
                self.pure_done.add(node["id"])
                for o in outs.values():
                    self.say(f"{o} = {{}};") if guard else None
                stmt = f"{temp} = {call};" if ret else f"{call};"
                if ret:
                    self.locals.setdefault(temp, (self._param_value_type(ret), ""))
                self.say(f"if ({guard}) {{ {stmt} }}" if guard else stmt)
            if want_pin == "ReturnValue":
                return temp
            return outs.get(want_pin) or self._fail_pin(node, want_pin)
        if pure:
            if want_pin not in (None, "ReturnValue"):
                return self._fail_pin(node, want_pin)
            if guard:
                default = default_of(self._param_value_type(ret)) if ret else "void()"
                return f"({guard} ? {call} : {default})"
            return call
        return call, outs, ret

    def guard_target(self, node: dict, obj: str) -> str:
        """A target object other than self: evaluated once into a local, so the null check and the call see the
        same object (the Blueprint evaluates the target pin once)."""
        name = f"Target_{node['id'][:8]}"
        pin = node["_pins"]["self"]
        t = self.types.of(pin["type"]) if pin["type"].get("object") else "UObject*"
        if t.startswith("TObjectPtr<"):
            t = t[len("TObjectPtr<"):-1] + "*"
        self.locals.setdefault(name, (t, "nullptr"))
        self.say(f"{name} = {obj};")
        return name

    def _fail_pin(self, node, pin):
        raise Unsupported(f"output {pin} of {node.get('member')}", node)

    def _param_value_type(self, prm: dict) -> str:
        t = prm["cpp"].replace("const ", "").replace("&", "").strip()
        return t

    def lvalue(self, node: dict, pin_name: str) -> str:
        p = node["_pins"][pin_name]
        links = p.get("links") or []
        if not links:
            raise Unsupported(f"by-reference pin {pin_name} is not connected to a variable", node)
        src = self.node(links[0]["node"])
        if src["class"] == "K2Node_VariableGet":
            return self.var_get(src)
        raise Unsupported(f"by-reference pin {pin_name} must come from a variable", node)

    def self_call(self, node: dict, want_pin, pure: bool):
        fn = self.plan.function_item(node["member"])
        if fn is None or not fn.migrated:
            raise Unsupported(f"calls the Blueprint function '{node['member']}' that stays in Blueprint", node)
        sig = fn.extra
        args = []
        out_temps = {}
        for pin_name, cpp_name, is_out, t in sig["params"]:
            if is_out:
                temp = f"Out_{node['id'][:8]}_{cpp_name}"
                self.locals.setdefault(temp, (t, ""))
                out_temps[pin_name] = temp
                args.append(temp)
            else:
                args.append(self.input(node, pin_name))
        target = node["_pins"].get("self")
        prefix = f"{self.input(node, 'self')}->" if target and target.get("links") else ""
        call = f"{prefix}{fn.cpp}({', '.join(args)})"
        ret = {"cpp": sig["return"], "name": "ReturnValue"} if sig.get("return") else None
        if pure:
            temp = f"Ret_{node['id'][:8]}"
            if ret:
                self.locals.setdefault(temp, (sig["return"], ""))
                self.say(f"{temp} = {call};")
            else:
                self.say(f"{call};")
            if want_pin == "ReturnValue" and ret:
                return temp
            return out_temps.get(want_pin) or self._fail_pin(node, want_pin)
        return call, out_temps, ret

    def select(self, node: dict) -> str:
        idx = self.input(node, "Index")
        options = [p for p in node["pins"] if p["dir"] == "in" and p["name"] not in ("Index",) and p["type"].get("category") != "exec"]
        if len(options) == 2 and node["_pins"]["Index"]["type"].get("category") == "bool":
            a, b = options
            return f"(({idx}) ? {self.input(node, a['name'])} : {self.input(node, b['name'])})"
        parts = [f"(({idx}) == {i}) ? {self.input(node, o['name'])} :" for i, o in enumerate(options[:-1])]
        return "(" + " ".join(parts) + f" {self.input(node, options[-1]['name'])})"

    def format_text(self, node: dict) -> str:
        fmt = self.lit.text(node["_pins"]["Format"].get("defaultText", node["_pins"]["Format"].get("default", "")))
        if node["_pins"]["Format"].get("links"):
            fmt = self.input(node, "Format")
        args = [p for p in node["pins"] if p["dir"] == "in" and p["name"] != "Format"]
        sets = " ".join(f"A.Add(TEXT(\"{p['name']}\"), FFormatArgumentValue({self.input(node, p['name'])}));" for p in args)
        return f"[&]() {{ FFormatNamedArguments A; {sets} return FText::Format({fmt}, A); }}()"

    # -- statements
    def flow(self, node: dict | None):
        """Emits node and everything its exec outputs lead to."""
        while node is not None:
            if node.get("disabled"):
                raise Unsupported("a disabled node is in the execution path", node)
            nid = node["id"]
            if nid in self.emitted:
                if nid not in self.labels:
                    raise Unsupported("execution loops back into a running block (unstructured loop)", node)
                self.say(f"goto {self.labels[nid]};")
                return
            self.emitted.add(nid)
            self.used_nodes.add(nid)
            if self.incoming.get(nid, 0) > 1:
                label = f"L_{nid[:8]}"
                self.labels[nid] = label
                self.lines.append(f"{label}:")
            self.pure_done = set()
            self.guarded = None
            node = self.statement(node)

    def block(self, node: dict | None):
        self.say("{")
        self.depth += 1
        self.flow(node)
        self.depth -= 1
        self.say("}")

    def statement(self, node: dict) -> dict | None:
        cls, kind = node["class"], node.get("kind")
        if cls in ("K2Node_CallFunction", "K2Node_CallParentFunction", "K2Node_CommutativeAssociativeBinaryOperator", "K2Node_PromotableOperator"):
            if node.get("latent"):
                raise Unsupported(f"{node.get('member')} is latent; latent actions stay in Blueprint", node)
            if cls == "K2Node_CallParentFunction":
                call = self.parent_call(node)
                self.say(call + ";")
                return self.exec_target(node, "then")
            self.guarded = None
            call, outs, ret = self.call_expr(node, None, pure=False)
            guard = self.guarded
            ret_pin = node["_pins"].get("ReturnValue")
            body = []
            if ret and ret_pin and ret_pin.get("links"):
                temp = self.declare_temp(node, ret_pin)
                if guard:
                    self.say(f"{temp} = {{}};")      # a skipped call leaves the default, as in the Blueprint
                body.append(f"{temp} = {call};")
            else:
                body.append(f"{call};")
            for pin_name, temp in outs.items():
                pin = node["_pins"].get(pin_name)
                if pin and pin.get("links"):
                    t = self.declare_temp(node, pin)
                    if guard:
                        self.say(f"{t} = {{}};")
                    body.append(f"{t} = {temp};")
            if guard:
                self.say(f"if ({guard})")
                self.say("{")
                for b in body:
                    self.say("\t" + b)
                self.say("}")
            else:
                for b in body:
                    self.say(b)
            return self.exec_target(node, "then")
        if cls == "K2Node_VariableSet":
            target = node["_pins"].get("self")
            if target and target.get("links") and not self.ex.is_self_class(node.get("memberOwner", "")):
                raise Unsupported("sets another object's Blueprint variable", node)
            var = self.plan.local_names.get(node["member"]) or self.plan.var_cpp(node["member"], node)
            value_pin = next(p for p in node["pins"] if p["dir"] == "in" and p["type"].get("category") != "exec" and p["name"] != "self")
            self.say(f"{var} = {self.input(node, value_pin['name'])};")
            notify = self.plan.rep_notify(node["member"])
            if notify:
                self.say(f"{notify}();")
            return self.exec_target(node, "then")
        if cls == "K2Node_IfThenElse":
            self.say(f"if ({self.input(node, 'Condition')})")
            self.block(self.exec_target(node, "then"))
            else_node = self.exec_target(node, "else")
            if else_node:
                self.say("else")
                self.block(else_node)
            return None
        if cls == "K2Node_ExecutionSequence":
            outs = [p["name"] for p in node["pins"] if p["dir"] == "out" and p["type"].get("category") == "exec"]
            for name in outs:
                target = self.exec_target(node, name)
                if target:
                    self.block(target)
            return None
        if cls in ("K2Node_SwitchInteger", "K2Node_SwitchEnum", "K2Node_SwitchString", "K2Node_SwitchName"):
            return self.switch(node)
        if cls == "K2Node_DynamicCast":
            target = self.types._class(node["member"], False)
            as_pin = next(p for p in node["pins"] if p["dir"] == "out" and p["type"].get("category") == "object")
            temp = self.declare_temp(node, as_pin)
            self.say(f"{temp} = Cast<{target}>({self.input(node, 'Object')});")
            self.say(f"if ({temp})")
            self.block(self.exec_target(node, "then"))
            fail = self.exec_target(node, "CastFailed")
            if fail:
                self.say("else")
                self.block(fail)
            return None
        if cls == "K2Node_FunctionResult":
            for pin in node["pins"]:
                if pin["dir"] == "in" and pin["type"].get("category") != "exec":
                    if pin["name"] == "ReturnValue" and self.return_type != "void":
                        self.say(f"return {self.input(node, pin['name'])};")
                        return None
                    self.say(f"{ident(pin['name'])} = {self.input(node, pin['name'])};")
            self.say("return;" if self.return_type == "void" else "return {};")
            return None
        if cls == "K2Node_MacroInstance":
            return self.macro(node)
        if cls == "K2Node_CallDelegate":
            d = self.plan.dispatcher(node["member"])
            args = [self.input(node, p["name"]) for p in node["pins"] if p["dir"] == "in" and p["type"].get("category") != "exec" and p["name"] != "self"]
            target = node["_pins"].get("self")
            obj = f"{self.input(node, 'self')}->" if target and target.get("links") else ""
            self.say(f"{obj}{d}.Broadcast({', '.join(args)});")
            return self.exec_target(node, "then")
        if cls == "K2Node_Knot":
            return self.exec_target(node, "OutputPin")
        if cls == "K2Node_VariableGet":            # validated get: branches on IsValid
            self.say(f"if (IsValid({self.var_get(node)}))")
            self.block(self.exec_target(node, "then"))
            other = self.exec_target(node, "else")
            if other:
                self.say("else")
                self.block(other)
            return None
        raise Unsupported(f"node {cls.replace('K2Node_', '')} ({node.get('title', '').splitlines()[0]}) has no C++ translation yet", node)

    def parent_call(self, node):
        f = self.native_fn(node)
        args = [self.input(node, p["name"]) for p in f["params"] if not p.get("return") and not p.get("out")]
        name = f["name"] + ("_Implementation" if "BlueprintEvent" in f.get("flags", []) and "Native" in f.get("flags", []) else "")
        return f"Super::{name}({', '.join(args)})"

    def switch(self, node: dict):
        sel = self.input(node, "Selection")
        cases = [p for p in node["pins"] if p["dir"] == "out" and p["type"].get("category") == "exec" and p["name"] != "Default"]
        cls = node["class"]
        if cls in ("K2Node_SwitchInteger", "K2Node_SwitchEnum"):
            self.say(f"switch ({sel})")
            self.say("{")
            for c in cases:
                if cls == "K2Node_SwitchEnum":
                    enum_path = node["_pins"]["Selection"]["type"].get("object", "")
                    label = self.lit.enum(enum_path, c["name"])
                else:
                    label = str(int(number(c["name"]) or 0))
                self.say(f"case {label}:")
                self.depth += 1
                self.block(self.exec_target(node, c["name"]))
                self.say("break;")
                self.depth -= 1
            default = self.exec_target(node, "Default")
            self.say("default:")
            self.depth += 1
            if default:
                self.block(default)
            self.say("break;")
            self.depth -= 1
            self.say("}")
            return None
        # string / name: compare in order
        first = True
        for c in cases:
            lit = cpp_string(c["name"])
            cmp = f"({sel}).Equals({lit}, ESearchCase::IgnoreCase)" if cls == "K2Node_SwitchString" else f"({sel}) == FName({lit})"
            self.say(("if" if first else "else if") + f" ({cmp})")
            self.block(self.exec_target(node, c["name"]))
            first = False
        default = self.exec_target(node, "Default")
        if default:
            self.say("else")
            self.block(default)
        return None

    def macro(self, node: dict):
        name = node.get("member") or ""
        owner = node.get("memberOwner") or ""
        if not owner.startswith(STANDARD_MACROS):
            raise Unsupported(f"Blueprint macro '{name}' (only standard macros translate)", node)
        pins = node["_pins"]
        if name == "ForLoop":
            var = f"Index_{node['id'][:8]}"
            self.loop_vars[(node["id"], "Index")] = var
            self.say(f"for (int32 {var} = {self.input(node, 'FirstIndex')}; {var} <= {self.input(node, 'LastIndex')}; ++{var})")
            self.block(self.exec_target(node, "LoopBody"))
            return self.exec_target(node, "Completed")
        if name in ("ForEachLoop", "ForEachLoopWithBreak"):
            if name == "ForEachLoopWithBreak":
                raise Unsupported("ForEachLoopWithBreak (Break pin) is not translated yet", node)
            var = f"Index_{node['id'][:8]}"
            arr = self.input(node, "Array")
            self.loop_vars[(node["id"], "Array Index")] = var
            self.loop_vars[(node["id"], "Array Element")] = f"({arr})[{var}]"
            self.say(f"for (int32 {var} = 0; {var} < ({arr}).Num(); ++{var})")
            self.block(self.exec_target(node, "LoopBody"))
            return self.exec_target(node, "Completed")
        if name == "WhileLoop":
            self.say(f"while ({self.input(node, 'Condition')})")
            self.block(self.exec_target(node, "LoopBody"))
            return self.exec_target(node, "Completed")
        if name == "IsValid":
            self.say(f"if (IsValid({self.input(node, 'InputObject')}))")
            self.block(self.exec_target(node, "Is Valid"))
            other = self.exec_target(node, "Is Not Valid")
            if other:
                self.say("else")
                self.block(other)
            return None
        if name == "FlipFlop":
            state = self.plan.state_member(node, "FlipFlop", "bool", "true")
            self.loop_vars[(node["id"], "IsA")] = state
            self.say(f"{state} = !{state};")
            self.say(f"if ({state})")
            self.block(self.exec_target(node, "A"))
            b = self.exec_target(node, "B")
            if b:
                self.say("else")
                self.block(b)
            return None
        if name == "DoOnce":
            if pins.get("Reset") and pins["Reset"].get("links"):
                raise Unsupported("DoOnce with a Reset input is not translated yet", node)
            state = self.plan.state_member(node, "DoOnceDone", "bool", "false")
            start_closed = (pins.get("Start Closed") or {}).get("default", "false").lower() == "true"
            if start_closed:
                raise Unsupported("DoOnce that starts closed", node)
            self.say(f"if (!{state})")
            self.say("{")
            self.depth += 1
            self.say(f"{state} = true;")
            self.flow(self.exec_target(node, "Completed"))
            self.depth -= 1
            self.say("}")
            return None
        raise Unsupported(f"standard macro '{name}' is not translated yet", node)

    def translate(self, start: dict | None):
        self.flow(start)
        return self


# ------------------------------------------------------------------------------------------------ plan

class Plan:
    def __init__(self, doc: dict, class_name: str | None = None, module: str = "Game", api: str | None = None):
        self.ex = Export(doc)
        ex = self.ex
        parent_path = ex.native_parent()
        self.parent = ex.type_info(parent_path)
        if not self.parent.get("native"):
            raise SystemExit(f"{ex.name}: its native parent class is not in the export")
        self.parent_path = parent_path
        self.parent_cpp = self.parent["cpp"]
        prefix = self.parent_cpp[0]
        base = re.sub(r"^(BP|B|WBP|WB|ABP|AB|BTT|BTS|BTD|BPI|UI|W)_", "", ex.name)
        self.cpp = class_name or f"{prefix}{ident(base)}Base"
        self.module = module
        self.api = api or f"{re.sub(r'[^A-Za-z0-9]', '_', module).upper()}_API"
        self.types = Types(ex, self.cpp)
        self.literals = Literals(self.types, self)
        self.kind = self._kind()
        self.vars: dict[str, Item] = {}
        self.components: dict[str, Item] = {}
        self.widgets: dict[str, Item] = {}
        self.funcs: dict[str, Item] = {}
        self.events: list[Item] = []
        self.dispatchers: dict[str, Item] = {}
        self.literal_props: dict[str, dict] = {}
        self.state_members: dict[str, tuple[str, str]] = {}
        self.event_members: dict[str, tuple[str, bool]] = {}
        self.bodies: dict[str, Fn] = {}
        self.notes: list[str] = []
        self.local_names: dict[str, str] = {}

    def _kind(self) -> str:
        chain = " ".join(short(p) for p in self.ex.doc.get("parentChain", []))
        cls = self.ex.doc.get("assetClass", "")
        if cls == "WidgetBlueprint":
            return "widget"
        if cls == "AnimBlueprint":
            return "anim"
        if self.parent.get("actor"):
            return "actor"
        if self.parent.get("component"):
            return "component"
        return "object"

    # -- lookups used while translating
    def inherits(self, owner_path: str) -> bool:
        """Self (the generated class) is a kind of owner: owner is the native parent or one of its ancestors."""
        if self.ex.is_self_class(owner_path):
            return True
        names = {short(p) for p in self.ex.doc.get("parentChain", [])}
        names |= set(self.parent_ancestors())
        return short(owner_path) in names

    def parent_ancestors(self) -> list[str]:
        known = {"Character": ["Pawn", "Actor", "Object"], "Pawn": ["Actor", "Object"], "Actor": ["Object"],
                 "PlayerController": ["Controller", "Actor", "Object"], "Controller": ["Actor", "Object"],
                 "AIController": ["Controller", "Actor", "Object"], "GameModeBase": ["Info", "Actor", "Object"],
                 "GameMode": ["GameModeBase", "Info", "Actor", "Object"], "GameStateBase": ["Info", "Actor", "Object"],
                 "PlayerState": ["Info", "Actor", "Object"], "HUD": ["Actor", "Object"],
                 "UserWidget": ["Widget", "Visual", "Object"], "AnimInstance": ["Object"],
                 "ActorComponent": ["Object"], "SceneComponent": ["ActorComponent", "Object"],
                 "PrimitiveComponent": ["SceneComponent", "ActorComponent", "Object"]}
        out = []
        for p in [short(x) for x in self.ex.doc.get("parentChain", [])]:
            out += [p] + known.get(p, [])
        # the native parent may be a project class: its ancestors come from the export's types (nativeParent)
        return out

    def var_cpp(self, bp_name: str, node=None) -> str:
        v = self.vars.get(bp_name)
        if v and v.migrated:
            return v.cpp
        c = self.components.get(bp_name)
        if c and c.migrated:
            return c.cpp
        w = self.widgets.get(bp_name)
        if w and w.migrated:
            return w.cpp
        if v is None and c is None and w is None:
            # a variable the native parent declares (Controller, RootComponent ...)
            return bp_name
        raise Unsupported(f"uses the variable '{bp_name}' that stays in Blueprint ({(v or c or w).reason})", node)

    def rep_notify(self, bp_name: str) -> str | None:
        v = self.vars.get(bp_name)
        return v.extra.get("notifyCpp") if v else None

    def function_item(self, bp_name: str) -> Item | None:
        return self.funcs.get(bp_name)

    def dispatcher(self, bp_name: str) -> str:
        d = self.dispatchers.get(bp_name)
        if not d or not d.migrated:
            raise Unsupported(f"event dispatcher '{bp_name}' stays in Blueprint")
        return d.cpp

    def literal_property(self, t: dict, path: str) -> str:
        key = path
        if key not in self.literal_props:
            cat = t.get("category")
            obj = t.get("object", "")
            cls = self.types._class(obj, True) if obj else "UObject"
            cpp_type = f"TSubclassOf<{cls}>" if cat == "class" else f"TObjectPtr<{cls}>"
            name = f"NativeLiteral_{len(self.literal_props)}"
            self.literal_props[key] = {"cpp": name, "type": cpp_type, "path": path}
        return self.literal_props[key]["cpp"]

    def state_member(self, node: dict, what: str, t: str, init: str) -> str:
        name = f"b{what}_{node['id'][:8]}"
        self.state_members[name] = (t, init)
        return name

    # -- planning
    def build(self):
        self.plan_variables()
        self.plan_components()
        self.plan_widgets()
        self.plan_dispatchers()
        self.plan_functions()
        # fixed point: a function may only be migrated if everything it uses is
        for _ in range(8):
            changed = self.translate_all()
            if not changed:
                break
        return self

    def plan_variables(self):
        for v in self.ex.doc.get("variables", []):
            item = Item(v["name"], ident(v["name"]))
            if item.cpp in self.ex.parent_members:
                item.reason = f"the native parent already has a member named {item.cpp}"
                self.vars[v["name"]] = item
                continue
            try:
                t = self.types.of(v["type"], header=True, as_property=True)
                item.extra["type"] = t
                item.migrated = True
                flags = set(v.get("flags", []))
                if v.get("repNotify"):
                    item.extra["repNotify"] = v["repNotify"]
                item.extra.update(flags=sorted(flags), category=v.get("category", ""), tooltip=v.get("tooltip", ""),
                                  default=v.get("default"), pinType=v["type"])
            except Unsupported as e:
                item.reason = e.reason
            self.vars[v["name"]] = item

    def plan_components(self):
        for c in self.ex.doc.get("components", []):
            item = Item(c["name"], ident(c["name"]), extra=dict(c))
            path = c.get("classPath", "")
            t = self.ex.type_info(path)
            if c["name"] in self.ex.parent_members:
                item.reason = f"the native parent already has a member named {c['name']}"
            elif not t.get("native"):
                item.reason = f"component class {c.get('class')} is not native"
            elif item.cpp != c["name"]:
                item.reason = "component name is not a C++ identifier (rename it in the Blueprint first)"
            else:
                item.migrated = True
                item.extra["cppClass"] = t["cpp"]
                if t.get("include"):
                    self.types.includes.add(t["include"])
                self.types.forward.add(t["cpp"])
            self.components[c["name"]] = item
        # a child of a component that stays in Blueprint cannot be native either
        for item in self.components.values():
            parent = item.extra.get("parent")
            if item.migrated and parent in self.components and not self.components[parent].migrated:
                item.migrated, item.reason = False, f"its parent {parent} stays in Blueprint"

    def plan_widgets(self):
        if self.kind != "widget":
            return
        for w in self.ex.doc.get("widgets", []):
            if not w.get("variable"):
                continue
            item = Item(w["name"], w["name"], extra=dict(w))
            t = next((v for k, v in self.ex.types.items() if short(k) == w["class"] and v.get("native")), None)
            if w["name"] in self.ex.parent_members:
                item.reason = "the native parent already declares it (BindWidget there)"
            elif not t:
                item.reason = f"widget class {w['class']} is not described or not native"
            elif ident(w["name"]) != w["name"]:
                item.reason = "widget name is not a C++ identifier (BindWidget needs the same name)"
            else:
                item.migrated = True
                item.extra["cppClass"] = t["cpp"]
                if t.get("include"):
                    self.types.includes.add(t["include"])
                self.types.forward.add(t["cpp"])
            self.widgets[w["name"]] = item

    def plan_dispatchers(self):
        for g in self.ex.graphs:
            if g["kind"] != "dispatcher":
                continue
            item = Item(g["name"], ident(g["name"]))
            entry = next((n for n in g["nodes"] if n["class"] == "K2Node_FunctionEntry"), None)
            try:
                params = []
                for p in (entry or {}).get("pins", []):
                    if p["dir"] == "out" and p["type"].get("category") != "exec":
                        params.append((self.types.of(p["type"], header=True), ident(p["name"])))
                if len(params) > 9:
                    raise Unsupported("more than 9 dispatcher parameters")
                item.migrated = True
                item.extra["params"] = params
            except Unsupported as e:
                item.reason = e.reason
            self.dispatchers[g["name"]] = item

    def plan_functions(self):
        for g in self.ex.graphs:
            if g["kind"] != "function":
                continue
            entry = next((n for n in g["nodes"] if n["class"] == "K2Node_FunctionEntry"), None)
            if not entry:
                continue
            item = Item(g["name"], ident(g["name"]), extra={"graph": g, "entry": entry})
            if item.cpp in self.ex.parent_members and g["name"] != "UserConstructionScript":
                item.reason = f"the native parent already has a member named {item.cpp} (an override: kept in Blueprint for now)"
                self.funcs[g["name"]] = item
                continue
            flags = set(entry.get("functionFlags", []))
            item.extra["flags"] = sorted(flags)
            # an override of a native function (BlueprintNativeEvent implemented in Blueprint)
            if entry.get("member") and self.ex.functions.get(f"{self.parent_path}:{entry['member']}"):
                item.extra["override"] = True
            try:
                if g["name"] == "UserConstructionScript":
                    if self.kind != "actor":
                        raise Unsupported("a Construction Script outside an actor")
                    item.cpp = "OnConstruction"
                    item.extra["construction"] = True
                if flags & {"Net", "Server", "Client", "Multicast"}:
                    raise Unsupported("replicated functions are not translated yet")
                params, ret = [], None
                for p in entry["pins"]:
                    if p["dir"] == "out" and p["type"].get("category") != "exec":
                        params.append((p["name"], ident(p["name"]), False, self.types.of(p["type"])))
                result = next((n for n in g["nodes"] if n["class"] == "K2Node_FunctionResult"), None)
                outs = [p for p in (result or {}).get("pins", []) if p["dir"] == "in" and p["type"].get("category") != "exec"]
                if len(outs) == 1 and outs[0]["name"] == "ReturnValue":
                    ret = self.types.of(outs[0]["type"])
                else:
                    for p in outs:
                        params.append((p["name"], ident(p["name"]), True, self.types.of(p["type"])))
                item.extra.update(params=params, **{"return": ret})
                item.migrated = True        # tentative; translation decides
            except Unsupported as e:
                item.reason = e.reason
            self.funcs[g["name"]] = item
        # custom events are callable like functions: callers need to know they exist before the events translate
        for g, n in self.event_roots():
            if n["class"] == "K2Node_CustomEvent":
                try:
                    params = [(p["name"], ident(p["name"]), False, self.types.of(p["type"])) for p in n["pins"]
                              if p["dir"] == "out" and p["type"].get("category") not in ("exec", "delegate")]
                    self.funcs[n["member"]] = Item(n["member"], ident(n["member"]), True,
                                                   extra={"params": params, "return": None, "customEvent": True, "node": n["id"]})
                except Unsupported as e:
                    self.funcs[n["member"]] = Item(n["member"], ident(n["member"]), False, e.reason, extra={"customEvent": True, "node": n["id"]})

    def event_roots(self) -> list[tuple[dict, dict]]:
        out = []
        for g in self.ex.graphs:
            if g["kind"] != "event":
                continue
            for n in g["nodes"]:
                if n["class"] in ("K2Node_Event", "K2Node_CustomEvent", "K2Node_ComponentBoundEvent", "K2Node_EnhancedInputAction",
                                  "K2Node_InputAction", "K2Node_InputKey", "K2Node_WidgetAnimationEvent", "K2Node_InputAxisEvent"):
                    out.append((g, n))
        return out

    def override_for(self, event: dict):
        member = event.get("member", "")
        owner = short(event.get("memberOwner", ""))
        candidates = [owner] + [b for b in OVERRIDE_BASES if b in self.parent_ancestors()]
        for o in candidates:
            if (o, member) in OVERRIDES:
                return OVERRIDES[(o, member)]
        return None

    def translate_all(self) -> bool:
        """One pass over every function and event; returns True when an item changed to 'stays in Blueprint'."""
        changed = False
        self.bodies.clear()
        self.event_members.clear()
        self.state_members.clear()
        self.literal_props.clear()
        for name, item in self.funcs.items():
            if not item.migrated or item.extra.get("customEvent"):
                continue
            g, entry = item.extra["graph"], item.extra["entry"]
            params = {(entry["id"], p): cpp for p, cpp, out, _t in item.extra["params"] if not out}
            fn = Fn(self, g, entry, "function", params)
            fn.return_type = item.extra.get("return") or "void"
            for local in entry.get("locals", []):
                try:
                    t = self.types.of(local["type"])
                    init = self.literals.value(local["type"], local.get("default", "")) if local.get("default") not in (None, "") else ""
                    fn.locals[ident(local["name"])] = (t, init)
                except Unsupported as e:
                    item.migrated, item.reason = False, f"local variable {local['name']}: {e.reason}"
                    changed = True
                    break
            if not item.migrated:
                continue
            # local variable nodes read and write the local, not a member
            self.local_names = {l["name"]: ident(l["name"]) for l in entry.get("locals", [])}
            try:
                fn.translate(fn.exec_target(entry, "then"))
                if fn.return_type != "void" and not any(n["class"] == "K2Node_FunctionResult" for n in g["nodes"]):
                    fn.say("return {};")
                item.extra["nodes"] = sorted(fn.used_nodes)
                self.bodies["fn:" + name] = fn
            except Unsupported as e:
                item.migrated = False
                item.reason = e.reason + (f" [node {e.node['id'][:8]} {e.node.get('title', '').splitlines()[0]}]" if e.node else "")
                changed = True
            finally:
                self.local_names = {}
        events = []
        for g, n in self.event_roots():
            item = Item(n.get("member") or n.get("title", ""), "", extra={"node": n["id"], "class": n["class"]})
            wired = any(p.get("links") for p in n["pins"] if p["dir"] == "out")
            if not wired and n["class"] in ("K2Node_Event",):
                item.reason = "empty (nothing is wired to it): nothing to migrate"
                item.extra["empty"] = True
                events.append(item)
                continue
            try:
                self.translate_event(g, n, item)
                item.migrated = True
            except Unsupported as e:
                item.reason = e.reason + (f" [node {e.node['id'][:8]} {e.node.get('title', '').splitlines()[0]}]" if e.node and e.node is not n else "")
            events.append(item)
        prev = {(e.extra["node"], e.migrated) for e in self.events}
        self.events = events
        for e in events:                                  # a custom event that failed: its callers must stay too
            if e.extra["class"] == "K2Node_CustomEvent" and not e.migrated:
                f = self.funcs.get(e.bp)
                if f and f.migrated:
                    f.migrated, f.reason = False, e.reason
                    changed = True
        if prev and prev != {(e.extra["node"], e.migrated) for e in events}:
            changed = True
        # a custom event other code calls must be migrated for those callers to stay migrated: handled by the
        # caller translation (self_call looks custom events up in funcs)
        return changed

    def translate_event(self, g: dict, n: dict, item: Item):
        cls = n["class"]
        params: dict = {}
        if cls == "K2Node_Event":
            ov = self.override_for(n)
            native = self.ex.functions.get(f"{n.get('memberOwner', '')}:{n.get('member', '')}") or {}
            if ov:
                sig, order, super_call, names = ov
                outs = [p for p in n["pins"] if p["dir"] == "out" and p["type"].get("category") not in ("exec", "delegate")]
                for p, cpp in zip(outs, names):
                    params[(n["id"], p["name"])] = cpp
                item.cpp = re.match(r"\w+\s+(\w+)", sig).group(1)
                item.extra.update(signature=sig, order=order, superCall=super_call, override=True)
            elif native.get("native") and "BlueprintEvent" in native.get("flags", []) and "Native" in native.get("flags", []):
                # BlueprintNativeEvent: the Blueprint replaced the C++ implementation
                args = []
                for prm in native["params"]:
                    if prm.get("return"):
                        raise Unsupported("native event with a return value")
                    args.append(f"{prm['cpp']} {prm['name']}")
                    params[(n["id"], prm["name"])] = prm["name"]
                item.cpp = native["name"] + "_Implementation"
                item.extra.update(signature=f"void {item.cpp}({', '.join(args)})", order="none", superCall="", override=True)
            else:
                raise Unsupported(f"event {n.get('member')} has no native override to put it in (BlueprintImplementableEvent)")
        elif cls == "K2Node_CustomEvent":
            if set(n.get("memberFlags", [])) & {"Net", "Server", "Client", "Multicast"}:
                raise Unsupported("replicated custom events are not translated yet")
            args = []
            for p in n["pins"]:
                if p["dir"] == "out" and p["type"].get("category") not in ("exec", "delegate"):
                    t = self.types.of(p["type"], header=True)
                    args.append(f"{t} {ident(p['name'])}")
                    params[(n["id"], p["name"])] = ident(p["name"])
            item.cpp = ident(n["member"])
            item.extra.update(signature=f"void {item.cpp}({', '.join(args)})", order="none", superCall="", custom=True)
        elif cls == "K2Node_ComponentBoundEvent":
            comp = n.get("component", "")
            owner_item = self.components.get(comp) or self.widgets.get(comp)
            if owner_item is None or not owner_item.migrated:
                raise Unsupported(f"bound to {comp}, which is not a native member")
            sig_fn = next((f for k, f in self.ex.functions.items() if f["name"].startswith(n["member"]) or k.endswith("__DelegateSignature")
                           and n["member"] in k), None)
            outs = [p for p in n["pins"] if p["dir"] == "out" and p["type"].get("category") not in ("exec", "delegate")]
            args = []
            for p in outs:
                t = self.types.of(p["type"], header=True)
                if p["type"].get("ref") or t.startswith("F") and t not in ("FName", "FString"):
                    t = f"const {t}&" if not t.endswith("*") else t
                args.append(f"{t} {ident(p['name'])}")
                params[(n["id"], p["name"])] = ident(p["name"])
            item.cpp = f"On{ident(comp)}{ident(n['member'])}"
            item.extra.update(signature=f"void {item.cpp}({', '.join(args)})", order="none", superCall="", bind=(owner_item.cpp, n["member"]))
        else:
            raise Unsupported(f"{cls.replace('K2Node_', '')} events are not translated yet (input and animation events)")
        fn = Fn(self, g, n, "event", params)
        then = "then" if "then" in n["_pins"] else None
        fn.translate(fn.exec_target(n, then) if then else None)
        item.extra["nodes"] = sorted(fn.used_nodes)
        for k, v in fn.members.items():
            self.event_members[k] = v
        self.bodies[f"ev:{n['id']}"] = fn

    # ------------------------------------------------------------------------------------------ output
    def summary(self) -> dict:
        def items(d):
            return [{"bp": i.bp, "cpp": i.cpp, "migrated": i.migrated, "reason": i.reason} for i in d]
        ev = [{"bp": e.bp, "cpp": e.cpp, "node": e.extra["node"], "migrated": e.migrated, "reason": e.reason,
               "nodes": e.extra.get("nodes", [])} for e in self.events]
        fns = [{"bp": f.bp, "cpp": f.cpp, "migrated": f.migrated, "reason": f.reason, "nodes": f.extra.get("nodes", []),
                "params": [{"bp": p, "cpp": c, "out": o} for p, c, o, _t in f.extra.get("params", [])]}
               for f in self.funcs.values() if not f.extra.get("customEvent")]
        graph_nodes = {n["id"] for g in self.ex.graphs if g["kind"] in ("event", "function") for n in g["nodes"]
                       if n["class"] not in ("EdGraphNode_Comment",)}
        graph_nodes -= {e.extra["node"] for e in self.events if e.extra.get("empty")}   # unwired template events
        moved = set()
        for e in self.events:
            if e.migrated:
                moved |= set(e.extra.get("nodes", []))
        for f in self.funcs.values():
            if f.migrated:
                moved |= set(f.extra.get("nodes", []))
                if f.extra.get("graph"):
                    moved |= {n["id"] for n in f.extra["graph"]["nodes"] if n["class"] != "EdGraphNode_Comment"}
        return {
            "schema": SCHEMA, "blueprint": self.ex.doc.get("asset"), "package": self.ex.doc.get("package"), "name": self.ex.name,
            "kind": self.kind, "class": self.cpp, "classPath": f"/Script/{self.module}.{self.cpp[1:]}", "module": self.module,
            "parent": self.parent_cpp, "parentPath": self.parent_path, "exportedAtUtc": self.ex.doc.get("exportedAtUtc"),
            "variables": items(self.vars.values()), "components": items(self.components.values()),
            "widgets": items(self.widgets.values()), "dispatchers": items(self.dispatchers.values()),
            "functions": fns, "events": ev,
            "literals": [{"cpp": v["cpp"], "path": v["path"], "type": v["type"]} for v in self.literal_props.values()],
            "modules": sorted(m for m in self.types.modules if m not in ("CoreUObject", "Engine", "Core") and m != self.module),
            "coverage": {"nodes": len(graph_nodes), "migrated": len(moved & graph_nodes)},
        }


# ------------------------------------------------------------------------------------------------ C++ writer

def literals_header() -> str:
    return """// Generated by dwookit native migration. Literal helpers that parse values exactly as the Blueprint compiler
// does (FDefaultValueHelper first, then the struct's ImportText), evaluated once per use site.
#pragma once

#include "CoreMinimal.h"
#include "Misc/DefaultValueHelper.h"
#include "UObject/Class.h"

namespace DwoNative
{
	inline FVector ParseVector(const TCHAR* Text)
	{
		FVector Value(ForceInit);
		if (!FDefaultValueHelper::ParseVector(Text, Value)) { TBaseStructure<FVector>::Get()->ImportText(Text, &Value, nullptr, PPF_None, GLog, TEXT("DwoNative")); }
		return Value;
	}
	inline FRotator ParseRotator(const TCHAR* Text)
	{
		FRotator Value(ForceInit);
		if (!FDefaultValueHelper::ParseRotator(Text, Value)) { TBaseStructure<FRotator>::Get()->ImportText(Text, &Value, nullptr, PPF_None, GLog, TEXT("DwoNative")); }
		return Value;
	}
	inline FTransform ParseTransform(const TCHAR* Text)
	{
		FTransform Value = FTransform::Identity;
		if (!Value.InitFromString(Text)) { TBaseStructure<FTransform>::Get()->ImportText(Text, &Value, nullptr, PPF_None, GLog, TEXT("DwoNative")); }
		return Value;
	}
	template <typename T>
	T ImportStruct(const TCHAR* Text)
	{
		T Value{};
		TBaseStructure<T>::Get()->ImportText(Text, &Value, nullptr, PPF_None, GLog, TEXT("DwoNative"));
		return Value;
	}
}
"""


def write(plan: Plan, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    s = plan.summary()
    cls, parent = plan.cpp, plan.parent_cpp
    t = plan.types
    header_name = cls[1:]
    pi = plan.parent.get("include", "")
    h_inc = sorted(t.header_includes - {pi})
    vars_ = [v for v in plan.vars.values() if v.migrated]
    comps = [c for c in plan.components.values() if c.migrated]
    widgets = [w for w in plan.widgets.values() if w.migrated]
    funcs = [f for f in plan.funcs.values() if f.migrated and not f.extra.get("customEvent") and not f.extra.get("construction")]
    construction = next((f for f in plan.funcs.values() if f.migrated and f.extra.get("construction")), None)
    events = [e for e in plan.events if e.migrated]
    dispatchers = [d for d in plan.dispatchers.values() if d.migrated]
    needs_net = any("Replicated" in v.extra.get("flags", []) for v in vars_)

    H = [f"// Generated by dwookit native migration from {plan.ex.doc.get('asset')} ({plan.ex.doc.get('exportedAtUtc', '')}).",
         f"// The Blueprint {plan.ex.name} is rebased onto this class; what is listed in {plan.ex.name}.native.json as",
         "// staying in Blueprint is still in the asset. Regenerating overwrites this file.",
         "#pragma once", "", '#include "CoreMinimal.h"']
    if pi:
        H.append(f'#include "{pi}"')
    H += [f'#include "{i}"' for i in h_inc]
    H.append(f'#include "{header_name}.generated.h"')
    H.append("")
    fwd = sorted(c for c in t.forward if c not in (cls, parent) and c[0] in "UA")
    H += [f"class {c};" for c in fwd]
    if fwd:
        H.append("")
    count_names = ["", "_OneParam", "_TwoParams", "_ThreeParams", "_FourParams", "_FiveParams", "_SixParams", "_SevenParams", "_EightParams", "_NineParams"]
    for d in dispatchers:
        params = d.extra["params"]
        flat = ", ".join(f"{ty}, {nm}" for ty, nm in params)
        H.append(f"DECLARE_DYNAMIC_MULTICAST_DELEGATE{count_names[len(params)]}(F{cls[1:]}_{d.cpp}{', ' if flat else ''}{flat});")
    if dispatchers:
        H.append("")
    H += [f"UCLASS(Blueprintable, BlueprintType)", f"class {plan.api} {cls} : public {parent}", "{", "\tGENERATED_BODY()", "", "public:"]
    if plan.kind in ("actor", "component"):
        H.append(f"\t{cls}();")
    elif plan.kind == "widget":
        pass
    if plan.kind == "object" or plan.kind == "anim":
        pass
    H.append("")
    for c in comps:
        H.append(f'\tUPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Components")')
        H.append(f"\tTObjectPtr<{c.extra['cppClass']}> {c.cpp};")
        H.append("")
    for w in widgets:
        H.append("\tUPROPERTY(BlueprintReadOnly, meta = (BindWidget))")
        H.append(f"\tTObjectPtr<{w.extra['cppClass']}> {w.cpp};")
        H.append("")
    for d in dispatchers:
        H.append('\tUPROPERTY(BlueprintAssignable, Category = "Events")')
        H.append(f"\tF{cls[1:]}_{d.cpp} {d.cpp};")
        H.append("")
    for v in vars_:
        flags = set(v.extra.get("flags", []))
        spec = ["EditAnywhere" if "InstanceEditable" in flags else "EditDefaultsOnly",
                "BlueprintReadOnly" if "ReadOnly" in flags else "BlueprintReadWrite"]
        cat = v.extra.get("category") or "Default"
        if cat in ("디폴트",):
            cat = "Default"
        spec.append(f'Category = "{cat}"')
        if "Replicated" in flags:
            notify = v.extra.get("repNotify")
            f = plan.funcs.get(notify) if notify else None
            if notify and f and f.migrated:
                spec.append(f"ReplicatedUsing = {f.cpp}")
                v.extra["notifyCpp"] = f.cpp
            else:
                spec.append("Replicated")
        for flag, word in (("Transient", "Transient"), ("SaveGame", "SaveGame"), ("Config", "Config")):
            if flag in flags:
                spec.append(word)
        meta = []
        if "ExposeOnSpawn" in flags:
            meta.append("ExposeOnSpawn = true")
        if v.cpp != v.bp:
            meta.append(f'DisplayName = "{v.bp}"')
        if meta:
            spec.append(f"meta = ({', '.join(meta)})")
        if v.extra.get("tooltip"):
            H.append(f"\t/** {v.extra['tooltip']} */")
        H.append(f"\tUPROPERTY({', '.join(spec)})")
        init = ""
        try:
            d = v.extra.get("default")
            if d not in (None, "") and v.extra["pinType"].get("category") in ("bool", "int", "int64", "real", "float", "double", "name", "string", "byte", "enum") \
                    and not v.extra["pinType"].get("container"):
                init = " = " + plan.literals.value(v.extra["pinType"], d)
            elif d not in (None, "") and short(v.extra["pinType"].get("object", "")) in ("Vector", "Rotator", "Vector2D", "LinearColor") \
                    and not v.extra["pinType"].get("container"):
                lit = plan.literals.value(v.extra["pinType"], d)
                if "DwoNative" not in lit:
                    init = " = " + lit
        except Unsupported:
            init = ""
        H.append(f"\t{v.extra['type']} {v.cpp}{init};")
        H.append("")
    for f in funcs:
        pure = "Pure" in f.extra.get("flags", [])
        spec = ["BlueprintPure" if pure else "BlueprintCallable", 'Category = "Default"']
        args = []
        for pin, cpp, out, ty in f.extra["params"]:
            args.append(f"{ty}& {cpp}" if out else (f"{ty} {cpp}" if ty in ("bool", "int32", "int64", "double", "float", "uint8") or ty.endswith("*") else f"const {ty}& {cpp}"))
        H.append(f"\tUFUNCTION({', '.join(spec)}{', meta = (DisplayName = \"' + f.bp + '\")' if f.cpp != f.bp else ''})")
        H.append(f"\t{f.extra.get('return') or 'void'} {f.cpp}({', '.join(args)}){' const' if 'Const' in f.extra.get('flags', []) else ''};")
        H.append("")
    for e in events:
        if e.extra.get("custom"):
            H.append('\tUFUNCTION(BlueprintCallable, Category = "Default")')
            H.append(f"\t{e.extra['signature']};")
            H.append("")
    protected = [e for e in events if e.extra.get("override")]
    bound = [e for e in events if e.extra.get("bind")]
    if protected or bound or needs_net or construction:
        H.append("protected:")
        for e in protected:
            H.append(f"\tvirtual {e.extra['signature']} override;")
        if construction:
            H.append("\tvirtual void OnConstruction(const FTransform& Transform) override;")
        if needs_net:
            H.append("\tvirtual void GetLifetimeReplicatedProps(TArray<FLifetimeProperty>& OutLifetimeProps) const override;")
        if bound:
            if plan.kind == "widget":
                H.append("\tvirtual void NativeOnInitialized() override;" if not any(e.cpp == "NativeOnInitialized" for e in protected) else "")
            else:
                H.append("\tvirtual void PostInitializeComponents() override;")
            for e in bound:
                H.append("\tUFUNCTION()")
                H.append(f"\t{e.extra['signature']};")
        H.append("")
    privates = []
    for name, (ty, is_prop) in sorted(plan.event_members.items()):
        if is_prop:
            privates.append("\tUPROPERTY(Transient)")
        privates.append(f"\t{ty} {name}{{}};")
    for name, (ty, init) in sorted(plan.state_members.items()):
        privates.append(f"\t{ty} {name} = {init};")
    for lit in plan.literal_props.values():
        privates.append("\tUPROPERTY(EditDefaultsOnly, Category = \"Native Literals\", meta = (DisplayName = \"" + short(lit["path"]) + "\"))")
        privates.append(f"\t{lit['type']} {lit['cpp']};")
    if privates:
        H += ["private:", "\t// Blueprint event graph temporaries and node state (the Blueprint kept them in its frame)."] + privates
    H.append("};")
    H = [l for l in H if l is not None]

    C = [f"// Generated by dwookit native migration from {plan.ex.doc.get('asset')}.", f'#include "{header_name}.h"']
    inc = sorted(t.includes - {pi} - set(h_inc))
    C += [f'#include "{i}"' for i in inc]
    if plan.literals.uses_helpers:
        C.append('#include "DwoNativeLiterals.h"')
    if needs_net:
        C.append('#include "Net/UnrealNetwork.h"')
    C.append("")
    if plan.kind in ("actor", "component"):
        C.append(f"{cls}::{cls}()")
        C.append("{")
        if any(e.cpp == "Tick" or e.cpp == "TickComponent" for e in events):
            C.append("\tPrimaryActorTick.bCanEverTick = true;" if plan.kind == "actor" else "\tPrimaryComponentTick.bCanEverTick = true;")
        # components: created with their Blueprint names, attached as the Blueprint had them
        for c in comps:
            C.append(f'\t{c.cpp} = CreateDefaultSubobject<{c.extra["cppClass"]}>(TEXT("{c.bp}"));')
        for c in comps:
            parent_name = c.extra.get("parent")
            socket = c.extra.get("socket")
            sock = f", FName(TEXT(\"{socket}\"))" if socket else ""
            if c.extra.get("root") and not parent_name:
                C.append(f"\tSetRootComponent({c.cpp});")
            elif parent_name in plan.components and plan.components[parent_name].migrated:
                C.append(f"\t{c.cpp}->SetupAttachment({plan.components[parent_name].cpp}{sock});")
            elif parent_name:
                C.append(f'\t{c.cpp}->SetupAttachment(Cast<USceneComponent>(GetDefaultSubobjectByName(TEXT("{parent_name}"))){sock});')
        C.append("}")
        C.append("")
    if bound:
        host = "NativeOnInitialized" if plan.kind == "widget" else "PostInitializeComponents"
        hosted = next((e for e in protected if e.cpp == host), None)
        if not hosted:
            C.append(f"void {cls}::{host}()")
            C.append("{")
            C.append(f"\tSuper::{host}();")
            for e in bound:
                comp, delegate = e.extra["bind"]
                C.append(f"\tif ({comp}) {{ {comp}->{delegate}.AddDynamic(this, &ThisClass::{e.cpp}); }}")
            C.append("}")
            C.append("")
    if needs_net:
        C.append(f"void {cls}::GetLifetimeReplicatedProps(TArray<FLifetimeProperty>& OutLifetimeProps) const")
        C.append("{")
        C.append("\tSuper::GetLifetimeReplicatedProps(OutLifetimeProps);")
        for v in vars_:
            if "Replicated" in v.extra.get("flags", []):
                C.append(f"\tDOREPLIFETIME({cls}, {v.cpp});")
        C.append("}")
        C.append("")

    def body(fn: Fn) -> list[str]:
        out = []
        for name, (ty, init) in fn.locals.items():
            out.append(f"\t{ty} {name}{(' = ' + init) if init else '{}'};")
        return out + fn.lines

    for f in funcs:
        fn = plan.bodies.get("fn:" + f.bp)
        args = []
        for pin, cpp, out_, ty in f.extra["params"]:
            args.append(f"{ty}& {cpp}" if out_ else (f"{ty} {cpp}" if ty in ("bool", "int32", "int64", "double", "float", "uint8") or ty.endswith("*") else f"const {ty}& {cpp}"))
        C.append(f"{f.extra.get('return') or 'void'} {cls}::{f.cpp}({', '.join(args)}){' const' if 'Const' in f.extra.get('flags', []) else ''}")
        C.append("{")
        C += body(fn)
        C.append("}")
        C.append("")
    if construction:
        fn = plan.bodies.get("fn:" + construction.bp)
        C.append(f"void {cls}::OnConstruction(const FTransform& Transform)")
        C.append("{")
        C.append("\tSuper::OnConstruction(Transform);")
        C += body(fn)
        C.append("}")
        C.append("")
    for e in events:
        fn = plan.bodies.get(f"ev:{e.extra['node']}")
        sig = e.extra["signature"]
        m = re.match(r"(\w[\w:<>*&\s]*?)\s+(\w+)\((.*)\)$", sig)
        ret, name, args = m.group(1), m.group(2), m.group(3)
        C.append(f"{ret} {cls}::{name}({args})")
        C.append("{")
        if e.extra.get("order") == "after":
            C.append("\t" + e.extra["superCall"])
        elif e.extra.get("order") == "custom":
            C.append("\t" + e.extra["superCall"])
        if name in ("NativeOnInitialized", "PostInitializeComponents") and bound:
            for b in bound:
                comp, delegate = b.extra["bind"]
                C.append(f"\tif ({comp}) {{ {comp}->{delegate}.AddDynamic(this, &ThisClass::{b.cpp}); }}")
        C += body(fn)
        if e.extra.get("order") == "before":
            C.append("\t" + e.extra["superCall"])
        C.append("}")
        C.append("")

    files = {f"{header_name}.h": "\n".join(H) + "\n", f"{header_name}.cpp": "\n".join(C) + "\n"}
    if plan.literals.uses_helpers:
        files["DwoNativeLiterals.h"] = literals_header()
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8", newline="\n")
    s["files"] = sorted(files)
    (out / f"{plan.ex.name}.native.json").write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / f"{plan.ex.name}.native.md").write_text(report_md(s), encoding="utf-8")
    return s


def report_md(s: dict) -> str:
    cov = s["coverage"]
    pct = 100 * cov["migrated"] // max(1, cov["nodes"])
    lines = [f"# {s['name']} -> {s['class']} ({s['kind']})", "",
             f"Parent {s['parent']}. Nodes moved to C++: {cov['migrated']} of {cov['nodes']} ({pct}%).", ""]

    def table(title, rows, cols):
        if not rows:
            return
        lines.append(f"## {title} ({sum(1 for r in rows if r['migrated'])} of {len(rows)} migrated)")
        lines.append("")
        lines.append("| " + " | ".join(c for c, _ in cols) + " |")
        lines.append("|" + "---|" * len(cols))
        for r in rows:
            lines.append("| " + " | ".join(str(f(r)) for _, f in cols) + " |")
        lines.append("")
    state = lambda r: "C++" if r["migrated"] else "Blueprint"
    table("Variables", s["variables"], [("Blueprint", lambda r: r["bp"]), ("C++", lambda r: r["cpp"]), ("Where", state), ("Why it stays", lambda r: r["reason"])])
    table("Components", s["components"], [("Blueprint", lambda r: r["bp"]), ("Where", state), ("Why it stays", lambda r: r["reason"])])
    table("Widgets (BindWidget)", s["widgets"], [("Widget", lambda r: r["bp"]), ("Where", state), ("Why it stays", lambda r: r["reason"])])
    table("Event dispatchers", s["dispatchers"], [("Blueprint", lambda r: r["bp"]), ("Where", state), ("Why it stays", lambda r: r["reason"])])
    table("Functions", s["functions"], [("Blueprint", lambda r: r["bp"]), ("C++", lambda r: r["cpp"]), ("Where", state), ("Nodes", lambda r: len(r["nodes"])), ("Why it stays", lambda r: r["reason"])])
    table("Events", s["events"], [("Blueprint", lambda r: r["bp"]), ("C++", lambda r: r["cpp"] or "-"), ("Where", state), ("Nodes", lambda r: len(r["nodes"])), ("Why it stays", lambda r: r["reason"])])
    if s["literals"]:
        lines += ["## Asset references used by the code", "", "Kept as hidden properties the rebase fills from the Blueprint, so they stay hard references (cooked).", ""]
        lines += [f"- `{l['cpp']}` = `{l['path']}`" for l in s["literals"]]
    return "\n".join(lines) + "\n"


def add_modules(build_cs: Path, modules: list[str]) -> list[str]:
    """Adds the modules the generated code needs to the game module's Build.cs, on one marked line, so they are easy
    to see and remove. Returns the ones added (already listed anywhere in the file: skipped)."""
    text = build_cs.read_text(encoding="utf-8-sig")
    missing = [m for m in modules if f'"{m}"' not in text]
    if not missing:
        return []
    marker = "// dwookit native migration"
    line_re = re.compile(r'^(\s*)PrivateDependencyModuleNames\.AddRange\(new string\[\] \{ (.*) \}\); ' + re.escape(marker) + r'$', re.M)
    m = line_re.search(text)
    if m:
        names = [x.strip() for x in m.group(2).split(",") if x.strip()] + [f'"{x}"' for x in missing]
        text = text[:m.start()] + f'{m.group(1)}PrivateDependencyModuleNames.AddRange(new string[] {{ {", ".join(names)} }}); {marker}' + text[m.end():]
    else:
        anchor = re.search(r'^(\s*)PublicDependencyModuleNames\.AddRange\(', text, re.M)
        if not anchor:
            raise SystemExit(f"{build_cs}: no PublicDependencyModuleNames to put the new modules next to")
        indent = anchor.group(1)
        quoted = ", ".join(f'"{x}"' for x in missing)
        line = f"{indent}PrivateDependencyModuleNames.AddRange(new string[] {{ {quoted} }}); {marker}\n"
        text = text[:anchor.start()] + line + text[anchor.start():]
    build_cs.write_text(text, encoding="utf-8")
    return missing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "generate"):
        p = sub.add_parser(name)
        p.add_argument("export", type=Path)
        p.add_argument("--module", default="Game")
        p.add_argument("--class-name")
        p.add_argument("--api")
        if name == "generate":
            p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("modules")
    p.add_argument("build_cs", type=Path)
    p.add_argument("modules")
    args = ap.parse_args(argv)
    if args.cmd == "modules":
        added = add_modules(args.build_cs, [m for m in args.modules.split(",") if m])
        print(f"native: {args.build_cs.name}: " + (f"added {', '.join(added)}" if added else "already has every module"))
        return 0
    doc = json.loads(args.export.read_text(encoding="utf-8-sig"))
    plan = Plan(doc, args.class_name, args.module, args.api).build()
    if args.cmd == "plan":
        s = plan.summary()
        print(report_md(s))
        return 0
    s = write(plan, args.out)
    cov = s["coverage"]
    print(f"native: {s['name']} -> {s['class']} ({cov['migrated']}/{cov['nodes']} nodes) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
