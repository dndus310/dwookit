"""Unit tests for the dwookit Lens Python tools. Standard library only.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "raw_temp_proj"
sys.path.insert(0, str(TOOLS))

import lens_analyze as analyze  # noqa: E402
import lens_behaviors as behaviors  # noqa: E402
import lens_bp  # noqa: E402
import lens_features as features  # noqa: E402
import lens_logs as logs  # noqa: E402
import lens_quality as quality  # noqa: E402
import lens_runtime as runtime  # noqa: E402
import lens_verify as verify  # noqa: E402


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lens-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# =============================================================================== log parser

class LogParserTests(TempDir):
    LOG = "\n".join([
        "[2026.10.27-16.40.57:829][  0]LogInit: Display: starting",
        "[2026.10.27-16.40.58:001][  1]LogBlueprint: Error: [Compiler] Missing pin on node 12 in /Game/BP_A",
        "[2026.10.27-16.40.58:002][  1]LogBlueprint: Error: [Compiler] Missing pin on node 99 in /Game/BP_A",
        "[2026.10.27-16.40.58:002][  1]LogBlueprint: Error: [Compiler] Missing pin on node 7 in /Game/BP_B",
        "[2026.10.27-16.40.58:003][  2]LogTemp: Warning: Slow frame 33.4 ms",
        "LogContentValidation: Warning: Asset C:\\Proj\\Content\\X.uasset has issues",
        "[2026.10.27-16.40.58:004][  3]LogOutputDevice: Ensure condition failed: Ptr != nullptr",
        "[2026.10.27-16.40.58:005][  3]LogWindows: Fatal: Assertion failed: Index < Num",
        "  0x00007ff6 UnrealEditor-Core.dll!FOo::Bar()",
        "  0x00007ff7 UnrealEditor-Engine.dll!UWorld::Tick()",
        "",
        "[2026.10.27-16.40.58:006][  4]LogConfig: Warning: ignored category line",
    ])

    def parse(self, ignore=frozenset({"LogConfig"})):
        return logs.parse_file(write(self.tmp / "a.log", self.LOG), set(ignore))

    def test_severities_and_categories(self):
        events = self.parse()
        kinds = [(e["severity"], e["category"]) for e in events]
        self.assertIn(("Error", "LogBlueprint"), kinds)
        self.assertIn(("Warning", "LogContentValidation"), kinds)  # line without timestamp
        self.assertIn(("Error", "LogOutputDevice"), kinds)  # ensure without explicit severity
        self.assertIn(("Fatal", "LogWindows"), kinds)
        self.assertNotIn("LogConfig", [e["category"] for e in events])  # ignored category
        self.assertNotIn("LogInit", [e["category"] for e in events])  # Display is not a failure

    def test_fatal_captures_call_stack(self):
        fatal = [e for e in self.parse() if e["severity"] == "Fatal"][0]
        self.assertEqual(len(fatal["detail"]), 2)
        self.assertIn("UWorld::Tick", fatal["detail"][1])

    def test_fingerprint_ignores_numbers_and_paths(self):
        a = logs.fingerprint("LogX", "Missing pin on node 12 at C:\\A\\b.uasset 0xDEADBEEF")
        b = logs.fingerprint("LogX", "Missing pin on node 99 at D:\\Z\\q.uasset 0x00000001")
        self.assertEqual(a, b)
        self.assertNotEqual(a, logs.fingerprint("LogY", "Missing pin on node 12 at C:\\A\\b.uasset 0xDEADBEEF"))

    def test_grouping_and_baseline(self):
        summary = logs.summarize(self.parse(), [])
        blueprint = sorted((g for g in summary["groups"] if g["category"] == "LogBlueprint"), key=lambda g: -g["count"])
        # Same error on the same asset groups together; a different asset is a different problem.
        self.assertEqual([g["count"] for g in blueprint], [2, 1])
        self.assertEqual(summary["groups"][0]["severity"], "Fatal")  # fatal sorts first
        again = logs.summarize(self.parse(), [], baseline=summary)
        self.assertEqual(again["newGroups"], 0)
        fewer = logs.summarize([e for e in self.parse() if e["severity"] != "Fatal"], [])
        with_new = logs.summarize(self.parse(), [], baseline=fewer)
        self.assertEqual(with_new["newGroups"], 1)

    def test_verdicts(self):
        empty = logs.summarize([], [])
        self.assertEqual(logs.verdict_of(empty), "pass")
        warn_only = logs.summarize([e for e in self.parse() if e["severity"] == "Warning"], [])
        self.assertEqual(logs.verdict_of(warn_only), "warn")
        errors = [e for e in self.parse() if e["severity"] == "Error"]
        self.assertEqual(logs.verdict_of(logs.summarize(errors, [])), "warn")  # no baseline: not new
        base = logs.summarize([], [])
        self.assertEqual(logs.verdict_of(logs.summarize(errors, [], baseline=base)), "fail")  # new errors
        self.assertEqual(logs.verdict_of(logs.summarize(errors, [], baseline=base), warn_on_new=False), "warn")
        self.assertEqual(logs.verdict_of(logs.summarize(self.parse(), [])), "fail")  # fatal

    def test_crash_context(self):
        crash = self.tmp / "Crashes" / "UECC-1"
        write(crash / "CrashContext.runtime-xml",
              "<FGenericCrashContext><RuntimeProperties><ErrorMessage>Access violation</ErrorMessage>"
              "<CallStack>A()\nB()\n</CallStack><EngineVersion>5.7.4</EngineVersion></RuntimeProperties></FGenericCrashContext>")
        write(self.tmp / "Crashes" / "UECC-2" / "CrashContext.runtime-xml", "<broken")
        crashes = logs.parse_crashes(self.tmp / "Crashes")
        self.assertEqual(len(crashes), 2)
        self.assertEqual(crashes[0]["error"], "Access violation")
        self.assertEqual(crashes[0]["callstack"], ["A()", "B()"])
        self.assertIn("unreadable", crashes[1]["error"])
        self.assertEqual(logs.verdict_of(logs.summarize([], crashes)), "fail")  # no baseline: all new

    def test_old_crashes_only_warn(self):
        old = [{"folder": "UECC-1", "error": "x", "callstack": []}]
        base = logs.summarize([], [dict(c) for c in old])
        again = logs.summarize([], [dict(c) for c in old], baseline=base)
        self.assertEqual(again["newCrashes"], 0)
        self.assertEqual(logs.verdict_of(again), "warn")
        more = logs.summarize([], [dict(c) for c in old] + [{"folder": "UECC-2", "error": "y", "callstack": []}], baseline=base)
        self.assertEqual(more["newCrashes"], 1)
        self.assertEqual(logs.verdict_of(more), "fail")

    def test_load_json_accepts_bom(self):
        p = self.tmp / "bom.uproject"
        p.write_bytes(b"\xef\xbb\xbf" + b'{"FileVersion": 3}')
        self.assertEqual(analyze.load_json(p)["FileVersion"], 3)


# =============================================================================== analyzer

class EnsureFoldingTests(TempDir):
    def test_an_ensure_is_one_error_with_its_stack(self):
        log = write(self.tmp / "compile.log", "\n".join([
            "[2026.11.16-16.08.16:052][  0]LogStats: FPlatformStackWalk::StackWalkAndDump -  0.799 s",
            "[2026.11.16-16.08.16:052][  0]LogOutputDevice: Error: === Handled ensure: ===",
            "[2026.11.16-16.08.16:061][  0]LogOutputDevice: Error: ",
            "[2026.11.16-16.08.16:068][  0]LogOutputDevice: Error: Ensure condition failed: Seen.Contains(It.Key())  "
            "[File:D:\\build\\WidgetBlueprintCompiler.cpp] [Line: 828] ",
            "[2026.11.16-16.08.16:094][  0]LogOutputDevice: Error: Variable [Thumb] was deleted but still has a GUID",
            "[2026.11.16-16.08.16:114][  0]LogOutputDevice: Error: Stack: ",
            "[2026.11.16-16.08.16:121][  0]LogOutputDevice: Error: [Callstack] 0x00007ffdfc7518c0 UnrealEditor-UMGEditor.dll!A()",
            "[2026.11.16-16.08.16:156][  0]LogOutputDevice: Error: [Callstack] 0x00007ffdfc6438aa UnrealEditor-UMGEditor.dll!B()",
            "[2026.11.16-16.08.17:000][  0]LogBlueprint: Error: [Compiler] Real error after the ensure",
        ]))
        events = logs.parse_file(log, set())
        self.assertEqual(len(events), 2, [e["message"] for e in events])
        ensure = events[0]
        self.assertTrue(ensure["message"].startswith("Ensure condition failed"))
        self.assertIn("Variable [Thumb] was deleted", ensure["message"])
        self.assertEqual(len(ensure["detail"]), 2)
        self.assertTrue(ensure["detail"][0].startswith("0x00007ffdfc7518c0"))
        self.assertEqual(events[1]["category"], "LogBlueprint")


class AnalyzerHelperTests(unittest.TestCase):
    def test_normalize_skeleton_class(self):
        self.assertEqual(analyze.normalize_class("/Game/W.SKEL_W_C"), "/Game/W.W_C")
        self.assertEqual(analyze.normalize_class("/Script/UMG.UserWidget"), "/Script/UMG.UserWidget")

    def test_collapse_external_actor_packages(self):
        roots = ["/Game"]
        self.assertEqual(analyze.collapse_package("/Game/__ExternalActors__/Maps/L1/5/1A/ABC", roots), "/Game/Maps/L1")
        self.assertEqual(analyze.collapse_package("/Game/BP_A", roots), "/Game/BP_A")

    def test_tarjan_cycles(self):
        cycles = analyze.tarjan({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}})
        self.assertEqual(cycles, [["a", "b", "c"]])
        self.assertEqual(analyze.tarjan({"a": {"b"}, "b": set()}), [])

    def test_reach_counts(self):
        graph = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}, "e": set()}
        self.assertEqual(analyze.reach_counts(graph, ["a", "b", "c", "d", "e"]), {"a": 2, "b": 2, "c": 2, "d": 3, "e": 0})
        long_chain = {str(i): {str(i + 1)} for i in range(3000)}
        counts = analyze.reach_counts(long_chain, ["0", "2999"])
        self.assertEqual(counts, {"0": 3000, "2999": 1})

    def test_tarjan_deep_chain_has_no_recursion_error(self):
        chain = {str(i): {str(i + 1)} for i in range(5000)}
        self.assertEqual(analyze.tarjan(chain), [])


class AnalyzerFixtureTests(TempDir):
    """Runs the full analyzer on real data extracted from a Blueprint-only project."""

    def run_cli(self, out: Path) -> Path:
        cmd = [sys.executable, str(TOOLS / "lens_analyze.py"), "--raw", str(FIXTURE),
               "--project", str(FIXTURE / "temp_proj.uproject"), "--out", str(out), "--run-id", "test"]
        subprocess.run(cmd, check=True, capture_output=True)
        return out

    def test_graph_contents(self):
        out = self.run_cli(self.tmp / "out")
        graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
        keys = {s["key"] for s in graph["symbols"]}
        self.assertIn("bp:/Game/WBP_Item.WBP_Item_C", keys)
        inherits = {(r["source"], r["target"]) for r in graph["relations"] if r["type"] == "Inherits"}
        self.assertIn(("bp:/Game/WBP_Item.WBP_Item_C", "uclass:/Script/UMG.UserWidget"), inherits)
        hard = {(r["source"], r["target"]) for r in graph["relations"] if r["type"] == "AssetHardRef"}
        self.assertIn(("asset:/Game/WBP_ListView", "asset:/Game/WBP_Item"), hard)
        self.assertTrue(all(r["status"] in ("verified", "rule", "inferred", "unknown") for r in graph["relations"]))
        for name in ("report.html", "index.db", "class_uml.puml", "findings.json", "quality.json", "statemonitor.json", "features.json"):
            self.assertTrue((out / name).exists(), name)
        self.assertFalse((out / "flows").exists(), "the feature view replaced the Mermaid exec graphs")

    def test_deterministic_output(self):
        a = self.run_cli(self.tmp / "a")
        b = self.run_cli(self.tmp / "b")
        for name in ("graph.json", "findings.json", "quality.json", "class_uml.mmd", "features.json"):
            ha = hashlib.sha256((a / name).read_bytes()).hexdigest()
            hb = hashlib.sha256((b / name).read_bytes()).hexdigest()
            self.assertEqual(ha, hb, f"{name} differs between identical runs")

    def test_sqlite_index(self):
        import sqlite3
        out = self.run_cli(self.tmp / "out")
        db = sqlite3.connect(out / "index.db")
        (count,) = db.execute("SELECT COUNT(*) FROM relation WHERE relation_type = 'Inherits'").fetchone()
        db.close()
        self.assertGreater(count, 0)


class ModuleScanTests(TempDir):
    def test_header_locations_for_the_architecture_tree(self):
        write(self.tmp / "Source/Game/UI/ShopWidget.h",
              "#pragma once\nUCLASS(BlueprintType, meta=(DisplayName=\"Shop\"))\nclass GAME_API UShopWidget : public UUserWidget\n{\n};\n"
              "USTRUCT(BlueprintType)\nstruct FShopItem\n{\n};\n")
        write(self.tmp / "Source/Game/GameCharacter.h", "UCLASS()\nclass AGameCharacter : public ACharacter {};\n")
        write(self.tmp / "Source/Game/Plain.h", "class FNotReflected {};\n")
        write(self.tmp / "Plugins/glTFRuntime/Source/glTFRuntime/Public/glTFRuntimeAsset.h",
              "UCLASS(BlueprintType)\nclass GLTFRUNTIME_API UglTFRuntimeAsset : public UObject {};\n")
        write(self.tmp / "Plugins/glTFRuntime/Intermediate/Build/x.generated.h", "UCLASS()\nclass UIgnored : public UObject {};\n")
        found = analyze.scan_headers(self.tmp)
        self.assertEqual(found["ShopWidget"], "Source/Game/UI/ShopWidget.h")
        self.assertEqual(found["ShopItem"], "Source/Game/UI/ShopWidget.h")
        self.assertEqual(found["GameCharacter"], "Source/Game/GameCharacter.h")
        self.assertEqual(found["glTFRuntimeAsset"], "Plugins/glTFRuntime/Source/glTFRuntime/Public/glTFRuntimeAsset.h",
                         "reflection drops the prefix even before a lower-case letter")
        self.assertNotIn("NotReflected", found)
        self.assertNotIn("Ignored", found)

    def test_build_cs_dependencies_and_cycle(self):
        write(self.tmp / "P.uproject", json.dumps({"Modules": [{"Name": "Game"}]}))
        write(self.tmp / "Source/Game/Game.Build.cs",
              'PublicDependencyModuleNames.AddRange(new string[] { "Core", "Ui" });\n'
              '// PrivateDependencyModuleNames.Add("Commented");\n')
        write(self.tmp / "Source/Ui/Ui.Build.cs", 'PrivateDependencyModuleNames.Add("Game");\n')
        g = analyze.Graph()
        deps = analyze.scan_modules(self.tmp / "P.uproject", g)
        self.assertEqual(deps["Game"], {"Core", "Ui"})
        self.assertNotIn("Commented", deps["Game"])
        self.assertTrue(g.symbols["module:Core"]["external"])
        self.assertFalse(g.symbols["module:Ui"]["external"])
        ctx = {"module_deps": deps, "asset_hard": {}, "meta": {}, "tick_users": [], "asset_class": {}}
        result = analyze.run_checks(g, ctx)
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["findings"][0]["rule"], "module-cycle")


CHARACTER_CPP = """
#include "HeroCharacter.h"

