"""DwoLens file explanation: one tracked Blueprint as a person reads it, from the whole down to the logic.

    1. summary      one sentence: what kind of object it is and what it does
    2. composition  what it is made of: components (with their role), settings (editable values), state (values
                    that change while the game runs, and which behaviors change them), signals, timelines, widgets
    3. behaviors    one per entry point (event, input, custom event, function): when it runs, the author's own
                    note, what it does in a few words, what it changes and calls, who calls it
    4. flow         each behavior's steps as a tree in plain words. Calls go deeper instead of stopping: into the
                    Blueprint's own functions and custom events, into other tracked Blueprints, and into the C++
                    function bodies of the project (read from Source/)
    5. logic        under every step, the pseudocode line it comes from (lens_bp.py)

Rule-based (no LLM, no network). For better wording an agent can add <Name>.agent.json (see the prompt file this
module writes next to it); the window and the HTML page show that text next to the rule-based one.

    python lens_explain.py --dir <__dwookit/blueprints> [--project <dir>]

Writes, per Blueprint folder: <Name>.explain.json (lens.explain/1), <Name>.html, <Name>.explain-prompt.md, and
index.html in the root.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lens_bp  # noqa: E402

SCHEMA = "lens.explain/1"
MAX_EXPAND_DEPTH = 4        # own functions / other Blueprints / C++ followed this deep
MAX_STEPS = 600             # per behavior, after expansion
MAX_CPP_LINES = 40

TRIGGERS = {
    "BeginPlay": "게임이 시작되거나 배치된 뒤 처음 한 번",
    "Tick": "매 프레임",
    "EndPlay": "게임에서 빠질 때",
    "Destroyed": "제거될 때",
    "ActorBeginOverlap": "다른 액터가 겹치기 시작하면",
    "ActorEndOverlap": "겹쳐 있던 액터가 떠나면",
    "Hit": "무언가와 부딪히면",
    "AnyDamage": "피해를 받으면",
    "PointDamage": "한 점에 피해를 받으면",
    "RadialDamage": "범위 피해를 받으면",
    "Possessed": "컨트롤러가 조종을 시작하면",
    "UnPossessed": "조종이 끝나면",
    "Landed": "땅에 닿으면",
    "Construct": "위젯이 화면용으로 만들어질 때",
    "PreConstruct": "위젯을 디자이너·실행에서 준비할 때",
    "Destruct": "위젯이 없어질 때",
    "OnInitialized": "위젯이 처음 초기화될 때",
    "UserConstructionScript": "에디터에 배치하거나 값을 바꿀 때 (Construction Script)",
    "BlueprintInitializeAnimation": "애니메이션이 초기화될 때",
    "BlueprintUpdateAnimation": "애니메이션을 매 프레임 갱신할 때",
    "BlueprintThreadSafeUpdateAnimation": "애니메이션을 매 프레임 갱신할 때 (작업 스레드)",
    "K2_ActivateAbility": "어빌리티가 실행되면", "ActivateAbility": "어빌리티가 실행되면",
    "K2_ActivateAbilityFromEvent": "게임플레이 이벤트로 어빌리티가 실행되면", "ActivateAbilityFromEvent": "게임플레이 이벤트로 어빌리티가 실행되면",
    "K2_OnEndAbility": "어빌리티가 끝나면", "OnEndAbility": "어빌리티가 끝나면", "K2_CanActivateAbility": "어빌리티를 실행할 수 있는지 물을 때",
    "K2_OnAbilityAdded": "어빌리티를 받았을 때", "K2_OnAbilityRemoved": "어빌리티가 없어질 때",
    "OnBurst": "게임플레이 큐가 한 번 터질 때", "WhileActive": "게임플레이 큐가 켜져 있는 동안", "OnRemove": "게임플레이 큐가 꺼질 때",
    "Received_NotifyTick": "애님 노티파이 구간 동안", "Received_Notify": "애니메이션의 노티파이 지점에서",
    "OnPossess": "컨트롤러가 폰을 조종하기 시작하면", "OnUnPossess": "컨트롤러가 조종을 끝내면",
    "ReceiveActorBeginCursorOver": "마우스가 올라오면", "ReceiveActorOnClicked": "클릭되면",
}
BOUND = {
    "OnComponentBeginOverlap": "{c}에 무언가 겹치기 시작하면", "OnComponentEndOverlap": "{c}에서 겹침이 끝나면",
    "OnComponentHit": "{c}이(가) 부딪히면", "OnClicked": "{c}을(를) 클릭하면", "OnPressed": "{c}을(를) 누르면",
    "OnReleased": "{c}에서 손을 떼면", "OnHovered": "{c}에 마우스를 올리면", "OnUnhovered": "{c}에서 마우스가 벗어나면",
    "OnValueChanged": "{c}의 값이 바뀌면", "OnTextChanged": "{c}의 글자가 바뀌면", "OnTextCommitted": "{c}의 입력을 마치면",
    "OnCheckStateChanged": "{c}의 체크가 바뀌면", "OnSelectionChanged": "{c}의 선택이 바뀌면",
}
VERBS = [
    # (function name pattern, Korean phrase; {t} = target)
    (r"^SetRelativeLocation$|^SetActorLocation$|^SetWorldLocation$|^K2_SetActorLocation$", "{t} 위치 옮기기"),
    (r"^SetRelativeRotation$|^SetActorRotation$|^SetWorldRotation$", "{t} 회전 바꾸기"),
    (r"^SetRelativeScale3D$|^SetActorScale3D$|^SetWorldScale3D$", "{t} 크기 바꾸기"),
    (r"^SetActorTransform$|^SetRelativeTransform$|^SetWorldTransform$", "{t} 위치·회전·크기 바꾸기"),
    (r"^AddMovementInput$", "이동 입력 넣기"), (r"^Jump$", "점프"), (r"^StopJumping$", "점프 멈추기"),
    (r"^LaunchCharacter$", "{t}을(를) 튕겨 보내기"), (r"^AddControllerYawInput$|^AddControllerPitchInput$", "시점 돌리기"),
    (r"^SpawnActor", "액터 생성"), (r"^DestroyActor$|^K2_DestroyActor$", "{t} 제거"),
    (r"^DestroyComponent$|^K2_DestroyComponent$", "{t} 컴포넌트 제거"), (r"^AddComponent", "컴포넌트 추가"),
    (r"^PlaySound|^SpawnSound", "소리 재생"), (r"^SpawnEmitter|^SpawnSystem", "이펙트 생성"),
    (r"^PrintString$|^PrintText$", "화면·로그에 글자 출력"), (r"^Create.*Widget$|^Create$", "위젯 만들기"),
    (r"^AddToViewport$|^AddToPlayerScreen$", "{t} 화면에 띄우기"), (r"^RemoveFromParent$", "{t} 화면에서 내리기"),
    (r"^SetVisibility$|^SetHiddenInGame$|^SetActorHiddenInGame$", "{t} 보이기·숨기기"),
    (r"^SetText$", "{t} 글자 바꾸기"), (r"^SetBrush", "{t} 그림 바꾸기"), (r"^SetPercent$", "{t} 진행도 바꾸기"),
    (r"^SetTimerBy|^K2_SetTimer", "타이머 걸기"), (r"^ClearTimer|^ClearAndInvalidateTimer|^K2_ClearTimer", "타이머 풀기"),
    (r"^OpenLevel", "레벨 열기"), (r"^QuitGame$", "게임 종료"), (r"^ApplyDamage$|^ApplyPointDamage$|^ApplyRadialDamage", "피해 주기"),
    (r"^SetCollisionEnabled$|^SetActorEnableCollision$", "{t} 충돌 켜기·끄기"), (r"^SetCollisionResponse", "{t} 충돌 반응 바꾸기"),
    (r"^PlayAnimMontage$|^Montage_Play$", "몽타주 재생"), (r"^StopAnimMontage$|^Montage_Stop$", "몽타주 멈추기"),
    (r"^SetStaticMesh$", "{t} 메시 바꾸기"), (r"^SetMaterial$", "{t} 머티리얼 바꾸기"), (r"^SetSkeletalMesh", "{t} 메시 바꾸기"),
    (r"^SetLightColor$|^SetIntensity$", "{t} 조명 바꾸기"), (r"^SetVariable", "{t} 파라미터 바꾸기"),
    (r"^SetBoxExtent$|^SetSphereRadius$|^SetCapsuleSize$", "{t} 감지 영역 크기 바꾸기"),
    (r"^SetInputMode", "입력 모드 바꾸기"), (r"^SetShowMouseCursor$", "마우스 커서 보이기·숨기기"),
    (r"^AddMappingContext$", "입력 매핑 추가"), (r"^RemoveMappingContext$", "입력 매핑 제거"),
    (r"^Possess$", "조종 시작"), (r"^UnPossess$", "조종 끝내기"), (r"^MoveTo|^AIMoveTo|^SimpleMoveTo", "목표로 이동"),
    (r"^RunBehaviorTree$", "행동 트리 실행"), (r"^SetValueAs", "블랙보드 값 쓰기"),
    (r"^PlayWorldCameraShake$|^StartCameraShake$|^ClientStartCameraShake$", "카메라 흔들기"),
    (r"^SetViewTarget", "카메라 대상 바꾸기"), (r"^Delay$", "기다리기"), (r"^RetriggerableDelay$", "기다리기 (다시 오면 처음부터)"),
    (r"^Play$|^PlayFromStart$", "{t} 재생"), (r"^Reverse$|^ReverseFromEnd$", "{t} 거꾸로 재생"), (r"^Stop$", "{t} 멈추기"),
    (r"^Broadcast$|\.broadcast$", "신호 보내기"), (r"^Activate$", "{t} 켜기"), (r"^Deactivate$", "{t} 끄기"),
    (r"^SaveGameToSlot$|^AsyncSaveGameToSlot$", "게임 저장"), (r"^LoadGameFromSlot$|^AsyncLoadGameFromSlot$", "게임 불러오기"),
    (r"^SetScalarParameterValue|^SetVectorParameterValue|^SetTextureParameterValue", "{t} 머티리얼 값 바꾸기"),
    (r"^CreateDynamicMaterialInstance$|^GetDynamicMaterial$", "{t} 머티리얼 인스턴스 만들기"),
    (r"^PlayAnimation(Forward|Reverse)?$|^PlayAnimationTimeRange", "UI 애니메이션 재생"), (r"^StopAnimation", "UI 애니메이션 멈추기"),
    (r"^PlayMontageAndWait|^K2_PlayMontage", "몽타주 재생 (끝날 때까지)"), (r"^SendGameplayEventToActor$", "게임플레이 이벤트 보내기"),
    (r"^ExecuteGameplayCue|^K2_ExecuteGameplayCue|^AddGameplayCue", "게임플레이 큐(이펙트·소리) 실행"),
    (r"^ApplyGameplayEffect|^BP_ApplyGameplayEffect", "게임플레이 이펙트 적용"), (r"^CommitAbility|^K2_CommitAbility", "어빌리티 비용·쿨다운 적용"),
    (r"^EndAbility$|^K2_EndAbility$", "어빌리티 끝내기"), (r"^ActivateAbility|^K2_ActivateAbility", "어빌리티 실행"),
    (r"^WaitGameplayEvent$|^WaitInputRelease$|^WaitInputPress$|^WaitDelay$", "기다리기 ({fn})"),
    (r"^AddImpulse|^AddForce", "{t}에 힘 주기"), (r"^SetSimulatePhysics$|^SetAllBodiesBelowSimulatePhysics$", "{t} 물리 시뮬레이션 켜기·끄기"),
    (r"^SetCollisionProfileName$", "{t} 충돌 설정 바꾸기"), (r"^Array_Add|^Add$", "목록에 추가"), (r"^Array_Remove|^Remove$", "목록에서 빼기"),
    (r"^Array_Clear|^Clear$", "목록 비우기"), (r"^RegisterExtension|^RegisterWidget", "UI 확장 등록"), (r"^Unregister", "등록 해제"),
    (r"^PushContentToLayer", "UI 레이어에 화면 올리기"), (r"^SetWidthOverride$|^SetHeightOverride$", "{t} 크기 바꾸기"),
    (r"^SetColorAndOpacity$|^SetRenderOpacity$", "{t} 색·투명도 바꾸기"), (r"^SetRenderTransform", "{t} 화면 위치·회전 바꾸기"),
]
COMPONENT_ROLES = [
    (r"^(Box|Sphere|Capsule)Component$", "감지·충돌 영역"), (r"StaticMeshComponent$|^InstancedStaticMesh", "보이는 모양 (메시)"),
    (r"SkeletalMeshComponent$", "움직이는 모양 (스켈레탈 메시)"), (r"^CameraComponent$", "카메라"),
    (r"^SpringArmComponent$", "카메라 거리·충돌 (스프링 암)"), (r"LightComponent$", "조명"),
    (r"^NiagaraComponent$|^ParticleSystemComponent$", "이펙트"), (r"^AudioComponent$", "소리"),
    (r"^WidgetComponent$", "월드에 띄운 UI"), (r"MovementComponent$", "이동 처리"), (r"^SceneComponent$", "기준점 (위치만)"),
    (r"^ArrowComponent$", "방향 표시 (에디터용)"), (r"^BillboardComponent$", "아이콘 (에디터용)"),
    (r"^TextRenderComponent$", "월드에 쓴 글자"), (r"^DecalComponent$", "바닥·벽에 찍는 그림 (데칼)"),
    (r"PhysicsConstraint", "물리 연결"), (r"^ChildActorComponent$", "붙어 있는 다른 액터"),
]
KIND_WORDS = {"Actor": "월드에 놓이는 액터", "Character": "걸어 다니는 캐릭터", "Pawn": "조종할 수 있는 폰",
              "PlayerController": "플레이어 입력을 받는 컨트롤러", "AIController": "AI 컨트롤러",
              "GameModeBase": "게임 규칙 (게임 모드)", "GameMode": "게임 규칙 (게임 모드)", "GameStateBase": "게임 전체 상태",
              "PlayerState": "플레이어별 상태", "GameInstance": "게임 전체에서 살아 있는 인스턴스", "HUD": "HUD",
              "UserWidget": "UI 위젯", "ActorComponent": "액터에 붙이는 기능 (컴포넌트)", "SceneComponent": "위치가 있는 컴포넌트",
              "AnimInstance": "애니메이션 로직", "Object": "데이터·로직 객체", "CameraShakeBase": "카메라 흔들림 설정",
              "DefaultCameraShakeBase": "카메라 흔들림 설정", "PlayerCameraManager": "카메라 관리자",
              "GameplayAbility": "게임플레이 어빌리티 (GAS 능력)", "GameplayEffect": "게임플레이 이펙트 (GAS 수치 변화)",
              "GameplayCueNotify_Actor": "게임플레이 큐 (이펙트·소리 액터)", "GameplayCueNotify_Static": "게임플레이 큐 (이펙트·소리)",
              "GameplayCueNotify_Burst": "게임플레이 큐 (한 번 터지는 이펙트)", "GameplayCueNotify_Looping": "게임플레이 큐 (반복 이펙트)",
              "AnimNotify": "애님 노티파이 (애니메이션의 한 지점)", "AnimNotifyState": "애님 노티파이 구간",
              "BlueprintFunctionLibrary": "함수 모음 (라이브러리)", "DataAsset": "데이터 에셋", "PrimaryDataAsset": "데이터 에셋",
              "SaveGame": "저장 데이터", "WorldSubsystem": "월드 서브시스템", "GameInstanceSubsystem": "게임 인스턴스 서브시스템",
              "ModularGameplayActor": "월드에 놓이는 액터", "ModularCharacter": "걸어 다니는 캐릭터",
              "PackedLevelActor": "묶어 둔 레벨 조각 (Packed Level Actor)", "LevelInstance": "레벨 인스턴스", "CommonActivatableWidget": "UI 화면 (활성화되는 위젯)", "CommonUserWidget": "UI 위젯", "CommonButtonBase": "UI 버튼"}


# ---------------------------------------------------------------------------------------------- small helpers

def split_words(name: str) -> str:
    name = re.sub(r"^K2_|^Receive|^BP_", "", name)
    name = re.sub(r"^b(?=[A-Z])", "", name)                    # bool prefix: bZOverride -> Z Override
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").strip()


def split_top(text: str, sep: str = ",") -> list[str]:
    """Splits at separators outside parentheses and quotes."""
    out, depth, cur, quote = [], 0, "", False
    for ch in text:
        if ch == '"':
            quote = not quote
        elif not quote and ch in "([":
            depth += 1
        elif not quote and ch in ")]":
            depth -= 1
        if ch == sep and depth == 0 and not quote:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def unwrap(expr: str) -> str:
    """Drops parentheses around the whole expression: '((A == 0))' -> 'A == 0'."""
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        for i, ch in enumerate(expr):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(expr) - 1:
                return expr                      # '(a) + (b)': the first group closes early
        expr = expr[1:-1].strip()
    return expr


# ---------------------------------------------------------------------------------------------- Korean particles
# Templates write 을(를), 이(가), (으)로 after names that can be Korean or English; pick the one that fits the
# word before it so the text reads "Position을 계산값으로 바꾸기", not "Position을(를) 계산한 값(으)로 바꾸기".
_SKIP = "'\"‘’“”)]}> "
_DIGIT_FINAL = {"0": "ㅇ", "1": "ㄹ", "3": "ㅁ", "6": "ㄱ", "7": "ㄹ", "8": "ㄹ"}


def final_consonant(word: str) -> str:
    """The final consonant of how the word ends when read in Korean: '' (none), 'ㄹ', or another one ('x')."""
    w = word.rstrip(_SKIP)
    if not w:
        return ""
    ch = w[-1]
    if "가" <= ch <= "힣":
        jong = (ord(ch) - 0xAC00) % 28
        return "" if jong == 0 else ("ㄹ" if jong == 8 else "x")
    if ch.isdigit():
        return _DIGIT_FINAL.get(ch, "")
    low = w.lower()
    if low[-1] in "kpt" and len(low) > 1 and low[-2] not in "aeiouyc":    # Component 컴포넌트, Jump 점프: no final
        return ""
    if low[-1] in "klmnpt" or low.endswith("ng") or low.endswith("ck") or (low[-1] == "c" and len(low) > 1):
        return "ㄹ" if low[-1] == "l" else "x"
    if low.endswith(("ne", "me")):                         # Engine, Game, Time
        return "x"
    if low.endswith("le"):                                 # Scale, Toggle
        return "ㄹ"
    return ""


def particles(text: str) -> str:
    def pick(m):
        before = text[:m.start()]
        fin = final_consonant(before)
        tag = m.group(0)
        if tag == "을(를)":
            return "을" if fin else "를"
        if tag == "이(가)":
            return "이" if fin else "가"
        return "으로" if fin and fin != "ㄹ" else "로"
    return re.sub(r"을\(를\)|이\(가\)|\(으\)로", pick, text)


def fix_particles(value, key: str = ""):
    """Apply particles() to every display string of an explanation (not to pseudocode or C++ bodies)."""
    if key in ("pseudocode", "code", "signature"):
        return value
    if isinstance(value, str):
        return particles(value)
    if isinstance(value, list):
        return [fix_particles(v, key) for v in value]
    if isinstance(value, dict):
        return {k: fix_particles(v, k) for k, v in value.items()}
    return value


def human_expr(expr: str, depth: int = 0) -> str:
    """A pure expression in words where a pattern is known; otherwise the expression itself."""
    expr = unwrap(expr)
    if depth > 4:
        return expr
    m = re.fullmatch(r"as(\w+)", expr)
    if m:
        return f"변환한 {m.group(1)}"
    m = re.fullmatch(r"(?:Array_)?Length\((?:TargetArray=)?(.+)\)", expr)
    if m:
        return f"{human_expr(m.group(1), depth + 1)}의 개수"
    m = re.fullmatch(r"([\w .]+?)\.Get(\w+)\((.*)\)", expr)
    if m:
        args = [a.split("=", 1)[-1] for a in split_top(m.group(3))]
        what = split_words(m.group(2))
        if what.startswith("Overlapping"):
            what = "겹친 " + (args[0] if args else "") + " 목록" if args else "겹친 액터 목록"
            return f"{human_expr(m.group(1), depth + 1)}에 {what}"
        return f"{human_expr(m.group(1), depth + 1)}의 {what}"
    m = re.fullmatch(r"([\w .]+?)\.ActorHasTag\(Tag=(.+)\)", expr)
    if m:
        return f"{human_expr(m.group(1), depth + 1)}에 태그 {m.group(2)}이(가) 붙어 있음"
    m = re.fullmatch(r"isValid\(([^()]+)\)", expr)
    if m:
        return f"{m.group(1)}이(가) 있음"
    m = re.fullmatch(r"(.+?) (==|!=|<=|>=|<|>) (.+)", expr)
    if m and len(split_top(expr, " ")) >= 3:
        op = {"==": "=", "!=": "≠"}.get(m.group(2), m.group(2))
        return f"{human_expr(m.group(1), depth + 1)} {op} {human_expr(m.group(3), depth + 1)}"
    return expr


def shorten(text: str, limit: int = 48) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def role_of(component_class: str) -> str:
    for pat, role in COMPONENT_ROLES:
        if re.search(pat, component_class):
            return role
    return ""


def kind_words(bp: dict, cpp: "CppIndex | None" = None) -> str:
    chain = [lens_bp.short(p) for p in bp.get("parentChain", [])] + [lens_bp.short(bp.get("parent"))]
    for c in chain:
        if c in KIND_WORDS:
            return KIND_WORDS[c]
        seen = 0
        while cpp and c in cpp.parents and seen < 8:        # project C++ parent: walk up to an engine class
            c = cpp.parents[c]
            seen += 1
            if c in KIND_WORDS:
                return KIND_WORDS[c]
    return {"WidgetBlueprint": "UI 위젯", "AnimBlueprint": "애니메이션 로직"}.get(bp.get("assetClass", ""), "블루프린트")


def trigger_of(event: str, params: str, kind: str, bp: dict) -> str:
    if kind != "event":
        if event in TRIGGERS:
            return TRIGGERS[event]
        return f"함수 '{event}'을(를) 부를 때"
    if "." in event:
        comp, _, member = event.partition(".")
        for key, text in BOUND.items():
            if member.startswith(key):
                return text.format(c=comp)
        return f"{comp}의 '{split_words(member)}' 이벤트가 오면"
    base = re.sub(r"^Receive", "", event.replace(" ", ""))
    if base in TRIGGERS:
        return TRIGGERS[base]
    if event.replace(" ", "") in TRIGGERS:
        return TRIGGERS[event.replace(" ", "")]
    if re.match(r"^(IA_|InputAction|EnhancedInputAction)", event) or "Input" in event:
        return f"입력 '{re.sub(r'^(EnhancedInputAction|InputAction)\s*', '', event)}'이(가) 들어오면"
    custom = any(g.get("kind") == "event" for g in bp.get("graphs", []))
    return f"'{event}'이(가) 호출되면" if custom else f"'{event}' 이벤트가 오면"


# ---------------------------------------------------------------------------------------------- C++ bodies

class CppIndex:
    """Project C++ member function definitions (Source/ and project plugins), found by class and function name."""

    DEF = re.compile(r"^[\w:<>,\s\*&]*?\b([AUFI]?\w+)::(\w+)\s*\(", re.M)

    def __init__(self, project: Path | None):
        self.funcs: dict[tuple[str, str], dict] = {}
        self.by_name: dict[str, list[dict]] = {}
        self.parents: dict[str, str] = {}              # CombatCharacter -> Character (prefix A/U dropped)
        if not project:
            return
        try:
            import lens_targets
            for c in lens_targets.scan_classes(project):
                self.parents[c["runtimeClass"]] = re.sub(r"^[AU]", "", c["parent"])
        except Exception:                               # the explanation works without it
            pass
        roots = [project / "Source"] + [p.parent / "Source" for p in (project / "Plugins").rglob("*.uplugin")
                                        if not p.parent.name.startswith("__")] if (project / "Plugins").is_dir() else [project / "Source"]
        for root in roots:
            if not root.is_dir():
                continue
            for f in root.rglob("*.cpp"):
                try:
                    text = f.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                for m in self.DEF.finditer(text):
                    cls, fn = m.group(1), m.group(2)
                    if cls == fn or fn.startswith("~"):
                        continue
                    body = self._body(text, m.end())
                    if body is None:
                        continue
                    line = text.count("\n", 0, m.start(1)) + 1
                    entry = {"class": cls, "function": fn, "file": f.relative_to(project).as_posix(), "line": line, "body": body}
                    key = (re.sub(r"^[AU]", "", cls), fn)
                    self.funcs.setdefault(key, entry)
                    self.by_name.setdefault(fn, []).append(entry)

    @staticmethod
    def _body(text: str, at: int) -> list[str] | None:
        depth, i = 0, at
        # skip the parameter list, then expect '{' (a declaration or call ends with ';')
        while i < len(text) and text[i] not in "{;":
            i += 1
        if i >= len(text) or text[i] == ";":
            return None
        start = i
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[start + 1:i].strip("\n").splitlines()
            i += 1
        return None

    def callers(self, fn: str) -> list[str]:
        """Project C++ functions whose body calls fn (how C++ fires a BlueprintImplementableEvent)."""
        pat = re.compile(r"(?<![\w.>:])" + re.escape(fn) + r"\s*\(")
        out = []
        for (cls, name), entry in self.funcs.items():
            if name != fn and any(pat.search(l) for l in entry["body"]):
                out.append(f"{entry['class']}::{name}")
        return sorted(out)[:4]

    def find(self, classes: list[str], fn: str) -> dict | None:
        for c in classes:
            hit = self.funcs.get((re.sub(r"^[AU]", "", c), fn))
            if hit:
                return hit
        return None


# ---------------------------------------------------------------------------------------------- flow

def parse_tree(lines: list[str]) -> list[dict]:
    """Pseudocode lines (2-space indent) to a tree of {line, children}."""
    root: list[dict] = []
    stack: list[tuple[int, list[dict]]] = [(-1, root)]
    for raw in lines:
        if not raw.strip():
            continue
        indent = (len(raw) - len(raw.lstrip(" "))) // 2
        node = {"line": raw.strip(), "children": []}
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack[-1][1].append(node)
        stack.append((indent, node["children"]))
    return root


def parse_call(text: str):
    """'await out = Target.Fn(args)  # tail' with any parentheses in Target ('Sub().Fn(x)'); None if not a call."""
    m = re.match(r"^(?P<await>await )?(?:(?P<out>[\w ]+?) = )?(?P<rest>.+)$", text)
    if not m:
        return None
    rest = m.group("rest")
    tail = ""
    t = re.search(r"\)(\s+#.*)$", rest)
    if t:
        tail = t.group(1)
        rest = rest[:t.start() + 1]
    if not rest.endswith(")"):
        return None
    depth = 0
    for i in range(len(rest) - 1, -1, -1):
        depth += rest[i] == ")"
        depth -= rest[i] == "("
        if depth == 0:
            open_at = i
            break
    else:
        return None
    head = rest[:open_at]
    fm = re.search(r"([\w ]+)$", head)
    if not fm or not fm.group(1).strip():
        return None
    fn = fm.group(1).strip()
    target = head[:fm.start(1)]
    if target and not target.endswith("."):
        return None
    return {"await": m.group("await"), "out": m.group("out"), "target": target, "fn": fn,
            "args": rest[open_at + 1:-1], "tail": tail}


CALL = re.compile(r"^(?:(?P<await>await )?(?:(?P<out>[\w ]+?) = )?)(?P<target>[\w .\[\]]+?\.)?(?P<fn>[\w ]+?)\((?P<args>.*)\)(?P<tail>.*)$")


class Explainer:
    def __init__(self, bp: dict, parts: list[dict], others: dict[str, "Explainer"], cpp: CppIndex):
        self.bp = bp
        self.parts = parts
        self.others = others            # other tracked Blueprints by name
        self.cpp = cpp
        self.vars = {v["name"]: v for v in bp.get("variables", [])}
        self.comps = {c["name"]: c for c in (bp.get("components") or []) + (bp.get("inheritedComponents") or [])}
        self.own_fns = {p["name"]: p for p in parts if p["kind"] in ("fn", "macro", "collapsed")}
        self.events: dict[str, list[str]] = {}
        for p in parts:
            if p["kind"] == "event":
                for name, lines in self.event_blocks(p["lines"]).items():
                    self.events[name] = lines
        self.expanded: set[str] = set()
        self.cpp_classes = [lens_bp.short(c) for c in bp.get("parentChain", [])] + [lens_bp.short(bp.get("parent"))]

    @staticmethod
    def event_blocks(lines: list[str]) -> dict[str, list[str]]:
        """'on X(...):' blocks of an event part, each with the author comments right above it."""
        blocks: dict[str, list[str]] = {}
        pending: list[str] = []
        current = None
        for line in lines:
            m = re.match(r"^on (.+?)\(", line)
            if m:
                current = m.group(1)
                blocks[current] = pending + [line]
                pending = []
            elif line.startswith("# ") and current is not None and not line.startswith("  "):
                pending.append(line)
            elif current is not None:
                blocks[current] += pending + [line]
                pending = []
        return blocks

    # -- one pseudocode line to words
    def target_words(self, target: str) -> str:
        t = target.rstrip(".")
        return human_expr(t) if t else "자기 자신"

    def step(self, node: dict, depth: int, seen: set[str], budget: list[int]) -> dict:
        line = node["line"]
        kids = node["children"]
        budget[0] -= 1

        def sub(nodes):
            out = []
            for n in nodes:
                if budget[0] <= 0:
                    out.append({"text": "… 이후 단계 생략 (표시 한도)", "kind": "more", "detail": "", "children": []})
                    break
                out.append(self.step(n, depth, seen, budget))
            return out

        s = {"text": line, "kind": "action", "detail": line, "children": []}
        if line.startswith("# "):
            s.update(text=line[2:], kind="note", detail="")
            return s
        m = re.match(r"^(\(disabled\) )?if (not )?(.+):(  # .*)?$", line)
        valid = re.fullmatch(r"(?:isValid|IsValidClass|IsValid)\((?:Class=|Object=)?(.+)\)", m.group(3)) if m else None
        if valid and split_top(valid.group(1)) == [valid.group(1)]:
            what = human_expr(valid.group(1))
            s.update(text=f"{what}이(가) {'없으면' if m.group(2) else '있으면'}", kind="if", children=sub(kids))
            return s
        if m and re.fullmatch(r"as(\w+)", m.group(3)):
            what = re.fullmatch(r"as(\w+)", m.group(3)).group(1)
            s.update(text=f"{what}이(가) {'아니면' if m.group(2) else '맞으면'}", kind="if", children=sub(kids))
            return s
        if m and re.fullmatch(r"isValid\(([^()]+)\)", m.group(3)):
            what = re.fullmatch(r"isValid\(([^()]+)\)", m.group(3)).group(1)
            s.update(text=f"{what}이(가) {'없으면' if m.group(2) else '있으면'}", kind="if", children=sub(kids))
            return s
        if m:
            cond = self.condition(m.group(3))
            if cond.endswith("붙어 있음"):
                text = cond[:-2] + ("있지 않으면" if m.group(2) else "있으면")
            elif cond.endswith("있음"):
                text = cond[:-2] + ("없으면" if m.group(2) else "있으면")
            else:
                text = f"{cond}{'이(가) 아니면' if m.group(2) else '이면'}"
            s.update(text=text, kind="if", children=sub(kids))
            return s
        if line == "pass":
            s.update(text="하는 일 없음", kind="more", detail="")
            return s
        if line == "else:":
            s.update(text="그렇지 않으면", kind="else", children=sub(kids))
            return s
        m = re.match(r"^for (\w+)(?:, (\w+))? in (.+):$", line)
        if m:
            s.update(text=f"{self.condition(m.group(3))}의 각 항목({m.group(1)})마다", kind="loop", children=sub(kids))
            return s
        m = re.match(r"^while (.+):$", line)
        if m:
            s.update(text=f"{self.condition(m.group(1))}인 동안 반복", kind="loop", children=sub(kids))
            return s
        m = re.match(r"^(switch|case) (.+):$", line)
        if m:
            s.update(text=(f"{self.condition(m.group(2))} 값에 따라" if m.group(1) == "switch" else f"'{m.group(2)}'이면"),
                     kind="if", children=sub(kids))
            return s
        m = re.match(r"^on (.+?)(\(.*\))?:", line)
        if m:
            label = m.group(1)
            words = {"Update": "진행되는 동안", "Finished": "끝나면", "Completed": "끝나면", "then": "그다음",
                     "Exec": "실행되면", "Loop Body": "반복할 때마다", "A": "번갈아: 첫 번째", "B": "번갈아: 두 번째",
                     "On Success": "성공하면", "On Fail": "실패하면", "Is Valid": "유효하면", "Is Not Valid": "유효하지 않으면"}
            s.update(text=words.get(label, f"'{label}'일 때"), kind="on", children=sub(kids))
            return s
        m = re.match(r"^(L\d+):$", line)
        if m:
            s.update(text=f"[{m.group(1)}] 여기서 합류", kind="label", detail="")
            return s
        m = re.match(r"^goto (L\d+)", line)
        if m:
            s.update(text=f"[{m.group(1)}] 단계로 이어짐", kind="goto")
            return s
        m = re.match(r"^return ?(.*)$", line)
        if m:
            s.update(text=f"결과로 {shorten(self.condition(m.group(1)))} 돌려주기" if m.group(1) else "여기서 끝", kind="return")
            return s
        m = re.match(r"^(\w+) = (.+) as (\w+)$", line)
        if m:
            s.update(text=f"{m.group(2)}이(가) {m.group(3)}인지 확인", kind="cast")
            return s
        m = re.match(r"^(\(disabled\) )?([\w .\[\]]+?) = (.+?)(  # .*)?$", line)
        if m:
            name, value = m.group(2).strip(), m.group(3).strip()
            looks_call = bool(re.match(r"^(await )?[\w .\[\]]+\(", value)) and not value.startswith("(")
            if name in self.vars or "." in name or not looks_call:
                shown = self.condition(value)
                if len(shown) > 40 or shown.count("(") > 1:
                    shown = "계산값"
                s.update(text=f"{name}을(를) {shown}(으)로 바꾸기", kind="set")
                return s
        m = re.match(r"^(.+?) (\+|-)= (.+)$", line)
        if m:
            what = m.group(3).lstrip("@")
            s.update(text=f"{human_expr(m.group(1))} 신호에 '{what}' {'연결' if m.group(2) == '+' else '연결 해제'}", kind="bind")
            return s
        same = re.search(r"  # outputs as at (L\d+)$", line)
        if same:
            line = line[:same.start()]
        m = parse_call(line.replace("(disabled) ", ""))
        if m:
            s = self.call_step(m, s, kids, depth, seen, budget, sub)
            if same:
                s["text"] += f" (이후 단계는 [{same.group(1)}]과 같음)"
            return s
        s["children"] = sub(kids)
        return s

    def condition(self, expr: str) -> str:
        parts = [human_expr(x) for x in re.split(r" and | or ", unwrap(expr))]
        joins = re.findall(r" (and|or) ", unwrap(expr))
        text = parts[0]
        for j, nxt in zip(joins, parts[1:]):
            text += (" 그리고 " if j == "and" else " 또는 ") + nxt
        return shorten(text, 70)

    def call_step(self, m, s, kids, depth, seen, budget, sub):
        fn = m["fn"].strip()
        target = (m["target"] or "").rstrip(".")
        args = m["args"]
        tail = m["tail"] or ""
        latent = bool(m["await"])
        s["kind"] = "call"
        # own function or custom event: go into it
        if not target and (fn in self.own_fns or fn in self.events):
            s["text"] = f"'{fn}' 실행"
            s["kind"] = "own"
            if fn in self.expanded:
                s["text"] += " (위와 같은 흐름, 다시 펼치지 않음)"
            elif depth < MAX_EXPAND_DEPTH and fn not in seen:
                self.expanded.add(fn)
                lines = self.own_fns[fn]["lines"] if fn in self.own_fns else self.events[fn][1:]
                inner = [self.step(n, depth + 1, seen | {fn}, budget) for n in parse_tree(lines)]
                s["children"] = inner
                s["expanded"] = "function" if fn in self.own_fns else "event"
            elif fn in seen:
                s["text"] += " (재귀 호출: 위 흐름 반복)"
            s["children"] += sub(kids)
            return s
        # another tracked Blueprint, reached through a variable or cast of that type
        other = self.tracked_target(target)
        if other and depth < MAX_EXPAND_DEPTH and (fn in other.own_fns or fn in other.events):
            s["text"] = f"{target}의 '{fn}' 실행 ({other.bp.get('name')})"
            s["kind"] = "other"
            lines = other.own_fns[fn]["lines"] if fn in other.own_fns else other.events[fn][1:]
            s["children"] = [other.step(n, depth + 1, seen | {f"{other.bp.get('name')}.{fn}"}, budget) for n in parse_tree(lines)]
            s["expanded"] = other.bp.get("name")
            s["children"] += sub(kids)
            return s
        phrase = None
        for pat, text in VERBS:
            if re.search(pat, fn.replace(" ", "")):
                phrase = text.format(t=self.target_words(target), fn=split_words(fn))
                phrase = re.sub(r"^자기 자신 (?=[^을이에의])", "", phrase)      # "화면에서 내리기", not "자기 자신 화면에서 내리기"
                break
        if phrase is None:
            phrase = (f"{target}의 " if target else "") + f"'{split_words(fn)}' 실행"
        arg_text = self.args_words(fn, args)
        if latent and fn in ("Delay", "RetriggerableDelay"):
            sec = re.search(r"Duration=([\d.]+)", args)
            phrase = f"{sec.group(1)}초 기다리기" if sec else phrase
            arg_text = ""
        elif latent:
            phrase += " (끝날 때까지 기다림)"
        s["text"] = phrase + (f" ({arg_text})" if arg_text else "") + (" " + tail.strip() if tail.strip().startswith("#") else "")
        # C++ of the project: show its body as the next level
        cpp = None
        if not target or target in ("self",):
            cpp = self.cpp.find(self.cpp_classes, fn.replace(" ", ""))
        else:
            tclass = self.type_of(target)
            if tclass:
                cpp = self.cpp.find([tclass], fn.replace(" ", ""))
        if cpp:
            s["cpp"] = {"class": cpp["class"], "function": cpp["function"], "file": cpp["file"], "line": cpp["line"],
                        "code": cpp["body"][:MAX_CPP_LINES], "more": max(0, len(cpp["body"]) - MAX_CPP_LINES)}
            s["kind"] = "cpp"
        s["children"] = sub(kids)
        return s

    def args_words(self, fn: str, args: str) -> str:
        if not args:
            return ""
        parts = split_top(args)
        shown = []
        for a in parts[:3]:
            k, eq, v = a.partition("=")
            if not eq:
                k, v = a, a
            v = human_expr(v)
            if "(" in v and not re.fullmatch(r"\([\d.,=XYZRGBA -]+\)", v) and not re.fullmatch(r'"[^"]*"', v):
                v = "계산값"
            shown.append(f"{split_words(k) if v != k else ''}{': ' if v != k else ''}{shorten(v, 28)}")
        return ", ".join(shown) + (" …" if len(parts) > 3 else "")

    def type_of(self, target: str) -> str | None:
        name = target.split(".")[0]
        v = self.vars.get(name)
        if v:
            obj = (v.get("type") or {}).get("object") or ""
            return lens_bp.short(obj) or None
        c = self.comps.get(name)
        if c:
            return c.get("class")
        m = re.match(r"^as(\w+)$", name)
        return m.group(1) if m else None

    def tracked_target(self, target: str):
        t = self.type_of(target) if target else None
        return self.others.get(t) if t else None

    # -- the whole document
    def behaviors(self) -> list[dict]:
        out = []
        callers: dict[str, list[str]] = {}
        entries: list[tuple[str, str, list[str], list[str]]] = []      # kind, name, head lines, body lines
        for p in self.parts:
            if p["kind"] == "event":
                for name, block in self.event_blocks(p["lines"]).items():
                    notes = [l[2:] for l in block if l.startswith("# ")]
                    head = next(l for l in block if l.startswith("on "))
                    body = block[block.index(head) + 1:]
                    entries.append(("event", name, [head] + notes, body))
            else:
                notes = [h[2:] for h in p["head"][1:] if h.startswith("# ")]
                entries.append((p["kind"], p["name"], [p["head"][0]] + notes, p["lines"]))
        names = [e[1] for e in entries]
        bound: dict[str, str] = {}
        for _, _, _, body in entries:
            for line in body:
                bm = re.match(r"^\s*(.+?) \+= @(.+)$", line)
                if bm:
                    bound.setdefault(bm.group(2).strip(), human_expr(bm.group(1)))
        for kind, name, head, body in entries:
            text = "\n".join(body)
            for other in names:
                if other != name and re.search(r"(?<![\w.])" + re.escape(other) + r"\(", text):
                    callers.setdefault(other, []).append(name)
        for i, (kind, name, head, body) in enumerate(entries):
            params = re.search(r"\((.*)\)", head[0])
            budget = [MAX_STEPS]
            self.expanded = set()
            steps = [self.step(n, 0, {name}, budget) for n in parse_tree(body)]
            writes = []
            for line in body:
                m = re.match(r"^\s*(?:\(disabled\) )?(.+?) = ", line)
                if m and m.group(1) in self.vars and m.group(1) not in writes:
                    writes.append(m.group(1))
            calls = [n for n in names if n != name and re.search(r"(?<![\w.])" + re.escape(n) + r"\(", "\n".join(body))]
            effects = []
            for st in self.flatten(steps):
                if st["kind"] in ("call", "own", "other", "cpp", "set"):
                    e = st["text"].split(" (")[0]
                    if e not in effects:
                        effects.append(e)
            effects = effects[:4]
            if all(n["line"] == "pass" for n in parse_tree(body)):
                continue                                   # nothing in it (an untouched Construction Script)
            trig = trigger_of(name, params.group(1) if params else "", "event" if kind == "event" else "fn", self.bp)
            cpp_callers = self.cpp.callers(name.replace(" ", "")) if kind == "event" and trig.startswith("'") else []
            if name in callers and kind == "event" and trig.startswith("'"):
                trig = f"'{name}'이(가) 호출되면 ({', '.join(callers[name])}에서)"
            elif cpp_callers:
                trig = f"C++ {', '.join(cpp_callers)}에서 '{name}'을(를) 부르면"
            if name in bound and kind == "event":
                trig = f"{bound[name]} 신호가 오면 (바인딩된 이벤트)"
            out.append({
                "id": f"b{i}", "kind": "event" if kind == "event" else kind, "name": name,
                "title": self.title(name, kind),
                "trigger": trig, "notes": head[1:], "signature": head[0],
                "summary": f"{trig} → " + (", ".join(effects) if effects else "하는 일 없음"),
                "writes": writes, "calls": calls, "calledBy": callers.get(name, []) + cpp_callers,
                "origins": [self.cpp_ref(c) for c in cpp_callers],
                "steps": steps, "pseudocode": body,
            })
        # engine-driven events first, then events and functions others call, Construction Script last
        def rank(b):
            if b["name"] == "UserConstructionScript":
                return 3
            if b["kind"] == "event":
                return 1 if b["calledBy"] else 0
            return 2
        out.sort(key=rank)
        return out

    def cpp_ref(self, qualified: str) -> dict:
        cls, _, fn = qualified.partition("::")
        e = self.cpp.funcs.get((re.sub(r"^[AU]", "", cls), fn)) or {}
        body = e.get("body", [])
        return {"class": cls, "function": fn, "file": e.get("file", ""), "line": e.get("line", 0),
                "code": body[:MAX_CPP_LINES], "more": max(0, len(body) - MAX_CPP_LINES)}

    def title(self, name: str, kind: str) -> str:
        if name == "UserConstructionScript":
            return "배치·수정할 때 (Construction Script)"
        if kind == "event" and "." in name:
            comp, _, member = name.partition(".")
            return f"{comp} {split_words(member)}"
        return split_words(name) if kind == "event" else f"함수 {name}"

    @staticmethod
    def flatten(steps: list[dict]):
        for s in steps:
            yield s
            yield from Explainer.flatten(s.get("children", []))

    def composition(self, behaviors: list[dict]) -> dict:
        written_by: dict[str, list[str]] = {}
        for b in behaviors:
            for w in b["writes"]:
                written_by.setdefault(w, []).append(b["title"])
        settings, state = [], []
        for v in self.bp.get("variables", []):
            item = {"name": v["name"], "type": lens_bp.ptype(v), "default": lens_bp.format_value(v.get("default") or "", v),
                    "category": v.get("category", ""), "tooltip": v.get("tooltip", ""), "changedBy": written_by.get(v["name"], [])}
            if "InstanceEditable" in v.get("flags", []) or "ExposeOnSpawn" in v.get("flags", []):
                settings.append(item)
            else:
                state.append(item)
        comps = []
        for c in (self.bp.get("components") or []):
            comps.append({"name": c["name"], "class": c["class"], "role": role_of(c["class"]), "parent": c.get("parent", ""),
                          "root": bool(c.get("root")), "changed": [f"{x['name']}={lens_bp.format_value(x['value'])}" for x in c.get("changed", [])][:4]})
        for c in (self.bp.get("inheritedComponents") or []):
            comps.append({"name": c["name"], "class": c["class"], "role": role_of(c["class"]), "parent": "", "root": False,
                          "inherited": True, "changed": [f"{x['name']}={lens_bp.format_value(x['value'])}" for x in c.get("changed", [])][:4]})
        return {
            "components": comps, "settings": settings, "state": state,
            "signals": [g["name"] for g in self.bp.get("graphs", []) if g.get("kind") == "dispatcher"],
            "timelines": [f"{t['name']} ({t.get('length', 0):g}초{', 반복' if t.get('loop') else ''})" for t in self.bp.get("timelines", [])],
            "widgets": [f"{w['name']}: {w['class']}" for w in self.bp.get("widgets", [])][:40],
            "interfaces": [lens_bp.short(i) for i in self.bp.get("interfaces", [])],
            "defaults": [f"{x['name']}={lens_bp.format_value(x['value'])}" for x in self.bp.get("classDefaults", [])][:8],
        }

    def summary(self, comp: dict, behaviors: list[dict]) -> str:
        what = kind_words(self.bp, self.cpp)
        parent = lens_bp.short(self.bp.get("parent"))
        roles = []
        for c in comp["components"]:
            if c["role"] and c["role"] not in roles and not c["role"].startswith("기준점") and "에디터용" not in c["role"]:
                roles.append(c["role"])

        acts = [b for b in behaviors if b["kind"] == "event" and b["name"] != "UserConstructionScript"]
        acts = [b for b in acts if not b["calledBy"]] or acts
        does = "; ".join(f"{b['trigger']} {', '.join(b['summary'].split(' → ', 1)[1].split(', ')[:3])}" for b in acts[:3])
        text = f"{what}, 부모 클래스 {parent}."
        if roles:
            text += f" 구성: {', '.join(roles[:4])}."
        if does:
            text += f" 주요 동작: {does}" + (f" 외 {len(acts) - 3}개" if len(acts) > 3 else "") + "."
        elif not behaviors:
            changed = comp["defaults"] + [f"{c['name']}: {', '.join(c['changed'][:2])}" for c in comp["components"] if c["changed"]]
            text += " 그래프 로직 없음" + (f", 바꾼 기본값: {'; '.join(shorten(x, 60) for x in changed[:3])}" if changed else "") + "."
        return particles(text)


def explain(bp: dict, others: dict[str, Explainer] | None = None, cpp: CppIndex | None = None) -> dict:
    parts, _ = lens_bp.graph_parts(bp)
    ex = Explainer(bp, parts, others or {}, cpp or CppIndex(None))
    behaviors = ex.behaviors()
    comp = ex.composition(behaviors)
    return fix_particles({"schema": SCHEMA, "name": bp.get("name"), "package": bp.get("package"), "assetClass": bp.get("assetClass"),
            "parent": lens_bp.short(bp.get("parent")), "parentChain": [lens_bp.short(c) for c in bp.get("parentChain", [])],
            "fingerprint": lens_bp.fingerprint(bp),
            "kind": kind_words(bp), "summary": ex.summary(comp, behaviors), "composition": comp, "behaviors": behaviors})


# ---------------------------------------------------------------------------------------------- outputs

PROMPT = """\
# Explain {name} for a person (dwookit)

