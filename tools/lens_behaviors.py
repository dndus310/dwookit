"""DwoLens behaviors: what a class does, in the words of the game ("Jump", "Move", "Interact"), above its entry points.

A feature in features.json is one entry point: an input binding, an event, an override, a timer. Several of them
usually serve one behavior: the Jump input pressed and released, a touch button, the BlueprintCallable DoJumpStart
and the timer that resets a wall jump are all "Jump". This module groups the entry points of a class family into
behaviors, without an LLM and without the network:

    1. Each entry point gets a stem: the meaningful words of the function it hands work to (DoJumpStart -> jump),
       or of its own name (Touch Interact Start -> interact). Words like Do, On, Start, Pressed, Touch are dropped.
    2. Entry points with the same stem form a behavior. A stem that only adds words to another behavior's stem
       (reset wall jump, multi jump) joins it.
    3. Engine callbacks (lifecycle, timers, events) that change a state the behavior uses join it too (Landed
       resets bHasDoubleJumped, which Jump reads). What is left forms "Lifecycle" and its own behaviors.

For each behavior the output lists its entry points, the functions doing the work, the cases of its main function
(branch conditions and what each runs), the states it reads and writes and the classes it reaches.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

STOP = {
    "do", "on", "handle", "handler", "touch", "input", "ia", "start", "started", "end", "ended", "stop", "stopped", "begin",
    "pressed", "released", "triggered", "completed", "ongoing", "canceled", "cancelled", "event", "action", "k", "receive",
    "bp", "server", "client", "multicast", "implementation", "rep", "the", "a", "an", "of", "to", "and", "or", "for", "with",
    "this", "notify", "internal", "impl", "update", "tick", "callback", "delegate", "func", "function", "native", "timer",
    "이벤트", "타이머", "set", "get", "is", "has",
    # event suffixes: OnClicked, OnValueChanged, OnFilesDropped say when, not what
    "clicked", "hovered", "unhovered", "changed", "committed", "dropped", "requested", "finished", "begin", "initialized",
}
# verbs come last in Korean: Toggle Camera -> 카메라 전환, Open Door -> 문 열기
VERBS = {"toggle", "open", "close", "take", "reset", "refresh", "select", "show", "hide", "use", "equip", "load", "save",
         "spawn", "pick", "apply", "check", "notify", "play", "stop", "set", "add", "remove", "delete", "import", "export",
         "build", "update", "push", "drop", "fire", "reload", "start"}
# The engine and the template name these; a behavior is never called "component" or "actor".
WEAK = {"component", "actor", "pawn", "character", "controller", "player", "game", "mode", "widget", "ui", "object", "c"}
KO = {
    "jump": "점프", "move": "이동", "movement": "이동", "look": "시점 회전", "turn": "회전", "attack": "공격", "combo": "콤보",
    "charged": "차지", "charge": "차지", "interact": "상호작용", "interaction": "상호작용", "drop": "내려가기", "fire": "발사",
    "shoot": "사격", "reload": "재장전", "crouch": "앉기", "sprint": "달리기", "run": "달리기", "walk": "걷기", "dash": "대시",
    "aim": "조준", "toggle": "전환", "camera": "카메라", "damage": "피해", "dealt": "가한", "received": "받은", "heal": "회복",
    "die": "사망", "death": "사망", "dead": "사망", "respawn": "부활", "spawn": "생성", "spawner": "생성기", "pickup": "줍기",
    "pick": "줍기", "open": "열기", "close": "닫기", "door": "문", "wall": "벽", "climb": "오르기", "swim": "수영", "fly": "비행",
    "brake": "브레이크", "forward": "전진", "backward": "후진", "reverse": "후진", "steer": "조향", "throttle": "가속",
    "accelerate": "가속", "boost": "부스트", "use": "사용", "equip": "장착", "inventory": "인벤토리", "menu": "메뉴",
    "pause": "일시정지", "score": "점수", "health": "체력", "hp": "체력", "life": "체력", "bar": "막대", "mouse": "마우스",
    "overlap": "겹침", "hit": "충돌", "landed": "착지", "land": "착지", "fall": "낙하", "falling": "낙하", "possess": "빙의",
    "possessed": "빙의", "load": "불러오기", "save": "저장", "asset": "에셋", "mesh": "메시", "list": "목록", "item": "항목",
    "select": "선택", "selection": "선택", "click": "클릭", "clicked": "클릭", "hover": "호버", "hovered": "호버",
    "thumbnail": "썸네일", "search": "검색", "filter": "필터", "refresh": "새로 고침", "download": "내려받기",
    "upload": "올리기", "layer": "레이어", "panel": "패널", "screen": "화면", "show": "표시", "hide": "숨기기",
    "enemy": "적", "target": "대상", "lava": "용암", "checkpoint": "체크포인트", "activation": "활성화", "volume": "볼륨",
    "platform": "발판", "soft": "통과", "collision": "충돌", "multi": "다단", "double": "이중", "reset": "초기화",
    "primary": "주", "secondary": "보조", "thumbstick": "스틱", "stick": "스틱", "notify": "알림", "incoming": "다가오는",
    "lifecycle": "생명주기", "take": "받기", "apply": "적용", "add": "추가", "remove": "제거", "delete": "삭제",
    "import": "가져오기", "export": "내보내기", "build": "만들기", "building": "건물", "tab": "탭", "check": "확인",
    "status": "상태", "sync": "동기화", "catalog": "목록", "files": "파일", "file": "파일", "attribute": "속성", "ragdoll": "래그돌", "push": "밀기", "impulse": "충격", "trace": "추적", "projectile": "발사체",
}
LIFECYCLE_CATS = {"lifecycle"}
ENGINE_CATS = {"lifecycle", "timer", "event", "network", "collision"}


def words(name: str) -> list[str]:
    """CamelCase and separators to lower-case words: DoJumpStart -> do jump start, IA_Move -> ia move."""
    name = name.split("->")[-1].split(".")[-1].split("::")[-1]
    name = re.sub(r"\(.*", "", name)
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+|[가-힣]+", name)
    return [p.lower() for p in parts]


def stem(name: str) -> tuple[str, ...]:
    w = [x for x in words(name) if x not in STOP and not x.isdigit() and len(x) > 1]
    strong = [x for x in w if x not in WEAK]
    return tuple(strong or w)


def display(st: tuple[str, ...]) -> tuple[str, str]:
    en = " ".join(x.capitalize() for x in st) or "Behavior"
    order = list(st[1:]) + [st[0]] if len(st) > 1 and st[0] in VERBS else list(st)
    ko = " ".join(KO.get(x, x.capitalize()) for x in order) or en
    return ko, en


def calls_of(steps: list[dict], depth: int = 0, top_only: bool = False):
    """(step, depth) for every call in the flow, branches included."""
    for s in steps:
        if s.get("kind") == "call":
            yield s, depth
        for b in s.get("branches", []):
            yield from calls_of(b["steps"], depth, top_only)
        if not top_only:
            yield from calls_of(s.get("inner", []), depth + 1, top_only)


def call_name(step: dict) -> str:
    return step["text"].split(".")[-1].strip()


def handlers(feature: dict, own_names: set[str] = frozenset()) -> list[str]:
    """The project functions an entry point hands its work to: top-level calls that expand into a body, or that
    name one of the family's own events and functions (a Blueprint custom event)."""
    out = []
    for s, _ in calls_of(feature["steps"], top_only=True):
        if s.get("inner") or s.get("target") or call_name(s) in own_names:
            n = call_name(s)
            if n not in out and stem(n):
                out.append(n)
    return out


