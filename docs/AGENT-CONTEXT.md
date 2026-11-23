# 에이전트 컨텍스트 (설계)

상태: 설계 초안 (2026-11-22). 일부 구현 (2026-11-24): 블루프린트 추적(`bp`), 큰 블루프린트 부분 분리, `context/AGENT.md`, 안내 파일(CLAUDE.local.md, Copilot 지침 두 개: dwo-Forge 안내 + 사용자 지침 빈 파일), `agent` 명령, 사람이 읽는 파일 분석(`lens_explain.py`, 창의 DwoLens › 파일 분석, HTML). 미구현: overview/features/problems 묶음, `ctx` 조회 명령, MCP.

## 1. 목적

Claude Code, GitHub Copilot 같은 코딩 에이전트가 dwo-Forge에 연결된 언리얼 프로젝트에서 작업할 때, **DwoLens가 이미 분석해 둔 정보를 알고 시작**하게 만듭니다. 사용자는 요구사항만 말하고, 에이전트는 다음 순서로 일합니다.

1. 프로젝트 개요(모듈, 주요 클래스, BP, 기능, 현재 문제)를 먼저 읽는다.
2. 요구사항과 관련된 클래스·BP·에셋을 조회 명령으로 좁힌다.
3. 바꾼다.
4. dwo-Forge 검사(compile, verify, analyze)로 확인한다.

### 지금 안 되는 이유

| 문제 | 현재 상태 |
|---|---|
| 에이전트가 데이터의 존재를 모름 | `CLAUDE.md`, `AGENTS.md`, Copilot 지시 파일 어디에도 안내가 없음 |
| 검색에서 빠짐 | `__dwo-forge/`가 `.git/info/exclude`에 있어 ripgrep 기반 검색(Claude Code Grep/Glob)과 Copilot 인덱싱이 건너뜀 |
| 최신 실행을 모름 | `data/runs/<id>/`가 쌓이기만 하고 최신을 가리키는 파일이 없음 |
| 너무 큼 | `graph.json`은 큰 프로젝트에서 100MB 이상. 에이전트가 통째로 읽을 수 없음 |
| 조각 조회 수단 없음 | `index.db`(SQLite)는 있지만 스키마 안내도, 조회 명령도 없음 |

## 2. 원칙

- **프로젝트의 git 상태를 바꾸지 않는다.** dwo-Forge의 기존 약속(연결 전과 제거 후 `git status`가 같음)을 지킵니다. 새로 만드는 파일은 모두 exclude 블록에 넣고, 사용자가 커밋해 둔 파일은 고치지 않습니다.
- **에이전트는 요약을 먼저 읽고, 세부는 명령으로 가져온다.** 항상 읽히는 파일은 작게(목표 8천 토큰 이하) 유지하고, 나머지는 `ctx` 명령이 필요한 만큼만 돌려줍니다.
- **분석이 끝날 때마다 자동으로 갱신한다.** 사용자가 따로 실행할 명령은 없습니다.
- **오래된 정보임을 숨기지 않는다.** 분석 시점, 그때의 git HEAD, 이후 바뀐 파일 수를 함께 보여 주고, 오래되었으면 `analyze`를 다시 하라고 안내합니다.
- **표준 라이브러리만 쓰고 네트워크·LLM을 쓰지 않는다.** 기존 `lens_*.py`와 같습니다.

## 3. 구성

```
<Project>/
├─ CLAUDE.local.md                         # (Claude Code) 관리 블록 하나: @__dwo-forge/context/AGENT.md
├─ .github/instructions/dwo-forge.instructions.md   # (Copilot, VS Code) applyTo "**", 같은 안내
├─ AGENTS.md                               # (선택) 없을 때만 만듦. Codex·Cursor·Copilot 에이전트용
└─ __dwo-forge/
   ├─ context/                             # 매 실행 뒤 다시 씀 (사람이 고치지 않음)
   │  ├─ AGENT.md                          # 진입점: 사용법, 작업 순서, 명령, 신선도
   │  ├─ overview.md                       # 모듈·플러그인·주요 클래스·BP 폴더·진입 클래스
   │  ├─ features.md                       # 클래스별 기능(점프, 상호작용 ...) 요약
   │  ├─ problems.md                       # 현재 문제: analyze·verify·logs·compile, 새 문제 표시
   │  └─ latest.json                       # 검사 종류별 최신 runId, 분석 시점 git HEAD
   └─ data/runs/<id>/...                   # 기존 그대로
```

### 3.1 컨텍스트 묶음 (`__dwo-forge/context/`)

새 모듈 `tools/lens_context.py`가 만듭니다. `Complete-RunOutputs`([forge.ps1](../tools/forge.ps1)) 끝에서 호출하므로 analyze뿐 아니라 verify, logs, compile, package가 끝날 때도 `problems.md`와 `latest.json`이 갱신됩니다. 구조 정보(`overview.md`, `features.md`)는 마지막 `architecture-report` 실행에서 가져옵니다.