Read `{name}.explain.json` (rule-based) and, only where a step is unclear, `{name}.md` or `parts/`. Write
`{name}.agent.json` next to them, in Korean, exactly this shape, nothing else:

{{"fingerprint": "copy the fingerprint from {name}.explain.json (it says which version of the Blueprint this is for)",
  "summary": "2-3 sentences: what this object is and what it does in the game",
  "behaviors": {{"<behavior id from explain.json>": "1-2 sentences: when it runs and what the player sees happen"}},
  "notes": ["at most 5 short remarks: risks, duplicated logic, things to move to C++"]}}

Describe gameplay ("the door slides open when a character walks in"), not nodes. No explanation of your process.
If `{name}.agent.json` exists with the same fingerprint as `{name}.explain.json`, it is still correct: do not rewrite it.
"""


def esc(text: str) -> str:
    return html.escape(str(text))


def html_steps(steps: list[dict]) -> str:
    out = ["<ul class='steps'>"]
    for s in steps:
        cls = esc(s.get("kind", ""))
        detail = f"<code class='pc'>{esc(s['detail'])}</code>" if s.get("detail") and s.get("detail") != s.get("text") else ""
        cpp = ""
        if s.get("cpp"):
            c = s["cpp"]
            more = f"\n// … {c['more']}줄 더" if c.get("more") else ""
            cpp = (f"<details class='cpp'><summary>C++ {esc(c['class'])}::{esc(c['function'])} — {esc(c['file'])}:{c['line']}</summary>"
                   f"<pre>{esc(chr(10).join(c['code']) + more)}</pre></details>")
        if s.get("children"):
            out.append(f"<li class='{cls}'><details{' open' if s.get('kind') in ('if', 'else', 'on', 'loop') else ''}>"
                       f"<summary>{esc(s['text'])} {detail}</summary>{cpp}{html_steps(s['children'])}</details></li>")
        else:
            out.append(f"<li class='{cls}'>{esc(s['text'])} {detail}{cpp}</li>")
    out.append("</ul>")
    return "".join(out)


CSS = """
:root{--bg:#fff;--fg:#1c1d20;--dim:#6b7078;--line:#e3e5e8;--card:#f6f7f9;--acc:#3574f0;--warn:#b87a1e}
@media (prefers-color-scheme:dark){:root{--bg:#1e1f22;--fg:#dfe1e5;--dim:#8c919a;--line:#3c3f41;--card:#2b2d30;--acc:#5c8ff5;--warn:#e09b3a}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 'Segoe UI','Malgun Gothic',sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.sub{color:var(--dim)}.summary{background:var(--card);border-radius:8px;padding:12px 14px;margin:12px 0}
.agent{border-left:3px solid var(--acc);padding-left:10px;margin:8px 0}.tag{color:var(--dim);font-size:12px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
.beh{background:var(--card);border-radius:8px;padding:10px 14px;margin:10px 0}.beh>summary{font-weight:600;cursor:pointer}
.steps{list-style:none;padding-left:18px;margin:4px 0}.steps li{margin:2px 0}.steps summary{cursor:pointer}
.pc{display:none;color:var(--dim);font-size:12px;margin-left:6px}body.logic .pc{display:inline}
li.note{color:var(--warn)}li.if>details>summary,li.else>details>summary{color:var(--acc)}
li.own>details>summary::after,li.other>details>summary::after{content:' · 아래에 펼침';color:var(--dim);font-size:12px}
pre{background:var(--card);padding:8px;border-radius:6px;overflow:auto;font-size:12px}
a{color:var(--acc)}label{user-select:none}
"""


def agent_current(doc: dict, agent: dict | None) -> bool:
    """An agent's explanation fits this version of the Blueprint (same fingerprint as the rule-based one)."""
    return bool(agent) and agent.get("fingerprint") == doc.get("fingerprint")


def render_html(doc: dict, agent: dict | None) -> str:
    c = doc["composition"]
    rows = lambda items, cols: "".join("<tr>" + "".join(f"<td>{esc(i.get(k, '')) if not isinstance(i.get(k), list) else esc(', '.join(i.get(k)))}</td>" for k in cols) + "</tr>" for i in items)
    parts = [f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>{esc(doc['name'])}</title><style>{CSS}</style></head><body><main>",
             f"<p class='sub'><a href='{'../' * (doc['package'].count('/') - 1)}index.html'>추적 중인 파일</a> · {esc(doc['package'])}</p>",
             f"<h1>{esc(doc['name'])}</h1><div class='sub'>{esc(doc['kind'])} · {esc(' < '.join(doc['parentChain']) or doc['parent'])}</div>",
             f"<div class='summary'>{esc(doc['summary'])}"]
    if agent and not agent_current(doc, agent):
        parts.append("<p class='sub'>에이전트 설명이 이 블루프린트의 이전 버전에 대한 것이라 표시하지 않습니다. "
                     f"{esc(doc.get('name'))}.explain-prompt.md로 다시 쓰게 하세요.</p>")
        agent = None
    if agent and agent.get("summary"):
        parts.append(f"<div class='agent'><span class='tag'>에이전트 설명</span><br>{esc(agent['summary'])}</div>")
    parts.append("</div>")
    if agent and agent.get("notes"):
        parts.append("<ul>" + "".join(f"<li>{esc(n)}</li>" for n in agent["notes"]) + "</ul>")
    parts.append("<h2>구성</h2>")
    if c["components"]:
        parts.append("<h3>컴포넌트</h3><table><tr><th>이름</th><th>역할</th><th>클래스</th><th>붙은 곳</th><th>바꾼 값</th></tr>" +
                     rows(c["components"], ["name", "role", "class", "parent", "changed"]) + "</table>")
    if c["settings"]:
        parts.append("<h3>설정값 (에디터에서 바꾸는 값)</h3><table><tr><th>이름</th><th>타입</th><th>기본값</th><th>바꾸는 행동</th><th>설명</th></tr>" +
                     rows(c["settings"], ["name", "type", "default", "changedBy", "tooltip"]) + "</table>")
    if c["state"]:
        parts.append("<h3>상태 (실행 중 바뀌는 값)</h3><table><tr><th>이름</th><th>타입</th><th>기본값</th><th>바꾸는 행동</th></tr>" +
                     rows(c["state"], ["name", "type", "default", "changedBy"]) + "</table>")
    for key, label in (("signals", "밖으로 보내는 신호"), ("timelines", "타임라인"), ("interfaces", "구현한 인터페이스"),
                       ("defaults", "바꾼 기본값"), ("widgets", "위젯")):
        if c.get(key):
            parts.append(f"<p><b>{label}</b>: {esc(', '.join(c[key]))}</p>")
    parts.append("<h2>행동과 흐름</h2><label><input type='checkbox' onchange=\"document.body.classList.toggle('logic',this.checked)\"> "
                 "의사코드 함께 보기</label>")
    for b in doc["behaviors"]:
        agent_text = (agent or {}).get("behaviors", {}).get(b["id"])
        meta = []
        if b["writes"]:
            meta.append("바꾸는 값: " + ", ".join(b["writes"]))
        if b["calls"]:
            meta.append("이어서 실행: " + ", ".join(b["calls"]))
        if b["calledBy"]:
            meta.append("부르는 곳: " + ", ".join(b["calledBy"]))
        parts.append(f"<details class='beh' open><summary>{esc(b['title'])}</summary><div class='sub'>{esc(b['trigger'])}</div>"
                     + "".join(f"<div><i>{esc(n)}</i></div>" for n in b["notes"])
                     + (f"<div class='agent'><span class='tag'>에이전트 설명</span><br>{esc(agent_text)}</div>" if agent_text else "")
                     + (f"<div class='tag'>{esc(' · '.join(meta))}</div>" if meta else "")
                     + "".join(f"<details class='cpp'><summary>이 행동을 부르는 C++ {esc(o['class'])}::{esc(o['function'])} — {esc(o['file'])}:{o['line']}</summary>"
                               f"<pre>{esc(chr(10).join(o['code']))}</pre></details>" for o in b.get("origins", []))
                     + html_steps(b["steps"]) + "</details>")
    parts.append(f"<p class='sub'>규칙 기반 설명 (dwookit). 전체 의사코드: {esc(doc['name'])}.md · 전체 스크립트: {esc(doc['name'])}.json</p></main></body></html>")
    return "".join(parts)


def write_all(root: Path, project: Path | None) -> int:
    manifest = lens_bp.load(root / "export.json") or {}
    entries = manifest.get("exported", [])
    bps = {}
    for e in entries:
        bp = lens_bp.load(root / e["dir"] / f"{e['name']}.json")
        if bp:
            bps[e["name"]] = (e, bp)
    cpp = CppIndex(project)
    others: dict[str, Explainer] = {}
    for name, (e, bp) in bps.items():
        parts, _ = lens_bp.graph_parts(bp)
        others[name] = Explainer(bp, parts, others, cpp)
    rows = []
    for name, (e, bp) in bps.items():
        folder = root / e["dir"]
        ex = others[name]
        behaviors = ex.behaviors()
        comp = ex.composition(behaviors)
        doc = fix_particles({"schema": SCHEMA, "name": name, "package": bp.get("package"), "assetClass": bp.get("assetClass"),
               "parent": lens_bp.short(bp.get("parent")), "parentChain": [lens_bp.short(c) for c in bp.get("parentChain", [])],
            "fingerprint": lens_bp.fingerprint(bp),
               "kind": kind_words(bp, cpp), "summary": ex.summary(comp, behaviors), "composition": comp, "behaviors": behaviors})
        agent = lens_bp.load(folder / f"{name}.agent.json")
        (folder / f"{name}.explain.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        (folder / f"{name}.html").write_text(render_html(doc, agent), encoding="utf-8")
        (folder / f"{name}.explain-prompt.md").write_text(PROMPT.format(name=name), encoding="utf-8")
        rows.append((e["dir"], name, doc))
    index = [f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>추적 중인 파일</title><style>{CSS}</style></head><body><main><h1>추적 중인 파일</h1>"
             f"<p class='sub'>추적 중인 블루프린트 {len(rows)}개. 파일마다 구성, 행동, 흐름 순서입니다. 내보낸 시각 {esc(manifest.get('exportedAtUtc', ''))}</p><table>"
             "<tr><th>파일</th><th>무엇</th><th>요약</th></tr>"]
    for d, name, doc in sorted(rows):
        index.append(f"<tr><td><a href='{esc(d)}/{esc(name)}.html'>{esc(name)}</a></td><td>{esc(doc['kind'])}</td><td>{esc(doc['summary'])}</td></tr>")
    index.append("</table></main></body></html>")
    (root / "index.html").write_text("".join(index), encoding="utf-8")
    print(f"lens_explain: {len(rows)} file(s) -> {root / 'index.html'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True, type=Path)
    ap.add_argument("--project", type=Path)
    args = ap.parse_args(argv)
    return write_all(args.dir, args.project)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