# member-like names in a condition (bFlags, ...Value/Count/Time... names), not calls (Get...()) and not fields of
# locals (OutHit.bBlockingHit)
IDENT = re.compile(r"(?<![.>\w])(b[A-Z]\w*|[A-Z][A-Za-z0-9]*(?:Value|Count|Time|HP|Health|State|Mode|Index|Speed|Position|Rotation|Transform)\w*)\b(?!\s*\()")


def states(steps: list[dict], out_set: set, out_read: set):
    for s in steps:
        if s.get("kind") == "set":
            out_set.add(s["text"].replace("설정 ", "").replace("값 ", "").strip())
        if s.get("kind") in ("branch", "loop"):
            out_read.update(x for x in IDENT.findall(s.get("text", "")) if not x.startswith(("Get", "Is", "Has")))
        for b in s.get("branches", []):
            states(b["steps"], out_set, out_read)
        states(s.get("inner", []), out_set, out_read)


def reaches(steps: list[dict], out: set):
    for s in steps:
        if s.get("target"):
            out.add(s["target"])
        for b in s.get("branches", []):
            reaches(b["steps"], out)
        reaches(s.get("inner", []), out)


def find_body(features: list[dict], name: str) -> list[dict] | None:
    """The expanded body of a function, from any entry point that calls it."""
    for f in features:
        for s, _ in calls_of(f["steps"]):
            if call_name(s) == name and s.get("inner"):
                return s["inner"]
    return None