void AHeroCharacter::BeginPlay()
{
	Super::BeginPlay();
	// a comment with Fake(); calls and { braces
	ResetHealth();
	GetWorld()->GetTimerManager().SetTimer(RegenTimer, this, &AHeroCharacter::Regen, 1.0f, true);
}

void AHeroCharacter::SetupPlayerInputComponent(UInputComponent* PlayerInputComponent)
{
	if (UEnhancedInputComponent* Input = Cast<UEnhancedInputComponent>(PlayerInputComponent))
	{
		Input->BindAction(JumpAction, ETriggerEvent::Started, this, &AHeroCharacter::DoJump);
		Input->BindAction(FireAction, ETriggerEvent::Triggered, this, &AHeroCharacter::Fire);
	}
	Hitbox->OnComponentBeginOverlap.AddDynamic(this, &AHeroCharacter::OnHitboxOverlap);
}

void AHeroCharacter::DoJump()
{
	if (bCanJump && !IsDead())
	{
		Jump();
	}
	else
	{
		PlayFailSound();
	}
	JumpCount += 1;
}

void AHeroCharacter::Fire()
{
	const FVector Muzzle(0.f, 0.f, 10.f);
	for (int32 i = 0; i < Pellets; ++i)
	{
		GetWorld()->SpawnActor<AProjectile>(ProjectileClass, Muzzle, FRotator::ZeroRotator);
	}
}

