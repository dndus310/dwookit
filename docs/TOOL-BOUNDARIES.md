# 도구 경계 점검 보고서

[CLAUDE.md](../CLAUDE.md)의 도구 경계 지침(모든 기능은 한 도구에 속하고, 도구는 설치 단위이며, 세부 기능이 화면에 드러나야 함)에 비춰 현재 코드를 점검한 결과입니다. 기준 시점: `feature/bp-native-migration` 43950b6.

## 요약

- 네 도구(DwoLens, DwoStateMonitor, DwoCanvas, DwoBuilder)의 설치·업데이트·제거 구조는 잘 되어 있습니다. 런처가 도구마다 상태와 설치 버튼을 보여 주고, 제거 때 지울 목록도 도구별로 있습니다.
- 가장 큰 문제는 **네이티브 C++ 전환(native)이 어느 도구에도 속하지 않은 채 DwoLens에 얹혀 있는 것**입니다. 카탈로그 항목, 도구 ID, 설치 확인, 화면이 모두 없습니다. 또 "읽기만 한다"는 DwoLens 플러그인이 에셋을 저장합니다.
- 다음으로 **도구의 세부 기능이 화면에 드러나지 않습니다.** 런처는 도구마다 한 줄 설명만 보여 주고, 세부 기능은 정보 화면의 카탈로그 표에만 있습니다.
- 그 밖에 이름·위치가 도구와 어긋난 코드(`lens_build.py` 등), 사용자 플러그인에 들어간 QA 코드, 사실과 달라진 문서 문구가 있습니다.

## 현재 도구 구성

| 도구 | 플러그인 | 카탈로그 기능 수 | 설치 확인 | 화면 |
|---|---|---|---|---|
| DwoLens | `__DwoLensInspector` (Editor, "read-only") | 35 (계획 중 6) | analyze, bp export, live만 | 개요·아키텍처·흐름·파일·문제·테스트·로그·성능·기록 |
| DwoStateMonitor | `__DwoStateMonitor` (Runtime) | 4 | 업데이트·제거 | DwoLens 아래 "실행 중 관찰" |
| DwoCanvas | `__DwoCanvas` (Runtime + Editor) | 4 | 가져오기·제거 | Canvas |
| DwoBuilder | 없음 (Jenkins, git 훅) | 3 | 없음(compile, package는 설치 없이 실행) | CI |
| (공용) | — | 4 | — | 런처, 정보 |
| **native (소속 없음)** | DwoLensInspector에 들어 있음 | **0** | **없음** | **없음** |

## 정리가 필요한 부분 (중요한 순서)

### 1. 네이티브 C++ 전환에 소속 도구가 없음
- 카탈로그에 native 항목이 하나도 없습니다. plan, generate, rebase, restore, verify(동작 비교, 네트워크 검사), status 모두 빠져 있습니다.
- 도구 ID가 없어 설치·제거할 수 없고, rebase와 verify는 DwoLens 플러그인이 설치됐는지도 확인하지 않습니다(dwookit.ps1:2123, :2140, :2149).
- 코드가 DwoLens에 흩어져 있습니다.
  - 플러그인: `DwoLensNativeRebase.cpp`(에셋 저장), `DwoLensBehaviour.cpp`(동작 비교, 네트워크 검사 자동화 테스트 `DwoLens.NativeNetCheck`)
  - Python: `lens_native.py`(3,659줄, 이름이 DwoLens)
  - README: "DwoLens 검사와 도구" 절 아래(README:438)
- 이 일 때문에 DwoLens 플러그인이 AIModule·GameplayTasks·UMGEditor에 의존하게 됐습니다(동작 비교용).
- 화면 어디에도 없습니다. 명령줄 전용입니다.