def size(steps: list[dict]) -> int:
    return sum(1 + sum(size(b["steps"]) for b in s.get("branches", [])) + size(s.get("inner", [])) for s in steps)


def core(features: list[dict], names: list[str]) -> tuple[str, list[dict] | None]:
    """The function that holds a behavior's logic: the largest body among its handlers, followed down through
    pass-through calls (Jump -> DoJumpStart -> MultiJump)."""
    best, body = "", None
    for h in names:
        b = find_body(features, h)
        if b and (body is None or size(b) > size(body)):
            best, body = h, b
    seen = {best}
    while body:
        calls = [s for s in body if s.get("kind") == "call" and s.get("inner")]
        plain = [s for s in body if not (s.get("kind") == "call" and s.get("inner")) and s.get("kind") not in ("set",)]
        if len(calls) == 1 and not plain and not any(s.get("branches") for s in body) and call_name(calls[0]) not in seen:
            best, body = call_name(calls[0]), calls[0]["inner"]
            seen.add(best)
        else:
            break
    return best, body


def flat_actions(steps: list[dict], limit: int = 6) -> list[str]:
    out = []
    for s in steps:
        if len(out) >= limit:
            break
        if s.get("kind") in ("branch", "loop", "sequence"):
            out.append(s["text"] if s["text"] not in ("분기", "순서대로") else "(분기)")
        else:
            out.append(s["text"] + (f" ×{s['repeat']}" if s.get("repeat", 1) > 1 else ""))
    return out


def cases_of(body: list[dict], limit: int = 8) -> list[dict]:
    """The cases of a function body: each top-level branch (condition -> what it runs), and plain runs in between."""
    out, plain = [], []
    for s in body:
        if s.get("branches") and s.get("kind") in ("branch", "loop", "sequence", "cast", "macro"):
            if plain:
                out.append({"when": "", "does": flat_actions(plain)})
                plain = []
            cond = s["text"].replace("조건: ", "")
            for b in s["branches"]:
                if not b["steps"]:
                    continue
                label = b["label"]
                when = cond if label in ("참", "다음", "") else f"{label}: {cond}" if s["kind"] != "sequence" else f"{label}단계"
                out.append({"when": when, "does": flat_actions(b["steps"])})
        else:
            plain.append(s)
    if plain:
        out.append({"when": "", "does": flat_actions(plain)})
    return out[:limit]


def family(classes: dict[str, dict], key: str) -> list[str]:
    chain, k = [], key
    while k and k in classes and k not in chain and len(chain) < 16:
        chain.append(k)
        k = classes[k].get("parent", "")
    chain += [c for c, v in classes.items() if v.get("parent") == key and c not in chain]
    return chain