void AHeroCharacter::ResetHealth() { Health = MaxHealth; }
void AHeroCharacter::Regen() { Health = FMath::Min(Health + 1.f, MaxHealth); }
void AHeroCharacter::OnHitboxOverlap(UPrimitiveComponent* Comp, AActor* Other, UPrimitiveComponent* OtherComp, int32 Index, bool bSweep, const FHitResult& Hit)
{
	TakeHit(Other);
}
void AHeroCharacter::Heal_Implementation(float Amount) { Health += Amount; }
"""


def bp_node(nid, cls, title, **kw):
    return {"id": nid, "class": cls, "title": title, "role": kw.pop("role", "other"), **kw}


class FeatureTests(TempDir):
    """Features: entry points from C++ source and Blueprint graphs, and the steps they run."""

    def build(self):
        write(self.tmp / "Source/Game/HeroCharacter.cpp", CHARACTER_CPP)
        reflection = {"native": [{"path": "/Script/Game.HeroCharacter", "name": "HeroCharacter", "super": "/Script/Engine.Character",
                                  "functions": [{"name": "Heal", "flags": ["Native", "Net", "NetServer"]},
                                                {"name": "DoJump", "flags": ["Native", "BlueprintCallable"]}]}],
                      "blueprint": []}
        graph = {"name": "EventGraph", "kind": "event", "nodes": [
            bp_node("e1", "K2Node_Event", "BeginPlay 이벤트", role="event", member="ReceiveBeginPlay"),
            bp_node("b1", "K2Node_IfThenElse", "Branch"),
            bp_node("c1", "K2Node_CallFunction", "Do Jump", role="call", member="DoJump", memberOwner="/Script/Game.HeroCharacter"),
            bp_node("c2", "K2Node_CallFunction", "Print String", role="call", member="PrintString",
                    memberOwner="/Script/Engine.KismetSystemLibrary"),
            bp_node("e2", "K2Node_Event", "Tick 이벤트", role="event", member="ReceiveTick"),   # unwired: not a feature
            bp_node("e3", "K2Node_ComponentBoundEvent", "On Component Begin Overlap (Box)", role="event"),
            bp_node("s1", "K2Node_VariableSet", "Set", member="Score"),
        ], "links": [
            {"from": "e1", "fromPin": "then", "to": "b1", "toPin": "execute", "kind": "exec"},
            {"from": "b1", "fromPin": "then", "to": "c1", "toPin": "execute", "kind": "exec"},
            {"from": "b1", "fromPin": "else", "to": "c2", "toPin": "execute", "kind": "exec"},
            {"from": "e3", "fromPin": "then", "to": "s1", "toPin": "execute", "kind": "exec"},
        ]}
        bp = {"asset": "/Game/BP_Hero.BP_Hero", "generatedClass": "/Game/BP_Hero.SKEL_BP_Hero_C",
              "parent": "/Script/Game.HeroCharacter", "graphs": [graph]}
        return features.build_features({"reflection": reflection, "blueprints": [bp]}, self.tmp, [])

    def test_cpp_entry_points_by_category(self):
        doc = self.build()
        hero = next(c for c in doc["classes"] if c["key"] == "uclass:/Script/Game.HeroCharacter")
        by_name = {f["name"]: f for f in hero["features"]}
        self.assertEqual(by_name["BeginPlay"]["category"], "lifecycle")
        self.assertEqual(by_name["Jump · Started"]["category"], "input")
        self.assertEqual(by_name["Fire · Triggered"]["category"], "input")
        self.assertEqual(by_name["Hitbox.OnComponentBeginOverlap"]["category"], "collision")
        self.assertEqual(by_name["타이머 → Regen"]["category"], "timer")
        self.assertEqual(by_name["Heal"]["category"], "network")
        self.assertTrue(by_name["BeginPlay"]["where"].startswith("Source/Game/HeroCharacter.cpp:"))
        self.assertTrue(all(f["estimated"] for f in hero["features"]))
        self.assertNotIn("DoJump", by_name, "a bound input handler is not listed again as a public function")
        texts = [s["text"] for s in by_name["BeginPlay"]["steps"]]
        self.assertIn("ResetHealth", texts)
        self.assertNotIn("Fake", " ".join(texts), "comments are not code")

    def test_cpp_flow_has_branches_loops_and_expansion(self):
        doc = self.build()
        hero = next(c for c in doc["classes"] if c["key"] == "uclass:/Script/Game.HeroCharacter")
        jump = next(f for f in hero["features"] if f["name"] == "Jump · Started")
        call = jump["steps"][0]
        self.assertEqual(call["text"], "DoJump")
        branch = call["inner"][0]
        self.assertEqual(branch["kind"], "branch")
        self.assertEqual([b["label"] for b in branch["branches"]], ["참", "거짓"])
        self.assertEqual(branch["branches"][0]["steps"][0]["text"], "Jump")
        self.assertEqual(branch["branches"][1]["steps"][0]["text"], "PlayFailSound")
        self.assertEqual(call["inner"][1], {"kind": "set", "text": "설정 JumpCount", "estimated": True})
        fire = next(f for f in hero["features"] if f["name"] == "Fire · Triggered")
        inner = fire["steps"][0]["inner"]
        self.assertNotIn("Muzzle", [s["text"] for s in inner], "a declared variable is not a call")
        loop = next(s for s in inner if s["kind"] == "loop")
        self.assertEqual(loop["branches"][0]["steps"][0]["kind"], "spawn")

    def test_blueprint_events_walk_exec_wires_into_cpp(self):
        doc = self.build()
        bp = next(c for c in doc["classes"] if c["key"] == "bp:/Game/BP_Hero.BP_Hero_C")
        self.assertEqual(bp["name"], "BP_Hero")
        self.assertEqual(bp["parent"], "uclass:/Script/Game.HeroCharacter")
        names = [f["name"] for f in bp["features"]]
        self.assertNotIn("Tick 이벤트", names, "an unwired default event does nothing")
        begin = next(f for f in bp["features"] if f["name"] == "BeginPlay 이벤트")
        self.assertEqual(begin["category"], "lifecycle")
        branch = begin["steps"][0]
        self.assertEqual(branch["kind"], "branch")
        labels = {b["label"]: b["steps"] for b in branch["branches"]}
        self.assertEqual(set(labels), {"참", "거짓"})
        jump = labels["참"][0]
        self.assertEqual(jump["target"], "uclass:/Script/Game.HeroCharacter")
        self.assertEqual(jump["inner"][0]["kind"], "branch", "the BP call continues into the C++ body")
        overlap = next(f for f in bp["features"] if f["name"].startswith("On Component Begin Overlap"))
        self.assertEqual(overlap["category"], "collision")
        self.assertEqual(overlap["steps"], [{"kind": "set", "text": "설정 Score"}])

    def test_report_section(self):
        html_text = features.render_html(self.build())
        self.assertIn("<h2>기능</h2>", html_text)
        self.assertIn("Jump · Started", html_text)
        self.assertIn("C++ 소스 추정", html_text)


def feat(fid, name, category, steps, owner="Hero"):
    return {"id": fid, "name": name, "category": category, "trigger": name, "owner": owner, "source": "cpp", "where": "x", "steps": steps}


def call(text, inner=None, **kw):
    return {"kind": "call", "text": text, **({"inner": inner} if inner else {}), **kw}


class BehaviorTests(unittest.TestCase):
    """Entry points grouped into what the class does (Jump, Move, Open Door)."""

    def jump_classes(self):
        multi = [{"kind": "branch", "text": "조건: !GetCharacterMovement()->IsFalling()", "branches": [{"label": "참", "steps": [call("Jump")]}]},
                 {"kind": "branch", "text": "조건: !bHasDoubleJumped", "branches": [{"label": "참", "steps": [
                     call("Jump"), {"kind": "set", "text": "설정 bHasDoubleJumped"}]}]}]
        start = [call("MultiJump", multi)]
        hero = {"key": "uclass:/Script/G.Hero", "name": "Hero", "kind": "class", "features": [
            feat("h#0", "Jump · Started", "input", [call("DoJumpStart", start)]),
            feat("h#1", "Jump · Completed", "input", [call("DoJumpEnd", [call("StopJumping")])]),
            feat("h#2", "타이머 → ResetWallJump", "timer", [call("ResetWallJump", [{"kind": "set", "text": "설정 bHasWallJumped"}])]),
            feat("h#3", "Landed", "lifecycle", [{"kind": "set", "text": "설정 bHasDoubleJumped"}]),
            feat("h#4", "Move · Triggered", "input", [call("DoMove", [call("AddMovementInput")])]),
            feat("h#5", "EndPlay", "lifecycle", [call("ClearTimer")]),
        ]}
        bp = {"key": "bp:/Game/BP_Hero.BP_Hero_C", "name": "BP_Hero", "kind": "blueprint", "parent": "uclass:/Script/G.Hero", "features": [
            feat("b#0", "Touch Jump Start 이벤트", "interface", [call("Hero.DoJumpStart", start, target="uclass:/Script/G.Hero")], "BP_Hero"),
            feat("b#1", "ActorBeginOverlap 이벤트", "collision", [call("Open Door")], "BP_Hero"),
            feat("b#2", "Open Door", "event", [{"kind": "timer", "text": "타임라인 Door"}], "BP_Hero"),
        ]}
        classes = {hero["key"]: hero, bp["key"]: bp}
        behaviors.infer(classes)
        return hero, bp

    def test_entry_points_of_one_behavior_are_grouped(self):
        hero, _ = self.jump_classes()
        by_name = {b["nameEn"]: b for b in hero["behaviors"]}
        jump = by_name["Jump"]
        self.assertEqual(jump["name"], "점프")
        self.assertEqual(set(jump["features"]), {"h#0", "h#1", "h#2", "h#3", "b#0"},
                         "input pressed/released, the child BP's touch event, the timer and Landed (shared state) are one behavior")
        self.assertTrue(jump["player"])
        self.assertEqual(by_name["Move"]["features"], ["h#4"])
        self.assertEqual(by_name["Lifecycle"]["features"], ["h#5"])
        self.assertEqual(hero["behaviors"][-1]["nameEn"], "Lifecycle", "lifecycle comes last")

    def test_core_function_and_its_cases(self):
        hero, _ = self.jump_classes()
        jump = next(b for b in hero["behaviors"] if b["nameEn"] == "Jump")
        self.assertEqual(jump["primary"], "MultiJump", "followed through DoJumpStart to the function with the logic")
        whens = [c["when"] for c in jump["cases"]]
        self.assertIn("!bHasDoubleJumped", whens)
        self.assertIn("bHasDoubleJumped", jump["writes"])
        self.assertNotIn("GetCharacterMovement", " ".join(jump["states"]), "calls are not states")

    def test_engine_event_is_named_by_what_it_calls(self):
        _, bp = self.jump_classes()
        door = next(b for b in bp["behaviors"] if b["nameEn"] == "Open Door")
        self.assertEqual(door["name"], "문 열기", "Korean puts the verb last")
        self.assertEqual(set(door["features"]), {"b#1", "b#2"})

    def test_words(self):
        self.assertEqual(behaviors.stem("DoJumpStart"), ("jump",))
        self.assertEqual(behaviors.stem("ChargedAttackPressed"), ("charged", "attack"))
        self.assertEqual(behaviors.stem("CloseButton.OnClicked"), (), "an event suffix alone names nothing")
        self.assertEqual(behaviors.display(("toggle", "camera")), ("카메라 전환", "Toggle Camera"))


class ClassModelTests(unittest.TestCase):
    def graph(self):
        g = analyze.Graph()
        g.symbol("class:/Script/Engine.Actor", "class", "Actor", external=True, module="Engine")
        for folder in ("/Game/A/BP_Door", "/Game/B/BP_Door"):   # same class name in two folders
            key = g.symbol(f"bp:{folder}.BP_Door_C", "blueprint", "BP_Door_C", package=folder, asset_class="Blueprint",
                           properties=[{"name": "Open Speed ", "type": "double"}], functions=["Open Door"])
            g.relate(key, "class:/Script/Engine.Actor", "Inherits", "parent")
        g.symbol("bp:/Game/A/BP_Empty.BP_Empty_C", "blueprint", "BP_Empty_C", package="/Game/A/BP_Empty")
        return g

    def test_mermaid_has_no_empty_class_body(self):
        mmd, _ = analyze.class_diagram(self.graph())
        self.assertIn("class BP_Empty_C\n", mmd + "\n")
        self.assertNotRegex(mmd, r"\{\s*\}")   # Mermaid 10 rejects "class X {" + "}"

    def test_xmi_packages_keep_same_names_apart(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(analyze.class_xmi(self.graph(), "P"))
        t = "{http://schema.omg.org/spec/XMI/2.1}type"
        classes = [e for e in root.iter() if e.get(t) == "uml:Class"]
        self.assertEqual(sorted(c.get("name") for c in classes), ["Actor", "BP_Door_C", "BP_Door_C", "BP_Empty_C"])
        parents = {}
        for pkg in (e for e in root.iter() if e.get(t) == "uml:Package"):
            for child in pkg:
                if child.get(t) == "uml:Class":
                    parents.setdefault(child.get("name"), set()).add(pkg.get("name"))
        self.assertEqual(parents["BP_Door_C"], {"A", "B"})
        self.assertEqual(parents["Actor"], {"Engine"})
        gens = [e for e in root.iter() if e.get(t) == "uml:Generalization"]
        self.assertEqual(len(gens), 2)
        attr = next(e for e in root.iter() if e.get(t) == "uml:Property")
        self.assertEqual(attr.get("name"), "Open Speed")


# =============================================================================== quality checks

class QualityTests(TempDir):
    def ctx(self, **extra):
        base = {"roots": ["/Game"], "asset_class": {}, "asset_hard": {}, "asset_soft": {},
                "bp_index": {}, "project_file": write(self.tmp / "P.uproject", "{}")}
        base.update(extra)
        return base

    def test_load_config_merges_sections(self):
        a = write(self.tmp / "a.json", json.dumps({"naming": {"forbidSpaces": True, "prefixes": {"Blueprint": ["BP_"]}}}))
        b = write(self.tmp / "b.json", json.dumps({"naming": {"forbidSpaces": False}}))
        cfg = quality.load_config([a, b, self.tmp / "missing.json"])
        self.assertFalse(cfg["naming"]["forbidSpaces"])
        self.assertEqual(cfg["naming"]["prefixes"]["Blueprint"], ["BP_"])

    def test_naming_rules(self):
        cfg = {"naming": {"prefixes": {"Blueprint": ["BP_"], "World": []}, "forbidAssetsAtContentRoot": True,
                          "forbidSpaces": True, "forbidNonAsciiNames": True, "ignoreFolders": ["/Game/Developers"]}}
        ctx = self.ctx(asset_class={
            "/Game/Chars/BP_Hero": "/Script/Engine.Blueprint",
            "/Game/Chars/Hero": "/Script/Engine.Blueprint",
            "/Game/BP_Root": "/Script/Engine.Blueprint",
            "/Game/Maps/My Map": "/Script/Engine.World",
            "/Game/Maps/맵": "/Script/Engine.World",
            "/Game/Developers/x/Thing": "/Script/Engine.Blueprint",
        })
        findings, data = quality.check_naming(ctx, cfg)
        bad = {v["asset"]: v["problems"] for v in data["violations"]}
        self.assertNotIn("/Game/Chars/BP_Hero", bad)
        self.assertIn("/Game/Chars/Hero", bad)
        self.assertIn("콘텐츠 루트에 직접 위치", bad["/Game/BP_Root"])
        self.assertIn("이름에 공백", bad["/Game/Maps/My Map"])
        self.assertIn("이름에 비ASCII 문자", bad["/Game/Maps/맵"])
        self.assertNotIn("/Game/Developers/x/Thing", bad)
        self.assertEqual(findings[0]["severity"], "warn")

    def test_unused_assets_and_redirectors(self):
        write(self.tmp / "Config/DefaultEngine.ini", "GameDefaultMap=/Game/Maps/Start.Start\n")
        ctx = self.ctx(asset_class={
            "/Game/Maps/Start": "/Script/Engine.World",
            "/Game/Maps/Other": "/Script/Engine.World",
            "/Game/UI/W_Hud": "/Script/UMGEditor.WidgetBlueprint",
            "/Game/UI/W_Old": "/Script/UMGEditor.WidgetBlueprint",
            "/Game/UI/W_Font": "/Script/Engine.Font",
            "/Game/UI/Redir": "/Script/CoreUObject.ObjectRedirector",
        }, asset_hard={"/Game/Maps/Start": {"/Game/UI/W_Hud"}}, asset_soft={"/Game/UI/W_Hud": {"/Game/UI/W_Font"}})
        findings, data = quality.check_unused(ctx, {"unused": {}})
        self.assertEqual(data["unused"], ["/Game/UI/W_Old"])
        self.assertEqual(data["redirectors"], ["/Game/UI/Redir"])
        self.assertIn("/Game/Maps/Start", data["iniRoots"])
        self.assertEqual({f["rule"] for f in findings}, {"unused-asset", "redirector"})

    def test_complexity_handles_cycles_and_thresholds(self):
        nodes = [{"id": str(i), "class": "K2Node_CallFunction"} for i in range(6)]
        nodes.append({"id": "b", "class": "K2Node_IfThenElse"})
        links = [{"from": str(i), "to": str(i + 1), "kind": "exec"} for i in range(5)]
        links.append({"from": "5", "to": "0", "kind": "exec"})  # loop back
        bp = {"asset": "/Game/BP_Loop.BP_Loop", "graphs": [{"name": "EventGraph", "kind": "event", "nodes": nodes, "links": links}]}
        findings, data = quality.check_complexity(self.ctx(bp_index={"bp:x": bp}),
                                                  {"complexity": {"warnNodesPerGraph": 5, "warnBranchesPerGraph": 5, "warnExecChainLength": 100}})
        row = data["graphs"][0]
        self.assertEqual(row["nodes"], 7)
        self.assertEqual(row["branches"], 1)
        self.assertEqual(row["execChain"], 6)
        self.assertEqual(row["over"], ["노드 수"])
        self.assertEqual(findings[0]["rule"], "bp-complexity")

    def test_state_monitor_detection(self):
        write(self.tmp / "P.uproject", json.dumps({"Plugins": [{"Name": "StateMonitor", "Enabled": True}]}))
        write(self.tmp / "Plugins/StateMonitor/StateMonitor.uplugin", "{}")
        write(self.tmp / "Plugins/__DwoLensInspector/StateMonitor.uplugin", "{}")  # never counted
        write(self.tmp / "Source/Game/Hud.cpp",
              '#include "StateMonitorSubsystem.h"\n'
              'void F(UObject* W) {\n'
              '  UStateMonitorSubsystem::Get(W)->Submit(TEXT("Player"), State);\n'
              '  UStateMonitorLibrary::SubmitStructSnapshot(W, TEXT("Wave"), FText(), T, &D);\n'
              '}\n')
        bp = {"asset": "/Game/BP_A.BP_A", "graphs": [{"name": "EventGraph", "nodes": [
            {"id": "n1", "role": "call", "member": "SubmitStateSnapshot", "memberOwner": "/Script/StateMonitor.StateMonitorLibrary"}]}]}
        findings, data = quality.check_state_monitor(self.ctx(bp_index={"bp:a": bp}), {"stateMonitor": {}})
        self.assertTrue(data["present"] and data["enabled"])
        self.assertEqual(data["sourceIds"], ["Player", "Wave"])
        self.assertEqual(len([r for r in data["cppRefs"] if r["submit"]]), 2)
        self.assertEqual(len(data["bpCalls"]), 1)
        self.assertEqual(data["testFilter"], "StateMonitor.")
        self.assertEqual(findings[0]["rule"], "state-monitor")

    def test_dwo_state_monitor_detection(self):
        # dwookit installs the renamed plugin as Plugins/__DwoStateMonitor; BP nodes reference /Script/DwoStateMonitor.
        write(self.tmp / "P.uproject", json.dumps({"Plugins": []}))
        write(self.tmp / "Plugins/__DwoStateMonitor/DwoStateMonitor.uplugin", "{}")
        bp = {"asset": "/Game/BP_A.BP_A", "graphs": [{"name": "EventGraph", "nodes": [
            {"id": "n1", "role": "call", "member": "SubmitStateSnapshot", "memberOwner": "/Script/DwoStateMonitor.StateMonitorLibrary"},
            {"id": "n2", "role": "call", "member": "WatchStateObject", "memberOwner": "/Script/StateMonitor.StateMonitorLibrary"}]}]}
        cfg = json.loads((ROOT / "config" / "default.json").read_text(encoding="utf-8"))
        findings, data = quality.check_state_monitor(self.ctx(bp_index={"bp:a": bp}), cfg)
        self.assertTrue(data["present"] and data["enabled"])
        self.assertEqual(data["plugin"].replace("\\", "/"), "Plugins/__DwoStateMonitor/DwoStateMonitor.uplugin")
        self.assertEqual(len(data["bpCalls"]), 2, "assets saved against the old module still count")
        self.assertEqual(data["testFilter"], "StateMonitor.")

    def test_state_monitor_absent(self):
        findings, data = quality.check_state_monitor(self.ctx(), {"stateMonitor": {}})
        self.assertFalse(data["present"])
        self.assertIsNone(data["testFilter"])
        self.assertEqual(findings, [])


# =============================================================================== verify line

class VerifyTests(TempDir):
    def automation(self, tests):
        index = {"succeeded": sum(t["state"] == "Success" for t in tests), "succeededWithWarnings": 0,
                 "failed": sum(t["state"] == "Fail" for t in tests), "notRun": 0, "tests": tests}
        path = write(self.tmp / "run/automation/index.json", "")
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(index).encode("utf-8"))  # UE writes a BOM

    def test_worst_ignores_info(self):
        self.assertEqual(verify.worst(["pass", "info"]), "pass")
        self.assertEqual(verify.worst(["pass", "warn", "fail"]), "fail")
        self.assertEqual(verify.worst([]), "pass")

    def test_all_pass(self):
        write(self.tmp / "run/compile.log", "[2026.10.27-16.40.57:829][  0]LogBlueprint: Display: ok\n")
        write(self.tmp / "run/datavalidation.log", "")
        self.automation([{"fullTestPath": "Project.A", "state": "Success", "entries": []}])
        result = verify.verify(self.tmp / "run", None)
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual([s["verdict"] for s in result["steps"]], ["pass", "pass", "pass"])

    def test_failed_test_and_compile_error(self):
        write(self.tmp / "run/compile.log", "[2026.10.27-16.40.57:829][  0]LogBlueprint: Error: [Compiler] broken\n")
        self.automation([{"fullTestPath": "Project.A", "state": "Fail",
                          "entries": [{"event": {"type": "Error", "message": "expected 1"}}]}])
        result = verify.verify(self.tmp / "run", None)
        steps = {s["id"]: s for s in result["steps"]}
        self.assertEqual(steps["V1"]["verdict"], "warn")  # errors without a baseline are reported, not new
        self.assertEqual(steps["V2"]["verdict"], "skip")
        self.assertEqual(steps["V3"]["verdict"], "fail")
        self.assertEqual(steps["V3"]["automation"]["tests"][0]["errors"], ["expected 1"])
        self.assertEqual(result["verdict"], "fail")

    def test_no_matching_tests_is_info(self):
        self.automation([])
        result = verify.verify(self.tmp / "run", None)
        self.assertEqual({s["id"]: s["verdict"] for s in result["steps"]}["V3"], "info")
        self.assertEqual(result["verdict"], "pass")

    def test_automation_log_without_report_fails(self):
        write(self.tmp / "run/automation.log", "crashed before writing the report")
        result = verify.verify(self.tmp / "run", None)
        self.assertEqual({s["id"]: s["verdict"] for s in result["steps"]}["V3"], "fail")


# =============================================================================== runtime lines

class PerfCsvTests(TempDir):
    def csv(self, frames):
        rows = ["EVENTS,FrameTime,GameThreadTime,RenderThreadTime,RHIThreadTime"]
        rows += [f",{ft},{ft / 2},{ft / 3},1.0" for ft in frames]
        rows += ["EVENTS,FrameTime,GameThreadTime,RenderThreadTime,RHIThreadTime",
                 "[HasHeaderRowAtEnd],1,[platform],Windows,[config],Development"]
        return write(self.tmp / "p.csv", "\n".join(rows) + "\n")

    def test_reads_rows_until_trailing_header_and_metadata(self):
        cols = runtime.read_profile_csv(self.csv([10, 20, 30]))
        self.assertEqual(cols["FrameTime"], [10.0, 20.0, 30.0])
        self.assertEqual(cols["RHIThreadTime"], [1.0, 1.0, 1.0])
        self.assertNotIn("EVENTS", cols)  # text-only column dropped

    def test_stats_percentiles_and_hitches(self):
        frames = [10.0] * 90 + [20.0] * 8 + [150.0, 200.0]
        cfg = {"perf": {"warmupFrames": 0, "budgetMs": 33.3, "hitchMs": 100}}
        r = runtime.perf(self.csv(frames), "/Game/M", cfg)
        ft = r["stats"]["FrameTime"]
        self.assertEqual((ft["p50"], ft["p95"], ft["p99"], ft["max"]), (10.0, 20.0, 150.0, 200.0))
        self.assertAlmostEqual(ft["avg"], 14.1, places=2)
        self.assertEqual([h["frame"] for h in r["hitches"]], [98, 99])
        self.assertEqual(r["verdict"], "warn")  # p95 within budget, hitches present
        self.assertEqual(r["frames"], 100)
        self.assertIn("GameThreadTime", r["stats"])

    def test_warmup_is_excluded_and_p95_over_budget_fails(self):
        r = runtime.perf(self.csv([500, 500] + [40] * 20), "/Game/M", {"perf": {"warmupFrames": 2, "hitchMs": 100}})
        self.assertEqual(r["frames"], 20)
        self.assertEqual(r["hitches"], [])
        self.assertEqual(r["verdict"], "fail")

    def test_missing_csv_fails(self):
        self.assertEqual(runtime.perf(None, "/Game/M", {})["verdict"], "fail")

    def test_downsample_keeps_peaks(self):
        values = [1.0] * 1200
        values[777] = 99.0
        out = runtime.downsample(values, 600)
        self.assertEqual(len(out), 600)
        self.assertIn(99.0, out)


class SizesTests(TempDir):
    def test_closure_weights_follow_hard_refs_and_cycles(self):
        graph = {"/Game/A": {"/Game/B"}, "/Game/B": {"/Game/C"}, "/Game/C": {"/Game/B"}, "/Game/D": set()}
        sizes = {"/Game/A": 1, "/Game/B": 10, "/Game/C": 100, "/Game/D": 1000}
        w = runtime.closure_weights(graph, sizes)
        self.assertEqual(w["/Game/A"], (111, 2))
        self.assertEqual(w["/Game/B"], (110, 1))  # B and C load each other
        self.assertEqual(w["/Game/D"], (1000, 0))

    def test_scan_folds_external_actors_and_ranks(self):
        c = self.tmp / "proj/Content"
        for rel, n in (("Maps/L_Main.umap", 100), ("__ExternalActors__/Maps/L_Main/0/AB/X1.uasset", 50),
                       ("Props/SM_Rock.uasset", 30), ("Props/SM_Rock.ubulk", 300), ("Props/T_Rock.uasset", 20),
                       ("readme.txt", 999)):
            (c / rel).parent.mkdir(parents=True, exist_ok=True)
            (c / rel).write_bytes(b"x" * n)
        graph = {"relations": [
            {"source": "asset:/Game/Maps/L_Main", "target": "asset:/Game/Props/SM_Rock", "type": "AssetHardRef"},
            {"source": "asset:/Game/Props/SM_Rock", "target": "asset:/Game/Props/T_Rock", "type": "AssetHardRef"},
            {"source": "asset:/Game/Props/T_Rock", "target": "asset:/Game/Maps/L_Main", "type": "AssetSoftRef"}]}
        r = runtime.sizes(self.tmp / "proj", {"sizes": {"warnAssetMB": 200 / 1048576}}, graph)
        self.assertEqual(r["totalBytes"], 500)
        self.assertEqual(r["assetCount"], 3)
        self.assertEqual(r["largest"][0], {"asset": "/Game/Props/SM_Rock", "bytes": 330})
        self.assertEqual(r["largest"][1], {"asset": "/Game/Maps/L_Main", "bytes": 150})
        self.assertEqual(r["heaviestClosures"][0], {"asset": "/Game/Maps/L_Main", "closureBytes": 500, "closureCount": 2})
        self.assertEqual({t["ext"] for t in r["byType"]}, {".umap", ".uasset", ".ubulk"})
        self.assertEqual(r["verdict"], "warn")
        self.assertEqual({o["asset"] for o in r["overLimit"]}, {"/Game/Props/SM_Rock"})
        self.assertEqual({f["folder"] for f in r["byFolder"]}, {"/Game/Maps", "/Game/Props"})

    def test_list_maps_skips_excluded_and_external(self):
        c = self.tmp / "proj/Content"
        for rel in ("Maps/A.umap", "Developers/me/Test.umap", "__ExternalActors__/Maps/A/0/X.umap", "B.umap"):
            write(c / rel, "")
        maps = runtime.list_maps(self.tmp / "proj", {"smoke": {"exclude": ["/Game/Developers/*"], "maxMaps": 20}})
        self.assertEqual(maps, ["/Game/B", "/Game/Maps/A"])
        self.assertEqual(runtime.list_maps(self.tmp / "proj", {"smoke": {"maxMaps": 1}}), ["/Game/B"])


class SmokeTests(TempDir):
    def run_smoke(self, log_text, **entry):
        log = write(self.tmp / "run/raw/smoke-1.log", log_text)
        e = {"map": "/Game/M", "log": str(log), "exitCode": None, "killed": True, "seconds": 15, **entry}
        write(self.tmp / "run/raw/smoke-runs.json", json.dumps(e))  # a single map arrives as an object
        return runtime.smoke(self.tmp / "run", self.tmp / "proj", {"smoke": {"maxErrors": 0, "maxWarnings": 1}})

    def test_clean_boot_passes(self):
        r = self.run_smoke("[2026.10.27-16.40.57:829][  0]LogInit: Display: ok\n")
        self.assertEqual((r["verdict"], r["maps"][0]["verdict"]), ("pass", "pass"))
        self.assertIsNone(r["maps"][0]["exitCode"])

    def test_ensure_fails(self):
        r = self.run_smoke("[2026.10.27-16.40.58:001][  1]LogOutputDevice: Warning: Ensure condition failed: x [File:a.cpp]\n")
        self.assertEqual(r["maps"][0]["ensures"], 1)
        self.assertEqual(r["verdict"], "fail")

    def test_errors_over_threshold_fail_and_warnings_warn(self):
        self.assertEqual(self.run_smoke("LogTemp: Error: boom\n")["verdict"], "fail")
        r = self.run_smoke("LogTemp: Warning: a\nLogTemp: Warning: b\n")
        self.assertEqual(r["verdict"], "warn")
        self.assertEqual(r["maps"][0]["warnings"], 2)

    def test_crash_exit_and_fatal(self):
        self.assertEqual(self.run_smoke("", exitCode=3, killed=False)["verdict"], "fail")
        r = self.run_smoke("[2026.10.27-16.40.58:001][  1]LogWindows: Error: === Critical error: ===\n")
        self.assertTrue(r["maps"][0]["fatal"])
        self.assertEqual(r["verdict"], "fail")

    def test_verdict_rules(self):
        base = {"errors": 0, "warnings": 0, "ensures": 0, "fatal": False, "killed": True, "exitCode": None}
        self.assertEqual(runtime.smoke_verdict(base, 0, 10)[0], "pass")
        self.assertEqual(runtime.smoke_verdict({**base, "errors": 2}, 5, 10)[0], "warn")
        self.assertEqual(runtime.smoke_verdict({**base, "errors": 6}, 5, 10)[0], "fail")
        self.assertEqual(runtime.smoke_verdict({**base, "killed": False, "exitCode": 0}, 0, 10)[0], "warn")
        self.assertEqual(runtime.smoke_verdict({**base, "logFound": False}, 0, 10)[0], "fail")


class BlueprintPseudocodeTests(TempDir):
    """lens_bp.py on real exports (tests/fixtures/bp_export, from the QA project) and small built graphs."""

    FIX = Path(__file__).resolve().parent / "fixtures" / "bp_export"

    def bp(self, rel: str) -> dict:
        name = rel.rsplit("/", 1)[-1]
        return json.loads((self.FIX / rel / f"{name}.json").read_text(encoding="utf-8"))

    def test_door_frame_reads_like_the_graph(self):
        md = lens_bp.render(self.bp("Game/LevelPrototyping/Interactable/Door/BP_DoorFrame"))
        for line in (
            "on BeginPlay():",
            "if isValid(Door):",                                  # validated get
            "Closed Position = Door.RelativeLocation",
            "Door Control.Play()",
            "on Update:",
            "Door Control.Reverse()  # outputs as at L1",          # second timeline input
            "== 0)",                                             # empty int default shown as 0
            "Lerp(A=Closed Position.X",                           # split struct pin
            "LocationX=Door Size.X",                              # BreakVector(V).X -> V.X
            "## timelines",
            "Door Control [0.3s] tracks: float:NewTrack_0",
            "## fn Set Mesh(NewMesh: StaticMesh, RelativeTransform: Transform, Material: MaterialInterface, NewMobility: EComponentMobility)",
            "# Store Door Position",                              # comment box
        ):
            self.assertIn(line, md)
        self.assertNotIn("BreakVector(", md)
        self.assertNotIn("Get()", md)
        # Every label is used, and labels are numbered from L1.
        labels = re.findall(r"^\s*(L\d+):$", md, re.M)
        for label in labels:
            self.assertRegex(md, rf"(goto|at) {label}\b")
        self.assertEqual(labels, [f"L{i + 1}" for i in range(len(labels))])

    def test_pseudocode_is_far_smaller_than_the_export(self):
        rel = "Game/LevelPrototyping/Interactable/Door/BP_DoorFrame"
        size = (self.FIX / rel / "BP_DoorFrame.json").stat().st_size
        self.assertLess(len(lens_bp.render(self.bp(rel)).encode("utf-8")), size / 10)

    def test_jump_pad_cast_and_negated_branch(self):
        md = lens_bp.render(self.bp("Game/LevelPrototyping/Interactable/JumpPad/BP_JumpPad"))
        self.assertIn("if not OtherActor.ActorHasTag(Tag=Exclusion Tag):", md)
        self.assertIn("asCharacter = OtherActor as Character", md)
        self.assertIn("asCharacter.LaunchCharacter(LaunchVelocity=Velocity, bZOverride=true)", md)
        self.assertIn("Velocity: Vector = (X=0,Y=0,Z=800)  [InstanceEditable", md)
        self.assertIn("Cylinder: StaticMeshComponent < DefaultSceneRoot  {StaticMesh=SM_Cylinder", md)

    def test_widget_tree(self):
        md = lens_bp.render(self.bp("Game/AttributeSample/UI/WBP_AttributeAssetItem"))
        self.assertIn("Background: Border (var)\n  Content: VerticalBox\n    ThumbnailBox: SizeBox\n      Thumbnail: Image (var)", md)

    def test_folder_writes_md_index_and_prunes_untracked(self):
        root = self.tmp / "blueprints"
        shutil.copytree(self.FIX, root)
        stale = root / "Game" / "Old" / "BP_Old"
        write(stale / "BP_Old.json", json.dumps({"schema": lens_bp.SCHEMA, "name": "BP_Old"}))
        write(stale / "BP_Old.md", "old")
        mine = write(root / "Game" / "Notes" / "notes.json", "{}")       # not ours: kept
        self.assertEqual(lens_bp.write_folder(root, None), 0)
        self.assertFalse(stale.exists())
        self.assertTrue(mine.exists())
        index = (root / "index.md").read_text(encoding="utf-8")
        self.assertEqual(index.count("| [BP_") + index.count("| [WBP_"), 3)
        self.assertTrue((root / "Game/LevelPrototyping/Interactable/JumpPad/BP_JumpPad/BP_JumpPad.md").exists())

    def test_changed_since_export(self):
        bp = self.bp("Game/LevelPrototyping/Interactable/JumpPad/BP_JumpPad")
        f = write(self.tmp / bp["source"]["file"], "x")
        self.assertTrue(lens_bp.source_changed(bp, self.tmp))
        f.unlink()
        self.assertTrue(lens_bp.source_changed(bp, self.tmp))
        self.assertIsNone(lens_bp.source_changed(bp, None))

    def test_values(self):
        v = lens_bp.format_value
        self.assertEqual(v("/Script/Engine.StaticMesh'/Game/M/SM_Door.SM_Door'"), "SM_Door")
        self.assertEqual(v("/Game/BP/BP_X.BP_X_C"), "BP_X")
        self.assertEqual(v("(X=0.000000,Y=1.500000,Z=0.000000)"), "(X=0,Y=1.5,Z=0)")
        self.assertEqual(v("", {"type": {"category": "int"}}), "0")
        self.assertEqual(v("abc", {"type": {"category": "string"}}), '"abc"')

    # -- built graphs for constructs the fixtures do not have
    @staticmethod
    def node(nid, kind, pins, **extra):
        n = {"id": nid, "kind": kind, "class": extra.pop("cls", "K2Node"), "title": extra.pop("title", nid), "pos": [0, 0], "pins": []}
        for name, direction, cat, links in pins:
            p = {"name": name, "dir": direction, "type": {"category": cat, "text": cat}}
            if links:
                p["links"] = [{"node": a, "pin": b} for a, b in links]
            n["pins"].append(p)
        n.update(extra)
        return n

    def test_loop_sequence_latent_and_cycle(self):
        N = self.node
        graph = {"name": "EventGraph", "kind": "event", "nodes": [
            N("E", "event", [("then", "out", "exec", [("S", "execute")])], title="Event Tick", member="ReceiveTick"),
            N("S", "sequence", [("execute", "in", "exec", [("E", "then")]), ("then_0", "out", "exec", [("F", "execute")]),
                                ("then_1", "out", "exec", [("D", "execute")])]),
            N("G", "get", [("Items", "out", "object", [("F", "Array")])], member="Items", pure=True),
            N("F", "macro", [("execute", "in", "exec", [("S", "then_0")]), ("Array", "in", "object", [("G", "Items")]),
                             ("LoopBody", "out", "exec", [("P", "execute")]), ("Array Element", "out", "object", [("P", "self")]),
                             ("Completed", "out", "exec", [])], member="ForEachLoop"),
            N("P", "call", [("execute", "in", "exec", [("F", "LoopBody")]), ("self", "in", "object", [("F", "Array Element")]),
                            ("then", "out", "exec", [])], member="K2_DestroyActor"),
            N("D", "call", [("execute", "in", "exec", [("S", "then_1"), ("D", "then")]), ("then", "out", "exec", [("D", "execute")])],
              member="Delay", latent=True),
        ]}
        w = lens_bp.Writer(lens_bp.Graph(graph), {})
        text = "\n".join(w.write_event_graph())
        self.assertIn("on Tick():", text)
        self.assertIn("  for arrayElement in Items:\n    arrayElement.DestroyActor()", text)
        self.assertIn("  L1:\n  await Delay()\n  goto L1", text)


class BlueprintPartsTests(TempDir):
    """Large Blueprints split into an overview and parts/<event|fn>.md that each read on their own."""

    FIX = BlueprintPseudocodeTests.FIX
    DOOR = "Game/LevelPrototyping/Interactable/Door/BP_DoorFrame"

    def door(self) -> dict:
        return json.loads((self.FIX / self.DOOR / "BP_DoorFrame.json").read_text(encoding="utf-8"))

    def test_events_joined_by_goto_stay_together(self):
        body = ["# about A", "on A():", "  L7:", "  X()", "on B():", "  goto L9", "on C():", "  X.Reverse()  # outputs as at L7",
                "on D():", "  L9:", "  Y()"]
        blocks = lens_bp.split_events(body)
        self.assertEqual([b[0] for b in blocks], ["# about A", "on B():"])
        self.assertEqual(blocks[0], ["# about A", "on A():", "  L1:", "  X()", "on C():", "  X.Reverse()  # outputs as at L1"])
        self.assertEqual(blocks[1], ["on B():", "  goto L1", "on D():", "  L1:", "  Y()"])

    def test_small_events_are_packed_with_distinct_labels(self):
        parts = [{"kind": "event", "graph": "G", "name": n, "events": [n], "lines": lines} for n, lines in (
            ("A", ["on A():", "  L1:", "  X()", "  goto L1"]), ("B", ["on B():", "  L1:", "  Y()", "  goto L1"]),
            ("C", ["on C():"] + ["  Z()"] * 40))]
        packed = lens_bp.pack_events(parts, limit=30)
        self.assertEqual([p["events"] for p in packed], [["A", "B"], ["C"]])
        self.assertEqual(packed[0]["lines"], ["on A():", "  L1:", "  X()", "  goto L1", "on B():", "  L2:", "  Y()", "  goto L2"])

    def test_door_parts_and_overview(self):
        overview, files = lens_bp.render_split(self.door(), pack_lines=0)
        self.assertEqual(sorted(files), ["fn_Set_Mesh.md", "fn_UserConstructionScript.md", "on_ActorBeginOverlap.md",
                                         "on_ActorEndOverlap.md", "on_BeginPlay.md", "on_Open_Door.md"])
        self.assertIn("| [on Open Door, on Close Door](parts/on_Open_Door.md) |", overview)
        self.assertIn("| [on BeginPlay](parts/on_BeginPlay.md) | 7 | Closed Position, Closed Position_0 | - |", overview)
        self.assertIn("| Door, Door2 | Set Mesh |", overview)
        self.assertIn("## vars", overview)
        self.assertNotIn("on BeginPlay():", overview)                    # graphs live in the parts only
        # Open Door and Close Door share the timeline (goto / 'outputs as at'), so one part holds both.
        door = files["on_Open_Door.md"]
        self.assertIn("on Close Door():\n  Door Control.Reverse()  # outputs as at L1", door)
        self.assertIn("Door Size: Vector = (X=400,Y=100,Z=400)", door)        # the vars it uses, with types
        # 'Open Door()' is a call, not a use of the variable 'Door'.
        overlap = files["on_ActorBeginOverlap.md"]
        self.assertNotIn("vars used", overlap)
        self.assertIn("calls own: Open Door", overlap)
        self.assertIn("components used: Box: BoxComponent", files["on_ActorEndOverlap.md"])
        self.assertTrue(files["fn_Set_Mesh.md"].startswith("# BP_DoorFrame > fn Set Mesh\n"))
        # Together the parts hold every line of the whole document's graphs.
        whole = lens_bp.render(self.door())
        for text in files.values():
            for line in text.split("\n## ", 1)[1].splitlines()[1:]:
                if line and not line.startswith("<!--") and not re.search(r"\bL\d+\b", line):
                    self.assertIn(line, whole)

    def test_folder_splits_over_the_threshold_only(self):
        root = self.tmp / "blueprints"
        shutil.copytree(self.FIX, root)
        door = root / self.DOOR
        self.assertEqual(lens_bp.write_folder(root, None, split_lines=50), 0)
        # The four events (23 lines together) share one part; the two functions have one each.
        self.assertEqual(sorted(p.name for p in (door / "parts").glob("*.md")),
                         ["fn_Set_Mesh.md", "fn_UserConstructionScript.md", "on_BeginPlay.md"])
        self.assertIn("## parts", (door / "BP_DoorFrame.md").read_text(encoding="utf-8"))
        self.assertFalse((root / "Game/LevelPrototyping/Interactable/JumpPad/BP_JumpPad/parts").exists())
        self.assertRegex((root / "index.md").read_text(encoding="utf-8"), r"\[BP_DoorFrame\].*\| 87 \| 3 \|")
        # Under the threshold again: one whole file, the parts folder goes away.
        self.assertEqual(lens_bp.write_folder(root, None), 0)
        self.assertFalse((door / "parts").exists())
        self.assertIn("on BeginPlay():", (door / "BP_DoorFrame.md").read_text(encoding="utf-8"))


NL, TAB = chr(10), chr(9)


class NativeMigrationTests(TempDir):
    """lens_native.py: Blueprint -> C++ base class, from real exports of the UE 5.7 sample project."""
    FIX = Path(__file__).resolve().parent / "fixtures" / "native"

    def plan(self, name, mutate=None):
        import lens_native
        doc = json.loads((self.FIX / f"{name}.json").read_text(encoding="utf-8"))
        if mutate:
            mutate(doc)
        return lens_native.Plan(doc, module="Game").build()

    def generate(self, name, mutate=None):
        import lens_native
        plan = self.plan(name, mutate)
        out = self.tmp / name
        summary = lens_native.write(plan, out)
        files = {f: (out / f).read_text(encoding="utf-8") for f in summary["files"]}
        return plan, summary, files

    def test_actor_moves_whole_with_same_names_and_values(self):
        plan, s, files = self.generate("BP_JumpPad")
        h, cpp = files["JumpPadBase.h"], files["JumpPadBase.cpp"]
        self.assertEqual(s["coverage"]["migrated"], s["coverage"]["nodes"])
        self.assertEqual(s["modules"], ["Niagara"])                                  # Build.cs needs it
        self.assertIn("class GAME_API AJumpPadBase : public AActor", h)
        self.assertIn("TObjectPtr<UNiagaraComponent> NS_JumpDemo;", h)               # component keeps its name
        self.assertIn('meta = (DisplayName = "Color Target"))', h)                  # renamed variable, old name shown
        self.assertIn("FVector Velocity = FVector(0.0, 0.0, 800.0);", h)
        self.assertIn('FName ExclusionTag = FName(TEXT("Dead"));', h)
        self.assertIn("virtual void NotifyActorBeginOverlap(AActor* OtherActor) override;", h)
        self.assertIn("virtual void OnConstruction(const FTransform& Transform) override;", h)
        self.assertIn('Cylinder1->SetupAttachment(DefaultSceneRoot);', cpp)
        self.assertIn("Super::NotifyActorBeginOverlap(OtherActor);", cpp)
        # a None target is skipped and outputs are defaults, as in Blueprint (no crash in C++)
        self.assertIn("if (Target_", cpp)
        self.assertIn("->LaunchCharacter(Velocity, false, true);", cpp)
        # a pure node with several used outputs runs once
        self.assertEqual(cpp.count("UKismetMathLibrary::BreakColor("), 1)

    def test_one_unknown_node_keeps_the_whole_event_in_blueprint(self):
        def mutate(doc):
            for g in doc["graphs"]:
                for n in g["nodes"]:
                    if n.get("member") == "LaunchCharacter":
                        n["class"] = "K2Node_SomethingNew"
        plan, s, files = self.generate("BP_JumpPad", mutate)
        ev = next(e for e in s["events"] if e["bp"] == "ReceiveActorBeginOverlap")
        self.assertFalse(ev["migrated"])
        self.assertIn("SomethingNew", ev["reason"])
        self.assertNotIn("NotifyActorBeginOverlap", files["JumpPadBase.h"])
        self.assertLess(s["coverage"]["migrated"], s["coverage"]["nodes"])

    def test_implementable_event_keeps_a_blueprint_event_that_calls_cpp(self):
        plan, s, files = self.generate("BP_SideScrollingMovingPlatform")
        ev = {e["bp"]: e for e in s["events"]}
        self.assertTrue(ev["BP_MoveToTarget"]["migrated"], ev["BP_MoveToTarget"]["reason"])
        self.assertEqual(ev["BP_MoveToTarget"]["kind"], "thunk")                    # rebase keeps the event, adds the call
        self.assertTrue(ev["OnComponentBeginOverlap"]["migrated"], ev["OnComponentBeginOverlap"]["reason"])
        h, cpp = files["SideScrollingMovingPlatformBase.h"], files["SideScrollingMovingPlatformBase.cpp"]
        self.assertIn('UFUNCTION(BlueprintCallable, Category = "Native")\n\tvoid BP_MoveToTargetNative();', h)
        self.assertIn("\t\tBP_MoveToTarget();", cpp)                                 # callers go through the event, as in Blueprint
        # MoveComponentTo: latent, entered through its 'Move' exec pin, resumes in its own UFUNCTION
        self.assertIn("EMoveComponentAction::Move, FLatentActionInfo(0, (int32)GetTypeHash(FGuid(0x90BA7C85u", cpp)
        self.assertIn('TEXT("Latent_90BA7C85"), this)', cpp)
        self.assertIn("void ASideScrollingMovingPlatformBase::Latent_90BA7C85()\n{\n\tResetInteraction();", cpp)
        self.assertIn("UFUNCTION()\n\tvoid Latent_90BA7C85();", h)
        # FlipFlop: A first (the macro's IsA starts false and flips before branching)
        self.assertIn("bool bFlipFlop_70BDFE5B = false;", h)

    def test_delay_timeline_and_private_parent_members(self):
        plan, s, files = self.generate("BP_CombatDamageableBox")
        self.assertEqual(s["coverage"]["migrated"], s["coverage"]["nodes"])
        tl = {t["bp"]: t for t in s["timelines"]}["Timeline"]
        self.assertTrue(tl["migrated"], tl["reason"])
        self.assertIn("Timeline_Scale", tl["properties"].values())                  # rebase renames the track property
        cpp = files["CombatDamageableBoxBase.cpp"]
        self.assertIn("UKismetSystemLibrary::Delay(this, 5.0f, FLatentActionInfo(0, (int32)GetTypeHash(FGuid(0x40B69EDAu", cpp)
        self.assertIn("if (Timeline) { Timeline->PlayFromStart(); }", cpp)
        self.assertIn('NewObject<UTimelineComponent>(this, FName(TEXT("Timeline")))', cpp)
        self.assertIn("NewTimeline->CreationMethod = EComponentCreationMethod::UserConstructionScript;", cpp)
        self.assertIn("DwoNative::SetCurve(Curve->FloatCurve, { DwoNative::Key(", cpp)
        self.assertIn('NewTimeline->AddInterpFloat(Curve, FOnTimelineFloat(), FName(TEXT("Timeline_Scale")), FName(TEXT("Scale")));', cpp)
        # Mesh is private in ACombatDamageableBox (AllowPrivateAccess): read through its property
        self.assertIn('FindFProperty<FObjectPropertyBase>(ACombatDamageableBox::StaticClass(), TEXT("Mesh"))', cpp)
        self.assertNotIn("Target_7C91F256 = Mesh;", cpp)
        self.assertIn('#include "../DwoNativeLiterals.h"', files["DwoNativeLiterals.h"])
        self.assertIn("inline FRichCurveKey Key(", (self.tmp / "DwoNativeLiterals.h").read_text(encoding="utf-8"))

    def test_timeline_stays_while_blueprint_code_drives_it(self):
        def keep_event(doc):                      # an unknown node in the event that plays the timeline
            for g in doc["graphs"]:
                for n in g["nodes"]:
                    if n.get("member") == "Delay":
                        n["class"] = "K2Node_Mystery"
        s = self.plan("BP_CombatDamageableBox", keep_event).summary()
        tl = {t["bp"]: t for t in s["timelines"]}["Timeline"]
        self.assertFalse(tl["migrated"])
        self.assertIn("stays in Blueprint", tl["reason"])

    @staticmethod
    def input_doc(doc):
        """BP_JumpPad plus three input events, as DwoLens exports them (the QA project has no input sample)."""
        exec_t = {"category": "exec"}

        def pin(name, d, t, links=None, **kw):
            p = {"name": name, "dir": d, "type": t, **kw}
            if links:
                p["links"] = links
            return p

        def bid(i):
            return f"BB{i:030d}"

        def branch(i):
            return {"id": bid(i), "class": "K2Node_IfThenElse", "title": "Branch", "kind": "branch",
                    "pins": [pin("execute", "in", exec_t), pin("Condition", "in", {"category": "bool"}, default="true"),
                             pin("then", "out", exec_t), pin("else", "out", exec_t)]}
        key_t = {"category": "struct", "object": "/Script/InputCore.Key"}
        flags = {"bConsumeInput": "True", "bExecuteWhenPaused": "False", "bOverrideParentBinding": "True"}
        nodes = [
            {"id": "A1" + "0" * 30, "class": "K2Node_InputAction", "title": "InputAction Jump", "kind": "node",
             "props": {"InputActionName": "Jump", **flags},
             "pins": [pin("Pressed", "out", exec_t, [{"node": bid(1), "pin": "execute"}]),
                      pin("Released", "out", exec_t), pin("Key", "out", key_t)]},
            {"id": "A2" + "0" * 30, "class": "K2Node_InputAxisEvent", "title": "MoveForward", "kind": "event",
             "props": {"InputAxisName": "MoveForward", **flags},
             "pins": [pin("then", "out", exec_t, [{"node": bid(2), "pin": "execute"}]),
                      pin("AxisValue", "out", {"category": "real", "subCategory": "float"})]},
            {"id": "A3" + "0" * 30, "class": "K2Node_EnhancedInputAction", "title": "IA_Move", "kind": "node",
             "props": {"InputAction": "/Script/EnhancedInput.InputAction'/Game/Input/IA_Move.IA_Move'"},
             "pins": [pin("Triggered", "out", exec_t, [{"node": bid(3), "pin": "execute"}]),
                      pin("Started", "out", exec_t), pin("Completed", "out", exec_t),
                      pin("ActionValue", "out", {"category": "struct", "object": "/Script/CoreUObject.Vector2D"}),
                      pin("ElapsedSeconds", "out", {"category": "real", "subCategory": "double"}),
                      pin("TriggeredSeconds", "out", {"category": "real", "subCategory": "double"}),
                      pin("InputAction", "out", {"category": "object", "object": "/Script/EnhancedInput.InputAction"})]},
            branch(1), branch(2), branch(3)]
        g = next(g for g in doc["graphs"] if g["kind"] == "event")
        g["nodes"] += nodes
        types = doc["native"]["types"]
        types["/Script/InputCore.Key"] = {"kind": "struct", "native": True, "cpp": "FKey", "include": "InputCoreTypes.h", "module": "InputCore"}
        types["/Script/CoreUObject.Vector2D"] = {"kind": "struct", "native": True, "cpp": "FVector2D", "module": "CoreUObject"}
        types["/Script/EnhancedInput.InputAction"] = {"kind": "class", "native": True, "cpp": "UInputAction", "include": "InputAction.h", "module": "EnhancedInput"}

    def test_input_events_bind_where_the_engine_binds_blueprint_input(self):
        plan, s, files = self.generate("BP_JumpPad", self.input_doc)
        ev = [e for e in s["events"] if e["kind"] == "input"]
        self.assertEqual(len(ev), 3)
        self.assertTrue(all(e["migrated"] for e in ev), [e["reason"] for e in ev])
        h, cpp = files["JumpPadBase.h"], files["JumpPadBase.cpp"]
        self.assertIn("virtual void EnableInput(APlayerController* PlayerController) override;", h)   # a plain actor
        self.assertIn("const bool bCreates = PlayerController && !InputComponent;", cpp)
        self.assertIn('FInputActionBinding B(FName(TEXT("Jump")), IE_Pressed);', cpp)
        self.assertIn("if (E.GetActionName() == B.GetActionName() && E.KeyEvent == B.KeyEvent) { IC->RemoveActionBinding(I); }", cpp)
        self.assertIn("GET_FUNCTION_NAME_CHECKED(ThisClass, InpAct_Jump_Pressed_A1000000)", cpp)
        self.assertIn("InKey_A1000000 = Key;", cpp)                                   # the Key pin, kept like the Blueprint's temp
        self.assertIn('FInputAxisBinding B(FName(TEXT("MoveForward")));', cpp)
        self.assertIn("EIC->BindAction(NativeLiteral_", cpp)
        self.assertIn("ETriggerEvent::Triggered, this, GET_FUNCTION_NAME_CHECKED(ThisClass, InpAct_IA_Move_Triggered_A3000000));", cpp)
        self.assertIn("EIA_A3000000_ActionValue = ActionValue.Get<FVector2D>();", cpp)
        self.assertIn("void InpAct_IA_Move_Triggered_A3000000(FInputActionValue ActionValue, float ElapsedTime, float TriggeredTime, const UInputAction* SourceAction);", h)
        self.assertIn("EnhancedInput", s["modules"])

        def pawn(doc):
            self.input_doc(doc)
            doc["parent"] = "/Script/Engine.Character"
            doc["parentChain"] = ["/Script/Engine.Character"]
        plan, s, files = self.generate("BP_JumpPad", pawn)
        self.assertIn("virtual void SetupPlayerInputComponent(UInputComponent* PlayerInputComponent) override;", files["JumpPadBase.h"])
        self.assertIn("Super::SetupPlayerInputComponent(PlayerInputComponent);", files["JumpPadBase.cpp"])

    def test_signature_helpers(self):
        import lens_native
        self.assertEqual(lens_native.enum_exec_value("TEnumAsByte<EMoveComponentAction::Type>", "Stop"), "EMoveComponentAction::Stop")
        self.assertEqual(lens_native.enum_exec_value("ETriggerEvent", "Started"), "ETriggerEvent::Started")
        self.assertEqual(lens_native.param_decl({"name": "Hit", "cpp": "FHitResult", "ref": True, "const": True}), "const FHitResult& Hit")
        self.assertEqual(lens_native.param_decl({"name": "R", "cpp": "TEnumAsByte<EEndPlayReason::Type>"}, enum_as_byte=False), "EEndPlayReason::Type R")
        self.assertEqual(lens_native.guid_args("90BA7C85447D62D6171E398EDD8E6B19"), "0x90BA7C85u, 0x447D62D6u, 0x171E398Eu, 0xDD8E6B19u")

    def test_anim_blueprint_uses_native_overrides_and_public_getters(self):
        plan, s, files = self.generate("ABP_Unarmed")
        ev = {e["bp"]: e for e in s["events"]}
        self.assertTrue(ev["BlueprintInitializeAnimation"]["migrated"], ev["BlueprintInitializeAnimation"]["reason"])
        cpp = files["UnarmedBase.cpp"]
        self.assertIn("void UUnarmedBase::NativeInitializeAnimation()", cpp)
        self.assertIn("GetCharacterMovement()", cpp)                                 # private member -> public getter
        self.assertNotIn("->CharacterMovement", cpp)

    def test_widget_members_the_parent_already_binds_are_not_redeclared(self):
        plan = self.plan("WBP_LeftPanel")
        s = plan.summary()
        widgets = {w["bp"]: w for w in s["widgets"]}
        self.assertFalse(widgets["BuildingTab"]["migrated"])                       # UAttributeLeftPanelWidget binds it
        self.assertIn("already declares", widgets["BuildingTab"]["reason"])
        empty = [e for e in s["events"] if "empty" in e["reason"]]
        self.assertTrue(empty)                                                      # unwired Event Construct etc.
        self.assertEqual(s["coverage"]["nodes"], 0)                                 # nothing real to move

    def trace(self, launch="X=0.000 Y=0.000 Z=800.000", var_name="Color Target", extra=None):
        step = {"name": "probe", "frame": 21, "subjectAlive": True,
                "subject": {"props": {var_name: "(R=1.000000,G=1.000000,B=1.000000,A=1.000000)", "ActorGuid": "AB12",
                                      "Owner": "/Temp/Untitled_3.Untitled_3:PersistentLevel.Thing", **(extra or {})},
                            "transform": "0,0,0|0,0,0|1,1,1", "components": {"Cylinder1": {"@world": "0.000001,0,0"}}},
                "probe": {"pendingLaunch": launch}}
        return {"ok": True, "steps": [step]}

    def test_behaviour_compare_same_with_float_noise_and_volatile_names(self):
        import lens_native
        before = self.trace()
        after = self.trace(var_name="ColorTarget")                               # renamed in C++
        after["steps"][0]["subject"]["props"]["ActorGuid"] = "CD34"               # new for every spawn
        after["steps"][0]["subject"]["props"]["Owner"] = "/Temp/Untitled_9.Untitled_9:PersistentLevel.Thing"
        after["steps"][0]["subject"]["components"]["Cylinder1"]["@world"] = "0.0000004,0,0"
        r = lens_native.compare_traces(before, after, {"Color Target": "ColorTarget"})
        self.assertEqual(r["verdict"], "same", r["differences"])

    def test_behaviour_compare_reports_a_changed_effect(self):
        import lens_native
        r = lens_native.compare_traces(self.trace(), self.trace(launch="X=0.000 Y=0.000 Z=400.000",
                                                               extra={"NativeLiteral_0": "/Game/M.M"}))
        self.assertEqual(r["verdict"], "different")
        self.assertEqual([d["value"] for d in r["differences"]], ["probe.pendingLaunch"])
        self.assertIn("actor.NativeLiteral_0", r["onlyOneSide"])                  # members only C++ has: listed, not counted
        self.assertIn("400", lens_native.behaviour_md("BP_JumpPad", r))
        self.assertEqual(lens_native.compare_traces({"ok": False, "error": "x"}, self.trace())["verdict"], "error")

    def test_build_cs_modules_are_added_once(self):
        import lens_native
        body = "public class Game : ModuleRules {" + NL + TAB + "public Game(ReadOnlyTargetRules T) : base(T) {" + NL + TAB * 2 + 'PublicDependencyModuleNames.AddRange(new string[] { "Core", "Engine" });' + NL + TAB + "}" + NL + "}" + NL
        cs = write(self.tmp / "Game.Build.cs", body)
        self.assertEqual(lens_native.add_modules(cs, ["Niagara", "Engine"]), ["Niagara"])
        self.assertEqual(lens_native.add_modules(cs, ["Niagara", "UMG"]), ["UMG"])
        text = cs.read_text(encoding="utf-8")
        self.assertEqual(text.count("// dwookit native migration"), 1)
        self.assertIn('{ "Niagara", "UMG" }', text)


class LibraryTests(TempDir):
    """lens_library.py: the instruction and skill library, and what a project gets for Claude Code and Copilot."""

    def setUp(self):
        super().setUp()
        self.lib = self.tmp / "library"
        self.project = self.tmp / "Proj"
        self.forge = self.project / "__dwookit"
        self.forge.mkdir(parents=True)
        self.old = os.environ.get("DWOOKIT_LIBRARY")
        os.environ["DWOOKIT_LIBRARY"] = str(self.lib)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("DWOOKIT_LIBRARY", None)
        else:
            os.environ["DWOOKIT_LIBRARY"] = self.old
        super().tearDown()

    def add_samples(self):
        import lens_library
        ins = write(self.tmp / "src" / "Team Rules.instructions.md",
                    "---\napplyTo: \"Source/**\"\ndescription: team C++ rules\n---\n# Team rules\nNo Tick in actors.\n")
        skill = self.tmp / "src" / "My Skill"
        write(skill / "SKILL.md", "# Review Blueprints\nChecks the exported pseudocode for Tick and casts.\n")
        write(skill / "scripts" / "check.py", "print('ok')\n")
        write(skill / ".git" / "HEAD", "x")
        n1, _ = lens_library.add_instruction(self.lib, ins)
        n2, notes = lens_library.add_skill(self.lib, skill)
        return n1, n2, notes

    def test_add_checks_names_and_front_matter(self):
        import lens_library
        n1, n2, notes = self.add_samples()
        self.assertEqual((n1, n2), ("team-rules", "my-skill"))
        self.assertTrue(any("description was missing" in n for n in notes))
        text = (self.lib / "skills" / "my-skill" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: my-skill", text)
        self.assertIn("description: Review Blueprints", text)
        self.assertTrue((self.lib / "skills" / "my-skill" / "scripts" / "check.py").exists())
        self.assertFalse((self.lib / "skills" / "my-skill" / ".git").exists())
        items = lens_library.list_items(self.lib)
        self.assertEqual(items["defaults"], {"instructions": ["team-rules"], "skills": ["my-skill"]})   # new items are defaults
        self.assertEqual(items["instructions"][0]["applyTo"], "Source/**")
        self.assertEqual(items["targets"], ["claude", "copilot"])

    def test_skill_from_a_zip_and_unsafe_zips(self):
        import lens_library
        import zipfile
        z = self.tmp / "pack.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("pack/deploy-check/SKILL.md", "---\nname: deploy-check\ndescription: Checks a package.\n---\nSteps.\n")
        self.assertEqual(lens_library.add_skill(self.lib, z)[0], "deploy-check")
        bad = self.tmp / "bad.zip"
        with zipfile.ZipFile(bad, "w") as f:
            f.writestr("../evil/SKILL.md", "x")
        with self.assertRaises(lens_library.LibraryError):
            lens_library.add_skill(self.lib, bad)
        with self.assertRaises(lens_library.LibraryError):
            lens_library.add_instruction(self.lib, write(self.tmp / "a.exe", "x"))

    def test_project_gets_the_selection_per_agent_and_loses_it(self):
        import lens_agent
        self.add_samples()
        write(self.project / ".claude" / "skills" / "my-skill" / "SKILL.md", "theirs")          # not dwookit's: kept
        self.assertEqual(lens_agent.main(["write", "--project", str(self.project)]), 0)
        claude = (self.project / "CLAUDE.local.md").read_text(encoding="utf-8")
        self.assertIn("@__dwookit/context/instructions/team-rules.md", claude)
        self.assertEqual((self.forge / "context" / "instructions" / "team-rules.md").read_text(encoding="utf-8").strip(),
                         "# Team rules\nNo Tick in actors.")
        cop = (self.project / ".github" / "instructions" / "dwookit-team-rules.instructions.md").read_text(encoding="utf-8")
        self.assertIn('applyTo: "Source/**"', cop)
        self.assertIn("No Tick in actors.", cop)
        self.assertEqual((self.project / ".claude" / "skills" / "my-skill" / "SKILL.md").read_text(encoding="utf-8"), "theirs")
        self.assertTrue((self.project / ".github" / "skills" / "my-skill" / ".dwookit-managed").exists())
        installed = json.loads((self.forge / "context" / "installed.json").read_text(encoding="utf-8"))["paths"]
        self.assertEqual(installed, [".github/instructions/dwookit-team-rules.instructions.md", ".github/skills/my-skill/"])
        self.assertEqual(json.loads((self.forge / "agent.json").read_text(encoding="utf-8"))["skills"], ["my-skill"])

        # Claude Code only: the Copilot files go, the Claude copy appears once the user's folder is gone
        shutil.rmtree(self.project / ".claude")
        self.assertEqual(lens_agent.main(["use", "--project", str(self.project), "--targets", "claude"]), 0)
        self.assertFalse((self.project / ".github" / "skills").exists())
        self.assertFalse((self.project / ".github" / "instructions" / "dwookit-team-rules.instructions.md").exists())
        self.assertFalse((self.project / ".github" / "instructions" / "dwookit.instructions.md").exists())
        self.assertTrue((self.project / ".claude" / "skills" / "my-skill" / ".dwookit-managed").exists())

        # nothing selected: block and copies removed, the user's instructions file stays
        self.assertEqual(lens_agent.main(["use", "--project", str(self.project), "--targets", "none"]), 0)
        self.assertFalse((self.project / ".claude").exists())
        self.assertFalse((self.project / "CLAUDE.local.md").exists())
        self.assertFalse((self.forge / "context" / "installed.json").exists())

        # uninstall removes every copy whatever the selection was
        lens_agent.main(["use", "--project", str(self.project), "--targets", "claude,copilot"])
        lens_agent.main(["remove", "--project", str(self.project)])
        for rel in (".claude", ".github", "CLAUDE.local.md"):
            self.assertFalse((self.project / rel).exists(), rel)

    def test_library_command_line(self):
        import lens_library
        n1, n2, _ = self.add_samples()
        self.assertEqual(lens_library.main(["defaults", "--instructions", "none", "--targets", "copilot"]), 0)
        items = lens_library.list_items(self.lib)
        self.assertEqual((items["defaults"]["instructions"], items["targets"]), ([], ["copilot"]))
        self.assertEqual(lens_library.main(["remove", "skill", n2]), 0)
        self.assertFalse((self.lib / "skills" / n2).exists())
        self.assertEqual(lens_library.main(["remove", "skill", n2]), 1)


class AgentContextTests(TempDir):
    """lens_agent.py: AGENT.md, the user's instructions shell and the pointer files for Claude Code and Copilot."""

    def setUp(self):
        super().setUp()
        self.project = self.tmp / "Proj"
        self.forge = self.project / "__dwookit"
        self.forge.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)

    def run_agent(self, *args):
        import lens_agent
        return lens_agent.main([*args, "--project", str(self.project)])

    def test_agent_md_instructions_and_pointers(self):
        shutil.copytree(BlueprintPseudocodeTests.FIX, self.forge / "blueprints")
        lens_bp.write_folder(self.forge / "blueprints", self.project)
        self.assertEqual(self.run_agent("write"), 0)
        agent = (self.forge / "context" / "AGENT.md").read_text(encoding="utf-8")
        for text in ("3 tracked Blueprint(s)", "__dwookit/blueprints/index.md", "parts/", "Haiku",
                     "UFUNCTION(BlueprintCallable)", "| # | Blueprint | graph / function | change |",
                     "No explanations", "never open them", "(empty so far)"):
            self.assertIn(text, agent)
        self.assertLess(len(agent.split()), 900)                         # read on every task: keep it short
        user = self.project / ".github/instructions/project.instructions.md"
        self.assertTrue(user.read_text(encoding="utf-8").startswith('---\napplyTo: "**"\n---\n<!-- dwookit'))
        claude = (self.project / "CLAUDE.local.md").read_text(encoding="utf-8")
        self.assertIn("@__dwookit/context/AGENT.md\n@.github/instructions/project.instructions.md", claude)
        copilot = (self.project / ".github/instructions/dwookit.instructions.md").read_text(encoding="utf-8")
        self.assertTrue(copilot.startswith('---\napplyTo: "**"\n---\n'))
        # The user's file is never overwritten, and it is announced as winning once it says something.
        mine = user.read_text(encoding="utf-8") + "- Tick 금지\n"
        user.write_text(mine, encoding="utf-8")
        self.run_agent("write")
        self.assertEqual(user.read_text(encoding="utf-8"), mine)
        self.assertIn("it wins over this file", (self.forge / "context" / "AGENT.md").read_text(encoding="utf-8"))
        # 'agent off' keeps it; uninstall backs it up, then removes everything and the empty .github folders.
        self.run_agent("off")
        self.assertEqual(user.read_text(encoding="utf-8"), mine)
        backup = self.tmp / "backup"
        self.run_agent("remove", "--backup", str(backup))
        self.assertEqual((backup / "project.instructions.md").read_text(encoding="utf-8"), mine)
        self.assertFalse((self.project / "CLAUDE.local.md").exists())
        self.assertFalse((self.project / ".github").exists())

    def test_existing_files_keep_their_bytes(self):
        claude = self.project / "CLAUDE.local.md"
        original = b"\xef\xbb\xbfmy notes\r\nline 2\r\n"
        claude.write_bytes(original)
        self.run_agent("write")
        self.run_agent("write")                                         # a second write replaces, never duplicates
        data = claude.read_bytes()
        self.assertTrue(data.startswith(original))
        self.assertEqual(data.count(b">>> dwookit managed"), 1)
        self.assertNotIn(b"\n<", data.replace(b"\r\n", b""))             # CRLF kept throughout
        self.run_agent("remove")
        self.assertEqual(claude.read_bytes(), original)

    def test_committed_and_foreign_files_are_left_alone(self):
        claude = write(self.project / "CLAUDE.local.md", "team file\n")
        subprocess.run(["git", "-C", str(self.project), "add", "CLAUDE.local.md"], check=True)
        foreign = write(self.project / ".github/instructions/dwookit.instructions.md", "someone else's\n")
        self.run_agent("write")
        self.run_agent("remove")
        self.assertEqual(claude.read_text(encoding="utf-8"), "team file\n")
        self.assertEqual(foreign.read_text(encoding="utf-8"), "someone else's\n")

    def test_off_removes_pointers_and_keeps_them_off(self):
        self.run_agent("write")
        self.run_agent("off")
        self.assertFalse((self.project / "CLAUDE.local.md").exists())
        self.run_agent("write")                                         # e.g. after the next bp export
        self.assertFalse((self.project / "CLAUDE.local.md").exists())
        self.assertTrue((self.forge / "context" / "AGENT.md").exists())
        self.run_agent("on")
        self.assertTrue((self.project / "CLAUDE.local.md").exists())