| 파일 | 내용 | 크기 상한 |
|---|---|---|
| `AGENT.md` | 이 묶음이 무엇인지, 작업 순서(1절), `ctx` 명령 목록과 예, 신선도 줄, 에이전트가 **할 수 없는 일**(아래) | 약 1.5k 토큰 |
| `overview.md` | 엔진 버전, 모듈과 의존성, 프로젝트 플러그인, 게임 진입 클래스(GameMode, PlayerController, Character, GameInstance와 그 BP 자식), 연결 수 상위 클래스·BP 30개, `/Game` 폴더별 BP·위젯 BP 수 | 약 4k 토큰 |
| `features.md` | `features.json`과 behaviors에서 클래스 계열별 기능 이름, 시작점, 핵심 함수 한 줄씩. 프로젝트 클래스만. 넘치면 연결 수 순으로 자르고 "`ctx class <이름>`으로 더 보기" | 약 3k 토큰 |
| `problems.md` | 검사별 결과(통과·경고·실패), 실패·경고 항목 상위 20개, 기준선 대비 **새 문제** 표시, 무시한 문제는 뺌 | 약 1.5k 토큰 |
| `latest.json` | `{ "architecture-report": "run-...", "verify": "run-...", ..., "analyzedHead": "<sha>", "analyzedAt": "..." }` | - |

에이전트가 할 수 없는 일 (`AGENT.md`에 명시):
- `.uasset`/`.umap` 직접 편집. BP 변경은 사람이 에디터에서 하도록 단계별 안내를 쓰거나, C++ 쪽(BlueprintCallable 함수, 부모 클래스)으로 구현합니다.
- 에디터가 열려 있을 때 commandlet 실행(검사 명령은 에디터를 닫고 실행).

### 3.2 조회 명령 (`__dwo-forge.bat ctx ...`)

같은 `lens_context.py`의 하위 명령입니다. 결과는 마크다운, 기본 상한 200줄, `--limit`으로 조절합니다. 모두 읽기 전용입니다.

| 명령 | 돌려주는 것 | 출처 |
|---|---|---|
| `ctx status` | 최신 실행들, 분석 뒤 바뀐 `Source/`·`Content/` 파일 수, 다시 분석이 필요한지 | `latest.json`, 파일 시각, git |
| `ctx find <텍스트>` | 이름이 맞는 클래스·BP·에셋·함수·기능 (종류, 키, 한 줄 설명) | `index.db` symbol, `features.json` |
| `ctx class <이름>` | 부모·자식, 인터페이스, 속성, 함수, 기능, 부르는 곳·불리는 곳, 참조하는 에셋 | `index.db`, `features.json` |
| `ctx bp <에셋>` | 추적 중인 BP의 의사코드(`__dwo-forge/blueprints/.../<Name>.md`, 구현됨: `bp add` / `bp export`). 추적하지 않은 BP는 `raw/blueprints.json`의 노드·링크 요약과 함께 `bp add`를 안내 | `blueprints/`, `raw/blueprints.json` |
| `ctx refs <키>` | 들어오는·나가는 관계(종류별: Calls, Inherits, RefHard, AssetHardRef ...) | `index.db` relation |
| `ctx feature <이름>` | 기능의 시작점, 단계, 경우(조건 → 하는 일), 상태 읽기·쓰기 | `features.json` |
| `ctx problems [--new]` | 현재 문제 전체 또는 새 문제만 | `findings.json`, `quality.json`, `verify.json`, `logs.json` |
| `ctx sql "<SELECT ...>"` | 읽기 전용 SQL (`SELECT`/`WITH`만, `mode=ro`로 열기) | `index.db` |

`ctx bp` 의사코드 예 (exec 링크를 따라 일렬로, 데이터 핀은 인자로 펼침):

```
WBP_Item (WidgetBlueprint, parent UserWidget)
function SetTitle(InText)
  EditableText_154.SetText(InText)
event Construct
  ...
```

`AGENT.md`는 `index.db` 스키마(`symbol(symbol_key, kind, name, is_external, attrs)`, `relation(source_key, target_key, relation_type, status, confidence, ...)`)와 키 형식(`uclass:/Script/<모듈>.<클래스>`, `bp:/Game/...`, `asset:/Game/...`, `module:...`), 관계 종류 목록을 짧게 담아 `ctx sql`을 바로 쓸 수 있게 합니다.

### 3.3 에이전트 안내 파일 (유도)

연결(`connect`)과 `update` 때 쓰고, `uninstall` 때 지웁니다. 대상은 설정 `agents.targets`(기본 `["claude", "copilot"]`)로 고릅니다.

| 대상 | 파일 | 동작 | 이유 |
|---|---|---|---|
| Claude Code | `CLAUDE.local.md` | 없으면 만들고, 있으면 관리 블록(`<!-- >>> dwo-forge managed >>> -->` ... `<!-- <<< dwo-forge managed <<< -->`)만 추가·교체. 블록 내용은 한 줄 소개와 `@__dwo-forge/context/AGENT.md` import | 개인용으로 자동 로드되는 파일이라 팀 저장소에 영향이 없음. 커밋된 `CLAUDE.md`는 건드리지 않음 |
| Copilot (VS Code) | `.github/instructions/dwo-forge.instructions.md` | 새 파일(`applyTo: "**"`). 같은 이름이 있고 우리 것이 아니면 건너뛰고 알림 | 기존 `.github/copilot-instructions.md`를 고치지 않고 추가 지시만 얹음 |
| 그 밖의 에이전트 | `AGENTS.md` | `agents.targets`에 `agents-md`가 있고 **파일이 없을 때만** 만듦 | 커밋된 `AGENTS.md`는 팀 문서라 고치지 않음 |