def infer(classes: dict[str, dict]) -> None:
    """Adds 'behaviors' to every class (over its family: project ancestors and direct children)."""
    for key, cls in classes.items():
        members = []          # (feature, owner class key)
        for k in family(classes, key):
            members += [(f, k) for f in classes[k]["features"]]
        if not members:
            continue
        info = {}
        own_names = {re.sub(r"\s*이벤트$", "", f["name"]) for f, _ in members}
        for f, k in members:
            hs = handlers(f, own_names)
            own = stem(re.sub(r"\s*이벤트$", "", f["name"]))
            key_stem = own
            for h in hs:
                hst = stem(h)
                if set(hst) & set(own) or not own:
                    key_stem = hst   # the function doing the work names the behavior
                    break
            else:
                # an engine event (overlap, hit, click, timer) is named by what it calls; an input action by its name
                if hs and (f["category"] != "input" or not own):
                    key_stem = stem(hs[0])
            s_set, s_read = set(), set()
            states(f["steps"], s_set, s_read)
            info[f["id"]] = {"stem": key_stem, "handlers": hs, "set": s_set, "read": s_read}

        groups: dict[tuple, list] = defaultdict(list)
        lifecycle, engine_left = [], []
        for f, k in members:
            st = info[f["id"]]["stem"]
            if f["category"] in LIFECYCLE_CATS:
                lifecycle.append(f)
            elif not st:
                engine_left.append(f)
            else:
                groups[st].append(f)

        def has_trigger(fs):
            return any(x["category"] in ("input", "ui", "interface", "collision") for x in fs)

        # longer stems join a shorter one they contain (reset wall jump -> jump), when that one is player-facing
        for st in sorted(groups, key=len, reverse=True):
            if len(st) < 2 or st not in groups:
                continue
            for other in sorted(groups, key=len):
                if other != st and set(other) < set(st) and has_trigger(groups[other]):
                    groups[other] += groups.pop(st)
                    break

        # engine callbacks join the behavior whose state they change
        def group_states(fs):
            s, r = set(), set()
            for x in fs:
                s |= info[x["id"]]["set"]
                r |= info[x["id"]]["read"]
            return s | r

        for pool in (lifecycle, engine_left):
            for f in list(pool):
                touched = info[f["id"]]["set"]
                hits = [st for st, fs in groups.items() if has_trigger(fs) and touched & group_states(fs)]
                if len(hits) == 1:
                    groups[hits[0]].append(f)
                    pool.remove(f)
        for st in list(groups):
            if not has_trigger(groups[st]) and all(x["category"] in ENGINE_CATS for x in groups[st]):
                touched = set().union(*(info[x["id"]]["set"] for x in groups[st]))
                hits = [o for o, fs in groups.items() if o != st and has_trigger(fs) and touched and touched & group_states(fs)]
                if len(hits) == 1:
                    groups[hits[0]] += groups.pop(st)
        if lifecycle or engine_left:
            groups[("lifecycle",)] += lifecycle + engine_left

        behaviors = []
        for st, fs in groups.items():
            hcount = Counter(h for x in fs for h in info[x["id"]]["handlers"])
            primary, body = core(fs, [h for h, _ in hcount.most_common()])
            if body is None and fs and st != ("lifecycle",):
                body = max(fs, key=lambda x: size(x["steps"]))["steps"]
            s_set, s_read, targets = set(), set(), set()
            for x in fs:
                s_set |= info[x["id"]]["set"]
                s_read |= info[x["id"]]["read"]
                reaches(x["steps"], targets)
            targets.discard(key)
            ko, en = display(st)
            if st == ("lifecycle",):
                ko, en = "생명주기 (준비·정리)", "Lifecycle"
            kinds = Counter(x["category"] for x in fs)
            behaviors.append({
                "name": ko, "nameEn": en, "stem": list(st),
                "features": [x["id"] for x in fs],
                "triggerHandlers": {x["id"]: info[x["id"]]["handlers"] for x in fs},
                "handlers": [h for h, _ in hcount.most_common()],
                "primary": primary,
                "cases": cases_of(body) if body else [],
                "states": sorted(s_set | s_read),
                "writes": sorted(s_set),
                "reaches": sorted(targets),
                "player": has_trigger(fs),
                "kinds": dict(kinds),
            })
        behaviors.sort(key=lambda b: (b["stem"] == ["lifecycle"], not b["player"], -len(b["features"]), b["name"]))
        for i, b in enumerate(behaviors):
            b["id"] = f"{key}@{i}"
        cls["behaviors"] = behaviors