class TargetListTests(TempDir):
    """lens_targets.py: Blueprints and C++ classes the user can pick, read from the project files."""

    def test_blueprints_classes_and_lists(self):
        import lens_targets
        p = self.tmp / "Proj"
        def asset(rel, *names):
            f = p / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"PKG" + b"".join(n.encode() + bytes(1) for n in names))
        asset("Content/Chars/BP_Hero.uasset", "Blueprint", "BlueprintGeneratedClass", "BP_Hero_C")
        asset("Content/UI/WBP_Hud.uasset", "WidgetBlueprint", "WidgetBlueprintGeneratedClass", "WBP_Hud_C")
        asset("Content/Data/DA_Item.uasset", "PrimaryDataAsset", "BlueprintGeneratedClass", "BP_Hero_C")   # only refers to one
        asset("Content/__ExternalActors__/L/0/AB12.uasset", "BlueprintGeneratedClass", "AB12_C")
        asset("Plugins/Feature/Content/BP_Gadget.uasset", "BlueprintGeneratedClass", "BP_Gadget_C")
        write(p / "Plugins/Feature/Feature.uplugin", "{}")
        asset("Plugins/__DwoCanvas/Content/BP_Ours.uasset", "BlueprintGeneratedClass", "BP_Ours_C")
        write(p / "Plugins/__DwoCanvas/DwoCanvas.uplugin", "{}")
        write(p / "Source/Game/Public/Enemy.h", "UCLASS(Blueprintable)\nclass GAME_API AEnemy : public ACharacter\n{};\n"
                                                "UCLASS(Abstract)\nclass UBaseThing final : public UObject {};\n")
        write(p / "__dwookit/bp-track.txt", "# list\n/Game/UI/\n")
        write(p / "__dwookit/sm-watch.txt", "BP_Hero_C 4\n")
        d = lens_targets.collect(p)
        bps = {b["package"]: b for b in d["blueprints"]}
        self.assertEqual(sorted(bps), ["/Feature/BP_Gadget", "/Game/Chars/BP_Hero", "/Game/UI/WBP_Hud"])
        self.assertEqual(bps["/Game/UI/WBP_Hud"]["kind"], "widget")
        self.assertTrue(bps["/Game/UI/WBP_Hud"]["tracked"])               # folder entry covers it
        self.assertFalse(bps["/Game/Chars/BP_Hero"]["tracked"])
        self.assertTrue(bps["/Game/Chars/BP_Hero"]["watched"])
        classes = {c["name"]: c for c in d["classes"]}
        self.assertEqual(classes["AEnemy"]["runtimeClass"], "Enemy")
        self.assertEqual(classes["AEnemy"]["parent"], "ACharacter")
        self.assertTrue(classes["AEnemy"]["actor"])
        self.assertTrue(classes["UBaseThing"]["abstract"])