### 2. "읽기만 한다"는 약속이 깨짐
사실과 달라진 문구:
- `DwoLensInspector.uplugin`: "read-only reflection and Blueprint graph extractor"
- README:14 "DwoLens와 DwoStateMonitor는 프로젝트를 읽기만 하며"
- README:329 "`.uproject`, `Build.cs`, 기존 파일은 건드리지 않습니다" (`native generate`는 Build.cs를 고침)
- README:628 "읽기 전용, 패키지를 저장하지 않음" (rebase와 테스트 블루프린트 생성은 저장함)
- dwookit.ps1:3 "Never modifies tracked project files"

### 3. QA 전용 코드가 사용자 플러그인에 들어감
- `DwoLensBuildBlueprint.cpp`(테스트 블루프린트를 만들어 저장하는 dwookit 자체 QA용)가 모든 사용자 프로젝트의 DwoLens 플러그인에 컴파일됩니다.
- 네트워크 검사 자동화 테스트가 사용자 에디터의 테스트 목록에 보입니다.
- `tests/fixtures/native/lab`의 생성 명세는 저장소에만 있고 설치되지 않으므로 그대로 두어도 됩니다.

### 4. 도구의 세부 기능이 화면에 드러나지 않음
- 런처의 도구 항목은 한 줄 설명, 상태, 버튼, 명령줄 목록만 보여 줍니다(views_launcher.h:409-554). 세부 기능 목록은 정보 화면의 카탈로그 표에만 있습니다(views.h:3074).
- 기능마다 "어디서 실행하는지", "결과 파일", "필요한 도구", "프로젝트에서 바꾸는 것"이 화면에 없습니다.

### 5. 이름·위치가 도구와 어긋난 코드
| 파일 | 실제 도구 | 문제 |
|---|---|---|
| `lens_build.py` | DwoBuilder (compile, package) | 이름이 lens, `lens_runtime`·`lens_verify`를 가져다 씀 |
| `lens_agent.py`, `lens_library.py`, `lens_targets.py` | 공용 | 이름이 lens (`lens_targets`는 DwoStateMonitor 관찰 목록에도 씀) |
| `dwookit_ci.py` | DwoBuilder | `lens_quality.load_config`를 가져다 씀 |
| `lens_quality.py`의 DwoStateMonitor 어댑터 | DwoStateMonitor | DwoLens 코드 안에 있음 |
| 카탈로그 `ci-export` | 모든 실행 | tool이 lens로 표시됨 |
| DwoBuilder의 ID | — | ps1·화면은 `ci`, 카탈로그·별칭은 `builder` (CI 화면의 두 버튼이 서로 다른 ID를 씀, views_ci.h:323, :510) |
| `kLines` (app.h:20-33) | DwoLens 7개 + DwoBuilder 2개 | 한 목록에 섞이고 `kLensLineCount`로 구분 |

### 6. DwoStateMonitor 화면이 DwoLens 아래에 있음
- 이전 결정(docs/LIVE-LENS.md §4)은 "플러그인은 따로, 화면은 DwoLens › 실행 중 관찰로"입니다. 그래서 사이드바에 DwoStateMonitor가 보이지 않습니다(main.cpp:704).
- 새 지침("도구마다 화면에서 자기 자리")과 맞지 않습니다. 그대로 둘지 다시 정해야 합니다(아래 결정 2).

### 7. 문서
- README 도구 표(README:5-10)에 DwoLens의 `bp`, `live`, `agent`와 native가 없습니다.
- README 구성 표(:607-630)에 `lens_native`, `lens_bp`, `lens_explain`, `lens_flow`, `lens_live`가 없고, 카탈로그를 "세 도구별로 묶음"이라고 적어 두었습니다(지금은 네 도구 + 공용).
- 명령 목록(:48-103)에 native가 없습니다.
- 설계 가이드(guide.html)의 도구 소개에도 native가 없습니다.

## 정리안