- 새로 만든 파일은 `$ManagedPaths`(exclude 블록)와 `created-files.txt`에 기록합니다. `CLAUDE.local.md`에 블록만 추가한 경우는 uninstall이 블록만 지우고, 파일이 비면 파일도 지웁니다.
- 안내 파일 내용은 짧게 유지합니다(경로, 작업 순서, `ctx` 명령 세 줄). 세부는 `AGENT.md`에 둡니다.
- Copilot 안내 파일에는 import 문법이 없으므로 "작업 전에 `__dwo-forge/context/AGENT.md`를 읽어라"를 문장으로 씁니다. 파일 도구로 읽는 것은 exclude와 상관없이 됩니다(검색에서만 빠짐).

**한계:** GitHub에서 도는 클라우드 에이전트(Copilot coding agent 등)는 로컬에서만 제외된 파일을 볼 수 없습니다. 이 설계는 로컬에서 실행하는 에이전트(Claude Code CLI·데스크톱, VS Code Copilot Chat·agent 모드)를 대상으로 합니다.

### 3.4 (3단계) MCP 서버

같은 조회를 MCP 도구(`forge_status`, `forge_find`, `forge_class`, `forge_bp`, `forge_refs`, `forge_problems`, `forge_sql`)로 내보내는 stdio 서버 `__dwo-forge.bat mcp`입니다. 검사 실행 도구(`forge_verify`, `forge_compile`)는 오래 걸리고 에디터 상태에 영향을 받으므로 별도 단계에서 판단합니다.

등록은 프로젝트 파일을 건드리지 않는 방법만 씁니다. Claude Code는 `claude mcp add --scope local`(사용자 설정에 저장)을 쓰고, VS Code는 등록 명령을 안내만 합니다. 1·2단계로 쓸모가 확인된 뒤 진행합니다.

## 4. 단계

| 단계 | 범위 | 끝난 상태 |
|---|---|---|
| 1 | `lens_context.py`: 컨텍스트 묶음 생성 + `ctx` 하위 명령, `Complete-RunOutputs` 연결, `forge.ps1`에 `ctx` 명령 | analyze 뒤 `context/`가 생기고, `ctx find/class/bp/refs/problems/status/sql`이 동작 |
| 2 | 안내 파일 쓰기·지우기(connect, update, uninstall, doctor), `agents.targets` 설정, README 절 추가 | 연결만 하면 Claude Code·Copilot이 첫 대화에서 `AGENT.md`를 읽음. 제거 뒤 `git status`가 연결 전과 같음 |
| 3 | MCP 서버 | Claude Code·VS Code에서 도구로 조회 |
| (선택) | GUI 도구 화면에 "에이전트 컨텍스트" 상태(최신 여부, 대상별 설치 상태) | - |

## 5. 검증

- `tests/test_tools.py`: 고정 데이터(`tests/fixtures/raw_temp_proj`)로 컨텍스트 묶음 생성, 파일별 크기 상한, `ctx` 출력 형식, `ctx sql`이 쓰기 문을 거부하는지.
- `tests/stress.py`: 큰 그래프(심볼 3만, 관계 40만 이상)에서 묶음 생성 시간과 크기 상한.
- `tests/selftest.ps1`: connect → analyze → `context/AGENT.md` 존재 → 안내 파일 존재 → uninstall → `git status` 연결 전과 같음. 기존 `CLAUDE.local.md`가 있는 경우 블록만 지워지는지.
- 수동: 실제 프로젝트에서 Claude Code와 VS Code Copilot에 "점프를 두 번까지 되게 해줘" 같은 요구사항을 주고, 첫 응답 전에 `AGENT.md`와 `ctx`를 쓰는지 확인.

## 6. 정해야 할 것

1. **기본 대상**: Claude Code + Copilot을 기본으로 켤지, 연결할 때 물어볼지.
2. **커밋된 `CLAUDE.md`/`AGENTS.md`가 있을 때**: 지금 설계는 손대지 않고 개인 파일(`CLAUDE.local.md`, `.github/instructions/`)만 씁니다. 팀 전체가 쓰게 하려면 사용자가 직접 커밋할 한 줄을 출력해 주는 정도로 둘지.
3. **묶음 언어**: 에이전트는 한국어·영어 모두 읽습니다. README와 GUI가 한국어이므로 한국어로 쓰고, 키·명령·심볼 이름은 원문 그대로 둘지.
4. **확인 필요**: VS Code Copilot이 git에서 제외된 `.github/instructions/*.instructions.md`를 실제로 로드하는지(파일 시스템 기준이면 문제없음). 1단계 수동 검증 때 확인합니다.