class ExplainTests(TempDir):
    """lens_explain.py: a tracked Blueprint as a person reads it (composition, behaviors, flow, logic)."""

    FIX = BlueprintPseudocodeTests.FIX

    def bp(self, rel: str) -> dict:
        name = rel.rsplit("/", 1)[-1]
        return json.loads((self.FIX / rel / f"{name}.json").read_text(encoding="utf-8"))

    @staticmethod
    def texts(steps):
        for s in steps:
            yield s["text"]
            yield from ExplainTests.texts(s.get("children", []))

    def test_door_behaviors_in_plain_words(self):
        import lens_explain
        d = lens_explain.explain(self.bp("Game/LevelPrototyping/Interactable/Door/BP_DoorFrame"))
        names = [b["name"] for b in d["behaviors"]]
        # engine events first, events others call next, functions, Construction Script last
        self.assertEqual(names[:3], ["BeginPlay", "ActorBeginOverlap", "ActorEndOverlap"])
        self.assertEqual(names[-1], "UserConstructionScript")
        by = {b["name"]: b for b in d["behaviors"]}
        self.assertEqual(by["ActorBeginOverlap"]["trigger"], "다른 액터가 겹치기 시작하면")
        overlap = list(self.texts(by["ActorBeginOverlap"]["steps"]))
        self.assertIn("'Open Door' 실행", overlap)
        self.assertIn("Door Control 재생", overlap)                  # the custom event is expanded in place
        self.assertIn("Door2가 있으면", overlap)
        end = list(self.texts(by["ActorEndOverlap"]["steps"]))
        self.assertIn("Box에 겹친 Character 목록의 개수 = 0이면", end)
        self.assertIn("ActorBeginOverlap", by["Open Door"]["calledBy"])
        self.assertEqual(by["BeginPlay"]["writes"], ["Closed Position", "Closed Position_0"])
        # Set Mesh is called five times: expanded once, then referred to
        ucs = list(self.texts(by["UserConstructionScript"]["steps"]))
        self.assertEqual(sum(1 for t in ucs if t == "컴포넌트 추가 (RelativeTransform)"), 1)
        self.assertIn("'Set Mesh' 실행 (위와 같은 흐름, 다시 펼치지 않음)", ucs)
        comp = d["composition"]
        self.assertEqual([c["role"] for c in comp["components"]][:2], ["기준점 (위치만)", "감지·충돌 영역"])
        self.assertIn("Door Size", [s["name"] for s in comp["settings"]])
        self.assertIn("Closed Position", [s["name"] for s in comp["state"]])
        self.assertIn("BeginPlay".replace("BeginPlay", "Begin Play"), [b["title"] for b in d["behaviors"]])
        # every step keeps the pseudocode line it comes from (the logic level)
        self.assertTrue(any(s.get("detail", "").startswith("Door Control.Play") for s in lens_explain.Explainer.flatten(by["Open Door"]["steps"])))

    def test_jump_pad_negation_cast_and_summary(self):
        import lens_explain
        d = lens_explain.explain(self.bp("Game/LevelPrototyping/Interactable/JumpPad/BP_JumpPad"))
        steps = list(self.texts(d["behaviors"][0]["steps"]))
        self.assertIn("OtherActor에 태그 Exclusion Tag가 붙어 있지 않으면", steps)
        self.assertIn("Character가 맞으면", steps)
        self.assertIn("변환한 Character를 튕겨 보내기 (Launch Velocity: Velocity, ZOverride: true)", steps)
        self.assertIn("다른 액터가 겹치기 시작하면 변환한 Character를 튕겨 보내기", d["summary"])
        self.assertNotIn("(를)", json.dumps(d, ensure_ascii=False))     # particles picked, never hedged
        self.assertIn("감지·충돌 영역", d["summary"])

    def test_cpp_bodies_and_callers(self):
        import lens_explain
        p = self.tmp / "Proj"
        write(p / "Source/Game/Hero.h", "UCLASS()\nclass GAME_API AHero : public ACharacter\n{\n};\n")
        write(p / "Source/Game/Hero.cpp", "#include \"Hero.h\"\n\nvoid AHero::DoMove(float Right, float Forward)\n{\n"
                                          "\tAddMovementInput(GetActorRightVector(), Right);\n}\n\n"
                                          "void AHero::TakeHit(float Damage)\n{\n\tHealth -= Damage;\n\tOnHurt();\n}\n")
        cpp = lens_explain.CppIndex(p)
        hit = cpp.find(["Hero"], "DoMove")
        self.assertEqual(hit["file"], "Source/Game/Hero.cpp")
        self.assertEqual(hit["line"], 3)
        self.assertEqual(hit["body"], ["\tAddMovementInput(GetActorRightVector(), Right);"])
        self.assertEqual(cpp.callers("OnHurt"), ["AHero::TakeHit"])
        self.assertEqual(cpp.parents["Hero"], "Character")
        bp = {"name": "BP_Hero", "parent": "/Script/Game.Hero", "parentChain": ["/Script/Game.Hero"], "graphs": []}
        self.assertEqual(lens_explain.kind_words(bp, cpp), "걸어 다니는 캐릭터")

    def test_folder_writes_html_json_and_prompt(self):
        import lens_explain
        root = self.tmp / "blueprints"
        shutil.copytree(self.FIX, root)
        lens_bp.write_folder(root, None)
        self.assertEqual(lens_explain.write_all(root, None), 0)
        door = root / "Game/LevelPrototyping/Interactable/Door/BP_DoorFrame"
        doc = json.loads((door / "BP_DoorFrame.explain.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], "lens.explain/1")
        page = (door / "BP_DoorFrame.html").read_text(encoding="utf-8")
        for text in ("<h2>구성</h2>", "<h2>행동과 흐름</h2>", "다른 액터가 겹치기 시작하면", "class='pc'"):
            self.assertIn(text, page)
        self.assertIn("BP_DoorFrame.agent.json", (door / "BP_DoorFrame.explain-prompt.md").read_text(encoding="utf-8"))
        self.assertIn("BP_DoorFrame.html", (root / "index.html").read_text(encoding="utf-8"))
        # an agent's wording is shown next to the rule-based text
        write(door / "BP_DoorFrame.agent.json", json.dumps({"summary": "캐릭터가 다가오면 열리는 문", "behaviors": {"b1": "문이 열린다"}}, ensure_ascii=False))
        lens_explain.write_all(root, None)
        self.assertIn("캐릭터가 다가오면 열리는 문", (door / "BP_DoorFrame.html").read_text(encoding="utf-8"))


class GameFlowTests(TempDir):
    """lens_flow.py: map -> GameMode -> player objects -> what they spawn, own and use."""

    def make_run(self):
        project = self.tmp / "Proj"
        run = self.tmp / "run"
        raw, out = run / "raw", run / "out"
        raw.mkdir(parents=True)
        out.mkdir(parents=True)

        def bp(name, folder="/Game/G"):
            return f"bp:{folder}/{name}.{name}_C"

        def uc(name):
            return f"uclass:/Script/Game.{name}"

        symbols = [
            {"key": uc("GameModeBase").replace("Game.", "Engine."), "kind": "class", "name": "GameModeBase", "external": True},
            {"key": "uclass:/Script/Engine.PlayerController", "kind": "class", "name": "PlayerController", "external": True},
            {"key": "uclass:/Script/Engine.Character", "kind": "class", "name": "Character", "external": True},
            {"key": "uclass:/Script/Engine.Actor", "kind": "class", "name": "Actor", "external": True},
            {"key": "uclass:/Script/UMG.UserWidget", "kind": "class", "name": "UserWidget", "external": True},
            {"key": uc("HeroGameMode"), "kind": "class", "name": "HeroGameMode", "external": False, "file": "Source/Game/HeroGameMode.h"},
            {"key": uc("Spawner"), "kind": "class", "name": "Spawner", "external": False, "file": "Source/Game/Spawner.h"},
            {"key": bp("BP_GameMode"), "kind": "blueprint", "name": "BP_GameMode", "external": False, "package": "/Game/G/BP_GameMode"},
            {"key": bp("BP_Controller"), "kind": "blueprint", "name": "BP_Controller", "external": False, "package": "/Game/G/BP_Controller"},
            {"key": bp("BP_Hero"), "kind": "blueprint", "name": "BP_Hero", "external": False, "package": "/Game/G/BP_Hero"},
            {"key": bp("BP_Spawner"), "kind": "blueprint", "name": "BP_Spawner", "external": False, "package": "/Game/G/BP_Spawner"},
            {"key": bp("BP_Enemy"), "kind": "blueprint", "name": "BP_Enemy", "external": False, "package": "/Game/G/BP_Enemy"},
            {"key": bp("WBP_Hud"), "kind": "blueprint", "name": "WBP_Hud", "external": False, "package": "/Game/G/WBP_Hud"},
            {"key": bp("BP_Unused"), "kind": "blueprint", "name": "BP_Unused", "external": False, "package": "/Game/G/BP_Unused"},
            {"key": "asset:/Game/Maps/L_Main", "kind": "asset", "name": "L_Main", "external": False, "asset_class": "/Script/Engine.World"},
            {"key": "asset:/Game/G/BP_GameMode", "kind": "asset", "name": "BP_GameMode", "external": False, "asset_class": "/Script/Engine.Blueprint"},
            {"key": "asset:/Game/G/BP_Spawner", "kind": "asset", "name": "BP_Spawner", "external": False, "asset_class": "/Script/Engine.Blueprint"},
        ]
        inherits = [
            (uc("HeroGameMode"), "uclass:/Script/Engine.GameModeBase"), (bp("BP_GameMode"), uc("HeroGameMode")),
            (bp("BP_Controller"), "uclass:/Script/Engine.PlayerController"), (bp("BP_Hero"), "uclass:/Script/Engine.Character"),
            (uc("Spawner"), "uclass:/Script/Engine.Actor"), (bp("BP_Spawner"), uc("Spawner")),
            (bp("BP_Enemy"), "uclass:/Script/Engine.Character"), (bp("WBP_Hud"), "uclass:/Script/UMG.UserWidget"),
            (bp("BP_Unused"), "uclass:/Script/Engine.Actor"),
        ]
        relations = [{"source": s, "target": t, "type": "Inherits"} for s, t in inherits]
        relations += [
            {"source": "asset:/Game/Maps/L_Main", "target": "asset:/Game/G/BP_GameMode", "type": "AssetHardRef"},
            {"source": "asset:/Game/Maps/L_Main", "target": "asset:/Game/G/BP_Spawner", "type": "AssetHardRef"},
            {"source": bp("BP_Spawner"), "target": uc("Spawner"), "type": "Calls", "evidence": ["own parent"]},
        ]
        write(out / "graph.json", json.dumps({"symbols": symbols, "relations": relations}))
        cls = "/Script/Engine.BlueprintGeneratedClass'/Game/G/{0}.{0}_C'"
        write(raw / "blueprints.json", json.dumps({"blueprints": [
            {"generatedClass": "/Game/G/BP_GameMode.BP_GameMode_C",
             "classDefaults": [{"name": "PlayerControllerClass", "value": cls.format("BP_Controller")}]},
            {"generatedClass": "/Game/G/BP_Spawner.BP_Spawner_C",
             "classDefaults": [{"name": "EnemyClass", "value": cls.format("BP_Enemy")}]},
            {"generatedClass": "/Game/G/BP_Hero.BP_Hero_C", "graphs": [{"name": "EventGraph", "nodes": [
                {"class": "K2Node_CreateWidget", "title": "Create WBP Hud Widget", "classArg": "/Game/G/WBP_Hud.WBP_Hud_C"}]}]},
        ]}))
        write(out / "features.json", json.dumps({"classes": [{"key": bp("BP_Hero"), "behaviors": [{"name": "점프"}, {"name": "공격"}]}]}))
        # C++: the GameMode's pawn comes from a ConstructorHelpers class finder
        write(project / "Source/Game/HeroGameMode.cpp",
              'AHeroGameMode::AHeroGameMode()\n{\n\tstatic ConstructorHelpers::FClassFinder<APawn> PawnBP(TEXT("/Game/G/BP_Hero"));\n'
              "\tDefaultPawnClass = PawnBP.Class;\n}\n")
        write(project / "Config/DefaultEngine.ini", "[/Script/EngineSettings.GameMapsSettings]\nGameDefaultMap=/Game/Maps/L_Main.L_Main\n"
                                                     "GlobalDefaultGameMode=/Game/G/BP_GameMode.BP_GameMode_C\n")
        return raw, out, project, bp

    def test_flow_from_map_to_what_objects_make(self):
        import lens_flow
        raw, out, project, bp = self.make_run()
        doc = lens_flow.build(raw, out, project)
        edges = {(e["from"], e["to"]): e for e in doc["edges"]}
        self.assertEqual(edges[("map:/Game/Maps/L_Main", bp("BP_GameMode"))]["kind"], "slot")       # World Settings
        self.assertEqual(edges[("map:/Game/Maps/L_Main", bp("BP_Spawner"))]["kind"], "places")
        self.assertEqual(edges[(bp("BP_GameMode"), bp("BP_Controller"))]["labels"][0], "플레이어 컨트롤러")
        # DefaultPawnClass is set in the C++ parent's constructor and reaches the Blueprint child
        self.assertEqual(edges[(bp("BP_GameMode"), bp("BP_Hero"))]["labels"][0], "플레이어 폰")
        self.assertEqual(edges[(bp("BP_Controller"), bp("BP_Hero"))]["kind"], "possesses")
        self.assertEqual(edges[(bp("BP_Spawner"), bp("BP_Enemy"))]["labels"], ["EnemyClass"])
        self.assertEqual(edges[(bp("BP_Hero"), bp("WBP_Hud"))]["kind"], "creates")
        self.assertNotIn((bp("BP_Spawner"), "uclass:/Script/Game.Spawner"), edges)                 # calls into its own parent
        self.assertEqual(doc["roots"][0], {"key": "map:/Game/Maps/L_Main", "name": "L_Main", "kind": "map", "default": True})
        self.assertEqual(doc["nodes"][bp("BP_Hero")]["role"], "character")
        self.assertEqual(doc["nodes"][bp("BP_Hero")]["behaviors"], ["점프", "공격"])
        self.assertEqual([u["name"] for u in doc["unreached"]], ["BP_Unused"])                        # C++ bases count via children
        keys, shown = lens_flow.reachable(doc, "map:/Game/Maps/L_Main", depth=4)
        self.assertIn(bp("WBP_Hud"), keys)                                                          # map -> mode -> pawn -> widget
        html_text = lens_flow.render_html(doc)
        self.assertIn("<h2>게임 흐름</h2>", html_text)
        self.assertIn("flowchart LR", html_text)


class LiveLensTests(TempDir):
    """lens_live.py: editor-session raw data merged into the cache, change history and retention."""

    @staticmethod
    def raw(path: Path, bps: list[str], partial: bool, assets: list[str] | None = None, skipped: bool = False):
        meta = {"schema": "archi.raw/0.1", "partial": partial, "assetsSkipped": skipped}
        if partial:
            meta["packages"] = bps
        write(path / "meta.json", json.dumps(meta))
        write(path / "blueprints.json", json.dumps({"blueprints": [{"package": p, "asset": f"{p}.X", "rev": str(path)} for p in bps]}))
        write(path / "reflection.json", json.dumps({"native": [{"path": "/Script/Game.A"}],
                                                    "blueprint": [{"path": f"{p}.X_C", "package": p} for p in bps]}))
        write(path / "assets.json", json.dumps({"assets": [{"package": p} for p in (assets if assets is not None else bps)]}))

    def test_full_then_partial_merge(self):
        import lens_live
        base = self.tmp / "live" / "base"
        s1 = self.tmp / "live" / "sessions" / "1"
        self.raw(s1 / "raw", ["/Game/A", "/Game/B", "/Game/C"], partial=False)
        write(s1 / "session.json", json.dumps({"id": "1"}))
        self.assertEqual(lens_live.cmd_merge(s1, base), 0)
        # session 2: B saved twice (save timing), C removed, exit collection refreshes the asset list only
        s2 = self.tmp / "live" / "sessions" / "2"
        self.raw(s2 / "saves" / "0001", ["/Game/B"], partial=True, skipped=True)
        self.raw(s2 / "saves" / "0002", ["/Game/B"], partial=True, skipped=True)
        self.raw(s2 / "raw", [], partial=True, assets=["/Game/A", "/Game/B"])
        write(s2 / "session.json", json.dumps({"id": "2", "removed": ["/Game/C"]}))
        self.assertEqual(lens_live.cmd_merge(s2, base), 0)
        bps = {b["package"]: b for b in json.loads((base / "blueprints.json").read_text(encoding="utf-8"))["blueprints"]}
        self.assertEqual(sorted(bps), ["/Game/A", "/Game/B"])
        self.assertTrue(bps["/Game/B"]["rev"].endswith("0002"))                 # the last save wins
        self.assertTrue(bps["/Game/A"]["rev"].endswith("raw"))                  # untouched from the full collection
        refl = json.loads((base / "reflection.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(c["package"] for c in refl["blueprint"]), ["/Game/A", "/Game/B"])
        self.assertFalse(json.loads((base / "meta.json").read_text(encoding="utf-8"))["partial"])
        # a partial collection without a cache is refused (live rebuild collects everything again)
        with self.assertRaises(SystemExit):
            lens_live.cmd_merge(s2, self.tmp / "empty-base")

    def make_live_run(self, runs: Path, name: str, classes: list[str]) -> Path:
        run = runs / name
        write(run / "run.json", json.dumps({"source": "live", "line": "architecture-report"}))
        write(run / "raw" / "meta.json", "{}")
        write(run / "out" / "graph.json", json.dumps({"symbols": [{"key": f"bp:/Game/{c}.{c}_C", "kind": "blueprint", "external": False} for c in classes]}))
        write(run / "out" / "flow.json", json.dumps({"edges": [{"from": f"bp:/Game/{a}.{a}_C", "to": f"bp:/Game/{b}.{b}_C", "kind": "spawns"}
                                                               for a, b in zip(classes, classes[1:])]}))
        return run

    def test_accumulate_changes_runtime_and_retention(self):
        import lens_live
        project = self.tmp / "Proj"
        runs = project / "__dwookit" / "data" / "runs"
        session = project / "__dwookit" / "live" / "sessions" / "2"
        write(session / "session.json", json.dumps({"id": "2", "ended": "normal", "collected": "partial"}))
        write(session / "events.jsonl", "\n".join(json.dumps(e) for e in [
            {"type": "asset_saved", "package": "/Game/B"}, {"type": "asset_opened", "package": "/Game/B"},
            {"type": "pie_end", "classes": {"/Game/A.A_C": 1, "/Game/B.B_C": 3}},
            {"type": "log", "level": "error", "category": "LogBlueprint", "message": "bad"}]))
        old = self.make_live_run(runs, "run-1", ["A", "B"])
        new = self.make_live_run(runs, "run-2", ["A", "B", "C"])
        self.assertEqual(lens_live.cmd_accumulate(project, new, session, "history"), 0)
        changes = json.loads((new / "out" / "changes.json").read_text(encoding="utf-8"))
        self.assertEqual(changes["classesAdded"], ["C"])
        self.assertEqual(changes["flowAdded"], ["B -spawns-> C"])
        self.assertEqual(changes["activity"]["saved"], ["/Game/B"])
        self.assertEqual(changes["activity"]["pieRuns"], 1)
        self.assertEqual(changes["activity"]["logErrors"], 1)
        runtime = json.loads((project / "__dwookit" / "live" / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["classes"]["/Game/B.B_C"]["maxCount"], 3)
        self.assertTrue((project / "__dwookit" / "live" / "history.jsonl").exists())
        self.assertTrue(old.exists() and not (old / "raw").exists())           # history: results kept, raw dropped
        self.assertEqual(json.loads((session / "session.json").read_text(encoding="utf-8"))["ingested"], "run-2")
        newer = self.make_live_run(runs, "run-3", ["A"])
        lens_live.cmd_accumulate(project, newer, session, "latest")
        self.assertFalse(old.exists() or new.exists())                          # latest: only the newest run
        snap = self.make_live_run(runs, "run-4", ["A"])
        lens_live.cmd_accumulate(project, snap, session, "snapshot")
        self.assertTrue((newer / "raw").exists())                               # snapshot: everything kept


class ReportTests(GameFlowTests):
    """lens_report.py: the interactive single-file report built from an analysis run."""

    def test_report_embeds_the_run(self):
        import lens_flow, lens_report
        raw, out, project, bp = self.make_run()
        (out / "flow.json").write_text(json.dumps(lens_flow.build(raw, out, project)), encoding="utf-8")
        write(out / "findings.json", json.dumps({"verdict": "warn", "findings": [
            {"rule": "tick-usage", "severity": "warn", "message": "Tick </script> 사용", "items": [f"/Game/X{i}" for i in range(400)]}]}))
        write(out.parent / "run.json", json.dumps({"runId": "run-1", "source": "live"}))
        path = lens_report.write(out, project)
        html = path.read_text(encoding="utf-8")
        self.assertIn("<title>", html)
        self.assertNotIn("Tick </script>", html)                 # data cannot close the script tag
        start = html.index("const DATA = ") + len("const DATA = ")
        data = json.loads(html[start:html.index(";\nconst $ =")])
        self.assertEqual(data["source"], "live")
        self.assertEqual(data["problems"][0]["more"], 100)       # long lists are capped
        self.assertEqual(data["classes"][bp("BP_Hero")]["role"], "character")
        self.assertTrue(any(r["kind"] == "map" for r in data["flow"]["roots"]))
        self.assertEqual(data["rules"]["tick-usage"]["name"], "Tick 사용")     # every problem carries why and how to fix
        self.assertTrue(data["rules"]["tick-usage"]["fix"])
        self.assertIsNone(data["compare"])                       # no earlier run to compare with
        self.assertNotIn("__COMMON__", html)

    def test_report_compares_with_the_previous_run(self):
        import lens_report
        runs = self.tmp / "runs"
        def run(name, findings):
            out = runs / name / "out"
            write(out / "findings.json", json.dumps({"verdict": "warn", "findings": findings}))
            return out
        tick = lambda bp: {"rule": "tick-usage", "severity": "warn", "message": f"{bp}: Tick",
                           "items": [{"blueprint": bp, "graph": "EventGraph", "node": "AB"}]}
        unused = lambda n: {"rule": "unused-asset", "severity": "info", "message": f"미사용 {n}개", "items": ["/Game/A"]}
        run("run-1", [tick("B_A"), tick("B_B"), unused(3)])
        data = lens_report.build_data(run("run-2", [tick("B_A"), tick("B_C"), unused(5)]), None)
        cmp = data["compare"]
        self.assertEqual((cmp["previousRun"], cmp["new"], cmp["fixed"]), ("run-1", 1, 1))
        self.assertEqual([p["change"] for p in data["problems"]], ["same", "new", "same"])   # aggregate rules match by rule
        self.assertEqual(data["problems"][0]["items"], ["B_A · EventGraph"])                  # record targets become one line


class RunReportTests(TempDir):
    """lens_report_runs.py: interactive verify / logs / smoke reports."""

    @staticmethod
    def embedded(html: str) -> dict:
        start = html.index("const DATA = ") + len("const DATA = ")
        return json.loads(html[start:html.index(";\nconst $ =")])

    def run_dir(self, name: str) -> Path:
        project = self.tmp / "Proj"
        write(project / "__dwookit" / "state.json", json.dumps({"project": "MyGame"}))
        run = project / "__dwookit" / "data" / "runs" / name
        (run / "out").mkdir(parents=True)
        return run

    def test_each_kind_embeds_its_result(self):
        import lens_report_runs
        group = {"fingerprint": "ab", "severity": "Error", "category": "LogTemp", "sample": "bad </script>", "count": 2,
                 "where": ["Game.log:3"], "detail": [], "new": True}
        logs = {"verdict": "fail", "counts": {"Fatal": 0, "Error": 2, "Warning": 0}, "groups": [group], "crashes": [],
                "newGroups": 1, "newCrashes": 0, "files": ["Game.log"]}
        verify = {"verdict": "fail", "steps": [
            {"id": "V1", "name": "compile", "verdict": "fail", "summary": logs},
            {"id": "V3", "name": "tests", "verdict": "fail", "automation": {
                "summary": {"succeeded": 1, "succeededWithWarnings": 0, "failed": 1, "notRun": 0},
                "tests": [{"name": "Project.A.Ok", "state": "Success", "errors": [], "warnings": []},
                          {"name": "Project.A.Bad", "state": "Fail", "errors": ["expected 1"], "warnings": []}]}}]}
        smoke = {"verdict": "pass", "seconds": 15, "maxErrors": 0, "maxWarnings": 200, "maps": [
            {"map": "/Game/Maps/L_A", "verdict": "pass", "seconds": 15, "errors": 0, "warnings": 0, "ensures": 0,
             "fatal": False, "exitCode": None, "killed": True, "crashes": [], "groups": [], "reasons": [], "log": "a.log"}]}
        for kind, result in (("verify", verify), ("logs", logs), ("smoke", smoke)):
            run = self.run_dir(f"{kind}-1")
            path = lens_report_runs.write(run, kind, result)
            html = path.read_text(encoding="utf-8")
            self.assertNotIn("bad </script>", html)                 # data cannot close the script tag
            data = self.embedded(html)
            self.assertEqual((data["kind"], data["project"], data["runId"]), (kind, "MyGame", f"{kind}-1"))
            self.assertEqual(data["result"]["verdict"], result["verdict"])
            self.assertIn(f"<title>MyGame · {lens_report_runs.TITLES[kind]}</title>", html)
            self.assertIn("function dataTable(", html)               # shared filters and tables from lens_report
            self.assertNotIn("__COMMON__", html)

    def test_reads_the_json_and_works_outside_a_project(self):
        import lens_report_runs
        run = self.tmp / "loose" / "logs-2"
        write(run / "out" / "logs.json", json.dumps({"verdict": "pass", "counts": {"Fatal": 0, "Error": 0, "Warning": 0},
                                                      "groups": [], "crashes": [], "newGroups": 0, "newCrashes": 0, "files": []}))
        data = self.embedded(lens_report_runs.write(run, "logs").read_text(encoding="utf-8"))
        self.assertEqual(data["project"], "")


class CatalogAndConfigTests(unittest.TestCase):
    def test_catalog_is_consistent(self):
        catalog = json.loads((TOOLS / "catalog.json").read_text(encoding="utf-8"))
        ids = [t["id"] for t in catalog["tools"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate tool ids")
        for t in catalog["tools"]:
            self.assertIn(t["kind"], ("build", "wrap", "ai"))
            self.assertIn(t["status"], ("ready", "planned"))
            self.assertRegex(t["station"], r"^(ST-[1-5]|BUILD|OPS|UI)$")
            self.assertIn(t["tool"], ("lens", "statemonitor", "canvas", "builder", "forge"))
        tools = {t["tool"] for t in catalog["tools"]}
        self.assertEqual(tools, {"lens", "statemonitor", "canvas", "builder", "forge"}, "every dwookit tool group has catalog entries")
        commands = " ".join(t["command"] for t in catalog["tools"])
        for command in ("canvas-install", "canvas-remove", "canvas-import", "canvas-validate", "sm-install", "sm-remove"):
            self.assertIn(command, commands)

    def test_default_config_is_valid_json_with_all_sections(self):
        cfg = json.loads((ROOT / "config" / "default.json").read_text(encoding="utf-8"))
        for section in ("naming", "complexity", "unused", "verify", "logs", "smoke", "perf", "sizes", "trace", "stateMonitor"):
            self.assertIn(section, cfg)


if __name__ == "__main__":
    unittest.main()
