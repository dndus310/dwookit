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

# Deprecated engine functions Blueprints still call, and the replacement the engine names, where both have the
# same body (checked against the UE 5.7 source): (owner, function) -> (call, include, module)
REPLACED = {
    ("AnimInstance", "CalculateDirection"): ("UKismetAnimationLibrary::CalculateDirection", "KismetAnimationLibrary.h", "AnimGraphRuntime"),
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


def param_decl(prm: dict, enum_as_byte: bool = True) -> str:
    """A parameter as the C++ declaration UHT generated it from: out -> T&, ref -> (const) T&."""
    t = prm["cpp"]
    if not enum_as_byte:
        m = re.fullmatch(r"TEnumAsByte<(.+)>", t)
        t = m.group(1) if m else t
    if prm.get("out"):
        return f"{t}& {prm['name']}"
    if prm.get("ref"):
        return f"{'const ' if prm.get('const') else ''}{t}& {prm['name']}"
    return f"{'const ' if prm.get('const') else ''}{t} {prm['name']}"


def enum_exec_value(cpp_type: str, pin: str) -> str:
    """The enum value an ExpandEnumAsExecs exec pin stands for: TEnumAsByte<EMove::Type> + 'Stop' -> EMove::Stop."""
    m = re.fullmatch(r"TEnumAsByte<(.+)>", cpp_type.strip())
    t = m.group(1) if m else cpp_type.strip()
    t = re.sub(r"::Type$", "", t)
    return f"{t}::{pin}"


def guid_args(node_id: str) -> str:
    """FGuid(A, B, C, D) for a node id in Digits form: the Blueprint compiler keys latent actions on its hash."""
    h = node_id.replace("-", "")
    if not re.fullmatch(r"[0-9A-Fa-f]{32}", h):
        raise Unsupported(f"node id {node_id} is not a GUID")
    return ", ".join(f"0x{h[i:i + 8].upper()}u" for i in range(0, 32, 8))


def prop_bool(props: dict, name: str, default: bool = False) -> bool:
    v = (props or {}).get(name)
    return default if v is None else str(v).strip().lower() == "true"


def prop_path(text: str) -> str:
    """'/Script/EnhancedInput.InputAction'/Game/Input/IA_Jump.IA_Jump'' -> '/Game/Input/IA_Jump.IA_Jump'."""
    m = re.search(r"'([^']+)'", text or "")
    return (m.group(1) if m else (text or "")).strip('"')


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
        # '/Game/X.SKEL_X_C:F' and '/Game/X.X_C:F' are one function (nodes name either)
        self.functions = {strip_skel(k): v for k, v in self.native.get("functions", {}).items()}
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
        self.entered: str | None = None      # input exec pin the flow entered the current node through
        self.loop_depth = 0
        self.root_fn: Fn = self              # the event this flow belongs to (continuations point back to it)
        self.conts: set[str] = set()         # latent nodes whose continuation this flow schedules
        self.prologue: list[str] = []        # event arguments copied to members (continuations read them)
        self.arg_members: dict | None = None

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
        link = p["links"][0]
        n = self.node(link["node"])
        while n["class"] == "K2Node_Knot":       # reroute: part of the flow it carries
            self.used_nodes.add(n["id"])
            link = self.exec_out_link(n, "OutputPin")
            n = self.node(link["node"])
        self.entered = link["pin"]
        return n

    def exec_out_link(self, node, pin):
        p = node["_pins"].get(pin)
        if not p or not p.get("links"):
            raise Unsupported("a reroute node that leads nowhere", node)
        return p["links"][0]

    def multi_entry(self, node: dict) -> bool:
        """A node with several exec inputs (timeline, ExpandEnumAsExecs): what it does depends on the pin entered."""
        return sum(1 for p in node.get("pins", []) if p["dir"] == "in" and p["type"].get("category") == "exec") > 1

    def arg_params(self) -> dict:
        """The event's arguments as members, for code that runs later (a latent continuation): the Blueprint keeps
        them in its persistent frame until the event fires again, and so do these members."""
        root = self.root_fn
        if root.arg_members is None:
            root.arg_members = {}
            for (nid, pin_name), expr in root.params.items():
                pin = self.ex.nodes[nid]["_pins"].get(pin_name)
                if pin is None:
                    continue
                is_obj = self.types.is_object(pin["type"])
                member = f"EvArg_{nid[:8]}_{ident(pin_name)}"
                if member not in root.members:
                    root.members[member] = (self.types.of(pin["type"], header=True, as_property=is_obj), is_obj)
                    root.prologue.append(f"\t{member} = {expr};")
                root.arg_members[(nid, pin_name)] = member
        return dict(root.arg_members)

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
        if node.get("role") == "event" and node["id"] != self.root_fn.root["id"] and pin["dir"] == "out":
            # another event's argument: the Blueprint keeps the last one in its frame; so does a member
            return self.plan.shared_arg(node, pin)
        cls, kind = node["class"], node.get("kind")
        if node.get("disabled"):
            raise Unsupported("a disabled node feeds data", node)
        if cls == "K2Node_Knot":
            return self.input(node, "InputPin")
        if cls == "K2Node_Timeline":
            return self.plan.timeline_value(node, pin_name)
        if cls == "K2Node_Self":
            return "this"
        if cls == "K2Node_VariableGet":
            return self.var_get(node)
        if cls == "K2Node_VariableSet" and pin["dir"] == "out" and pin["type"].get("category") != "exec":
            return self.plan.local_names.get(node["member"]) or self.plan.var_cpp(node["member"], node)
        if cls == "K2Node_CallArrayFunction" and node.get("pure"):
            return self.array_value(node, pin_name)
        if cls == "K2Node_CallFunction" and short(node.get("memberOwner", "")) == "BlueprintMapLibrary" and node.get("pure"):
            return self.map_value(node, pin_name)
        if cls == "K2Node_MakeArray":
            t = self.types.of(pin["type"])
            items = [self.input(node, p["name"]) for p in node["pins"] if p["dir"] == "in"]
            return f"{t}{{{', '.join(items)}}}"
        if cls == "K2Node_MakeMap":
            t = self.types.of(pin["type"])
            ins = [p["name"] for p in node["pins"] if p["dir"] == "in"]
            pairs = [f"{{{self.input(node, k)}, {self.input(node, 'Value' + k[3:])}}}" for k in ins if k.startswith("Key ")]
            return f"{t}{{{', '.join(pairs)}}}"
        if cls in ("K2Node_EnumEquality", "K2Node_EnumInequality"):
            op = "==" if cls == "K2Node_EnumEquality" else "!="
            return f"(static_cast<uint8>({self.input(node, 'A')}) {op} static_cast<uint8>({self.input(node, 'B')}))"
        if cls in ("K2Node_GetEnumeratorName", "K2Node_GetEnumeratorNameAsString"):
            self.types.includes.add("Kismet/KismetNodeHelperLibrary.h")
            e = node["_pins"]["Enumerator"]
            fn = "GetEnumeratorName" if cls == "K2Node_GetEnumeratorName" else "GetEnumeratorUserFriendlyName"
            return f"UKismetNodeHelperLibrary::{fn}({self.enum_object(e)}, static_cast<uint8>({self.input(node, 'Enumerator')}))"
        if cls == "K2Node_GetClassDefaults":
            self.plan.literals.uses_helpers = True
            t = self.types.of(pin["type"])
            return f"DwoNative::GetProperty<{t}>(DwoNative::ClassDefault({self.input(node, 'Class')}), {cpp_string(pin_name)})"
        if cls == "K2Node_ClassDynamicCast" and node.get("pure"):
            if pin_name == "bSuccess":
                return f"({self.class_cast(node, pure=True)} != nullptr)"
            return self.class_cast(node, pure=True)
        if cls == "K2Node_SetFieldsInStruct" and pin_name == "StructOut":
            return self.lvalue(node, "StructRef")
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
            if node.get("memberAccess") == "private" and not self.plan.is_own_member(node["member"]):
                return self.private_read(node)
            return self.plan.var_cpp(node["member"], node)
        # another object's variable
        obj = self.input(node, "self") if target else "this"
        if not self.ex.type_info(owner).get("native") and not self.ex.is_self_class(owner):
            # a Blueprint class has no C++ type: read its property by name (a Blueprint variable is public)
            pin = next((p for p in node["pins"] if p["dir"] == "out" and p["type"].get("category") != "exec"), None)
            if pin is None:
                raise Unsupported(f"reads a variable of the Blueprint class {short(owner)}", node)
            t = self.native_object(pin) if self.types.is_object(pin["type"]) and pin["type"].get("category") == "object" else self.types.of(pin["type"])
            self.plan.literals.uses_helpers = True
            return f"DwoNative::GetProperty<{t}>({obj}, {cpp_string(node.get('memberName') or node['member'])})"
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
        member = node.get("memberName") or node["member"]
        if node.get("memberGetter"):
            member = node["memberGetter"] + "()"          # Blueprint reads it through its BlueprintGetter
        elif node.get("memberAccessor") and not node.get("memberSetter"):
            raise Unsupported(f"{node['member']} has a Blueprint getter", node)
        if obj == "this":
            return member
        pin = next((p for p in node["pins"] if p["dir"] == "out"), None)
        default = f"{self.types.of(pin['type'])}{{}}" if pin else "{}"
        t = self.types.of(pin["type"]) if pin else "UObject*"
        ot = self.types.of(target["type"]) if target and target["type"].get("object") else "UObject*"
        return f"[&]() -> {t} {{ {ot} O = {obj}; if (O) {{ return O->{member}; }} return {default_of(t)}; }}()"

    def private_read(self, node: dict) -> str:
        """A private C++ member of the native parent that Blueprint reads (AllowPrivateAccess): C++ cannot name it,
        so it is read through its property, as the Blueprint VM reads it."""
        pin = next((p for p in node["pins"] if p["dir"] == "out" and p["type"].get("category") != "exec"), None)
        if pin is None:
            raise Unsupported("a variable read without an output", node)
        t = self.types.of(pin["type"])
        name = cpp_string(node.get("memberName") or node["member"])
        owner = self.plan.parent_cpp
        if self.types.is_object(pin["type"]) and pin["type"].get("category") == "object":
            return (f"[&]() -> {t} {{ static const FObjectPropertyBase* P = FindFProperty<FObjectPropertyBase>({owner}::StaticClass(), {name}); "
                    f"return P ? Cast<{t[:-1]}>(P->GetObjectPropertyValue_InContainer(this)) : nullptr; }}()")
        return (f"[&]() -> {t} {{ static const FProperty* P = FindFProperty<FProperty>({owner}::StaticClass(), {name}); "
                f"return P ? *P->ContainerPtrToValuePtr<{t}>(this) : {default_of(t)}; }}()")

    def native_fn(self, node: dict) -> dict:
        key = strip_skel(f"{node.get('memberOwner', '')}:{node.get('member', '')}")
        f = self.ex.functions.get(key)
        if (not f or not f.get("native")) and self.ex.is_self_class(node.get("memberOwner", "")) and self.plan.parent_function(node.get("member", "")):
            # a self call to a function the native parent declares (an implementable event, BlueprintCallable)
            f = self.plan.parent_function(node.get("member", ""))
        if not f:
            if self.ex.is_self_class(node.get("memberOwner", "")):
                return {}
            raise Unsupported(f"no signature for {short(node.get('memberOwner'))}.{node.get('member')}", node)
        if f.get("customThunk"):
            raise Unsupported(f"{node['member']} is a Blueprint-only (CustomThunk) function", node)
        if f.get("internalOnly"):
            raise Unsupported(f"{node['member']} is internal to Blueprint (BlueprintInternalUseOnly)", node)
        if not f.get("native"):
            raise Unsupported(f"{short(node.get('memberOwner'))}.{node['member']} is a Blueprint function", node)
        return f

    def call_expr(self, node: dict, want_pin: str | None, pure: bool, latent: str | None = None) -> str:
        """The call; for a pure call with out params, the call is run into temporaries first (pre-statements).
        latent: the continuation function a latent call resumes (its FLatentActionInfo)."""
        owner = node.get("memberOwner", "")
        if self.ex.is_self_class(owner):
            item = self.plan.function_item(node.get("member", ""))
            inherited = self.plan.parent_function(node.get("member", ""))
            if not inherited or (item is not None and not item.extra.get("thunk")):
                return self.self_call(node, want_pin, pure)
        f = self.native_fn(node)
        if f.get("latent") and not latent:
            raise Unsupported(f"{node['member']} is latent and is not called as a statement", node)
        owner_cpp = self.types._class(f["owner"], False)
        flags = set(f.get("flags", []))
        enum_execs = {x.strip() for x in (f.get("expandEnumAsExecs") or "").split(",") if x.strip()}
        args, outs = [], {}
        ret = None
        for prm in f["params"]:
            name = prm["name"]
            if prm.get("return"):
                ret = prm
                continue
            if latent and (name == f.get("latentInfo") or prm["cpp"] == "FLatentActionInfo"):
                # same UUID the Blueprint compiler gives this node: GetTypeHash(NodeGuid)
                args.append(f'FLatentActionInfo(0, (int32)GetTypeHash(FGuid({guid_args(node["id"])})), TEXT("{latent}"), this)')
                continue
            if name in enum_execs and not prm.get("out"):
                entered = self.entered or ""
                if entered not in node["_pins"]:
                    raise Unsupported(f"entered through an unknown pin of {node.get('member')}", node)
                args.append(enum_exec_value(prm["cpp"], entered))
                continue
            if name in enum_execs:
                raise Unsupported(f"{node.get('member')} branches on its result (ExpandEnumAsExecs output)", node)
            if prm.get("out") and latent:
                raise Unsupported(f"latent {node.get('member')} has output values (written when it completes)", node)
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
            if f.get("defaultToSelf") == name and not (node["_pins"].get(name) or {}).get("links"):
                args.append("this")                 # DefaultToSelf: an unconnected pin is self, as in the Blueprint
                continue
            pin = node["_pins"].get(name)
            if pin is None:
                if prm.get("cppDefault") is not None:
                    break                     # trailing C++ defaults: stop, the rest take their defaults
                raise Unsupported(f"parameter {name} has no pin", node)
            if pin["type"].get("category") == "delegate":
                args.append(self.delegate_value(node, name, self._param_value_type(prm)))
            elif prm.get("ref") and not prm.get("const"):
                args.append(self.lvalue(node, name))
            elif prm["cpp"] == "float" and pin["type"].get("category") in ("real", "double", "float") and pin["type"].get("subCategory") != "float":
                args.append(f"(float)({self.input(node, name)})")      # the same narrowing the Blueprint does
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
                repl = REPLACED.get((short(f["owner"]), f["name"]))
                if repl:
                    self.types.includes.add(repl[1])
                    self.types.modules.add(repl[2])
                    call = f"{repl[0]}({', '.join(args)})"
                else:
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
            self.used_nodes.add(src["id"])
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
            # a bool index picks Option 0 for false and Option 1 for true
            when_false, when_true = options
            return f"(({idx}) ? {self.input(node, when_true['name'])} : {self.input(node, when_false['name'])})"
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
            if self.multi_entry(node):
                self.used_nodes.add(nid)
                self.pure_done = set()
                self.guarded = None
                node = self.statement(node)
                continue
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

    def set_other(self, node: dict, value_pin: dict):
        """Set on another object: a native property, through its BlueprintSetter when it has one (as Blueprint
        does); a None target is skipped."""
        owner = node.get("memberOwner", "")
        out = next((p for p in node["pins"] if p["dir"] == "out" and p["type"].get("category") != "exec" and p.get("links")), None)
        if out:
            raise Unsupported("reads the output of a set on another object", node)
        if not self.ex.type_info(owner).get("native"):
            if node.get("memberFlags") and {"RepNotify", "Net"} & set(node.get("memberFlags", [])):
                raise Unsupported("sets another object's replicated Blueprint variable", node)
            # a Blueprint class has no C++ type: set its property by name (no RepNotify; checked above)
            self.plan.literals.uses_helpers = True
            value = self.input(node, value_pin["name"])
            self.say(f"DwoNative::SetProperty({self.input(node, 'self')}, {cpp_string(node.get('memberName') or node['member'])}, {value});")
            return self.exec_target(node, "then")
        value = self.input(node, value_pin["name"])
        obj = self.guard_target(node, self.input(node, "self"))
        member = node.get("memberName") or node["member"]
        if node.get("memberSetter"):
            stmt = f"{obj}->{node['memberSetter']}({value});"
        elif node.get("memberAccess") in ("private", "protected") or node.get("memberAccessor") or node.get("memberReadOnly"):
            raise Unsupported(f"{member} cannot be set from C++ here", node)
        else:
            stmt = f"{obj}->{member} = {value};"
        self.say(f"if ({obj}) {{ {stmt} }}")
        return self.exec_target(node, "then")

    def array_value(self, node: dict, pin_name: str) -> str:
        """Blueprint array functions are generic (CustomThunk): the TArray member that does the same."""
        f = node.get("member", "")
        if f == "Array_Identical":
            return f"({self.input(node, 'ArrayA')} == {self.input(node, 'ArrayB')})"
        arr = self.input(node, "TargetArray")
        if f == "Array_Length":
            return f"({arr}).Num()"
        if f == "Array_LastIndex":
            return f"(({arr}).Num() - 1)"
        if f == "Array_IsEmpty":
            return f"({arr}).IsEmpty()"
        if f == "Array_IsNotEmpty":
            return f"!({arr}).IsEmpty()"
        if f == "Array_IsValidIndex":
            return f"({arr}).IsValidIndex({self.input(node, 'IndexToTest')})"
        if f == "Array_Contains":
            return f"({arr}).Contains({self.input(node, 'ItemToFind')})"
        if f == "Array_Find":
            return f"({arr}).Find({self.input(node, 'ItemToFind')})"
        if f == "Array_Get" and pin_name == "Item":
            t = self.types.of(node["_pins"]["Item"]["type"])
            # out of range: the Blueprint logs an error and returns the default value
            return f"[&]() -> {t} {{ const auto& A = {arr}; const int32 I = {self.input(node, 'Index')}; return A.IsValidIndex(I) ? A[I] : {default_of(t)}; }}()"
        raise Unsupported(f"array function {f} has no C++ translation yet", node)

    def array_statement(self, node: dict):
        f = node.get("member", "")
        arr = self.lvalue(node, "TargetArray")
        ret = node["_pins"].get("ReturnValue")
        target = None
        if ret and ret.get("links"):
            target = self.declare_temp(node, ret)
        if f in ("Array_Add", "Array_AddUnique"):
            call = f"({arr}).{'Add' if f == 'Array_Add' else 'AddUnique'}({self.input(node, 'NewItem')})"
        elif f == "Array_Clear":
            call = f"({arr}).Empty()"
        elif f == "Array_RemoveItem":
            call = f"(({arr}).Remove({self.input(node, 'Item')}) > 0)"
        elif f == "Array_Remove":
            self.say(f"if (({arr}).IsValidIndex({self.input(node, 'IndexToRemove')})) {{ ({arr}).RemoveAt({self.input(node, 'IndexToRemove')}); }}")
            return self.exec_target(node, "then")
        elif f in ("Array_Insert", "Array_Set", "Array_Swap", "Array_Append", "Array_Reverse", "Array_Resize"):
            # the checks KismetArrayLibrary's generic functions make (out of range: nothing happens)
            k = f"A_{node['id'][:8]}"
            self.say("{")
            self.depth += 1
            self.say(f"auto& {k} = {arr};")
            if f == "Array_Insert":
                self.say(f"const int32 I = {self.input(node, 'Index')};")
                self.say(f"if (I >= 0 && I <= {k}.Num()) {{ {k}.Insert({self.input(node, 'NewItem')}, I); }}")
            elif f == "Array_Set":
                self.say(f"const int32 I = {self.input(node, 'Index')};")
                self.say(f"if (!{k}.IsValidIndex(I) && ({self.input(node, 'bSizeToFit')}) && I >= 0) {{ {k}.SetNum(I + 1); }}")
                self.say(f"if ({k}.IsValidIndex(I)) {{ {k}[I] = {self.input(node, 'Item')}; }}")
            elif f == "Array_Swap":
                self.say(f"const int32 I = {self.input(node, 'FirstIndex')}, J = {self.input(node, 'SecondIndex')};")
                self.say(f"if (I != J && {k}.IsValidIndex(I) && {k}.IsValidIndex(J)) {{ {k}.Swap(I, J); }}")
            elif f == "Array_Append":
                self.say(f"{k}.Append({self.input(node, 'SourceArray')});")
            elif f == "Array_Reverse":
                self.types.includes.add("Algo/Reverse.h")
                self.say(f"Algo::Reverse({k});")
            elif f == "Array_Resize":
                self.say(f"const int32 N = {self.input(node, 'Size')};")
                self.say(f"if (N >= 0) {{ {k}.SetNum(N); }}")
            self.depth -= 1
            self.say("}")
            return self.exec_target(node, "then")
        else:
            raise Unsupported(f"array function {f} has no C++ translation yet", node)
        self.say(f"{target} = {call};" if target else f"{call};")
        return self.exec_target(node, "then")

    def statement(self, node: dict) -> dict | None:
        cls, kind = node["class"], node.get("kind")
        if cls == "K2Node_CallArrayFunction":
            return self.array_statement(node)
        if cls == "K2Node_CallFunction" and short(node.get("memberOwner", "")) == "BlueprintMapLibrary":
            return self.map_statement(node)
        if cls in ("K2Node_CallFunction", "K2Node_CallParentFunction", "K2Node_CommutativeAssociativeBinaryOperator", "K2Node_PromotableOperator"):
            if node.get("latent"):
                return self.latent_call(node)
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
            if node.get("memberAccess") == "private" and not self.plan.is_own_member(node["member"]):
                raise Unsupported(f"sets {node['member']}, which is private in C++", node)
            target = node["_pins"].get("self")
            value_pin = next(p for p in node["pins"] if p["dir"] == "in" and p["type"].get("category") != "exec" and p["name"] != "self")
            if target and target.get("links") and not self.ex.is_self_class(node.get("memberOwner", "")):
                return self.set_other(node, value_pin)
            if node.get("memberSetter") and not self.plan.is_own_member(node["member"]):
                self.say(f"{node['memberSetter']}({self.input(node, value_pin['name'])});")   # the parent's BlueprintSetter
                return self.exec_target(node, "then")
            var = self.plan.local_names.get(node["member"]) or self.plan.var_cpp(node["member"], node)
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
        if cls == "K2Node_Timeline":
            return self.timeline_input(node)
        if cls == "K2Node_Message":
            return self.message(node)
        if cls == "K2Node_MultiGate":
            return self.multigate(node)
        if cls in ("K2Node_AddDelegate", "K2Node_RemoveDelegate", "K2Node_ClearDelegate"):
            return self.delegate_statement(node)
        if cls == "K2Node_SpawnActorFromClass":
            return self.spawn_actor(node)
        if cls in ("K2Node_GenericCreateObject", "K2Node_ConstructObjectFromClass", "K2Node_CreateWidget"):
            return self.create_object(node)
        if cls == "K2Node_SetFieldsInStruct":
            return self.set_fields(node)
        if cls == "K2Node_ClassDynamicCast":
            return self.class_cast(node, pure=False)
        if cls == "K2Node_VariableGet":            # validated get: branches on IsValid
            self.say(f"if (IsValid({self.var_get(node)}))")
            self.block(self.exec_target(node, "then"))
            other = self.exec_target(node, "else")
            if other:
                self.say("else")
                self.block(other)
            return None
        raise Unsupported(f"node {cls.replace('K2Node_', '')} ({node.get('title', '').splitlines()[0]}) has no C++ translation yet", node)

    def message(self, node: dict):
        """An interface message: runs only when the target's class implements the interface, found by name as the
        Blueprint VM finds it (a Blueprint interface has no C++ type to call through)."""
        owner = strip_skel(node.get("memberOwner", ""))
        f = self.ex.functions.get(f"{owner}:{node.get('member', '')}")
        if not f:
            raise Unsupported(f"no signature for the interface function {node.get('member')}", node)
        self.plan.literals.uses_helpers = True
        target = self.input(node, "self")
        lines = [f'DwoNative::FCall Call({target}, DwoNative::FindClass({cpp_string(owner)}), {cpp_string(node["member"])});']
        outs = []
        for p in node["pins"]:
            if p["type"].get("category") in ("exec",) or p["name"] == "self" or p.get("splitFrom"):
                continue
            if p["dir"] == "in":
                v = self.input(node, p["name"])
                if p["type"].get("category") in ("byte", "enum") and not p["type"].get("container"):
                    v = f"static_cast<int64>({v})"
                lines.append(f'Call.Set({cpp_string(p["name"])}, {v});')
            else:
                outs.append(p)
        lines.append("Call.Run();")
        for p in outs:
            temp = self.declare_temp(node, p)
            t = self.types.of(p["type"])
            if p["type"].get("category") in ("byte", "enum") and not p["type"].get("container"):
                lines.append(f'{temp} = static_cast<{t}>(Call.Get<int64>({cpp_string(p["name"])}));')
            else:
                lines.append(f'{temp} = Call.Get<{t}>({cpp_string(p["name"])});')
        self.say("{")
        for l in lines:
            self.say("\t" + l)
        self.say("}")
        return self.exec_target(node, "then")

    def latent_call(self, node: dict):
        """Delay, MoveComponentTo ...: the call schedules the latent action; what follows Completed becomes its own
        UFUNCTION, which the latent action manager calls when the action finishes (as the Blueprint's resume point)."""
        if self.mode != "event":
            raise Unsupported("a latent action outside the event graph", node)
        if self.loop_depth:
            raise Unsupported("a latent action inside a loop (Completed would run once, after the loop)", node)
        ret_pin = node["_pins"].get("ReturnValue")
        if ret_pin and ret_pin.get("links"):
            raise Unsupported(f"the result of latent {node.get('member')} is used", node)
        entered = self.entered
        name = self.plan.continuation(self, node)
        self.entered = entered
        self.guarded = None
        call, _outs, _ret = self.call_expr(node, None, pure=False, latent=name)
        if self.guarded:
            self.say(f"if ({self.guarded}) {{ {call}; }}")
        else:
            self.say(f"{call};")
        return None

    def timeline_input(self, node: dict):
        """Play / Reverse / Stop ... on the timeline component (None: skipped, as the Blueprint does)."""
        member = self.plan.timeline_member(node)
        entered = self.entered or ""
        calls = {"Play": "Play()", "PlayFromStart": "PlayFromStart()", "Stop": "Stop()", "Reverse": "Reverse()",
                 "ReverseFromEnd": "ReverseFromEnd()"}
        if entered == "SetNewTime":
            call = f"SetNewTime((float)({self.input(node, 'NewTime')}))"
        elif entered in calls:
            call = calls[entered]
        else:
            raise Unsupported(f"timeline entered through '{entered}'", node)
        self.say(f"if ({member}) {{ {member}->{call}; }}")
        return None

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
            self.loop_depth += 1
            self.block(self.exec_target(node, "LoopBody"))
            self.loop_depth -= 1
            return self.exec_target(node, "Completed")
        if name in ("ForLoopWithBreak", "ForEachLoopWithBreak", "ReverseForEachLoop") and self.entered == "Break":
            # the macro's Break sets its flag; the loop body that reached here then ends, and the loop stops
            self.say(f"{self.break_flag(node)} = true;")
            return None
        if name == "ForLoopWithBreak":
            # StandardMacros: Index = First; while (Index <= Last && !Break) { body; if (Break) Completed; Index++ }
            var = f"Index_{node['id'][:8]}"
            flag = self.break_flag(node)
            self.loop_vars[(node["id"], "Index")] = var
            self.say(f"{flag} = false;")
            self.say(f"for (int32 {var} = {self.input(node, 'FirstIndex')}; {var} <= {self.input(node, 'LastIndex')}; ++{var})")
            self.say("{")
            self.depth += 1
            self.loop_depth += 1
            self.block(self.exec_target(node, "LoopBody"))
            self.loop_depth -= 1
            self.say(f"if ({flag}) {{ break; }}")
            self.depth -= 1
            self.say("}")
            return self.exec_target(node, "Completed")
        if name in ("ForEachLoopWithBreak", "ReverseForEachLoop"):
            var = f"Index_{node['id'][:8]}"
            arr = self.input(node, "Array")
            reverse = name == "ReverseForEachLoop"
            index_pin, element_pin = ("ArrayIndex", "ArrayElement") if reverse else ("Array Index", "Array Element")
            self.loop_vars[(node["id"], index_pin)] = var
            self.loop_vars[(node["id"], element_pin)] = f"({arr})[{var}]"
            if reverse:
                self.say(f"for (int32 {var} = ({arr}).Num() - 1; {var} >= 0; --{var})")
                self.loop_depth += 1
                self.block(self.exec_target(node, "LoopBody"))
                self.loop_depth -= 1
                return self.exec_target(node, "Completed")
            flag = self.break_flag(node)
            self.say(f"{flag} = false;")
            self.say(f"for (int32 {var} = 0; !{flag} && {var} < ({arr}).Num(); ++{var})")
            self.loop_depth += 1
            self.block(self.exec_target(node, "LoopBody"))
            self.loop_depth -= 1
            return self.exec_target(node, "Completed")
        if name == "Gate":
            # StandardMacros: the first entry through any pin sets Open from Start Closed; Enter runs Exit when open
            is_open = self.plan.state_member(node, "GateOpen", "bool", "false")
            init = self.plan.state_member(node, "GateInit", "bool", "false")
            self.say(f"if (!{init}) {{ {init} = true; {is_open} = !({self.input(node, 'bStartClosed')}); }}")
            entered = self.entered or "Enter"
            if entered == "Open":
                self.say(f"{is_open} = true;")
            elif entered == "Close":
                self.say(f"{is_open} = false;")
            elif entered == "Toggle":
                self.say(f"{is_open} = !{is_open};")
            elif entered == "Enter":
                self.say(f"if ({is_open})")
                self.block(self.exec_target(node, "Exit"))
            else:
                raise Unsupported(f"Gate entered through {entered}", node)
            return None
        if name == "Do N":
            # StandardMacros: Enter runs Exit while Counter < N (Counter counts up first); Reset sets Counter to 0
            count = self.plan.state_member(node, "DoNCounter", "int32", "0")
            self.loop_vars[(node["id"], "Counter")] = count
            if self.entered == "Reset":
                self.say(f"{count} = 0;")
                return None
            self.say(f"if ({count} < {self.input(node, 'n')})")
            self.say("{")
            self.depth += 1
            self.say(f"{count} = {count} + 1;")
            self.flow(self.exec_target(node, "Exit"))
            self.depth -= 1
            self.say("}")
            return None
        if name in ("ForEachLoop", "ForEachLoopWithBreak"):
            var = f"Index_{node['id'][:8]}"
            arr = self.input(node, "Array")
            self.loop_vars[(node["id"], "Array Index")] = var
            self.loop_vars[(node["id"], "Array Element")] = f"({arr})[{var}]"
            self.say(f"for (int32 {var} = 0; {var} < ({arr}).Num(); ++{var})")
            self.loop_depth += 1
            self.block(self.exec_target(node, "LoopBody"))
            self.loop_depth -= 1
            return self.exec_target(node, "Completed")
        if name == "WhileLoop":
            self.say(f"while ({self.input(node, 'Condition')})")
            self.loop_depth += 1
            self.block(self.exec_target(node, "LoopBody"))
            self.loop_depth -= 1
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
            # the macro's IsA starts false and flips before branching: A runs first, with IsA true
            state = self.plan.state_member(node, "FlipFlop", "bool", "false")
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
            # StandardMacros: Reset opens it; the first Execute closes it first if Start Closed; Execute runs
            # Completed once while open
            state = self.plan.state_member(node, "DoOnceDone", "bool", "false")
            if self.entered == "Reset":
                self.say(f"{state} = false;")
                return None
            sc = pins.get("Start Closed") or {}
            if sc.get("links") or (sc.get("default") or "false").lower() == "true":
                init = self.plan.state_member(node, "DoOnceInit", "bool", "false")
                self.say(f"if (!{init}) {{ {init} = true; if ({self.input(node, 'Start Closed')}) {{ {state} = true; }} }}")
            self.say(f"if (!{state})")
            self.say("{")
            self.depth += 1
            self.say(f"{state} = true;")
            self.flow(self.exec_target(node, "Completed"))
            self.depth -= 1
            self.say("}")
            return None
        raise Unsupported(f"standard macro '{name}' is not translated yet", node)

    def break_flag(self, node: dict) -> str:
        """A loop macro's Break flag (a macro local, kept in the frame between the loop and its Break pin)."""
        name = f"bBreak_{node['id'][:8]}"
        if self.mode == "event":
            self.plan.state_members[name] = ("bool", "false")
        else:
            self.locals.setdefault(name, ("bool", "false"))
        return name

    def multigate(self, node: dict):
        """K2Node_MultiGate as the Blueprint compiler builds it (FKCHandler_MultiGate): a bit per output already run,
        the first entry starts at Start Index, Loop clears the bits when all ran."""
        self.types.includes.add("Kismet/KismetNodeHelperLibrary.h")
        data = self.plan.state_member(node, "MultiGateData", "int32", "0")
        if self.entered == "Reset":
            self.say(f"{data} = 0;")
            return None
        first = self.plan.state_member(node, "MultiGateStarted", "bool", "false")
        outs = [p["name"] for p in node["pins"] if p["dir"] == "out" and p["type"].get("category") == "exec"]
        n = len(outs)
        index, go = f"GateIndex_{node['id'][:8]}", f"bGateGo_{node['id'][:8]}"
        start, rnd, loop = self.input(node, "StartIndex"), self.input(node, "IsRandom"), self.input(node, "Loop")
        lib = "UKismetNodeHelperLibrary"
        self.say("{")
        self.depth += 1
        self.say(f"int32 {index} = -1;")
        self.say(f"bool {go} = true;")
        self.say(f"if (!{first}) {{ {first} = true; {index} = ({start}) > -1 ? ({start}) : {lib}::GetUnmarkedBit({data}, {start}, {n}, {rnd}); }}")
        self.say(f"else if ({lib}::HasUnmarkedBit({data}, {n})) {{ {index} = {lib}::GetUnmarkedBit({data}, {start}, {n}, {rnd}); }}")
        self.say(f"else if ({loop}) {{ {data} = 0; {index} = {lib}::GetUnmarkedBit({data}, {start}, {n}, {rnd}); }}")
        self.say(f"else {{ {go} = false; }}")
        self.say(f"if ({go})")
        self.say("{")
        self.depth += 1
        self.say(f"{lib}::MarkBit({data}, {index});")
        first_branch = True
        for i, pin in enumerate(outs):
            target = self.exec_target(node, pin)
            if not target:
                continue
            self.say(f"{'if' if first_branch else 'else if'} ({index} == {i})")
            self.block(target)
            first_branch = False
        self.depth -= 1
        self.say("}")
        self.depth -= 1
        self.say("}")
        return None

    # -- data and object nodes
    def native_object(self, pin: dict) -> str:
        """An object pin's C++ pointer type; a Blueprint class becomes its nearest native parent."""
        path = pin["type"].get("object", "")
        seen = set()
        while path and not self.ex.is_self_class(path) and path not in seen:
            seen.add(path)
            t = self.ex.type_info(path)
            if t.get("native"):
                break
            path = t.get("nativeParent") if self.ex.type_info(t.get("nativeParent", "")).get("native") else t.get("super", "")
        return self.types._class(path, True) + "*" if path else "UObject*"      # header: event temporaries are members

    def enum_object(self, pin: dict) -> str:
        path = pin["type"].get("object", "")
        if not path:
            raise Unsupported("an enum pin without its enum", None)
        self.plan.literals.uses_helpers = True
        return f"DwoNative::FindEnum({cpp_string(path)})"

    def delegate_value(self, node: dict, pin_name: str, cpp_type: str) -> str:
        """A delegate input: a Create Event node or a custom event's delegate output, bound by function name the way
        the Blueprint VM binds it (the function stays findable by that name, migrated or not)."""
        obj, fn = self.delegate_source(node, pin_name)
        return f"[&]() {{ {cpp_type} D; D.BindUFunction({obj}, FName({cpp_string(fn)})); return D; }}()"

    def delegate_source(self, node: dict, pin_name: str) -> tuple[str, str]:
        p = node["_pins"].get(pin_name) or {}
        links = p.get("links") or []
        if not links:
            raise Unsupported(f"delegate pin {pin_name} is not connected", node)
        src = self.node(links[0]["node"])
        self.used_nodes.add(src["id"])
        if src["class"] == "K2Node_CreateDelegate":
            obj = self.input(src, "self") if "self" in src["_pins"] else "this"
            fn = src.get("member") or ""
            item = self.plan.funcs.get(fn) if obj == "this" else None
            return obj, (item.cpp if item and item.migrated else fn)
        if src["class"] == "K2Node_CustomEvent" and links[0]["pin"] == "OutputDelegate":
            item = self.plan.funcs.get(src.get("member", ""))
            return "this", (item.cpp if item and item.migrated else src.get("member", ""))
        raise Unsupported(f"delegate from {src['class'].replace('K2Node_', '')}", node)

    def delegate_property(self, node: dict) -> str:
        member = node.get("member", "")
        if self.ex.is_self_class(node.get("memberOwner", "")):
            d = self.plan.dispatchers.get(member) if hasattr(self.plan, "dispatchers") else None
            if d is not None and not d.migrated:
                raise Unsupported(f"dispatcher {member} stays in Blueprint", node)
            return d.cpp if d is not None else member
        return member

    def delegate_statement(self, node: dict):
        """Bind / Unbind / Unbind all events on a dispatcher, through the delegate property (as the VM does, so a
        Blueprint class's dispatcher works too)."""
        self.plan.literals.uses_helpers = True
        cls = node["class"]
        target = self.input(node, "self") if "self" in node["_pins"] else "this"
        prop = cpp_string(self.delegate_property(node))
        if cls == "K2Node_ClearDelegate":
            self.say(f"DwoNative::ClearByName({target}, {prop});")
        else:
            obj, fn = self.delegate_source(node, "Delegate")
            helper = "AddDynamicByName" if cls == "K2Node_AddDelegate" else "RemoveDynamicByName"
            self.say(f"DwoNative::{helper}({target}, {prop}, {obj}, {cpp_string(fn)});")
        return self.exec_target(node, "then")

    def spawn_actor(self, node: dict):
        """SpawnActorFromClass as the compiler expands it: BeginDeferredActorSpawnFromClass, the exposed properties,
        FinishSpawningActor."""
        self.types.includes.add("Kismet/GameplayStatics.h")
        pins = node["_pins"]
        standard = {"execute", "then", "Class", "ReturnValue", "SpawnTransform", "CollisionHandlingOverride",
                    "TransformScaleMethod", "Owner", "Instigator", "WorldContextObject"}
        ret = pins["ReturnValue"]
        t = self.native_object(ret)
        temp = self.object_temp(node, ret, t)
        xf, began = f"SpawnXf_{node['id'][:8]}", f"Begun_{node['id'][:8]}"
        scale = self.input(node, "TransformScaleMethod") if "TransformScaleMethod" in pins else "ESpawnActorScaleMethod::MultiplyWithRoot"
        self.say("{")
        self.depth += 1
        self.say(f"const FTransform {xf} = {self.input(node, 'SpawnTransform')};")
        self.say(f"AActor* {began} = UGameplayStatics::BeginDeferredActorSpawnFromClass(this, {self.input(node, 'Class')}, {xf}, "
                 f"{self.input(node, 'CollisionHandlingOverride')}, {self.input(node, 'Owner')}, {scale});")
        self.assign_exposed(node, began, standard)
        self.say(f"{temp} = Cast<{t[:-1]}>(UGameplayStatics::FinishSpawningActor({began}, {xf}, {scale}));")
        self.depth -= 1
        self.say("}")
        return self.exec_target(node, "then")

    def create_object(self, node: dict):
        pins = node["_pins"]
        ret = pins["ReturnValue"]
        t = self.native_object(ret)
        temp = self.object_temp(node, ret, t)
        if node["class"] == "K2Node_CreateWidget":
            self.types.includes.add("Blueprint/WidgetBlueprintLibrary.h")
            self.types.modules.add("UMG")
            owner = self.input(node, "OwningPlayer") if "OwningPlayer" in pins else "nullptr"
            self.say(f"{temp} = Cast<{t[:-1]}>(UWidgetBlueprintLibrary::Create(this, {self.input(node, 'Class')}, {owner}));")
        else:
            self.types.includes.add("Kismet/GameplayStatics.h")
            outer = self.input(node, "Outer") if "Outer" in pins else self.input(node, "self") if "self" in pins else "this"
            self.say(f"{temp} = Cast<{t[:-1]}>(UGameplayStatics::SpawnObject({self.input(node, 'Class')}, {outer}));")
        self.assign_exposed(node, temp, {"execute", "then", "Class", "ReturnValue", "OwningPlayer", "Outer", "self", "WorldContextObject"})
        return self.exec_target(node, "then")

    def assign_exposed(self, node: dict, obj: str, standard: set):
        """Exposed-on-spawn pins, as FKismetCompilerUtilities::GenerateAssignmentNodes writes them (after the object
        exists, before FinishSpawning): only the pins the export marks (linked, or a default other than the class
        default), through the property's BlueprintSetter, else set by name like Set*PropertyByName (no RepNotify)."""
        exposed = node.get("exposed")
        if exposed is None:
            extra = [p for p in node["pins"] if p["dir"] == "in" and p["name"] not in standard and p["type"].get("category") != "exec"
                     and (p.get("links") or str(p.get("default", "")) != str(p.get("autoDefault", "")))]
            if extra:
                raise Unsupported("exposed-on-spawn pins: export again with the current DwoLens", node)
            return
        for e in exposed:
            if not e.get("assign"):
                continue
            if e.get("fieldNotify"):
                raise Unsupported(f"exposed property {e['pin']} broadcasts a field notification", node)
            value = self.input(node, e["pin"])
            setter = e.get("setter")
            if setter:
                owner = self.ex.type_info(e.get("owner", ""))
                if not owner.get("native"):
                    raise Unsupported(f"exposed property {e['pin']} has a Blueprint setter", node)
                cls = self.types._class(e["owner"], False)
                self.say(f"if ({cls}* O = Cast<{cls}>({obj})) {{ O->{setter}({value}); }}")
            else:
                self.plan.literals.uses_helpers = True
                self.say(f"DwoNative::SetProperty({obj}, {cpp_string(e['pin'])}, {value});")

    def object_temp(self, node: dict, pin: dict, t: str) -> str:
        name = self.temp_name(node, pin["name"])
        if self.mode == "event":
            self.members[name] = (f"TObjectPtr<{t[:-1]}>", True)
        else:
            self.locals[name] = (t, "nullptr")
        return name

    def set_fields(self, node: dict):
        target = self.lvalue(node, "StructRef")
        for p in node["pins"]:
            if p["dir"] != "in" or p["name"] in ("execute", "StructRef") or p["type"].get("category") == "exec" or p.get("splitFrom"):
                continue
            self.say(f"({target}).{p['name']} = {self.input(node, p['name'])};")
        return self.exec_target(node, "then")

    def class_cast(self, node: dict, pure: bool):
        target = self.types._class(node["member"], False)
        cls = self.input(node, "Class")
        as_pin = next(p for p in node["pins"] if p["dir"] == "out" and p["type"].get("category") == "class")
        expr = f"[&]() -> TSubclassOf<{target}> {{ UClass* C = {cls}; return C && C->IsChildOf({target}::StaticClass()) ? C : nullptr; }}()"
        if pure:
            return expr
        temp = self.declare_temp(node, as_pin)
        ok = self.declare_temp(node, node["_pins"]["bSuccess"])
        self.say(f"{temp} = {expr};")
        self.say(f"{ok} = {temp} != nullptr;")
        self.say(f"if ({ok})")
        self.block(self.exec_target(node, "then"))
        fail = self.exec_target(node, "CastFailed")
        if fail:
            self.say("else")
            self.block(fail)
        return None

    def map_value(self, node: dict, pin_name: str) -> str:
        f = node.get("member", "")
        m = self.input(node, "TargetMap")
        if f == "Map_Length":
            return f"({m}).Num()"
        if f == "Map_Contains":
            return f"({m}).Contains({self.input(node, 'Key')})"
        if f == "Map_Find":
            if pin_name == "ReturnValue":
                return f"({m}).Contains({self.input(node, 'Key')})"
            t = self.types.of(node["_pins"]["Value"]["type"])
            return f"[&]() -> {t} {{ const auto* V = ({m}).Find({self.input(node, 'Key')}); return V ? *V : {default_of(t)}; }}()"
        if f in ("Map_Keys", "Map_Values"):
            t = self.types.of(node["_pins"][pin_name]["type"])
            fn = "GenerateKeyArray" if f == "Map_Keys" else "GenerateValueArray"
            return f"[&]() {{ {t} A; ({m}).{fn}(A); return A; }}()"
        raise Unsupported(f"map function {f} has no C++ translation yet", node)

    def map_statement(self, node: dict):
        f = node.get("member", "")
        m = self.lvalue(node, "TargetMap")
        ret = node["_pins"].get("ReturnValue")
        if f == "Map_Add":
            self.say(f"({m}).Add({self.input(node, 'Key')}, {self.input(node, 'Value')});")
        elif f == "Map_Remove":
            call = f"(({m}).Remove({self.input(node, 'Key')}) > 0)"
            self.say(f"{self.declare_temp(node, ret)} = {call};" if ret and ret.get("links") else f"{call};")
        elif f == "Map_Clear":
            self.say(f"({m}).Empty();")
        else:
            raise Unsupported(f"map function {f} has no C++ translation yet", node)
        return self.exec_target(node, "then")

    def translate(self, start: dict | None):
        self.flow(start)
        # a node several flows run into gets a label; one no goto in this function uses (the other flows are other
        # events) would be an unreferenced label, an error with warnings as errors
        used = {m for line in self.lines for m in re.findall(r"goto (L_\w+);", line)}
        self.lines = [line for line in self.lines if not (re.fullmatch(r"L_\w+:", line.strip()) and line.strip()[:-1] not in used)]
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
        if "DwoNative/" in self.parent.get("include", "").replace("\\", "/"):
            # regenerating from a rebased Blueprint would build C++ from what is left in it and overwrite the class
            raise SystemExit(f"{ex.name} is already rebased onto {self.parent_cpp} (generated C++). "
                             "Restore it first (__dwookit.bat native restore) to migrate it again.")
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
        self.timelines: dict[str, Item] = {}
        self.continuations: dict[str, Fn | None] = {}   # latent node id -> its continuation (None while translating)
        self.shared_args: dict[tuple[str, str], str] = {}   # (event node, pin) another event reads -> member
        self.shared_changed = False

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
        # the native parent may be a project class: walk its super classes as the export describes them
        path, seen = self.parent_path, set()
        while path and path not in seen:
            seen.add(path)
            out.append(short(path))
            out += known.get(short(path), [])
            path = self.ex.type_info(path).get("super", "")
        return list(dict.fromkeys(out))

    def shared_arg(self, event: dict, pin: dict) -> str:
        """An event argument other events read: a member the event copies it to when it runs (registered here; the
        next translation pass writes the copy)."""
        if pin.get("splitFrom"):
            return f"({self.shared_arg(event, event['_pins'][pin['splitFrom']])}).{pin['name'][len(pin['splitFrom']) + 1:]}"
        key = (event["id"], pin["name"])
        if key not in self.shared_args:
            self.shared_args[key] = f"EvArg_{event['id'][:8]}_{ident(pin['name'])}"
            self.shared_changed = True
        return self.shared_args[key]

    def parent_function(self, name: str) -> dict | None:
        """A function the native parent (or an ancestor) declares under this name."""
        for f in self.ex.functions.values():
            if f.get("name") == name and not self.ex.is_self_class(f.get("owner", "")) and self.inherits(f.get("owner", "")):
                return f
        return None

    def input_host(self) -> str | None:
        anc = set(self.parent_ancestors())
        if "PlayerController" in anc:
            return "controller"
        if "Pawn" in anc:
            return "pawn"
        if self.kind == "actor":
            return "actor"
        return None

    def continuation(self, fn: Fn, node: dict) -> str:
        """The UFUNCTION a latent node resumes: the flow after its Completed pin, translated once."""
        key = node["id"]
        name = f"Latent_{key[:8]}"
        fn.conts.add(key)
        if key not in self.continuations:
            self.continuations[key] = None
            root = fn.root_fn
            cont = Fn(self, fn.graph, root.root, "event", root.arg_params() if root is fn else fn.arg_params())
            cont.root_fn = root
            try:
                cont.translate(cont.exec_target(node, "then"))
            except Unsupported:
                del self.continuations[key]
                raise
            self.continuations[key] = cont
        return name

    def cont_closure(self, keys: set[str]) -> set[str]:
        out, todo = set(), list(keys)
        while todo:
            k = todo.pop()
            if k in out:
                continue
            out.add(k)
            c = self.continuations.get(k)
            if c:
                todo += list(c.conts)
        return out

    def timeline_item(self, node: dict) -> Item:
        item = self.timelines.get(node.get("member", ""))
        if item is None:
            raise Unsupported(f"timeline {node.get('member')} is not in the export (export again)", node)
        if not item.migrated:
            raise Unsupported(f"timeline {item.bp} stays in Blueprint ({item.reason})", node)
        return item

    def timeline_member(self, node: dict) -> str:
        return self.timeline_item(node).cpp

    def timeline_value(self, node: dict, pin: str) -> str:
        item = self.timeline_item(node)
        if pin == "Direction":
            return item.extra["direction"]
        for tr in item.extra["tracks"]:
            if tr["name"] == pin and tr["type"] != "event":
                return tr["member"]
        raise Unsupported(f"timeline output {pin}", node)

    def var_cpp(self, bp_name: str, node=None) -> str:
        t = self.timelines.get(bp_name)
        if t is not None:
            if not t.migrated:
                raise Unsupported(f"uses the timeline '{bp_name}' that stays in Blueprint ({t.reason})", node)
            return t.cpp
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

    def is_own_member(self, bp_name: str) -> bool:
        return bp_name in self.vars or bp_name in self.components or bp_name in self.widgets or bp_name in self.timelines

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
        name = f"{'b' if t == 'bool' else ''}{what}_{node['id'][:8]}"
        self.state_members[name] = (t, init)
        return name

    # -- planning
    def build(self):
        self.plan_variables()
        self.plan_components()
        self.plan_widgets()
        self.plan_dispatchers()
        self.plan_timelines()
        self.plan_functions()
        # fixed point: a function may only be migrated if everything it uses is
        for _ in range(8):
            self.shared_changed = False
            changed = self.translate_all()
            if not changed and not self.shared_changed:
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
            if not t:
                # a widget Blueprint: BindWidget through its native parent class (the Blueprint compiler accepts a
                # subclass); its own dispatchers are bound by name
                bp = next((k for k, v in self.ex.types.items() if short(k) == w["class"] and not v.get("native")), None)
                np = self.ex.type_info(self.ex.type_info(bp).get("nativeParent", "")) if bp else {}
                if np.get("native") and np.get("cpp"):
                    t = np
                    item.extra["bpClass"] = bp
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

    def plan_timelines(self):
        for t in self.ex.doc.get("timelines", []):
            item = Item(t["name"], ident(t["name"]), extra={"template": t})
            node = next((n for g in self.ex.graphs if g["kind"] == "event" for n in g["nodes"]
                         if n["class"] == "K2Node_Timeline" and n.get("member") == t["name"]), None)
            taken = self.ex.parent_members | {v.cpp for v in self.vars.values()} | {c.cpp for c in self.components.values()}
            if self.kind != "actor":
                item.reason = "timelines only run in actors"
            elif "detail" not in t:
                item.reason = "the export has no timeline curves (export again with the current DwoLens)"
            elif node is None:
                item.reason = "no timeline node uses it"
            elif item.cpp in taken:
                item.reason = f"the name {item.cpp} is already taken"
            else:
                item.migrated = True
                item.extra["node"] = node
                item.extra["direction"] = f"{item.cpp}_Direction"
                item.extra["tracks"] = []
                for tr in t["detail"]:
                    entry = dict(tr)
                    if tr["type"] == "event":
                        entry["function"] = f"{item.cpp}__{ident(tr['name'])}"
                    else:
                        entry["member"] = f"{item.cpp}_{ident(tr['name'])}"
                        if tr.get("externalCurve"):
                            cls = {"float": "UCurveFloat", "vector": "UCurveVector", "color": "UCurveLinearColor"}[tr["type"]]
                            entry["literal"] = (f"TObjectPtr<{cls}>", tr["externalCurve"])
                    item.extra["tracks"].append(entry)
                item.extra["update"] = f"{item.cpp}__Update"
                item.extra["finished"] = f"{item.cpp}__Finished"
            self.timelines[t["name"]] = item

    def plan_interface_function(self, g: dict):
        """A Blueprint interface function with outputs, implemented as a graph. C++ cannot implement a Blueprint
        interface, so the body moves to <Name>Native (outputs as out parameters) and the interface graph keeps only
        entry -> call -> result (rebase writes that call), as for an implementable event."""
        entry = next((n for n in g["nodes"] if n["class"] == "K2Node_FunctionEntry"), None)
        if not entry:
            return
        item = Item(g["name"], f"{ident(g['name'])}Native", extra={"graph": g, "entry": entry, "interface": g.get("interface", "")})
        try:
            flags = set(entry.get("functionFlags", []))
            if flags & {"Net", "Server", "Client", "Multicast"}:
                raise Unsupported("replicated functions are not translated yet")
            if item.cpp in self.ex.parent_members:
                raise Unsupported(f"the name {item.cpp} is already taken")
            params, decls = [], []
            for p in entry["pins"]:
                if p["dir"] == "out" and p["type"].get("category") != "exec" and not p.get("splitFrom"):
                    t = self.types.of(p["type"])
                    params.append((p["name"], ident(p["name"]), False, t))
                    decls.append(f"{t} {ident(p['name'])}" if t in ("bool", "int32", "int64", "double", "float", "uint8") or t.endswith("*")
                                 else f"const {t}& {ident(p['name'])}")
            result = next((n for n in g["nodes"] if n["class"] == "K2Node_FunctionResult"), None)
            for p in (result or {}).get("pins", []):
                if p["dir"] == "in" and p["type"].get("category") != "exec" and not p.get("splitFrom"):
                    t = self.types.of(p["type"])
                    params.append((p["name"], ident(p["name"]), True, t))
                    decls.append(f"{t}& {ident(p['name'])}")
            item.extra.update(params=params, decls=decls, thunk=True, flags=sorted(flags), **{"return": None})
            item.migrated = True
        except Unsupported as e:
            item.reason = e.reason
        self.funcs[g["name"]] = item

    def plan_functions(self):
        for g in self.ex.graphs:
            if g["kind"] == "interface":
                self.plan_interface_function(g)
                continue
            if g["kind"] != "function":
                continue
            entry = next((n for n in g["nodes"] if n["class"] == "K2Node_FunctionEntry"), None)
            if not entry:
                continue
            item = Item(g["name"], ident(g["name"]), extra={"graph": g, "entry": entry})
            if item.cpp in self.ex.parent_members and g["name"] != "UserConstructionScript":
                nf = self.parent_function(g["name"])
                flags = set((nf or {}).get("flags", []))
                if not nf or "BlueprintEvent" not in flags:
                    item.reason = f"the native parent already has a member named {item.cpp}"
                    self.funcs[g["name"]] = item
                    continue
                try:
                    self.plan_override_function(g, entry, item, nf)
                except Unsupported as e:
                    item.migrated, item.reason = False, e.reason
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

    def plan_override_function(self, g: dict, entry: dict, item: Item, nf: dict):
        """A BlueprintNativeEvent / BlueprintImplementableEvent the Blueprint implements as a function (it has outputs).
        Native event: becomes the C++ _Implementation. Implementable event: C++ cannot override it, so the body moves to
        a C++ function and the Blueprint function keeps only a call to it (rebase writes that call)."""
        native = "Native" in nf.get("flags", [])
        params, ret = [], None
        decls = []
        for prm in nf["params"]:
            if prm.get("return"):
                ret = prm["cpp"]
                continue
            t = prm["cpp"]
            params.append((prm["name"], prm["name"], bool(prm.get("out")), t))
            decls.append(param_decl(prm, enum_as_byte=not native))
        item.extra.update(params=params, decls=decls, flags=sorted(set(entry.get("functionFlags", []))),
                          **{"return": ret})
        if native:
            item.extra["impl"] = f"{g['name']}_Implementation"
            item.extra["bne"] = True
            item.cpp = g["name"]                 # the Blueprint calls keep calling the UFUNCTION
        else:
            item.extra["thunk"] = True
            item.cpp = f"{g['name']}Native"
            if item.cpp in self.ex.parent_members:
                raise Unsupported(f"the name {item.cpp} is already taken")
        item.migrated = True

    def event_roots(self) -> list[tuple[dict, dict]]:
        out = []
        for g in self.ex.graphs:
            if g["kind"] != "event":
                continue
            for n in g["nodes"]:
                if n["class"] in ("K2Node_Event", "K2Node_CustomEvent", "K2Node_ComponentBoundEvent", "K2Node_EnhancedInputAction",
                                  "K2Node_InputAction", "K2Node_InputKey", "K2Node_WidgetAnimationEvent", "K2Node_InputAxisEvent",
                                  "K2Node_InputAxisKeyEvent", "K2Node_InputTouch", "K2Node_InputVectorAxisEvent"):
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
        self.continuations.clear()
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
        # timelines: their Update / Finished / event track outputs are functions the timeline component calls
        for name, item in self.timelines.items():
            if not item.migrated:
                continue
            try:
                self.translate_timeline(item)
            except Unsupported as e:
                item.migrated = False
                item.reason = e.reason + (f" [node {e.node['id'][:8]} {e.node.get('title', '').splitlines()[0]}]" if e.node else "")
                changed = True
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
        # a timeline moves only with everything that drives it: code that stays in Blueprint could not reach it
        moved = set()
        for e in events:
            if e.migrated:
                moved |= set(e.extra.get("nodes", []))
        for t in self.timelines.values():
            if t.migrated:
                moved |= set(t.extra.get("nodes", []))
        # a widget Blueprint member becomes the native parent's type: Blueprint code that stays would see that type
        for f in self.funcs.values():
            if f.migrated:
                moved |= set(f.extra.get("nodes", []))
        for w in self.widgets.values():
            if not (w.migrated and w.extra.get("bpClass")):
                continue
            for g in self.ex.graphs:
                for n in g["nodes"]:
                    if n["class"] in ("K2Node_VariableGet", "K2Node_VariableSet") and n.get("member") == w.bp and n["id"] not in moved:
                        w.migrated = False
                        w.reason = f"Blueprint code that stays uses it as {w.extra['class']} ('{g['name']}')"
                        changed = True
        for t in self.timelines.values():
            if not t.migrated:
                continue
            tl = t.extra["node"]
            g = self.ex.node_graph[tl["id"]]
            for n in g["nodes"]:
                for p in n.get("pins", []):
                    if p["dir"] == "out" and p["type"].get("category") == "exec" and any(l["node"] == tl["id"] for l in p.get("links", [])):
                        if n["id"] not in moved:
                            t.migrated = False
                            t.reason = f"driven by '{n.get('title', '').splitlines()[0]}', which stays in Blueprint"
                            changed = True
        # a custom event other code calls must be migrated for those callers to stay migrated: handled by the
        # caller translation (self_call looks custom events up in funcs)
        return changed

    def translate_timeline(self, item: Item):
        node = item.extra["node"]
        g = self.ex.node_graph[node["id"]]
        used, conts = {node["id"]}, set()
        outs = [("Update", item.extra["update"]), ("Finished", item.extra["finished"])]
        outs += [(tr["name"], tr["function"]) for tr in item.extra["tracks"] if tr["type"] == "event"]
        item.extra["functions"] = []
        for pin, fname in outs:
            fn = Fn(self, g, node, "event", {})
            fn.translate(fn.exec_target(node, pin) if node["_pins"].get(pin) else None)
            used |= fn.used_nodes
            conts |= fn.conts
            for k, v in fn.members.items():
                self.event_members[k] = v
            self.bodies[f"tl:{item.bp}:{pin}"] = fn
            item.extra["functions"].append((fname, f"tl:{item.bp}:{pin}"))
        for k in self.cont_closure(conts):
            c = self.continuations.get(k)
            if c:
                used |= c.used_nodes
                self.event_members.update(c.members)
        item.extra["continuations"] = sorted(self.cont_closure(conts))
        item.extra["nodes"] = sorted(used)
        for tr in item.extra["tracks"]:
            if tr.get("literal"):
                tr["literalCpp"] = self.literal_raw(*tr["literal"])

    def literal_raw(self, cpp_type: str, path: str) -> str:
        if path not in self.literal_props:
            self.literal_props[path] = {"cpp": f"NativeLiteral_{len(self.literal_props)}", "type": cpp_type, "path": path}
        return self.literal_props[path]["cpp"]

    def translate_input(self, g: dict, n: dict, item: Item):
        """Input events: one UFUNCTION per exec output, bound where the engine binds the Blueprint's input delegates
        (after SetupPlayerInputComponent / SetupInputComponent, or when EnableInput creates the input component)."""
        cls = n["class"]
        host = self.input_host()
        if host is None:
            raise Unsupported("input events outside an actor")
        props = n.get("props")
        if props is None:
            raise Unsupported("the export has no input node settings (export again with the current DwoLens)")
        nid8 = n["id"][:8]
        parts, bindings = [], []
        shared: dict = {}
        if cls in ("K2Node_InputAction", "K2Node_InputKey"):
            key_pin = n["_pins"]["Key"]
            member = f"InKey_{nid8}"
            self.event_members[member] = (self.types.of(key_pin["type"], header=True), False)
            self.types.header_includes.add("InputCoreTypes.h")
            self.types.modules.add("InputCore")
            shared[(n["id"], "Key")] = member
            if cls == "K2Node_InputAction":
                label = ident(props.get("InputActionName", ""))
                what = {"kind": "action", "name": props.get("InputActionName", "")}
            else:
                key = props.get("InputKey", "")
                key = re.sub(r"^\(KeyName=\"?([^\")]+)\"?\)$", r"\1", key)
                label = ident(key)
                what = {"kind": "key", "key": key, "shift": prop_bool(props, "bShift"), "ctrl": prop_bool(props, "bControl"),
                        "alt": prop_bool(props, "bAlt"), "cmd": prop_bool(props, "bCommand")}
                self.types.modules |= {"Slate", "SlateCore"}
            for pin, evt in (("Pressed", "IE_Pressed"), ("Released", "IE_Released")):
                if not n["_pins"].get(pin, {}).get("links"):
                    continue
                fname = f"Inp{'Act' if cls == 'K2Node_InputAction' else 'Key'}_{label}_{pin}_{nid8}"
                parts.append({"pin": pin, "function": fname, "signature": f"void {fname}(FKey Key)", "prologue": [f"\t{member} = Key;"]})
                bindings.append(dict(what, event=evt, function=fname, consume=prop_bool(props, "bConsumeInput", True),
                                     paused=prop_bool(props, "bExecuteWhenPaused"), override=prop_bool(props, "bOverrideParentBinding", True)))
        elif cls == "K2Node_InputAxisEvent":
            fname = f"InpAxis_{ident(props.get('InputAxisName', ''))}_{nid8}"
            shared[(n["id"], "AxisValue")] = "AxisValue"
            parts.append({"pin": "then", "function": fname, "signature": f"void {fname}(float AxisValue)", "prologue": []})
            bindings.append({"kind": "axis", "name": props.get("InputAxisName", ""), "function": fname,
                             "consume": prop_bool(props, "bConsumeInput", True), "paused": prop_bool(props, "bExecuteWhenPaused"),
                             "override": prop_bool(props, "bOverrideParentBinding", True)})
        elif cls == "K2Node_EnhancedInputAction":
            path = prop_path(props.get("InputAction", ""))
            if not path or path == "None":
                raise Unsupported("Enhanced Input event without an input action")
            ia_pin = n["_pins"].get("InputAction")
            if not ia_pin:
                raise Unsupported("Enhanced Input event without its InputAction pin")
            literal = self.literal_property(ia_pin["type"], path)
            self.types.modules.add("EnhancedInput")
            self.types.includes |= {"EnhancedInputComponent.h", "InputAction.h"}
            self.types.header_includes.add("InputActionValue.h")
            value_pin = n["_pins"]["ActionValue"]
            vt = value_pin["type"]
            get_t = {"bool": "bool", "real": "float", "double": "float", "float": "float"}.get(vt.get("category"))
            if vt.get("category") == "struct":
                get_t = {"Vector2D": "FVector2D", "Vector": "FVector"}.get(short(vt.get("object", "")))
            if not get_t:
                raise Unsupported(f"Enhanced Input value of type {vt.get('category')}")
            members = {}
            for pin_name, expr in (("ActionValue", f"ActionValue.Get<{get_t}>()"), ("ElapsedSeconds", "ElapsedTime"),
                                   ("TriggeredSeconds", "TriggeredTime"), ("InputAction", "const_cast<UInputAction*>(SourceAction)")):
                p = n["_pins"][pin_name]
                m = f"EIA_{nid8}_{pin_name}"
                is_obj = self.types.is_object(p["type"])
                self.event_members[m] = (self.types.of(p["type"], header=True, as_property=is_obj), is_obj)
                shared[(n["id"], pin_name)] = m
                members[m] = expr
            prologue = [f"\t{m} = {e};" for m, e in members.items()]
            for p in n["pins"]:
                if p["dir"] != "out" or p["type"].get("category") != "exec" or not p.get("links"):
                    continue
                fname = f"InpAct_{ident(short(path))}_{p['name']}_{nid8}"
                parts.append({"pin": p["name"], "function": fname,
                              "signature": f"void {fname}(FInputActionValue ActionValue, float ElapsedTime, float TriggeredTime, const UInputAction* SourceAction)",
                              "prologue": prologue})
                bindings.append({"kind": "enhanced", "literal": literal, "trigger": p["name"], "function": fname})
            self.types.forward.add("UInputAction")
        else:
            raise Unsupported(f"{cls.replace('K2Node_', '')} events are not translated yet")
        used, conts = {n["id"]}, set()
        for part in parts:
            fn = Fn(self, g, n, "event", dict(shared))
            fn.translate(fn.exec_target(n, part["pin"]))
            used |= fn.used_nodes
            conts |= fn.conts
            for k, v in fn.members.items():
                self.event_members[k] = v
            fn.lines = part["prologue"] + fn.prologue + fn.lines
            self.bodies[f"in:{n['id']}:{part['pin']}"] = fn
            part["body"] = f"in:{n['id']}:{part['pin']}"
        for k in self.cont_closure(conts):
            c = self.continuations.get(k)
            if c:
                used |= c.used_nodes
                self.event_members.update(c.members)
        item.cpp = ""
        item.extra.update(input=host, parts=parts, bindings=bindings, nodes=sorted(used), continuations=sorted(self.cont_closure(conts)))

    def translate_event(self, g: dict, n: dict, item: Item):
        cls = n["class"]
        params: dict = {}
        if cls.startswith("K2Node_Input") or cls == "K2Node_EnhancedInputAction":
            self.translate_input(g, n, item)
            return
        if cls == "K2Node_Event":
            ov = self.override_for(n)
            native = self.ex.functions.get(strip_skel(f"{n.get('memberOwner', '')}:{n.get('member', '')}")) or {}
            interface = strip_skel(n.get("memberOwner", "")) in {strip_skel(i) for i in self.ex.doc.get("interfaces", [])}
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
                    args.append(param_decl(prm, enum_as_byte=False))
                    params[(n["id"], prm["name"])] = prm["name"]
                item.cpp = native["name"] + "_Implementation"
                item.extra.update(signature=f"void {item.cpp}({', '.join(args)})", order="none", superCall="", override=True)
            elif interface and not native.get("native"):
                # an event of a Blueprint interface: C++ cannot implement that interface. Like an implementable event,
                # the body moves to a C++ function and the interface event stays with a single call to it.
                args = []
                for p in n["pins"]:
                    if p["dir"] == "out" and p["type"].get("category") not in ("exec", "delegate") and not p.get("splitFrom"):
                        t = self.types.of(p["type"], header=True)
                        args.append(f"{t} {ident(p['name'])}")
                        params[(n["id"], p["name"])] = ident(p["name"])
                item.cpp = f"{ident(n['member'])}Native"
                if item.cpp in self.ex.parent_members:
                    raise Unsupported(f"the name {item.cpp} is already taken")
                item.extra.update(signature=f"void {item.cpp}({', '.join(args)})", order="none", superCall="", thunk=True)
            elif native and "BlueprintEvent" in native.get("flags", []):
                # BlueprintImplementableEvent: C++ cannot override it. The body moves to a C++ function and the
                # Blueprint event stays with a single call to it, so the native parent still reaches it.
                args = []
                for prm in native["params"]:
                    if prm.get("return"):
                        raise Unsupported("implementable event with a return value")
                    args.append(param_decl(prm))
                    params[(n["id"], prm["name"])] = prm["name"]
                item.cpp = f"{native['name']}Native"
                if item.cpp in self.ex.parent_members:
                    raise Unsupported(f"the name {item.cpp} is already taken")
                item.extra.update(signature=f"void {item.cpp}({', '.join(args)})", order="none", superCall="", thunk=True)
            else:
                raise Unsupported(f"event {n.get('member')} has no native signature in the export")
        elif cls == "K2Node_CustomEvent":
            flags = set(n.get("memberFlags", []))
            net = None
            if "Net" in flags:
                # a replicated custom event is an RPC: Run on Server / Owning Client / Multicast, Reliable or not.
                # UHT generates the call (it sends or runs it); the body is <Name>_Implementation.
                where = "Server" if "Server" in flags else "Client" if "Client" in flags else "NetMulticast" if "Multicast" in flags else None
                if not where:
                    raise Unsupported("a replicated custom event without Server, Client or Multicast")
                net = f"{where}, {'Reliable' if 'Reliable' in flags else 'Unreliable'}"
            args = []
            for p in n["pins"]:
                if p["dir"] == "out" and p["type"].get("category") not in ("exec", "delegate"):
                    t = self.types.of(p["type"], header=True)
                    scalar = t in ("bool", "int32", "int64", "double", "float", "uint8") or t.endswith("*") or t.startswith("TEnumAsByte<") \
                        or p["type"].get("category") in ("byte", "enum")
                    # RPC parameters other than scalars travel by const reference (UHT requires it)
                    args.append(f"{t} {ident(p['name'])}" if not net or scalar else f"const {t}& {ident(p['name'])}")
                    params[(n["id"], p["name"])] = ident(p["name"])
            item.cpp = ident(n["member"])
            item.extra.update(signature=f"void {item.cpp}({', '.join(args)})", order="none", superCall="", custom=True)
            if net:
                item.extra["net"] = net
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
            item.extra.update(signature=f"void {item.cpp}({', '.join(args)})", order="none", superCall="", bind=(owner_item.cpp, n["member"]),
                              byName=bool(owner_item.extra.get("bpClass")))
        else:
            raise Unsupported(f"{cls.replace('K2Node_', '')} events are not translated yet (input and animation events)")
        fn = Fn(self, g, n, "event", params)
        for (nid, pin_name), member in self.shared_args.items():
            if nid == n["id"]:
                pin = n["_pins"][pin_name]
                if (nid, pin_name) not in params:
                    raise Unsupported(f"another event reads its argument {pin_name}, which it does not pass on")
                is_obj = self.types.is_object(pin["type"])
                fn.members[member] = (self.types.of(pin["type"], header=True, as_property=is_obj), is_obj)
                fn.prologue.append(f"\t{member} = {params[(nid, pin_name)]};")
        then = "then" if "then" in n["_pins"] else None
        fn.translate(fn.exec_target(n, then) if then else None)
        used = set(fn.used_nodes)
        for k in self.cont_closure(fn.conts):
            c = self.continuations.get(k)
            if c:
                used |= c.used_nodes
                self.event_members.update(c.members)
        fn.lines = fn.prologue + fn.lines
        item.extra["nodes"] = sorted(used)
        item.extra["continuations"] = sorted(self.cont_closure(fn.conts))
        for k, v in fn.members.items():
            self.event_members[k] = v
        self.bodies[f"ev:{n['id']}"] = fn

    # ------------------------------------------------------------------------------------------ output
    def summary(self) -> dict:
        def items(d):
            return [{"bp": i.bp, "cpp": i.cpp, "migrated": i.migrated, "reason": i.reason} for i in d]
        def ev_kind(e):
            x = e.extra
            return ("custom" if x.get("custom") else "thunk" if x.get("thunk") else "input" if x.get("input")
                    else "bound" if x.get("bind") else "override")
        ev = [{"bp": e.bp, "cpp": e.cpp, "node": e.extra["node"], "migrated": e.migrated, "reason": e.reason,
               "kind": ev_kind(e), "nodes": e.extra.get("nodes", [])} for e in self.events]
        fns = [{"bp": f.bp, "cpp": f.cpp, "migrated": f.migrated, "reason": f.reason, "nodes": f.extra.get("nodes", []),
                "thunk": bool(f.extra.get("thunk")), "override": bool(f.extra.get("bne")),
                "params": [{"bp": p, "cpp": c, "out": o} for p, c, o, _t in f.extra.get("params", [])]}
               for f in self.funcs.values() if not f.extra.get("customEvent")]
        tls = []
        for t in self.timelines.values():
            props = {}
            if t.migrated:
                tmpl = t.extra["template"]
                props[tmpl.get("directionProperty", "")] = t.extra["direction"]
                for tr in t.extra["tracks"]:
                    if tr.get("property"):
                        props[tr["property"]] = tr["member"]
            tls.append({"bp": t.bp, "cpp": t.cpp, "migrated": t.migrated, "reason": t.reason,
                        "node": (t.extra.get("node") or {}).get("id", ""), "nodes": t.extra.get("nodes", []),
                        "properties": {k: v for k, v in props.items() if k}})
        graph_nodes = {n["id"] for g in self.ex.graphs if g["kind"] in ("event", "function", "interface") for n in g["nodes"]
                       if n["class"] not in ("EdGraphNode_Comment",)}
        graph_nodes -= {e.extra["node"] for e in self.events if e.extra.get("empty")}   # unwired template events
        moved = set()
        for e in self.events:
            if e.migrated:
                moved |= set(e.extra.get("nodes", []))
        for t in self.timelines.values():
            if t.migrated:
                moved |= set(t.extra.get("nodes", []))
        for f in self.funcs.values():
            if f.migrated:
                moved |= set(f.extra.get("nodes", []))
                if f.extra.get("graph") and not f.extra.get("thunk"):
                    moved |= {n["id"] for n in f.extra["graph"]["nodes"] if n["class"] != "EdGraphNode_Comment"}
        return {
            "schema": SCHEMA, "blueprint": self.ex.doc.get("asset"), "package": self.ex.doc.get("package"), "name": self.ex.name,
            "kind": self.kind, "class": self.cpp, "classPath": f"/Script/{self.module}.{self.cpp[1:]}", "module": self.module,
            "parent": self.parent_cpp, "parentPath": self.parent_path, "exportedAtUtc": self.ex.doc.get("exportedAtUtc"),
            "variables": items(self.vars.values()), "components": items(self.components.values()),
            "widgets": items(self.widgets.values()), "dispatchers": items(self.dispatchers.values()),
            "functions": fns, "events": ev, "timelines": tls,
            "literals": [{"cpp": v["cpp"], "path": v["path"], "type": v["type"]} for v in self.literal_props.values()],
            "modules": sorted(m for m in self.types.modules if m not in ("CoreUObject", "Engine", "Core") and m != self.module),
            "coverage": {"nodes": len(graph_nodes), "migrated": len(moved & graph_nodes)},
        }


# ------------------------------------------------------------------------------------------------ C++ writer

LITERALS_FORWARD = ("// Generated by dwookit native migration: the helpers live in the shared copy one folder up.\n"
                    "#pragma once\n#include \"../DwoNativeLiterals.h\"\n")


def literals_header() -> str:
    return """// Generated by dwookit native migration. Literal helpers that parse values exactly as the Blueprint compiler
// does (FDefaultValueHelper first, then the struct's ImportText), evaluated once per use site.
#pragma once

#include "CoreMinimal.h"
#include "Curves/RichCurve.h"
#include "Misc/DefaultValueHelper.h"
#include "UObject/Class.h"
#include "UObject/UnrealType.h"
#include <type_traits>

namespace DwoNative
{
	/** A curve key exactly as the Blueprint's timeline stored it (tangents included, nothing recomputed). */
	inline FRichCurveKey Key(float Time, float Value, uint8 Interp, uint8 TangentMode, uint8 WeightMode, float Arrive, float ArriveWeight, float Leave, float LeaveWeight)
	{
		FRichCurveKey K(Time, Value, Arrive, Leave, static_cast<ERichCurveInterpMode>(Interp));
		K.TangentMode = static_cast<ERichCurveTangentMode>(TangentMode);
		K.TangentWeightMode = static_cast<ERichCurveTangentWeightMode>(WeightMode);
		K.ArriveTangentWeight = ArriveWeight;
		K.LeaveTangentWeight = LeaveWeight;
		return K;
	}
	inline void SetCurve(FRichCurve& Curve, const TArray<FRichCurveKey>& Keys, uint8 Pre, uint8 Post, float Default)
	{
		Curve.Reset();
		Curve.Keys = Keys;
		Curve.PreInfinityExtrap = static_cast<ERichCurveExtrapolation>(Pre);
		Curve.PostInfinityExtrap = static_cast<ERichCurveExtrapolation>(Post);
		Curve.DefaultValue = Default;
	}
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

	/** A class by path (a Blueprint class C++ has no type for): the loaded one, else loaded now. */
	inline UClass* FindClass(const TCHAR* Path)
	{
		UClass* Class = FindObject<UClass>(nullptr, Path);
		return Class ? Class : LoadObject<UClass>(nullptr, Path);
	}

	/** A call by name, as a Blueprint interface message makes it: nothing runs unless the target's class implements
	 *  the interface. Parameters are set and read by name; numbers convert as the Blueprint pins would. */
	class FCall
	{
	public:
		FCall(UObject* InTarget, UClass* Interface, const TCHAR* Name)
		{
			if (InTarget && Interface && InTarget->GetClass()->ImplementsInterface(Interface))
			{
				Function = InTarget->FindFunction(FName(Name));
			}
			if (Function)
			{
				// the parameters only, as UObject::CallFunctionByNameWithArguments does: a Blueprint function's struct
				// also holds its locals, which live past ParmsSize and belong to the VM's own frame
				Target = InTarget;
				Params.SetNumZeroed(FMath::Max<int32>(1, Function->ParmsSize));
				for (TFieldIterator<FProperty> It(Function); It && It->HasAnyPropertyFlags(CPF_Parm); ++It) { It->InitializeValue_InContainer(Params.GetData()); }
			}
		}
		~FCall()
		{
			if (!Function) { return; }
			for (TFieldIterator<FProperty> It(Function); It && It->HasAnyPropertyFlags(CPF_Parm); ++It) { It->DestroyValue_InContainer(Params.GetData()); }
		}
		FCall(const FCall&) = delete;
		FCall& operator=(const FCall&) = delete;

		template <typename T>
		void Set(const TCHAR* Name, const T& Value)
		{
			FProperty* P = Find(Name);
			if (!P) { return; }
			void* Ptr = P->ContainerPtrToValuePtr<void>(Params.GetData());
			if constexpr (std::is_arithmetic_v<T>)
			{
				if (FBoolProperty* B = CastField<FBoolProperty>(P)) { B->SetPropertyValue(Ptr, Value != 0); return; }
				if (FEnumProperty* E = CastField<FEnumProperty>(P)) { E->GetUnderlyingProperty()->SetIntPropertyValue(Ptr, static_cast<int64>(Value)); return; }
				if (FNumericProperty* N = CastField<FNumericProperty>(P))
				{
					if (N->IsFloatingPoint()) { N->SetFloatingPointPropertyValue(Ptr, static_cast<double>(Value)); }
					else { N->SetIntPropertyValue(Ptr, static_cast<int64>(Value)); }
					return;
				}
			}
			else if constexpr (std::is_convertible_v<T, const UObject*>)
			{
				if (FObjectPropertyBase* O = CastField<FObjectPropertyBase>(P)) { O->SetObjectPropertyValue(Ptr, const_cast<UObject*>(static_cast<const UObject*>(Value))); return; }
			}
			if (ensureMsgf(P->GetElementSize() == sizeof(T), TEXT("DwoNative: parameter %s has another type"), Name)) { P->CopySingleValue(Ptr, &Value); }
		}

		void Run()
		{
			if (Target) { Target->ProcessEvent(Function, Params.GetData()); }
		}

		template <typename T>
		T Get(const TCHAR* Name) const
		{
			FProperty* P = Find(Name);
			if (!P) { return T{}; }
			const void* Ptr = P->ContainerPtrToValuePtr<void>(Params.GetData());
			if constexpr (std::is_arithmetic_v<T>)
			{
				if (const FBoolProperty* B = CastField<FBoolProperty>(P)) { return static_cast<T>(B->GetPropertyValue(Ptr)); }
				if (const FEnumProperty* E = CastField<FEnumProperty>(P)) { return static_cast<T>(E->GetUnderlyingProperty()->GetSignedIntPropertyValue(Ptr)); }
				if (const FNumericProperty* N = CastField<FNumericProperty>(P))
				{
					return N->IsFloatingPoint() ? static_cast<T>(N->GetFloatingPointPropertyValue(Ptr)) : static_cast<T>(N->GetSignedIntPropertyValue(Ptr));
				}
			}
			else if constexpr (std::is_pointer_v<T>)
			{
				if (const FObjectPropertyBase* O = CastField<FObjectPropertyBase>(P)) { return Cast<std::remove_pointer_t<T>>(O->GetObjectPropertyValue(Ptr)); }
			}
			T Value{};
			if (ensureMsgf(P->GetElementSize() == sizeof(T), TEXT("DwoNative: parameter %s has another type"), Name)) { P->CopySingleValue(&Value, Ptr); }
			return Value;
		}

	private:
		FProperty* Find(const TCHAR* Name) const
		{
			return Function ? Function->FindPropertyByName(FName(Name)) : nullptr;
		}
		UObject* Target = nullptr;
		UFunction* Function = nullptr;
		TArray<uint8, TAlignedHeapAllocator<16>> Params;
	};

	/** An enum by path, for the Blueprint nodes that name its entries (Get Enumerator Name). */
	inline UEnum* FindEnum(const TCHAR* Path)
	{
		UEnum* Enum = FindObject<UEnum>(nullptr, Path);
		return Enum ? Enum : LoadObject<UEnum>(nullptr, Path);
	}

	/** The class default object Get Class Defaults reads (none for no class). */
	inline const UObject* ClassDefault(const UClass* Class)
	{
		return Class ? Class->GetDefaultObject() : nullptr;
	}

	/** A property read by name, as Get Class Defaults reads it: through reflection, so C++ access and bitfields do
	 *  not matter. No object or no such property: the type's default. */
	template <typename T>
	T GetProperty(const UObject* Object, const TCHAR* Name)
	{
		const FProperty* P = Object ? FindFProperty<FProperty>(Object->GetClass(), FName(Name)) : nullptr;
		if (!P) { return T{}; }
		const void* Ptr = P->ContainerPtrToValuePtr<void>(Object);
		if constexpr (std::is_arithmetic_v<T>)
		{
			if (const FBoolProperty* B = CastField<FBoolProperty>(P)) { return static_cast<T>(B->GetPropertyValue(Ptr)); }
			if (const FEnumProperty* E = CastField<FEnumProperty>(P)) { return static_cast<T>(E->GetUnderlyingProperty()->GetSignedIntPropertyValue(Ptr)); }
			if (const FNumericProperty* N = CastField<FNumericProperty>(P))
			{
				return N->IsFloatingPoint() ? static_cast<T>(N->GetFloatingPointPropertyValue(Ptr)) : static_cast<T>(N->GetSignedIntPropertyValue(Ptr));
			}
		}
		else if constexpr (std::is_pointer_v<T>)
		{
			if (const FObjectPropertyBase* O = CastField<FObjectPropertyBase>(P)) { return Cast<std::remove_pointer_t<T>>(O->GetObjectPropertyValue(Ptr)); }
		}
		T Value{};
		if (ensureMsgf(P->GetElementSize() == sizeof(T), TEXT("DwoNative: property %s has another type"), Name)) { P->CopySingleValue(&Value, Ptr); }
		return Value;
	}

	/** A property set by name, as Set*PropertyByName sets it (the exposed-on-spawn pins of SpawnActor, a Blueprint
	 *  class's variable): no setter, no RepNotify. Numbers convert as the Blueprint pins would. */
	template <typename T>
	void SetProperty(UObject* Object, const TCHAR* Name, const T& Value)
	{
		FProperty* P = Object ? FindFProperty<FProperty>(Object->GetClass(), FName(Name)) : nullptr;
		if (!P) { return; }
		void* Ptr = P->ContainerPtrToValuePtr<void>(Object);
		if constexpr (std::is_arithmetic_v<T>)
		{
			if (FBoolProperty* B = CastField<FBoolProperty>(P)) { B->SetPropertyValue(Ptr, Value != 0); return; }
			if (FEnumProperty* E = CastField<FEnumProperty>(P)) { E->GetUnderlyingProperty()->SetIntPropertyValue(Ptr, static_cast<int64>(Value)); return; }
			if (FNumericProperty* N = CastField<FNumericProperty>(P))
			{
				if (N->IsFloatingPoint()) { N->SetFloatingPointPropertyValue(Ptr, static_cast<double>(Value)); }
				else { N->SetIntPropertyValue(Ptr, static_cast<int64>(Value)); }
				return;
			}
		}
		else if constexpr (std::is_convertible_v<T, const UObject*>)
		{
			if (FObjectPropertyBase* O = CastField<FObjectPropertyBase>(P)) { O->SetObjectPropertyValue(Ptr, const_cast<UObject*>(static_cast<const UObject*>(Value))); return; }
		}
		if (ensureMsgf(P->GetElementSize() == sizeof(T), TEXT("DwoNative: property %s has another type"), Name)) { P->CopySingleValue(Ptr, &Value); }
	}

	/** Removes a function from a multicast delegate found by name (Unbind Event). */
	inline void RemoveDynamicByName(UObject* Source, const TCHAR* Delegate, UObject* Receiver, const TCHAR* Function)
	{
		if (!Source) { return; }
		if (FMulticastDelegateProperty* P = FindFProperty<FMulticastDelegateProperty>(Source->GetClass(), FName(Delegate)))
		{
			FScriptDelegate D;
			D.BindUFunction(Receiver, FName(Function));
			P->RemoveDelegate(D, Source);
		}
	}

	/** Removes every function from a multicast delegate found by name (Unbind All Events). */
	inline void ClearByName(UObject* Source, const TCHAR* Delegate)
	{
		if (!Source) { return; }
		if (FMulticastDelegateProperty* P = FindFProperty<FMulticastDelegateProperty>(Source->GetClass(), FName(Delegate)))
		{
			P->ClearDelegate(Source);
		}
	}

	/** Adds a function to a multicast delegate found by name (a Blueprint class's event dispatcher), as a Blueprint
	 *  bound event does when the component or widget is a Blueprint class. */
	inline void AddDynamicByName(UObject* Source, const TCHAR* Delegate, UObject* Receiver, const TCHAR* Function)
	{
		if (!Source) { return; }
		if (FMulticastDelegateProperty* P = FindFProperty<FMulticastDelegateProperty>(Source->GetClass(), FName(Delegate)))
		{
			FScriptDelegate D;
			D.BindUFunction(Receiver, FName(Function));
			P->AddDelegate(MoveTemp(D), Source);
		}
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
    all_funcs = [f for f in plan.funcs.values() if f.migrated and not f.extra.get("customEvent") and not f.extra.get("construction")]
    funcs = [f for f in all_funcs if not f.extra.get("bne") and not f.extra.get("thunk")]
    override_funcs = [f for f in all_funcs if f.extra.get("bne") or f.extra.get("thunk")]
    construction = next((f for f in plan.funcs.values() if f.migrated and f.extra.get("construction")), None)
    events = [e for e in plan.events if e.migrated]
    timelines = [t for t in plan.timelines.values() if t.migrated]
    inputs = [e for e in events if e.extra.get("input")]
    events = [e for e in events if not e.extra.get("input")]
    cont_keys = set()
    for x in events + inputs + timelines:
        cont_keys |= set(x.extra.get("continuations", []))
    conts = sorted(k for k in cont_keys if plan.continuations.get(k))
    on_construction = bool(construction or timelines)
    if timelines:
        t.header_includes.add("Components/TimelineComponent.h")
        t.includes |= {"Curves/CurveFloat.h", "Curves/CurveVector.h", "Curves/CurveLinearColor.h"}
        t.forward |= {"UCurveFloat", "UCurveVector", "UCurveLinearColor", "UTimelineComponent"}
        plan.literals.uses_helpers = True
    if inputs:
        t.includes.add("Components/InputComponent.h")
    h_inc = sorted(t.header_includes - {pi})
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
    for tl in timelines:
        meta = f', meta = (DisplayName = "{tl.bp}")' if tl.cpp != tl.bp else ""
        H.append(f'\t/** Timeline {tl.bp}: created at construction, as the Blueprint created it. */')
        H.append(f'\tUPROPERTY(BlueprintReadWrite, Category = "Timelines"{meta})')
        H.append(f"\tTObjectPtr<UTimelineComponent> {tl.cpp};")
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
            net = f", {e.extra['net']}" if e.extra.get("net") else ""
            H.append(f'\tUFUNCTION(BlueprintCallable{net}, Category = "Default")')
            H.append(f"\t{e.extra['signature']};")
            H.append("")
    protected = [e for e in events if e.extra.get("override")]
    bound = [e for e in events if e.extra.get("bind")]
    thunks = [e for e in events if e.extra.get("thunk")]
    hosts = sorted({e.extra["input"] for e in inputs})
    if protected or bound or needs_net or on_construction or thunks or override_funcs or hosts:
        H.append("protected:")
        for e in protected:
            H.append(f"\tvirtual {e.extra['signature']} override;")
        for e in thunks:
            H.append(f"\t/** The Blueprint's {e.bp} event calls this (an implementable event cannot be overridden in C++). */")
            H.append('\tUFUNCTION(BlueprintCallable, Category = "Native")')
            H.append(f"\t{e.extra['signature']};")
        for f in override_funcs:
            ret = f.extra.get("return") or "void"
            if f.extra.get("bne"):
                H.append(f"\tvirtual {ret} {f.extra['impl']}({', '.join(f.extra['decls'])}) override;")
            else:
                why = "C++ cannot implement a Blueprint interface" if f.extra.get("interface") else "an implementable event cannot be overridden in C++"
                H.append(f"\t/** The Blueprint's {f.bp} function calls this ({why}). */")
                H.append('\tUFUNCTION(BlueprintCallable, Category = "Native")')
                H.append(f"\t{ret} {f.cpp}({', '.join(f.extra['decls'])});")
        for h in hosts:
            H.append({"pawn": "\tvirtual void SetupPlayerInputComponent(UInputComponent* PlayerInputComponent) override;",
                      "controller": "\tvirtual void SetupInputComponent() override;",
                      "actor": "\tvirtual void EnableInput(APlayerController* PlayerController) override;"}[h])
        if on_construction:
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
    callbacks = []
    for e in inputs:
        for part in e.extra["parts"]:
            callbacks.append(f"\t{part['signature']};")
    for tl in timelines:
        for fname, _key in tl.extra["functions"]:
            callbacks.append(f"\tvoid {fname}();")
        privates.append(f"\tUPROPERTY()\n\tTEnumAsByte<ETimelineDirection::Type> {tl.extra['direction']};")
        for tr in tl.extra["tracks"]:
            if tr["type"] != "event":
                ty = {"float": "float", "vector": "FVector", "color": "FLinearColor"}[tr["type"]]
                privates.append(f"\tUPROPERTY()\n\t{ty} {tr['member']}{{}};")
    for k in conts:
        callbacks.append(f"\tvoid Latent_{k[:8]}();")
    for cb in callbacks:
        privates.append("\tUFUNCTION()")
        privates.append(cb)
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
                C.append(bind_line(e))
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

    for f in override_funcs:
        fn = plan.bodies.get("fn:" + f.bp)
        name = f.extra["impl"] if f.extra.get("bne") else f.cpp
        C.append(f"{f.extra.get('return') or 'void'} {cls}::{name}({', '.join(f.extra['decls'])})")
        C.append("{")
        C += body(fn)
        C.append("}")
        C.append("")
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
    if on_construction:
        C.append(f"void {cls}::OnConstruction(const FTransform& Transform)")
        C.append("{")
        C.append("\tSuper::OnConstruction(Transform);")
        for tl in timelines:
            C += timeline_construction(tl)
        if construction:
            C += body(plan.bodies.get("fn:" + construction.bp))
        C.append("}")
        C.append("")
    for tl in timelines:
        for fname, key in tl.extra["functions"]:
            C.append(f"void {cls}::{fname}()")
            C.append("{")
            C += body(plan.bodies[key])
            C.append("}")
            C.append("")
    for k in conts:
        C.append(f"void {cls}::Latent_{k[:8]}()")
        C.append("{")
        C += body(plan.continuations[k])
        C.append("}")
        C.append("")
    for h in hosts:
        C += input_host(cls, h, [b for e in inputs if e.extra["input"] == h for b in e.extra["bindings"]])
    for e in inputs:
        for part in e.extra["parts"]:
            m = re.match(r"void (\w+)\((.*)\)$", part["signature"])
            C.append(f"void {cls}::{m.group(1)}({m.group(2)})")
            C.append("{")
            C += body(plan.bodies[part["body"]])
            C.append("}")
            C.append("")
    for e in events:
        fn = plan.bodies.get(f"ev:{e.extra['node']}")
        sig = e.extra["signature"]
        m = re.match(r"(\w[\w:<>*&\s]*?)\s+(\w+)\((.*)\)$", sig)
        ret, name, args = m.group(1), m.group(2), m.group(3)
        if e.extra.get("net"):
            name += "_Implementation"            # an RPC: UHT's generated function sends or runs this
        C.append(f"{ret} {cls}::{name}({args})")
        C.append("{")
        if e.extra.get("order") == "after":
            C.append("\t" + e.extra["superCall"])
        elif e.extra.get("order") == "custom":
            C.append("\t" + e.extra["superCall"])
        if name in ("NativeOnInitialized", "PostInitializeComponents") and bound:
            for b in bound:
                C.append(bind_line(b))
        C += body(fn)
        if e.extra.get("order") == "before":
            C.append("\t" + e.extra["superCall"])
        C.append("}")
        C.append("")

    files = {f"{header_name}.h": "\n".join(H) + "\n", f"{header_name}.cpp": "\n".join(C) + "\n"}
    if plan.literals.uses_helpers:
        # one shared copy next to the Blueprint folders; each folder's copy only forwards to it, so a unity build
        # that puts two generated classes in one file never sees two different versions of the helpers
        (out.parent / "DwoNativeLiterals.h").write_text(literals_header(), encoding="utf-8", newline="\n")
        for old in out.parent.glob("*/DwoNativeLiterals.h"):
            old.write_text(LITERALS_FORWARD, encoding="utf-8", newline="\n")
        files["DwoNativeLiterals.h"] = LITERALS_FORWARD
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8", newline="\n")
    s["files"] = sorted(files)
    (out / f"{plan.ex.name}.native.json").write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / f"{plan.ex.name}.native.md").write_text(report_md(s), encoding="utf-8")
    return s


def bind_line(e: Item) -> str:
    comp, delegate = e.extra["bind"]
    if e.extra.get("byName"):
        return f"\tDwoNative::AddDynamicByName({comp}, {cpp_string(delegate)}, this, {cpp_string(e.cpp)});"
    return f"\tif ({comp}) {{ {comp}->{delegate}.AddDynamic(this, &ThisClass::{e.cpp}); }}"


def timeline_construction(tl: Item) -> list[str]:
    """UBlueprintGeneratedClass::CreateTimelineComponent, line for line, with this timeline's settings."""
    tm = tl.extra["template"]
    o = [f"\t// timeline {tl.bp} (as UBlueprintGeneratedClass::CreateTimelineComponent creates it)",
         "\tif (!IsTemplate())", "\t{",
         f"\t\tUTimelineComponent* NewTimeline = NewObject<UTimelineComponent>(this, FName({cpp_string(tl.bp)}));",
         "\t\tNewTimeline->CreationMethod = EComponentCreationMethod::UserConstructionScript;",
         "\t\tBlueprintCreatedComponents.Add(NewTimeline);",
         "\t\tNewTimeline->SetNetAddressable();",
         "\t\tNewTimeline->SetPropertySetObject(this);",
         f"\t\tNewTimeline->SetDirectionPropertyName(FName(TEXT(\"{tl.extra['direction']}\")));",
         f"\t\tNewTimeline->SetTimelineLength({fmt_real(tm.get('length', 5.0), True)});",
         f"\t\tNewTimeline->SetTimelineLengthMode({'TL_LastKeyFrame' if tm.get('lengthMode') == 'LastKeyFrame' else 'TL_TimelineLength'});",
         f"\t\tNewTimeline->PrimaryComponentTick.TickGroup = {tm.get('tickGroup') or 'TG_PrePhysics'};",
         f"\t\t{tl.cpp} = NewTimeline;"]

    def curve(target: str, c: dict) -> str:
        keys = ", ".join("DwoNative::Key(" + ", ".join([fmt_real(k[0], True), fmt_real(k[1], True), str(int(k[2])), str(int(k[3])), str(int(k[4])),
                                                         fmt_real(k[5], True), fmt_real(k[6], True), fmt_real(k[7], True), fmt_real(k[8], True)]) + ")"
                         for k in c["keys"])
        return f"\t\t\tDwoNative::SetCurve({target}, {{ {keys} }}, {int(c['pre'])}, {int(c['post'])}, {fmt_real(c['default'], True)});"

    for tr in tl.extra["tracks"]:
        if tr["type"] == "event" and tr.get("curves"):
            o.append("\t\t{")
            o.append("\t\t\tFScriptDelegate EventDelegate;")
            o.append(f"\t\t\tEventDelegate.BindUFunction(this, FName(TEXT(\"{tr['function']}\")));")
            for k in tr["curves"][0]["keys"]:
                o.append(f"\t\t\tNewTimeline->AddEvent({fmt_real(k[0], True)}, FOnTimelineEvent(EventDelegate));")
            o.append("\t\t}")
    for kind, cls, add, member, n in (("float", "UCurveFloat", "AddInterpFloat", "FloatCurve", 1), ("vector", "UCurveVector", "AddInterpVector", "FloatCurves", 3),
                                      ("color", "UCurveLinearColor", "AddInterpLinearColor", "FloatCurves", 4)):
        for tr in tl.extra["tracks"]:
            if tr["type"] != kind or not (tr.get("curves") or tr.get("literalCpp")):
                continue
            delegate = {"float": "FOnTimelineFloat", "vector": "FOnTimelineVector", "color": "FOnTimelineLinearColor"}[kind]
            o.append("\t\t{")
            if tr.get("literalCpp"):
                o.append(f"\t\t\t{cls}* Curve = {tr['literalCpp']};")
            else:
                o.append(f"\t\t\t{cls}* Curve = NewObject<{cls}>(this);")
                for i, c in enumerate(tr["curves"]):
                    o.append(curve(f"Curve->{member}" if n == 1 else f"Curve->{member}[{i}]", c))
            o.append(f"\t\t\tNewTimeline->{add}(Curve, {delegate}(), FName(TEXT(\"{tr['member']}\")), FName({cpp_string(tr['name'])}));")
            o.append("\t\t}")
    for what, fname in (("SetTimelinePostUpdateFunc", tl.extra["update"]), ("SetTimelineFinishedFunc", tl.extra["finished"])):
        o.append("\t\t{")
        o.append("\t\t\tFScriptDelegate Delegate;")
        o.append(f"\t\t\tDelegate.BindUFunction(this, FName(TEXT(\"{fname}\")));")
        o.append(f"\t\t\tNewTimeline->{what}(FOnTimelineEvent(Delegate));")
        o.append("\t\t}")
    o.append("\t\tNewTimeline->RegisterComponent();")
    if tm.get("autoPlay"):
        o += ["\t\tNewTimeline->bAutoActivate = true;", "\t\tNewTimeline->Play();"]
    if tm.get("loop"):
        o.append("\t\tNewTimeline->SetLooping(true);")
    if tm.get("replicated"):
        o.append("\t\tNewTimeline->SetIsReplicated(true);")
    if tm.get("ignoreTimeDilation"):
        o.append("\t\tNewTimeline->SetIgnoreTimeDilation(true);")
    o.append("\t}")
    return o


def input_host(cls: str, host: str, bindings: list[dict]) -> list[str]:
    """Input bindings as the engine's UInputActionDelegateBinding & co. add them for a Blueprint: per kind, bindings
    that override the parent's first remove existing ones with the same name and event, then all are added."""
    head = {"pawn": (f"void {cls}::SetupPlayerInputComponent(UInputComponent* PlayerInputComponent)", "\tSuper::SetupPlayerInputComponent(PlayerInputComponent);", "PlayerInputComponent"),
            "controller": (f"void {cls}::SetupInputComponent()", "\tSuper::SetupInputComponent();", "InputComponent"),
            "actor": (f"void {cls}::EnableInput(APlayerController* PlayerController)", "\tconst bool bCreates = PlayerController && !InputComponent;\n\tSuper::EnableInput(PlayerController);", "InputComponent")}[host]
    o = [head[0], "{", head[1]]
    cond = f"bCreates ? {head[2]}.Get() : nullptr" if host == "actor" else head[2]
    o.append(f"\tif (UInputComponent* IC = {cond})")
    o.append("\t{")
    actions = [b for b in bindings if b["kind"] == "action"]
    keys = [b for b in bindings if b["kind"] == "key"]
    axes = [b for b in bindings if b["kind"] == "axis"]
    enhanced = [b for b in bindings if b["kind"] == "enhanced"]
    if actions:
        o.append("\t\tTArray<FInputActionBinding> Actions;")
        for b in actions:
            o.append("\t\t{")
            o.append(f"\t\t\tFInputActionBinding B(FName({cpp_string(b['name'])}), {b['event']});")
            o.append(f"\t\t\tB.bConsumeInput = {'true' if b['consume'] else 'false'};")
            o.append(f"\t\t\tB.bExecuteWhenPaused = {'true' if b['paused'] else 'false'};")
            o.append(f"\t\t\tB.ActionDelegate.BindDelegate(this, GET_FUNCTION_NAME_CHECKED(ThisClass, {b['function']}));")
            if b["override"]:
                o.append("\t\t\tfor (int32 I = IC->GetNumActionBindings() - 1; I >= 0; --I)")
                o.append("\t\t\t{")
                o.append("\t\t\t\tconst FInputActionBinding& E = IC->GetActionBinding(I);")
                o.append("\t\t\t\tif (E.GetActionName() == B.GetActionName() && E.KeyEvent == B.KeyEvent) { IC->RemoveActionBinding(I); }")
                o.append("\t\t\t}")
            o.append("\t\t\tActions.Add(B);")
            o.append("\t\t}")
        o.append("\t\tfor (const FInputActionBinding& B : Actions) { IC->AddActionBinding(B); }")
    if keys:
        o.append("\t\tTArray<FInputKeyBinding> Keys;")
        for b in keys:
            chord = f"FInputChord(FKey(FName({cpp_string(b['key'])})), {'true' if b['shift'] else 'false'}, {'true' if b['ctrl'] else 'false'}, {'true' if b['alt'] else 'false'}, {'true' if b['cmd'] else 'false'})"
            o.append("\t\t{")
            o.append(f"\t\t\tFInputKeyBinding B({chord}, {b['event']});")
            o.append(f"\t\t\tB.bConsumeInput = {'true' if b['consume'] else 'false'};")
            o.append(f"\t\t\tB.bExecuteWhenPaused = {'true' if b['paused'] else 'false'};")
            o.append(f"\t\t\tB.KeyDelegate.BindDelegate(this, GET_FUNCTION_NAME_CHECKED(ThisClass, {b['function']}));")
            if b["override"]:
                o.append("\t\t\tfor (int32 I = IC->KeyBindings.Num() - 1; I >= 0; --I)")
                o.append("\t\t\t{")
                o.append("\t\t\t\tif (IC->KeyBindings[I].Chord == B.Chord && IC->KeyBindings[I].KeyEvent == B.KeyEvent) { IC->KeyBindings.RemoveAt(I); }")
                o.append("\t\t\t}")
            o.append("\t\t\tKeys.Add(B);")
            o.append("\t\t}")
        o.append("\t\tIC->KeyBindings.Append(Keys);")
    if axes:
        o.append("\t\tTArray<FInputAxisBinding> Axes;")
        for b in axes:
            o.append("\t\t{")
            o.append(f"\t\t\tFInputAxisBinding B(FName({cpp_string(b['name'])}));")
            o.append(f"\t\t\tB.bConsumeInput = {'true' if b['consume'] else 'false'};")
            o.append(f"\t\t\tB.bExecuteWhenPaused = {'true' if b['paused'] else 'false'};")
            o.append(f"\t\t\tB.AxisDelegate.BindDelegate(this, GET_FUNCTION_NAME_CHECKED(ThisClass, {b['function']}));")
            if b["override"]:
                o.append("\t\t\tfor (int32 I = IC->AxisBindings.Num() - 1; I >= 0; --I)")
                o.append("\t\t\t{")
                o.append("\t\t\t\tconst FInputAxisBinding& E = IC->AxisBindings[I];")
                o.append("\t\t\t\tconst bool bNoneDelegate = !E.AxisDelegate.GetDelegate().IsBound() && E.AxisDelegate.GetDynamicDelegate().GetFunctionName().IsNone();")
                o.append("\t\t\t\tif (E.AxisName == B.AxisName && !bNoneDelegate) { IC->AxisBindings.RemoveAt(I); }")
                o.append("\t\t\t}")
            o.append("\t\t\tAxes.Add(B);")
            o.append("\t\t}")
        o.append("\t\tIC->AxisBindings.Append(Axes);")
    if enhanced:
        o.append("\t\tif (UEnhancedInputComponent* EIC = Cast<UEnhancedInputComponent>(IC))")
        o.append("\t\t{")
        for b in enhanced:
            o.append(f"\t\t\tEIC->BindAction({b['literal']}, ETriggerEvent::{b['trigger']}, this, GET_FUNCTION_NAME_CHECKED(ThisClass, {b['function']}));")
        o.append("\t\t}")
    o += ["\t}", "}", ""]
    return o


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
    table("Timelines", s.get("timelines", []), [("Blueprint", lambda r: r["bp"]), ("C++", lambda r: r["cpp"]), ("Where", state), ("Nodes", lambda r: len(r["nodes"])), ("Why it stays", lambda r: r["reason"])])
    if s["literals"]:
        lines += ["## Asset references used by the code", "", "Kept as hidden properties the rebase fills from the Blueprint, so they stay hard references (cooked).", ""]
        lines += [f"- `{l['cpp']}` = `{l['path']}`" for l in s["literals"]]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------------ behaviour compare

NUM = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
PER_SPAWN = {"ActorGuid", "ActorInstanceGuid", "ContentBundleGuid"}   # new for every spawned actor
VOLATILE = re.compile(r"/Temp/[^.:'\" ]+|Untitled_\d+|DwoBehaviour(_\d+)?")


def _norm(text: str) -> str:
    return VOLATILE.sub("<world>", str(text))


def same_value(a, b, tol: float = 1e-3) -> bool:
    """Equal as text once world names are normalized; numbers inside may differ by float noise."""
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    if NUM.sub("#", a) != NUM.sub("#", b):
        return False
    for x, y in zip(NUM.findall(a), NUM.findall(b)):
        fx, fy = float(x), float(y)
        if abs(fx - fy) > tol + 1e-5 * max(abs(fx), abs(fy)):
            return False
    return True


def _flatten(step: dict, renames: dict) -> dict:
    out = {"subjectAlive": step.get("subjectAlive")}
    subject = step.get("subject") or {}
    for k, v in (subject.get("props") or {}).items():
        out[f"actor.{renames.get(k, k)}"] = v
    if "transform" in subject:
        out["actor.@transform"] = subject["transform"]
    for comp, props in (subject.get("components") or {}).items():
        for k, v in props.items():
            out[f"{comp}.{k}"] = v
    for k, v in (step.get("probe") or {}).items():
        out[f"probe.{k}"] = v
    for k, v in (step.get("pawn") or {}).items():
        out[f"pawn.{k}"] = v
    return out


BOUND_EVENT = re.compile(r"\.BndEvt__[^,()]*?__DelegateSignature")


def _bindings(value, handlers: set) -> str:
    """A dispatcher's bindings name the function they call: the Blueprint's generated bound-event function before,
    the C++ handler after. Both are the same binding."""
    text = BOUND_EVENT.sub(".<bound event>", str(value))
    for h in handlers:
        text = re.sub(rf"\.{re.escape(h)}(?=[,)])", ".<bound event>", text)
    return text


def compare_traces(before: dict, after: dict, renames: dict | None = None, ignore: set | None = None,
                   handlers: set | None = None) -> dict:
    """Differences between a run of the original Blueprint and of the migrated one, step by step. Values only one side
    has (members the generated class adds, like asset references it holds) are listed apart, not counted.
    handlers: the C++ functions that replaced bound events (their bindings compare equal to the Blueprint's)."""
    renames = renames or {}
    handlers = handlers or set()
    ignore = (ignore or set()) | PER_SPAWN
    if not before.get("ok") or not after.get("ok"):
        return {"verdict": "error", "error": before.get("error") or after.get("error"), "differences": [], "onlyOneSide": []}
    diffs, one_side = [], set()
    steps_a, steps_b = before.get("steps", []), after.get("steps", [])
    if [s["name"] for s in steps_a] != [s["name"] for s in steps_b]:
        diffs.append({"step": "-", "value": "steps", "before": [s["name"] for s in steps_a], "after": [s["name"] for s in steps_b]})
    compared = 0
    for sa, sb in zip(steps_a, steps_b):
        fa, fb = _flatten(sa, renames), _flatten(sb, renames)
        for key in sorted(set(fa) | set(fb)):
            if key in ignore or key.split(".", 1)[-1] in ignore:
                continue
            if key not in fa or key not in fb:
                one_side.add(key)
                continue
            compared += 1
            va, vb = fa[key], fb[key]
            if isinstance(va, str) and isinstance(vb, str) and va != vb:
                va, vb = _bindings(va, handlers), _bindings(vb, handlers)
            if not same_value(va, vb):
                diffs.append({"step": sa["name"], "value": key, "before": str(fa[key])[:300], "after": str(fb[key])[:300]})
    return {"verdict": "same" if not diffs else "different", "compared": compared, "differences": diffs,
            "onlyOneSide": sorted(one_side)}


def behaviour_md(name: str, result: dict) -> str:
    lines = [f"# {name}: behaviour before and after the migration", ""]
    if result["verdict"] == "error":
        return "\n".join(lines + [f"The check could not run: {result.get('error')}", ""])
    lines.append(f"Verdict: **{result['verdict']}**. {result['compared']} values compared, {len(result['differences'])} differ.")
    lines.append("")
    if result["differences"]:
        lines += ["| Step | Value | Before (Blueprint) | After (C++) |", "|---|---|---|---|"]
        for d in result["differences"][:200]:
            lines.append(f"| {d['step']} | `{d['value']}` | {d['before']} | {d['after']} |")
        lines.append("")
    if result["onlyOneSide"]:
        lines += ["Only on one side (not compared): " + ", ".join(f"`{k}`" for k in result["onlyOneSide"][:40]), ""]
    return "\n".join(lines)


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
    p = sub.add_parser("compare")
    p.add_argument("before", type=Path)
    p.add_argument("after", type=Path)
    p.add_argument("--map", type=Path, help="<Name>.native.json: renamed variables")
    p.add_argument("--out", type=Path, help="write <out>.json and <out>.md")
    args = ap.parse_args(argv)
    if args.cmd == "compare":
        renames, name, handlers = {}, args.after.stem, set()
        if args.map:
            m = json.loads(args.map.read_text(encoding="utf-8"))
            name = m["name"]
            renames = {v["bp"]: v["cpp"] for v in m["variables"] if v["migrated"] and v["bp"] != v["cpp"]}
            handlers = {e["cpp"] for e in m.get("events", []) if e.get("migrated") and e.get("cpp") and e.get("kind") in ("bound", "custom")}
        result = compare_traces(json.loads(args.before.read_text(encoding="utf-8")), json.loads(args.after.read_text(encoding="utf-8")), renames,
                                handlers=handlers)
        if args.out:
            Path(str(args.out) + ".json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            Path(str(args.out) + ".md").write_text(behaviour_md(name, result), encoding="utf-8")
        print(f"behaviour: {result['verdict']}" + (f" ({result['compared']} values compared, {len(result['differences'])} differ)" if result["verdict"] != "error" else f": {result.get('error')}"))
        for d in result["differences"][:15]:
            print(f"  {d['step']}: {d['value']}: {d['before'][:80]} -> {d['after'][:80]}")
        return 0 if result["verdict"] == "same" else 1
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