1. **새 도구 DwoNative(가칭, 화면 이름 "C++ 전환")**
   - 플러그인 `__DwoNative`(Editor): rebase, 동작 비교, 네트워크 검사
   - Python: `lens_native.py` → `native_*.py`
   - 명령: `native …`. 설치되지 않았으면 안내만 합니다.
   - 카탈로그: native-plan, generate, rebase, restore, behaviour-check, net-check, status
   - 필요한 도구: DwoLens(블루프린트 export), 컴파일
   - 바꾸는 것: 블루프린트 에셋(백업 후), `Source/<모듈>/DwoNative/`, `<모듈>.Build.cs`. 화면·README·uplugin에 적습니다.
   - 화면: 블루프린트별로 C++로 옮길 수 있는 비율, 생성됨, rebase됨, 동작 비교 결과, 남는 이유를 보여 줍니다.
2. **DwoLens 플러그인을 다시 읽기 전용으로.** export(전환용 "native" 정보 포함)는 읽기 전용이므로 DwoLens에 남깁니다. DwoNative는 그 결과 파일만 읽습니다(지침 2-4).
3. **QA 도구 분리.** 테스트 블루프린트 생성기는 셀프테스트에서만 빌드하는 개발용 플러그인(예: `plugin/_qa/DwoQA`)으로 옮기고, 사용자 플러그인에서 뺍니다.
4. **이름 정리.**
   - DwoBuilder 코드: `builder_*.py`
   - 공용 코드: `dwookit_*.py`
   - DwoStateMonitor 어댑터: 자기 파일로 분리
   - 도구 ID를 하나로 통일(`builder` 또는 `ci`)하고, 카탈로그의 `ci-export`는 공용으로
5. **화면.**
   - 런처의 도구 항목을 펼치면 그 도구의 세부 기능 목록(카탈로그에서 읽음)이 나오게 합니다: 준비됨·계획 중, 실행 위치, 결과 파일, 필요한 도구, 바꾸는 것.
   - `kLines`를 도구별로 나눕니다.
6. **문서.** 위 7의 문구와 표를 고치고, 설계 가이드에 DwoNative를 단계로 추가합니다(블루프린트 export → 계획 → 생성 → rebase → 동작 비교).

## 결정 (2026-11-28)

1. **C++ 전환과 DwoStateMonitor는 둘 다 DwoLens의 세부 기능으로 둡니다.** 위 정리안 1(DwoNative 분리)은 쓰지 않습니다. 대신 DwoLens가 이제 읽기·전환·실행 중 관찰을 모두 하므로 **"Lens"가 아닌 새 이름**을 붙입니다(이름은 정하는 중). 이 도구의 설명·README·화면에는 프로젝트에서 바꾸는 범위(전환 때 에셋 저장, `Source/<모듈>/DwoNative/`, `Build.cs`)를 적습니다. DwoStateMonitor의 런타임 플러그인은 지침 2-7에 따라 에디터 플러그인과 따로 두되, 같은 도구의 설치 단위로 묶습니다(DwoCanvas가 Runtime·Editor 모듈을 함께 갖는 것처럼).
2. **DwoBuilder 도구 ID는 `builder`로 통일합니다.**
3. 추가 요청: 언리얼 에디터의 블루프린트 편집기 툴바에 메뉴를 넣어, 통합된 도구의 기능(C++ 전환 포함)을 에디터 안에서 실행합니다.

## 작업 순서

1. 카탈로그에 C++ 전환 항목 등록(DwoLens 소속), 문서 문구 바로잡기 (작음, 동작 변경 없음)
2. QA 블루프린트 생성기 분리
3. DwoBuilder 도구 ID를 `builder`로 통일, Python 파일 이름 정리
4. DwoLens의 새 이름으로 변경: 플러그인 폴더, 명령, 화면, 문서, 이미 설치된 프로젝트의 이전 이름 처리 (QA 프로젝트에서 재검증 22개)
5. 화면: 런처 세부 기능 목록, C++ 전환 화면, DwoStateMonitor를 이 도구의 세부 기능으로 표시
6. 에디터 블루프린트 편집기 툴바 메뉴
