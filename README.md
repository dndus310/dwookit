# dwookit

언리얼 프로젝트용 도구 **런처**입니다. 프로젝트를 흔적 없이 연결하고, 그 프로젝트에 쓸 도구를 골라 설치·업데이트·제거하고 실행합니다. dwookit 폴더를 새 버전으로 바꾸면, 프로젝트에 설치된 도구 중 실제로 바뀐 것만 업데이트하라고 알려 줍니다.

| 도구 | 하는 일 | 프로젝트에 들어가는 것 |
|---|---|---|
| **DwoLens** | 분석·검증·로그 검사: 아키텍처 리포트(`analyze`), 검증(`verify`), 로그(`logs`), 맵 스모크(`smoke`), 성능(`perf`), 콘텐츠 용량(`sizes`), Insights 트레이스(`trace`) | `Plugins/__DwoLensInspector` (숨김, 미리 빌드, 기본 비활성) |
| **DwoStateMonitor** | 실행 중인 게임의 상태를 실시간으로 보는 런타임 플러그인 (예전 이름 StateMonitor) | `Plugins/__DwoStateMonitor` (숨김) |
| **DwoCanvas** | 디자인 파일(`*.dwocanvas.json`)을 UMG 위젯 블루프린트로 가져오는 플러그인 | `Plugins/__DwoCanvas` (숨김) |
| **DwoBuilder** | C++ 컴파일(`compile`)·패키지 빌드(`package`)를 바로 실행하고, push·merge·커밋 때 컴파일 → DwoLens 검사 → 패키지 빌드를 자동 실행 (Jenkins, 폐쇄망 지원) | 설치하면 Jenkins 작업과 `.git/hooks` (Jenkins 자체는 PC에 하나). 컴파일·패키지만 쓸 때는 설치 불필요 |

모든 도구는 선택 설치입니다. 프로젝트 연결은 도구와 별개로, `__dwookit` 폴더와 실행 파일을 만들고 git에서 숨기는 일만 합니다.

DwoLens와 DwoStateMonitor는 프로젝트를 읽기만 하며, 언제 떼어 내도 프로젝트는 그대로 동작합니다. DwoCanvas도 같은 방식으로 숨겨 설치하고 떼어 낼 수 있습니다. 다만 DwoCanvas로 만든 위젯이 남아 있는 동안에는 제거를 막습니다(아래 [DwoCanvas](#제공-도구-dwocanvas)).

> 이전 이름은 dwo-Forge(그 전에는 dwo-Lens)입니다. 이전 이름으로 연결한 프로젝트는 dwookit 명령을 처음 실행할 때(창에서 열 때 포함) 자동으로 옮겨집니다. 아래 [이전 이름에서 옮기기](#이전-이름에서-옮기기-dwo-forge-dwo-lens-1x)를 보세요.

## 시작하기 (3단계)

> **실행할 파일은 `dwookit.bat` 하나입니다.** 연결한 프로젝트에는 같은 일을 하는 `__dwookit.bat`이 생깁니다. `tools/`, `tests/` 안의 .bat은 직접 실행하지 않습니다(`tests/selftest.bat`은 개발용 검증).

1. dwookit를 받습니다. GitHub의 **Download ZIP**을 풀어도 되고 `git clone`해도 됩니다. 어느 폴더든 상관없습니다.
2. 받은 폴더의 **`dwookit.bat`을 더블클릭**합니다. 처음 한 번만 폴더를 등록하고 창을 빌드합니다(약 1분). 별도 설치 단계는 없습니다.
3. 창(런처)에서 프로젝트를 엽니다(`프로젝트 열기...`, 또는 프로젝트 폴더나 `.uproject`를 창에 **끌어다 놓기**). 함께 설치할 도구를 체크하고 **연결**을 누르면 끝입니다(DwoLens가 기본으로 체크되어 있습니다).

`dwookit.bat`에 프로젝트 폴더나 `.uproject`를 끌어다 놓아도 그 프로젝트로 바로 열립니다. 이미 연결된 프로젝트는 프로젝트 안의 `__dwookit.bat`을 더블클릭해도 창이 열립니다.

### 제거 (Uninstall)

도구 하나만 지우려면 런처의 도구 줄에서 `제거`(또는 `__dwookit.bat tool remove <도구>`)를 누릅니다. 프로젝트에서 dwookit를 모두 떼어 내려면 런처의 `연결 해제...`(또는 명령 팔레트 `Ctrl+K` → 연결 해제)를 누르거나 `__dwookit.bat uninstall`을 실행합니다. **플러그인을 먼저 따로 지울 필요가 없습니다.** 먼저 프로젝트를 검사해 dwookit가 남긴 것을 모두 보여 주고, 사용자가 정할 것만 물은 뒤 한 번에 지웁니다.

| 구분 | 내용 | 처리 |
|---|---|---|
| 플러그인 | `Plugins/__DwoLensInspector`, `__DwoStateMonitor`, `__DwoCanvas` (dwo-Lens 1.x의 `__StateMonitor` 포함) | 항상 지움 |
| 실행 파일·연결 정보 | `__dwookit.bat`, `__dwookit/` (dwo-Lens 1.x의 `__dwo-lens.bat`, `__dwo-lens/` 포함) | 항상 지움 |
| git exclude | 연결할 때 넣은 dwookit 블록 | 항상 지움 (다른 내용은 그대로) |
| 테스트 잔여물 | `Content/DwoCanvasTests`, `Saved/DwoCanvasTests` (DwoCanvas 자동화 테스트가 남긴 빈 폴더·파일) | 항상 지움 |
| 프로젝트 파일 등록 | `.uproject`의 `Plugins` 항목(DwoStateMonitor, DwoCanvas), `Build.cs` 의존성(`DwoStateMonitor`, `DwoCanvasRuntime`, `DwoCanvasEditor`), `Config/DefaultDwoCanvas.ini`, 에디터 개인 설정의 모듈 기록 줄 | 원본을 백업한 뒤 해당 항목만 지움. 나머지 내용과 서식은 그대로 (`--keep-references`로 건너뜀) |
| DwoCanvas로 만든 위젯 | `canvas-import`가 만든 위젯 블루프린트·이미지(`T_DB_…`)와 가져오기 기록(`DwoCanvas/Records`) | **선택**: 삭제(`--delete-generated`) 또는 남김(`--keep-generated`). DwoCanvas 없이 열면 깨지므로 묻습니다 |
| 실행 기록 | `__dwookit/data`, `Saved/DwoCanvas` 리포트, 성능 캡처 CSV | **선택**: 보존(`--keep-data`, `%LOCALAPPDATA%\dwookit\uninstalled\<프로젝트>-<시각>`으로 이동) 또는 삭제(`--delete-data`) |
| 플러그인을 쓰는 코드 | `#include "StateMonitorSubsystem.h"`처럼 프로젝트 C++ 코드가 플러그인을 쓰는 줄 | 자동으로 고치지 않음. 목록을 보여 주고 확인을 받은 뒤(`--force`) 제거. 제거 후 그 줄을 지워야 빌드됨 |

- 편집한 프로젝트 파일의 원본은 `%LOCALAPPDATA%\dwookit\uninstalled\<프로젝트>-<시각>\backup`에 남습니다.
- 명령줄에서 선택 플래그를 주지 않으면 콘솔에서 묻습니다. 묻을 수 없는 환경(스크립트)에서 위젯 처리 방법이나 코드 사용 확인이 빠지면, 아무것도 바꾸지 않고 멈춥니다.
- `__dwookit.bat uninstall --plan-json`은 바꾸지 않고 검사 결과만 JSON으로 출력합니다(창의 제거 대화상자가 사용).
- 제거 뒤 다시 검사해 남은 것이 있으면 알립니다. git 프로젝트는 `git status`가 연결 전과 같은지도 확인합니다. `Saved/`, `Intermediate/`, `DerivedDataCache/`처럼 에디터가 원래 쓰는 폴더는 dwookit 파일만 지웁니다.

### 명령줄

창 없이 쓰려면 받은 폴더의 `dwookit.bat <명령> <프로젝트 폴더>`, 또는 연결된 프로젝트 안의 `__dwookit.bat <명령>`을 씁니다.

```bat
dwookit.bat connect "C:\Projects\MyGame"  :: 프로젝트 연결 (도구는 설치하지 않음)
__dwookit.bat                     :: 창 열기 (연결 전이면 연결)

:: 도구 (lens, statemonitor, canvas, builder = DwoLens, DwoStateMonitor, DwoCanvas, DwoBuilder. 전체 이름도 됨). 플러그인 도구는 엔진 버전별 첫 설치 때만 빌드로 몇 분 걸림
__dwookit.bat tools [--json]      :: 도구별 상태: 설치 안 됨 / 최신 / 업데이트 있음 / 프로젝트 자체 포함 / 이전 형식, PC 구성요소
__dwookit.bat tool install lens   :: 설치 (statemonitor는 --runtime으로 패키지 게임 포함)
__dwookit.bat tool update builder :: 업데이트.  tool update --all = 업데이트가 있는 도구 모두
__dwookit.bat tool remove canvas  :: 제거 (프로젝트가 아직 쓰고 있으면 거부, --force)
dwookit.bat attach "C:\Projects\MyGame"   :: dwo-Lens 방식: 연결 + DwoLens 설치

:: DwoLens
__dwookit.bat analyze             :: 아키텍처 리포트 + 품질 점검
__dwookit.bat verify              :: BP 전체 컴파일, 데이터 검증, 자동화 테스트
__dwookit.bat logs                :: Saved/Logs, Saved/Crashes 분석 (이전 실행 대비 새 오류)
__dwookit.bat smoke               :: 맵마다 -game -nullrhi로 띄워 Fatal·크래시·ensure·오류 검사
__dwookit.bat perf [맵]           :: CSV 프로파일러로 프레임 캡처, p95·히치를 예산과 비교
__dwookit.bat sizes               :: Content 용량, 하드 참조로 함께 로드되는 용량 (엔진 불필요)
__dwookit.bat trace [맵]          :: Unreal Insights 트레이스(.utrace) 기록
__dwookit.bat play [맵]           :: 게임 창 실행 (DwoStateMonitor 연결용)
__dwookit.bat bp add <경로> [...]  :: 블루프린트 추적 시작 (Content\...\BP_X.uasset, /Game/.../BP_X, 폴더)
__dwookit.bat bp remove <경로>     :: 추적 중지 (파일은 다음 export 때 지워짐)
__dwookit.bat bp list              :: 추적 목록, export 뒤 바뀐 블루프린트
__dwookit.bat bp export            :: 추적 중인 블루프린트만 내보내기: 의사코드 .md + 전체 .json (에디터 닫고)
__dwookit.bat agent [status|on|off] :: 에이전트 안내(AGENT.md) 다시 쓰기, 상태, 안내 파일 켜기·끄기

:: DwoStateMonitor
__dwookit.bat sm-install [--runtime] | sm-remove [--force]   :: tool install|remove statemonitor와 같음

:: DwoCanvas
__dwookit.bat canvas-validate <파일>                  :: 디자인 파일 검사
__dwookit.bat canvas-import <파일> [--reset] [--doc 이름]  :: 위젯 블루프린트로 가져오기
:: canvas-install | canvas-remove [--force] 은 tool install|remove canvas와 같음

:: DwoBuilder (아래 'DwoBuilder')
__dwookit.bat compile            :: C++ 컴파일 (UnrealBuildTool, 에디터 대상). 에디터를 닫고 실행. 설치 불필요
__dwookit.bat package            :: 패키지 빌드 (RunUAT BuildCookRun). 결과물은 최근 3개만 보관. 설치 불필요
dwookit.bat ci-bundle                     :: (인터넷 PC) Jenkins·Java·플러그인 오프라인 번들 만들기
dwookit.bat ci-install [--bundle <zip>] [--port N]   :: 번들로 설치 (네트워크 불필요). ci-start / ci-stop / ci-status / ci-remove
__dwookit.bat ci-job [--remove]           :: 이 프로젝트의 CI 작업과 git 훅 등록 (해제) = tool install|remove ci
__dwookit.bat ci-hooks [--remove]         :: git 훅만 다시 설치 (clone·reset 뒤) 또는 제거
__dwookit.bat ci-trigger [--reason 내용] [--stages compile,lens,package]   :: 빌드 요청 (외부 이벤트 주입)

:: 관리
__dwookit.bat status | doctor     :: 연결 정보와 도구 상태 · 환경 점검
__dwookit.bat update              :: 연결 새로 고침(실행 파일, git 숨김) + 업데이트가 있는 도구 모두 업데이트
__dwookit.bat tools --catalog     :: 도구 카탈로그
__dwookit.bat uninstall           :: 한 번에 완전 제거. 기록·생성 위젯 처리를 물음 (--keep-data/--delete-data, --keep-generated/--delete-generated, --force)
```

창에서 실행한 명령의 출력은 하단 **실행 로그** 패널에 표시되고, `%LOCALAPPDATA%\dwookit\logs`에 파일로도 저장됩니다(최근 50개). 실패 알림의 **로그 파일 열기**로 바로 열 수 있습니다.

## 창 (런처)

명령어 대신 창에서 쓰려면 둘 중 하나를 실행합니다.

- dwookit 폴더의 **`dwookit.bat`을 더블클릭** → 런처에서 프로젝트를 열거나 창에 끌어다 놓기
- 연결된 프로젝트의 `__dwookit.bat`을 더블클릭

창 왼쪽은 **사이드바**입니다. 위에서부터 **홈**(런처), **PC 구성요소**, 설치한 **도구**(화면이 여러 개인 도구는 줄 오른쪽 화살표로 화면 목록을 접고 펼침. 연 도구는 펼쳐진 채로 시작), 맨 아래 **명령 검색**·**정보**·**접기**가 있습니다. `접기`를 누르면 아이콘만 남고(설정은 기억됨), 이름은 툴팁으로 보입니다. 창 폭이 좁으면 자동으로 접히고, 창은 960×620보다 작아지지 않습니다. 도구 아이콘은 홈 화면 썸네일과 같은 그림을 도구 색으로 표시합니다(DwoLens 돋보기, DwoStateMonitor 눈, DwoCanvas 팔레트, DwoBuilder 상자).

**홈(런처)** 은 카드 하나로 된 도구 라이브러리입니다(Meta Quest Developer Hub의 다운로드 화면과 같은 구성).

| 영역 | 내용 |
|---|---|
| 탭 | `모든 도구` · `설치됨 (n)` · `업데이트 (n)` |
| 필터 줄 | **프로젝트** 드롭다운(최근 프로젝트, `프로젝트 열기...`), 종류·엔진·버전 관리, **도구 검색**, `···` 프로젝트 메뉴(폴더 열기, 연결, 연결 새로 고침, 환경 점검, 상태 확인, 연결 해제) |
| 배너 | 연결 안 된 프로젝트: `연결만 하기`. 업데이트가 있으면 `모두 업데이트`, 엔진이 바뀌었으면 `연결 새로 고침` |
| 도구 항목 | 썸네일, 상태(설치됨·업데이트 있음·설치 안 됨·프로젝트에 포함·이전 형식), 이름, 설명, 범위와 설치 위치. 오른쪽에 주 버튼(`설치`·`업데이트`·`열기`)과 `···`(열기, 패키지 포함 설치, 업데이트, 제거, 자세히), 그 아래 버전(업데이트가 있으면 `설치 버전 → 최신 버전`) |
| 실행 로그 | 카드 아래 서랍. 명령이 시작되거나 실패하면 자동으로 열림 |

연결 안 된 프로젝트에서 도구의 `설치`를 누르면 연결과 설치를 함께 합니다. 항목을 두 번 누르면 그 도구를 엽니다. **PC 구성요소**는 `Jenkins (DwoBuilder)`(설치·시작·중지·번들 업데이트·제거, 계정, 이 Jenkins를 쓰는 프로젝트)와 `언리얼 엔진`(이 PC의 엔진 목록) 탭으로 나뉩니다. **정보**에는 버전, 문서, 도구 카탈로그가 있습니다.

**도구 화면**: 각 화면은 **탐색(왼쪽) · 본문(가운데) · 속성(오른쪽) · 하단 패널**로 나뉩니다. 패널은 끌어서 다시 배치할 수 있고 `보기 > 레이아웃 초기화`로 되돌립니다.

| 도구 | 화면 (단축키) | 내용 |
|---|---|---|
| - | 홈 (Ctrl+9) | 위 설명 |
| DwoLens | 대시보드 (Ctrl+1) | 프로젝트 상태 배너, KPI 카드(새 문제, 아키텍처 문제, BP 복잡도, 테스트 통과율, 로그 오류, 맵 스모크, 프레임 p95, 콘텐츠 용량, DwoStateMonitor)와 실행별 추세선, 검사별 최근 10회 결과, 주요 문제, 시작하기 체크리스트 |
| DwoLens | 아키텍처 (Ctrl+2) | 왼쪽 탐색은 기본이 **위치** 보기: 소스 코드(Source) · 콘텐츠(/Game) · 프로젝트 플러그인(Plugins) · 엔진·외부로 나눈 뒤 실제 폴더 트리를 따라가며, 폴더마다 항목 수와 문제 수, 항목마다 종류(C++, BP, 위젯 BP, DataTable 등)를 표시. 클래스는 `상속` 보기로, BP 호출·에셋 참조·모듈 의존성은 `연결 수 순` 목록으로 바꿀 수 있음. 가운데는 클래스 계층·BP 호출·에셋 참조·모듈 의존성 **노드 그래프**(이동·확대·미니맵·F 전체 보기), 품질 점검 표. **기능** 보기는 클래스가 하는 일을 게임의 말로 보여 줌: `점프`, `이동`, `상호작용`, `문 열기`처럼 **기능**을 먼저 나열하고(플레이어 기능 / 엔진·내부 동작), 기능을 펼치면 그 기능을 시작하는 **시작점**(입력 눌림·뗌, 터치 버튼, 타이머, 착지 같은 엔진 콜백), 고르면 기능 개요 그래프(기능 → 시작점 → 함수 → 핵심 함수 → 경우)와 속성 패널의 핵심 함수·경우(조건 → 하는 일)·상태(바꿈/읽음)·닿는 클래스. 시작점을 고르거나 그래프에서 더블클릭하면 그 시작점의 **로직**(호출, 값 설정, 분기 참/거짓, 반복, 형변환, 스폰)을 위에서 아래로 그림. BP 이벤트가 부르는 C++ 함수는 그 본문까지 이어서 펼치고(점선 `내부`), 부모 C++ 클래스와 자식 BP를 함께 봄. 기능 이름과 묶음은 함수·이벤트 이름(DoJumpStart → 점프), 호출 관계, 함께 쓰는 상태 변수로 추론하고(LLM·네트워크 없음), C++ 쪽은 소스 패턴(BindAction, AddDynamic, SetTimer, 오버라이드, if/for)으로 읽은 추정 |
| DwoLens | 문제 (Ctrl+3) | 모든 검사의 최신 실행에서 모은 문제. 심각도·범주 필터, 묶기, **NEW**(이전 실행 대비), 무시(기준선) |
| DwoLens | 검증 (Ctrl+4) | 단계 카드, 자동화 테스트 트리, 최근 10회 결과 띠, **새 실패·불안정(flaky) 테스트**, 맵 스모크 결과 |
| DwoLens | 로그 (Ctrl+5) | 지문으로 묶은 로그 그룹, 횟수 추세, 처음·마지막 발생, 콜스택, 크래시 |
| DwoLens | 성능·용량 (Ctrl+6) | 프레임 시간 차트(예산선·스레드별), p95 추세, 히치 / 폴더별 용량 트리맵, 큰 에셋, 함께 로드되는 용량 / Insights 트레이스 열기 |
| DwoLens | 실행 기록 (Ctrl+8) | 실행별 단계 띠, 두 실행 **비교**(지표 변화, 새로 생긴·해결된 문제, 결과가 바뀐 테스트), 결과 파일 |
| DwoStateMonitor | DwoStateMonitor (Ctrl+7) | 실행 중인 게임의 상태를 실시간으로 표시 (아래 '제공 도구: DwoStateMonitor') |
| DwoCanvas | DwoCanvas (Ctrl+0) | 디자인 파일 고르기(열기 대화상자 또는 `*.dwocanvas.json` 끌어다 놓기), 검사·가져오기·초기화하고 가져오기, 최신 리포트 요약 (아래 '제공 도구: DwoCanvas') |
| DwoBuilder | DwoBuilder | **로컬 빌드**: 컴파일·패키지 실행, 최근 실행, 오류 목록(파일·줄), 리포트·전체 로그·결과물 폴더. **자동 빌드**: Jenkins 빌드 목록·결과·실시간 로그, `지금 빌드`(단계 선택), 작업 설정 다시 적용. Jenkins 관리는 런처의 PC 구성요소. 설치하지 않아도 런처에서 열어 로컬 빌드를 쓸 수 있음 |

- **명령 팔레트 (Ctrl+K)**: 화면 이동, 검사 실행, 도구 설치·업데이트·제거, DwoCanvas 검사·가져오기, 리포트 저장을 검색해서 실행합니다.
- **뒤로·앞으로**: 화면과 그 안의 선택(클래스, 기능, 문제, 테스트, 로그, 실행)을 브라우저처럼 기억합니다. `Alt+←`/`Alt+→`, 마우스 뒤로·앞으로 버튼, 메뉴 막대의 화살표.
- **그 밖의 단축키**: `Ctrl+F` 이 화면의 검색 칸, `Ctrl+B` 사이드바 접기, `Ctrl+O` 프로젝트 열기, `F5` 실행 결과 다시 읽기, `F1` 단축키 목록(도움말 > 단축키).
- **도구 메뉴**: 도구마다 열기·설치·업데이트·제거, `모든 도구 업데이트`, 프로젝트 연결·새로 고침·연결 해제.
- **상태 표시줄**: 실행 중인 명령과 진행 상황(취소), 오류·경고 수, DwoStateMonitor 연결 상태.
- **대기열**: 실행 중에 다른 명령을 누르면 대기열에 들어가 차례로 실행됩니다. 끝나면 알림이 뜨고 결과로 이동할 수 있습니다.

### 분석 리포트 (`report.html`)

`analyze`(와 Live Lens) 결과의 `out/report.html`은 **파일 하나로 된 대화형 리포트**입니다. 왼쪽에서 볼 것을 고르고(검색 가능) 오른쪽에서 그것만 봅니다. 네트워크 없이 열립니다.

작성 원칙 (대화형 보고서의 파라미터·연쇄 필터·연동·드릴다운, 보고서 작성 모범 사례 참고):
- **결론 먼저**: 요약 첫 줄이 판정, 심각도별 건수, 이전 분석 대비 새 문제·해결 건수입니다. 그 아래 먼저 확인할 문제와 조치가 옵니다.
- **숫자는 목록으로 이어짐**: 요약 카드, 막대, 사이드바 항목을 누르면 그 조건으로 걸러진 목록이 열립니다.
- **조건은 주소에**: `#classes?role=character&top=/Game/Characters`처럼 필터·정렬이 주소에 남아 북마크·공유할 수 있습니다. 폴더 필터는 상위 폴더를 고르면 하위 폴더 목록이 바뀝니다(연쇄).
- **문제마다 무엇·어디·왜·어떻게**: 규칙별 영향과 조치(`RULE_INFO`), 대상(클래스로 이동), 새 문제인지 표시.
- 단위를 붙이고(개·건·회), 제목은 명사로, 같은 개념은 같은 용어로 씁니다. 빈 목록은 "0건"처럼 사실만 적습니다.

| 왼쪽 | 오른쪽 |
|---|---|
| 요약 | 판정·건수·이전 대비 변화, 먼저 확인할 문제(조치 포함), 역할별 클래스 막대, 행동이 많은 클래스, 기본 맵 흐름 |
| 문제 (규칙별) · 문제 전체 | 심각도·규칙·변화(새 문제/이전부터)·내용 필터, 정렬. 문제를 누르면 영향·조치·대상 목록. 해결된 문제 목록 |
| 클래스 (역할별) · 클래스 전체 | 역할·종류(BP/C++)·폴더 → 하위 폴더·이름 필터, 행동·참조하는 대상·참조되는 곳·PIE 확인 열 정렬. 클래스를 누르면 부모, 관련 문제, 참조 관계 표, 행동(진입점 → 단계 트리) |
| 게임 흐름 (레벨·게임 모드·게임 인스턴스) | 흐름 다이어그램. 깊이 1~6, 호출·참조 포함 여부, 잎 객체 묶음(눌러서 펼침), 단계별 재생. 노드를 누르면 아래 패널에 그 객체의 역할·행동·연결 수(연동), 더블클릭하면 클래스로. 노드·선에 마우스를 올리면 설명. 처음에는 노드 45개 이하가 되도록 깊이를 고릅니다 |
| 품질 | 블루프린트 복잡도(기준 초과 먼저), 미사용 에셋, 이름 규칙 위반. 전체 수와 수록 수를 함께 표시 |
| 파일 분석 · 모듈 | 추적 중인 블루프린트의 설명(구성, 행동과 흐름, C++ 본문), 모듈 의존성 |

모든 표와 다이어그램을 한 페이지에 펼친 이전 형식은 `report-classic.html`로 함께 남고, 종합 리포트 저장은 이 형식을 씁니다.

**검증·로그·스모크 리포트**도 같은 원칙과 같은 필터·표 코드(`lens_report.JS_COMMON`)를 씁니다(`tools/lens_report_runs.py`).

| 검사 | 요약 | 목록·상세 |
|---|---|---|
| `verify` | 판정, 실패한 단계·테스트 수, 단계 카드, 실패한 테스트와 첫 오류 | 테스트 탐색(결과·묶음·이름 필터), 단계별 오류·경고 그룹(심각도·카테고리·새 문제·메시지 필터) |
| `logs` | 판정, Fatal·Error·Warning 건수, 이전 대비 새 그룹·새 크래시, 먼저 확인할 오류, 카테고리별 횟수 막대(누르면 그 카테고리 목록) | 문제 탐색(심각도·카테고리·새 문제·메시지), 그룹(메시지, 이어진 줄, 위치), 크래시 콜스택, 읽은 파일 |
| `smoke` | 판정, 실패한 맵, 판정 기준, 맵 카드(판정 필터) | 맵별 수치, 오류·경고 그룹, 크래시, 로그 경로 |

성능·용량·트레이스 리포트는 한 페이지 그대로입니다.

### HTML 리포트 저장

| 버튼 / 명령 | 결과 |
|---|---|
| 실행 기록 > 속성 `HTML 저장` | 선택한 실행의 `report.html`(대화형, 파일 하나)을 원하는 위치에 저장 |
| `파일 > 종합 리포트 저장...` | DwoLens 검사마다 최신 실행의 리포트를 표지와 함께 HTML 파일 하나로 저장 (미완료 실행은 제외) |
| `dwookit-gui.exe --project <폴더> --export-report <파일.html>` | 창 없이 종합 리포트 생성 (자동화·CI용) |

처음 실행할 때 Visual Studio C++ 도구로 한 번 빌드합니다(약 1분, `%LOCALAPPDATA%\dwookit\gui-cache`). GUI는 `tools/dwookit.ps1`을 그대로 실행하고 같은 결과 파일을 읽으므로 명령줄과 결과가 같습니다.

## 제공 도구: DwoStateMonitor

실행 중인 게임의 상태를 dwookit 창으로 실시간 전송하는 런타임 플러그인입니다. **프로젝트 코드 없이** 동작합니다.

```bat
__dwookit.bat tool install statemonitor            :: 에디터·PIE 전용 (기본). 패키지 빌드에 영향 없음
__dwookit.bat tool install statemonitor --runtime  :: 패키지 게임에도 포함. .uproject(BP 전용) 또는 Build.cs(C++)에 DwoStateMonitor 등록 필요
__dwookit.bat sm-remove             :: 제거. 프로젝트 코드가 참조 중이면 거부 (--force로 강제)
__dwookit.bat play [맵]              :: 게임 창을 띄움 (DwoStateMonitor가 자동 연결)
```

### 사용 순서

1. 런처의 도구 목록에서 설치합니다(`tool install statemonitor`, 예전 명령 `sm-install`도 동작). `.uproject`는 수정하지 않으며 `Plugins/__DwoStateMonitor`는 git에서 숨겨집니다.
2. dwookit 창을 열고 **DwoStateMonitor** 화면(Ctrl+7)으로 갑니다. 창은 `127.0.0.1:47731`에서 기다립니다.
3. 에디터에서 **Play(PIE)** 하거나, `게임 실행` 버튼(`play`)으로 게임을 띄웁니다. 게임이 시작되면 자동으로 연결됩니다(PIE 클라이언트가 여럿이면 각각 세션으로 표시).
4. GameInstance, GameMode, GameState, PlayerController, PlayerState, Pawn과 컴포넌트, HUD가 노드로 나타나고, 값이 바뀌면 깜빡입니다. 기본은 프로젝트(BP·C++)가 선언한 프로퍼티만 보내며, `엔진 프로퍼티 포함`을 켜면 전체를 보냅니다.
5. 그 밖에 볼 객체는 왼쪽 **관찰할 클래스 › 목록에서 고르기**로 고릅니다. 프로젝트의 블루프린트·위젯·C++ 클래스가 폴더 트리로 나오고, 체크한 클래스의 객체(액터, 컴포넌트, 위젯, 런타임에 만든 UObject)를 최대 16개씩 관찰합니다. 목록은 프로젝트의 `__dwookit/sm-watch.txt`에 저장되고, 게임이 시작할 때 플러그인이 직접 읽으므로 창을 나중에 열어도, 게임 화면의 모니터만 켜도 적용됩니다.
6. **참조 깊이**(0~4, 기본 1): 관찰한 객체가 프로퍼티로 가리키는 런타임 객체(무기, 타깃, 인벤토리 항목, 위젯 ...)를 그 깊이만큼 따라가 함께 관찰합니다. 에셋·클래스는 따라가지 않습니다. 소스 이름은 `PC0.Pawn.CurrentWeapon`처럼 따라간 경로입니다.

### 보는 방법

| 보기 | 내용 |
|---|---|
| 객체 그래프 | 상태 소스마다 노드 하나. 실선은 소유(컨트롤러→Pawn→컴포넌트), 점선은 프로퍼티가 가리키는 참조. 방금 바뀐 필드는 노란색 |
| 상태 전이 | 열거형·bool·이름·작은 정수 필드를 상태 머신으로 표시. 현재 상태는 초록색 테두리와 경과 막대, 전이선 두께와 라벨은 전이 횟수, 방금 일어난 전이는 깜빡임 |
| 속성 | 현재 값 트리(이전 값 툴팁), 변경 이력, 상태 필드 목록, JSON |
| 타임라인 (하단) | 소스별 변경 표시, 선택한 상태 필드의 값 구간. 재생 위치를 끌면 그 시점의 값으로 모든 보기가 바뀜. 이전·다음 변경으로 이동 |
| 이벤트 (하단) | 모든 변경을 시간 역순으로. 클릭하면 그 시점으로 이동 |

- **대상 추가**: 목록에서 고르는 것 외에, Blueprint에서 `Watch State Object`(객체) 또는 `Submit State Snapshot`(구조체) 노드로 직접 추가할 수도 있습니다. `sm-watch.txt`는 한 줄에 `클래스 이름 [최대 개수]`입니다(예: `BP_Enemy_C 4`).
- **녹화**: 받은 메시지를 JSON Lines로 저장하고 나중에 다시 열어 같은 화면으로 볼 수 있습니다.
- **명령줄 스위치(게임)**: `-StateMonitorPort=N` 다른 포트, `-StateMonitorLive=0` 연결 끔, Shipping 빌드는 `-StateMonitorLive=1`일 때만 연결.
- 자세한 API와 프로토콜은 [plugin/DwoStateMonitor/README.md](plugin/DwoStateMonitor/README.md)에 있습니다.

설치하면 `analyze`가 제출 지점을 찾고 `verify`가 `StateMonitor.*` 테스트를 함께 실행합니다.

**StateMonitor에서 이름이 바뀌었습니다.** 플러그인·모듈 이름만 `DwoStateMonitor`이고 클래스·헤더 이름(`UStateMonitorSubsystem` 등)은 그대로입니다. 저장된 블루프린트의 `/Script/StateMonitor` 참조는 플러그인의 CoreRedirects가 새 모듈로 연결합니다. C++ 프로젝트는 `Build.cs`의 `"StateMonitor"`를 `"DwoStateMonitor"`로 바꿔야 하며, `status`와 `doctor`가 남은 곳을 경고합니다.

## 제공 도구: DwoCanvas

Figma·HTML 변환기·직접 작성 등 어디서 왔든 같은 형식(`dwocanvas/2`)의 디자인 파일 `*.dwocanvas.json`을 UMG **위젯 블루프린트 디자이너 트리**로 만듭니다. 다시 가져오면 디자인이 바뀐 곳만 갱신하고, UE에서 고친 위젯은 유지합니다. 예전 `designbridge/1` JSON도 읽습니다.

```bat
__dwookit.bat canvas-install                          :: Plugins/__DwoCanvas에 설치 (미리 빌드한 에디터 바이너리 포함, git에서 숨김)
__dwookit.bat canvas-validate Shop.dwocanvas.json      :: 스키마·규칙 검사 (엔진 불필요, 몇 초)
__dwookit.bat canvas-import Shop.dwocanvas.json        :: 가져오기. 리포트 경로를 출력
__dwookit.bat canvas-import Shop.dwocanvas.json --reset --doc Shop   :: UE 수정분까지 덮어쓰기, 문서 하나만
__dwookit.bat canvas-remove                           :: 제거 (Content의 .uasset이 DwoCanvasRuntime을 쓰면 거부, --force로 강제)
```

- **설치 방식은 DwoStateMonitor와 같습니다.** 원본은 dwookit의 `plugin/DwoCanvas`에 있고, `canvas-install`이 `Plugins/__DwoCanvas`에 복사합니다(git에서 숨김). `canvas-remove`와 `uninstall`이 지웁니다.
- 생성된 위젯은 런타임 모듈 `DwoCanvasRuntime`(`UDwoCanvasShape`, `UDwoCanvasFrame`)을 씁니다. `canvas-remove`는 그런 에셋이 남아 있으면 멈추고 목록을 보여 줍니다(`--force`로 강제). `uninstall`은 멈추지 않고 위젯을 지울지 남길지 묻습니다. 그 위젯을 여는 다른 PC에도 DwoCanvas를 설치해야 합니다.
- 2.0.0이 `Plugins/DwoCanvas`에 설치한 것은 다음 명령 때 `Plugins/__DwoCanvas`로 옮겨집니다.
- Blueprint 전용 프로젝트도 미리 빌드한 에디터 바이너리가 들어가서 에디터가 컴파일 없이 열립니다.
- `canvas-import`는 `UnrealEditor-Cmd <uproject> -run=DwoCanvasImport -File=<json> [-ResetOverrides] [-Doc=<이름>]`를 실행합니다. 오류가 없으면 종료 코드 0이고, 리포트는 `<프로젝트>/Saved/DwoCanvas/Reports/*.txt`에 저장됩니다. 에디터를 닫고 실행하세요.
- `canvas-validate`는 `python -m dwocanvas validate <파일> [--json]`(`PYTHONPATH=canvas/python`)를 실행합니다.
- 창에서는 **DwoCanvas** 화면(Ctrl+0)에서 같은 일을 합니다. 디자인 파일을 창에 끌어다 놓으면 바로 그 화면으로 열리고, 명령 출력은 하단 실행 로그에, 최신 리포트의 상태·개수·쓴 에셋·오류와 경고는 본문에 요약됩니다.
- 사용법과 매핑 규칙: [plugin/DwoCanvas/README.md](plugin/DwoCanvas/README.md) · 설계(스키마 우선 구조): [canvas/docs/SchemaFirstArchitecture.md](canvas/docs/SchemaFirstArchitecture.md)

## 이전 이름에서 옮기기 (dwo-Forge, dwo-Lens 1.x)

이름이 dwo-Lens 1.x → dwo-Forge → dwookit 순서로 바뀌었습니다. dwookit은 두 이전 이름이 남긴 것을 스스로 옮깁니다. 프로젝트에 대해 어떤 명령이든(창에서 프로젝트를 여는 것 포함) 처음 실행할 때:

| dwo-Forge | dwo-Lens 1.x | dwookit |
|---|---|---|
| `__dwo-forge/` | `__dwo-lens/` | `__dwookit/`로 이름 변경, 실행 기록·설정·연결 정보 유지. 에디터가 쓰고 있는 파일은 다음 명령 때 옮김 |
| `__dwo-forge.bat` | `__dwo-lens.bat` | `__dwookit.bat` 설치. 옛 실행 파일이 실행 중이면 한 번만 새 실행 파일로 넘겨 주는 파일로 바뀐 뒤 스스로 지워짐 |
| git exclude의 `dwo-forge managed` 블록 | `dwo-lens managed` 블록 | `dwookit managed` 블록으로 교체 |
| `CLAUDE.local.md`의 `dwo-forge managed` 블록, `.github/instructions/dwo-forge.instructions.md` | - | `dwookit` 블록과 `dwookit.instructions.md`로 교체 (직접 쓴 줄은 유지) |
| DwoBuilder git 훅 (`dwo-forge-ci-hook`, `*.dwo-forge-orig`) | - | 표시와 보관 파일 이름을 `dwookit`으로 변경 |
| `%LOCALAPPDATA%\dwo-Forge` | `%LOCALAPPDATA%\dwo-Lens` | `%LOCALAPPDATA%\dwookit` (캐시, 창 설정, 보존한 실행 기록) |
| 환경 변수 `DWO_FORGE_HOME` | `DWO_LENS_HOME` | `DWOOKIT_HOME` (옛 변수도 계속 읽음. `dwookit.bat`은 `DWO_FORGE_HOME`이 있으면 새 위치로 맞춰 둠) |
| - | `Plugins/__StateMonitor` | `update` 또는 `sm-install` 때 같은 모드의 `Plugins/__DwoStateMonitor`로 교체 |

- `tools/forge.ps1`(dwo-Forge), `tools/lens.ps1`(dwo-Lens)은 옛 실행 파일, 이름 변경 전에 빌드된 DwoLensInspector 플러그인, 옛 git 훅을 위해 `tools/dwookit.ps1`로 넘겨 주기만 합니다.
- 프로젝트에 설치된 플러그인(DwoLensInspector, DwoStateMonitor)은 다음 `update` 때 새 폴더 이름(`__dwookit`)을 쓰는 버전으로 바뀝니다. 그 전까지 Live Lens 기록과 관찰 목록은 옛 폴더에 쓰일 수 있고, 다음 명령 때 옮겨집니다.
- Jenkins 작업 이름 접두사가 `dwookit`으로 바뀌었습니다. `ci-job`을 다시 실행하면 새 이름의 작업이 만들어지며, 옛 `dwo-forge-…` 작업은 Jenkins에서 직접 지웁니다.
- `uninstall`은 새 이름과 옛 이름을 모두 지웁니다.

## 검증 (QA)

```bat
tests\selftest.bat "C:\Projects\MyGame"                :: 단위·정적·GUI·전체 명령 왕복 (격리 복사본에서, DwoCanvas 포함)
python -m unittest discover -s tests                   :: 엔진 없이 도는 테스트 (Python 도구, 이전·DwoCanvas 명령 규칙)
python tests\stress.py                                    :: 대형 분석·100만 줄 로그·깊은 그래프
powershell -File tests\package_check.ps1 -Source <폴더> -Name pkg -Config Shipping [-InstallStateMonitor]
```

결과와 발견·수정한 결함은 [docs/QA-REPORT.md](docs/QA-REPORT.md)에 있습니다. 결과 파일은 `.qa/`(git 제외)에 생깁니다.

### 전제 조건

- 형상관리는 선택입니다. git 저장소인 프로젝트는 dwookit 파일을 `.git/info/exclude`로 숨기고, 상위 폴더 저장소 안에 있으면 그 저장소의 exclude에 프로젝트 경로를 붙여 숨깁니다. git이 없는 프로젝트는 숨길 대상이 없으므로 exclude를 쓰지 않습니다. (DwoCanvas는 예외로 숨기지 않습니다.)
- Epic Launcher 또는 소스 빌드로 프로젝트의 `EngineAssociation` 엔진이 설치되어 있어야 합니다.
- Python 3.10 이상. git은 git 프로젝트일 때만 필요합니다.
- 창(GUI)을 쓰려면 Visual Studio 2022(C++ 데스크톱 개발 워크로드). UE C++ 개발 환경이면 이미 있습니다.
- C++ 프로젝트는 에디터 바이너리가 빌드된 상태여야 Commandlet이 실행됩니다.

## 프로젝트에 생기는 것

연결만 하면 `__dwookit.bat`과 `__dwookit/`만 생기고, 도구를 설치할 때마다 그 도구의 것이 추가됩니다.

```
A/
├─ __dwookit.bat                # 실행 파일 (dwookit 설치 폴더의 도구를 실행)
├─ __dwookit/
│  ├─ home.txt                    # dwookit 설치 폴더 위치
│  ├─ state.json                  # 연결 정보 (버전, 형상관리 방식, 연결 전 git 상태)와 도구별 설치 기록 (tools)
│  ├─ ci.json                     # (DwoBuilder 설치 시) Jenkins 작업, 훅, 설치 버전
│  ├─ data/runs/<id>/             # DwoLens 실행별 raw · out · run.json · 로그
│  ├─ data/canvas/                # canvas-import 엔진 로그, 마지막 가져오기 (last.json)
│  ├─ bp-track.txt                # (bp add) 추적 중인 블루프린트 목록
│  ├─ blueprints/                 # (bp export) 블루프린트별 폴더: <이름>.md 의사코드, <이름>.json 전체, 큰 것은 parts/,
│  │                              #   <이름>.html·.explain.json 사람이 읽는 설명, index.html
│  ├─ context/AGENT.md            # 에이전트 안내 (읽는 순서, 결과물 형식, 명령). 매번 다시 씀
│  ├─ context/instructions/       # library에서 고른 지침서 사본 (Claude Code가 import)
│  ├─ context/installed.json      # 고른 지침서·스킬 때문에 __dwookit 밖에 쓴 경로 (git exclude, 제거에 씀)
│  ├─ agent.json                  # 적용할 에이전트, 고른 지침서·스킬, (agent off) 안내 파일을 쓰지 않음
│  ├─ sm-watch.txt                # DwoStateMonitor가 관찰할 클래스 (창에서 고름, 게임이 시작할 때 읽음)
│  ├─ created-files.txt           # dwookit 실행이 프로젝트에 남긴 파일 (제거 때 처리)
│  ├─ config.json                 # (선택) 프로젝트별 규칙 덮어쓰기
│  └─ gui.json                    # 화면 설정 (무시한 문제)
├─ CLAUDE.local.md                # Claude Code 안내: 관리 블록 하나 (AGENT.md와 사용자 지침 import)
├─ .github/instructions/
│  ├─ dwookit.instructions.md   # Copilot 안내: AGENT.md를 먼저 읽으라는 짧은 지침
│  ├─ dwookit-<이름>.instructions.md  # (Copilot 선택 시) library에서 고른 지침서
│  └─ project.instructions.md     # 사용자 지침 (빈 파일로 만들어 둠, 다시 쓰지 않음, 제거 때 백업 후 삭제)
├─ .github/skills/<이름>/          # (Copilot 선택 시) library에서 고른 스킬 사본 (.dwookit-managed 표식)
├─ .claude/skills/<이름>/          # (Claude Code 선택 시) library에서 고른 스킬 사본 (.dwookit-managed 표식)
├─ Plugins/__DwoLensInspector/    # (DwoLens) 미리 빌드된 에디터 전용 플러그인 (Source 없음, 기본 비활성)
├─ Plugins/__DwoStateMonitor/     # (DwoStateMonitor) 숨김
└─ Plugins/__DwoCanvas/           # (DwoCanvas) 숨김
```

`state.json`의 `tools`에는 도구마다 설치한 dwookit 버전, 소스 해시, 빌드한 엔진 버전이 기록됩니다. dwookit 폴더를 새 버전으로 바꿨을 때 소스 해시가 다르거나, 이전 릴리스(`VERSION`)가 설치했거나, 프로젝트 엔진이 바뀐 도구가 "업데이트 있음"으로 표시됩니다. 업데이트(`모두 업데이트`, `tool update --all`, `update`)는 플러그인 파일·설명 파일·관리 표시·기록을 지금 버전으로 다시 씁니다. 플러그인 소스가 같으면 빌드 캐시를 써서 다시 빌드하지 않습니다. 기록이 없는 DwoStateMonitor·DwoCanvas(3.0 이전 설치)도 업데이트를 권합니다.

연결이 프로젝트에 두는 작은 파일(`__dwookit.bat`, `__dwookit/home.txt`)은 어떤 명령이든 실행할 때 지금 dwookit에 맞게 바로 교체됩니다(`tools`처럼 읽기만 하는 명령 제외). 도구 플러그인은 에디터가 DLL을 쥐고 있을 수 있어 자동으로 바꾸지 않고 업데이트로 표시합니다.

도구 자체는 프로젝트에 복사하지 않고 dwookit 설치 폴더에서 실행합니다. `.uproject`, `Build.cs`, 기존 파일은 건드리지 않습니다. 이전 버전처럼 `__dwookit/system` 복사본이 있으면 `update`나 연결 때 지웁니다.

## DwoLens 검사와 도구

| 검사 | 단계 | 도구 |
|---|---|---|
| `analyze` | ST-1 수집 | `-run=DwoLensInspector` (리플렉션, BP 그래프, 에셋 참조), 모듈·플러그인 스캔 |
| | ST-2 분석 | 통합 그래프, 순환 검사, 하드 참조 체인, 미사용 에셋·리다이렉터, BP 복잡도 |
| | ST-3 규칙 | 이름·폴더 규칙 (UE5 스타일 가이드 기본값) |
| | ST-4 연계 | DwoStateMonitor 어댑터: 프로젝트에 있으면 제출 지점·Source ID 수집 |
| `verify` | ST-3 검증 | `CompileAllBlueprints`, `DataValidation`, `Automation RunTests` (DwoStateMonitor가 있으면 `StateMonitor.*` 추가) |
| `logs` | ST-4 로그 | 구조화 로그 파서 (심각도·카테고리·지문 그룹, 이전 실행 대비 새 오류), 크래시 수집 |
| `smoke` | 맵 부팅 | `UnrealEditor-Cmd <맵> -game -nullrhi`를 `smoke.seconds`초 실행 후 종료, 로그 파서로 결과 판정. fail: Fatal·크래시·ensure·비정상 종료·Error > `smoke.maxErrors` |
| `perf` | 성능 캡처 | `-ExecCmds="csvprofile frames=N"` + `-ExitAfterCsvProfiling` (CSV 프로파일러, `Saved/Profiling/CSV`), FrameTime·GameThread·RenderThread·RHIThread p50/p95/p99, 히치. fail: p95 > `perf.budgetMs` |
| `sizes` | 용량 | `.uasset/.umap/.uexp/.ubulk` 합계, 최신 `analyze`의 `graph.json`으로 하드 참조 폐포 용량. warn: 에셋 > `sizes.warnAssetMB` |
| `trace` | 트레이스 | `-trace=<trace.channels> -tracefile=<run>/out/trace.utrace`, `trace.seconds`초 후 창을 닫음. Unreal Insights로 열기 |
| `play` | (실행 아님) | `UnrealEditor <맵> -game -windowed 1280x720` 분리 실행. GUI에서 DwoStateMonitor 뷰어로 상태를 받을 게임을 띄울 때 사용 |

맵 인수를 생략하면 `perf.map`, 그다음 `Config/DefaultEngine.ini`의 `GameDefaultMap`을 씁니다. 결과가 fail이면 종료 코드 2입니다.

모든 검사는 끝날 때 `out/junit.xml`(JUnit)과 `out/results.sarif`(SARIF 2.1.0)도 만듭니다. SARIF의 각 문제에는 이전 실행과 비교한 `baselineState`(new / unchanged)가 들어가므로, Jenkins·GitHub Actions·GitLab·TeamCity에서 **새 문제만** 품질 게이트로 쓸 수 있습니다. 환경 변수 `DWOOKIT_CI_OUT`이 있으면 두 파일과 리포트를 그 폴더의 `<실행 ID>/`로 복사합니다(CI 작업이 결과를 모을 때 사용).

실행 기록은 `retention`에 따라 검사마다 최근 30개(패키지는 3개)만 남기고 오래된 것부터 지웁니다. 패키지 빌드 전에 디스크 여유 공간이 `retention.minFreeDiskGB`보다 적으면 오래된 패키지부터 지웁니다.

### 블루프린트 추적 (`bp`)

`analyze`의 `raw/blueprints.json`은 프로젝트의 모든 블루프린트를 파일 하나에 담아 분석용으로 씁니다. 에이전트(Claude Code, Copilot 등)나 사람이 특정 블루프린트의 로직을 읽고 추론하려면, 추적할 블루프린트를 골라 **블루프린트마다 폴더**로 내보냅니다.

```
__dwookit/blueprints/
├─ index.md                                    # 추적 중인 블루프린트 표 (부모, 변수·함수·노드 수, export 뒤 바뀜 표시)
├─ export.json                                 # 마지막 export: 요청·내보냄·없음·실패
└─ Game/ThirdPerson/Blueprints/BP_Player/
   ├─ BP_Player.md                             # 압축 의사코드. 먼저 읽는 파일
   └─ BP_Player.json                           # 전체 스크립트 (lens.bp/1)
```

- **`.md` (의사코드)**: 작은 모델도 적은 토큰으로 읽도록 줄였습니다. 이벤트는 `on BeginPlay():`, 함수는 `## fn 이름(인자: 타입) -> 반환`, 분기·반복·형변환은 `if`/`for`/`as`, 지연 노드는 `await`, 합쳐지는 실행선은 `goto Ln`입니다. 순수 노드는 식으로 펼치고(`(Health > 0)`, `Door Size.X`), 작성자가 바꾼 인수만 적습니다. 변수(기본값·플래그), 컴포넌트(부모·바꾼 속성), 상속 컴포넌트에서 바꾼 값, 클래스 기본값, 타임라인, 위젯 트리·바인딩, 주석 박스(`# ...`)도 들어갑니다. 예시 BP_DoorFrame: JSON 267KB → MD 6KB.
- **`.json` (전체)**: 모든 그래프의 모든 노드(클래스, 제목, 위치, 주석, 비활성 여부)와 모든 핀(타입, 기본값, 숨김, 분할, 연결), 함수 시그니처·플래그·지역 변수, 변수, 컴포넌트, 디스패처, 인터페이스 그래프, 타임라인, 위젯 트리. 의사코드에서 줄인 세부가 필요할 때 봅니다.
- `bp add`는 `Content\...\BP_X.uasset`, `/Game/.../BP_X`(오브젝트 경로도 됨), 폴더(`Content\UI` 또는 `/Game/UI/`: 그 아래 블루프린트 전부), 플러그인 `Content` 경로를 받습니다. 목록은 `__dwookit/bp-track.txt`입니다.
- `bp export`는 추적 대상만 로드하므로 빠릅니다(에디터 시작 포함 10~30초). `analyze`도 추적 목록이 있으면 같은 에디터 실행에서 함께 갱신합니다. 추적을 그만둔 블루프린트의 폴더는 다음 export 때 지웁니다.
- `bp list`는 `.uasset`이 export 뒤 바뀐 블루프린트를 표시합니다.
- **큰 블루프린트는 나눕니다.** 의사코드가 150줄을 넘으면 `<이름>.md`는 개요(변수, 컴포넌트, 기본값, 위젯)와 **부분 표**만 담고, 이벤트·함수는 `parts/on_X.md`, `parts/fn_X.md`로 나뉩니다. 표에는 부분마다 줄 수, 쓰는 변수, 호출하는 자체 함수·이벤트가 있어 필요한 부분만 고를 수 있습니다. 부분 파일은 그것만 읽어도 되도록 쓰는 변수·컴포넌트를 타입과 함께 적습니다. `goto`로 이어진 이벤트(같은 타임라인을 여닫는 Open/Close 등)는 한 부분에 두고, 몇 줄짜리 이벤트들은 30줄까지 한 부분으로 묶습니다.

### DwoLens 쓰는 순서

| 알고 싶은 것 | 보는 곳 |
|---|---|
| 결과를 자동으로 쌓기 | **Live Lens**: 에디터를 쓰는 동안 수집, 닫으면 백그라운드 분석 |
| 실행 중인 게임의 값 | **실행 중 관찰** (DwoStateMonitor 플러그인) |
| 이 게임이 어떤 순서로 돌아가나 (전체 지도) | **게임 흐름**: 맵 → 게임 모드 → 컨트롤러·폰·HUD → 그들이 만들고 가지고 쓰는 객체 |
| 이 객체(클래스)가 무엇을 하나 | **아키텍처 › 기능**: 진입점별 기능과 그 전개 (게임 흐름의 객체에서 `기능 보기`) |
| 이 파일을 깊게 이해하기 | **파일 분석**: 구성 → 행동 → 흐름 → 논리, C++ 본문까지 (게임 흐름의 객체에서 `파일 분석`) |
| 구조 문제 | 문제, 아키텍처 › 품질 점검 (순환 참조, 하드 참조 체인, 미사용 에셋, 이름 규칙) |
| 깨지는지 | 검증(`verify`), 맵 스모크(`smoke`), 로그(`logs`) |
| 성능·용량 | 성능·용량(`perf`, `sizes`, `trace`) |

### Live Lens (에디터를 쓰는 동안 자동 수집)

에디터를 닫고 `analyze`를 돌리는 대신, **에디터를 쓰는 동안 DwoLens가 원본을 모으고 에디터를 닫으면 백그라운드에서 분석해 결과를 쌓습니다.** 창의 **DwoLens › 대시보드 › Live Lens**에서 켜거나 명령줄로 켭니다.

```bat
__dwookit.bat live on [--timing changes|exit|save] [--keep latest|history|snapshot] [--collect structure,logs,pie,work]
__dwookit.bat live off | status | rebuild
```

- 수집 시점: `changes`(기본, 바뀐 것만 종료 때, 종료 지연 거의 없음) / `exit`(종료 때 전체) / `save`(저장할 때마다).
- 쌓는 방식: `latest` / `history`(기본, 결과와 세션별 변경 기록) / `snapshot`(원본까지 전부).
- 모을 것: 구조, 세션 로그·오류, PIE 실행 기록(실제로 존재한 클래스 → 게임 흐름에 "플레이에서 확인"), 작업 기록(연 에셋, 저장한 에셋).
- 결과는 분석 실행과 같은 형식이라 모든 화면이 그대로 씁니다. 세션별 변경은 실행의 `out/changes.json`과 `__dwookit/live/history.jsonl`에 있습니다.
- `.uproject`는 바꾸지 않습니다(설치된 플러그인 사본만 켬). 자세한 구조, 비슷한 도구와의 비교, DwoStateMonitor와의 관계는 [docs/LIVE-LENS.md](docs/LIVE-LENS.md).

### 게임 흐름 (프로젝트 전체 지도)

`analyze`가 `out/flow.json`을 만들고, 창의 **DwoLens › 게임 흐름**과 리포트의 `게임 흐름` 절(맵마다 다이어그램)에 그립니다. 위에서 아래로 따라갑니다.

| 연결 | 근거 |
|---|---|
| 맵 → 게임 모드 | 맵의 World Settings(맵이 참조하는 GameMode), 없으면 프로젝트 기본값(`GlobalDefaultGameMode`) |
| 맵 → 배치된 객체 | 맵이 참조하는 블루프린트 |
| 게임 모드 → 플레이어 폰·컨트롤러·HUD·게임 상태·플레이어 상태 | 클래스 기본값(`DefaultPawnClass` ...). C++ 생성자(`DefaultPawnClass = ...`, `ConstructorHelpers::FClassFinder`)도 읽고, 블루프린트 자식에게 이어 줍니다 |
| 컨트롤러 → 폰 (조종), 컨트롤러 → HUD | 같은 게임 모드가 정한 짝 |
| 생성 · 만들기 · 가짐 | SpawnActor·CreateWidget·ConstructObject·AddComponent 노드에서 고른 클래스, 컴포넌트, C++의 `SpawnActor<>`·`CreateWidget<>`·`NewObject<>`·`CreateDefaultSubobject<>` |
| 사용 | 클래스를 값으로 가진 기본값(`EnemyClass=BP_CombatEnemy`, `AIControllerClass` ...) |
| 호출 · 형변환 · 참조 (켜야 보임) | 함수 호출, Cast To, 변수·에셋 참조 |

- 왼쪽에서 시작점(레벨, 게임 모드, 게임 인스턴스)과 깊이를 고릅니다. 노드는 객체의 역할(캐릭터, 플레이어 컨트롤러, UI 위젯 ...)과 하는 일(점프, 공격 ...)을 보여 줍니다. 더블클릭하면 그 객체에서 다시 시작합니다.
- 오른쪽에서 그 객체가 무엇을 만들고 쓰는지, 누가 그 객체를 만들고 쓰는지(근거 포함)를 보고, `기능 보기`(아키텍처 › 기능)나 `파일 분석`(추적 중이 아니면 `추적하고 파일 분석`)으로 들어갑니다.
- C++ 부모가 하는 일은 블루프린트 자식에게 이어 그립니다. 어떤 시작점에서도 이어지지 않는 클래스는 `흐름에서 안 보이는 클래스`에 역할별로 모읍니다(데이터·리플렉션으로 쓰이거나, 서브시스템처럼 자동으로 생기거나, 쓰이지 않는 것).
- Lyra처럼 경험(Experience) 데이터로 폰·어빌리티를 정하는 프로젝트는 데이터 에셋 안의 연결까지는 아직 따라가지 않습니다.

### 파일 분석 (사람이 읽는 설명)

추적 중인 블루프린트마다 **구성 → 행동 → 흐름 → 논리** 순서로 읽는 설명을 만듭니다. dwookit 창의 **DwoLens › 파일 분석** 화면과 HTML(`__dwookit/blueprints/index.html`, 블루프린트마다 `<이름>.html`)로 봅니다. `bp export`와 `analyze`가 함께 만듭니다.

| 단계 | 내용 |
|---|---|
| 요약 | 한 문장: 무엇을 바탕으로 한 무엇인지(컴포넌트 역할로), 주요 행동 몇 가지 |
| 구성 | 컴포넌트(역할: 감지·충돌 영역, 보이는 모양, 카메라 ...), 설정값(에디터에서 바꾸는 값), 상태(실행 중 바뀌는 값과 그것을 바꾸는 행동), 신호, 타임라인, 위젯, 바꾼 기본값 |
| 행동 | 진입점마다 하나: 언제 실행되는지("다른 액터가 겹치기 시작하면", "C++ ACombatDamageableBox::HandleDeath에서 부르면"), 작성자 주석, 바꾸는 값, 이어서 실행하는 것, 부르는 곳 |
| 흐름 | 단계 트리("Door2이(가) 있으면 → Door2 위치 옮기기"). 자체 함수·커스텀 이벤트, 추적 중인 다른 블루프린트로 이어지는 호출은 그 안으로 펼치고, 프로젝트 C++ 함수는 `Source/`의 본문 코드를 보여 줍니다. 이 행동을 부르는 C++ 함수의 코드도 볼 수 있습니다 |
| 논리 | 단계마다 그 단계가 나온 의사코드 줄(마우스를 올리거나 `단계마다 의사코드 보기`) |

애님 블루프린트는 포즈 트리(`pose = ControlRig(... ApplyAdditive(...))`), 상태 머신의 상태·전이(`transition Idle -> Start`), 전이 조건(`can enter = HasAcceleration ...`)으로 나옵니다.

설명은 규칙 기반입니다(LLM·네트워크 없음). 더 자연스러운 문장이 필요하면 화면 오른쪽 **요청 문장 복사**를 Claude Code나 Copilot에 붙여 넣으세요. 에이전트가 `<이름>.explain-prompt.md`대로 `<이름>.agent.json`을 쓰면, 그 설명이 화면과 HTML에 함께 표시됩니다.

추적 대상은 **파일 분석 › 목록에서 고르기**에서 프로젝트 블루프린트 트리로 고릅니다(명령줄의 `bp add`와 같은 `bp-track.txt`를 씁니다). 후보 목록은 에디터 없이 `.uasset` 헤더에서 읽습니다(`tools/lens_targets.py`, Lyra 506개 약 2초).

### 네이티브 C++ 마이그레이션 (`native`, 개발 중)

블루프린트(액터·컴포넌트, 위젯, 애니메이션 블루프린트)의 구성 요소, 값, 노드 로직을 C++ 부모 클래스로 옮기고, 원래 블루프린트를 그 클래스를 상속하도록 바꿉니다(rebase). 에셋, 참조, 레벨에 놓인 인스턴스, 옮기지 못한 로직은 블루프린트에 그대로 남아 계속 동작합니다.

```bat
__dwookit.bat native plan     /Game/Props/BP_JumpPad        :: 무엇이 C++로 가고 무엇이 왜 남는지
__dwookit.bat native generate /Game/Props/BP_JumpPad        :: Source\<모듈>\DwoNative\BP_JumpPad\ + Build.cs 모듈 추가(백업)
__dwookit.bat compile                                        :: 에디터 대상 빌드 (에디터 닫고)
__dwookit.bat native rebase   /Game/Props/BP_JumpPad --dry-run   :: 검증만
__dwookit.bat native rebase   /Game/Props/BP_JumpPad        :: 백업 후 상속 전환, 검증 통과 시에만 저장
__dwookit.bat native verify   /Game/Props/BP_JumpPad        :: 동작 비교: 원본과 마이그레이션본을 같은 시나리오로 실행
__dwookit.bat native verify   /Game/Props/BP_Box --frames 220 --no-probe --call OnBoxDestroyed@10   :: 부모 C++가 부르는 이벤트를 10프레임에 실행
__dwookit.bat native verify   /Game/Props/BP_Door --probe-offset 0:200:100                         :: 프로브가 떨어질 위치(액터 기준)
__dwookit.bat native restore  /Game/Props/BP_JumpPad        :: 마지막 백업으로 되돌리기
```

정확도 원칙:
- **정확하거나, 옮기지 않거나**: 함수·이벤트는 안의 노드가 모두 같은 의미의 C++로 바뀔 때만 옮깁니다. 노드 하나라도 모르면 그 함수 전체가 블루프린트에 남고, 리포트(`<이름>.native.md`)에 이유와 노드를 적습니다.
- **같은 이름**: 변수와 컴포넌트는 블루프린트 이름 그대로 C++ 멤버가 됩니다(참조, 레벨 인스턴스, 저장된 값이 그대로 연결). 식별자가 될 수 없는 이름(공백 등)은 바꾼 이름과 원래 이름(DisplayName)을 기록합니다.
- **블루프린트 의미 그대로**: 대상이 None인 호출은 건너뛰고 출력은 기본값(블루프린트와 같음, C++에서 크래시 없음). 이벤트 그래프의 임시 값은 멤버로(블루프린트의 지속 프레임), 순수 노드는 사용할 때마다 다시 계산, 리터럴은 블루프린트 컴파일러와 같은 방식으로 해석(`FDefaultValueHelper` 다음 `ImportText`), 엔진 이벤트는 엔진이 부르는 순서에 맞춘 네이티브 오버라이드(`BeginPlay`, `Tick`, `NativeConstruct`, `NativeUpdateAnimation` …).
- **정확한 시그니처**: 내보내기(`bp export`)가 호출하는 함수마다 C++ 매개변수 타입·순서·접근 수준, 헤더 경로, 열거형 C++ 표기를 기록하므로 추측하지 않습니다. 블루프린트 전용(CustomThunk, BlueprintInternalUseOnly) 함수는 옮기지 않습니다. 부모의 private 멤버는 알려진 getter로(`CharacterMovement` → `GetCharacterMovement()`), 없으면 블루프린트처럼 리플렉션으로 읽습니다(`AllowPrivateAccess`).
- **값은 블루프린트에**: 컴포넌트 템플릿 값, 에셋 참조, 변수 기본값은 블루프린트의 기본값으로 남깁니다(rebase가 옮기고 검증). C++ 초기값은 단순한 값만 같은 값으로 적습니다. 코드가 쓰는 에셋은 숨은 UPROPERTY로 만들어 하드 참조(쿠킹 포함)를 유지합니다.

안정성(rebase, DwoLensInspector commandlet):
1. 블루프린트 클래스 기본값과 컴포넌트 템플릿의 모든 값을 텍스트로 기록합니다.
2. 옮긴 변수·컴포넌트·함수·이벤트·디스패처를 블루프린트에서 빼고, 그것을 쓰던 노드는 이름으로 C++ 멤버에 다시 연결합니다. 엔진의 변수 삭제 함수는 그 변수를 쓰는 노드까지 지우므로 쓰지 않습니다.
3. 부모 클래스를 생성된 클래스로 바꾸고, 옮긴 컴포넌트에 붙어 있던 남은 컴포넌트는 네이티브 컴포넌트에 다시 붙입니다.
4. 기록한 값을 새 기본값과 네이티브 컴포넌트에 되돌려 씁니다.
5. 컴파일 오류가 없고, 다시 기록한 값이 처음과 하나도 다르지 않을 때만 저장합니다. 실패하면 저장하지 않고 백업을 되돌립니다. 결과는 `__dwookit/native/<이름>.rebase.json`.

동작 비교(`native verify`, 액터 블루프린트):
- 같은 시나리오를 두 번 실행합니다. 한 번은 전환한 에셋으로, 한 번은 전환 전 백업을 잠시 제자리에 두고(끝나면 전환한 에셋을 반드시 되돌림).
- 시나리오: 엔진 테스트 월드(게임 인스턴스, 프로젝트 게임 모드, 고정 시간 간격)에 블루프린트를 놓고 90프레임 실행합니다. 20프레임에 프로브 캐릭터를 위에서 쓸어내리듯 내려 겹침, 캐스트, 발사 같은 상호작용을 일으킵니다.
- 기록: 10프레임마다 액터와 각 컴포넌트의 편집 가능·블루프린트에서 보이는 모든 값과 월드 변환, 프로브의 위치·속도·이동 모드·발사 요청.
- 비교: 숫자는 부동소수 오차(1e-3)까지 같으면 같음으로 봅니다. 스폰마다 바뀌는 GUID와 임시 월드 이름은 제외하고, C++에만 있는 멤버는 따로 나열합니다. 결과는 `__dwookit/native/<이름>.behaviour.md`.
- 같은 원본을 두 번 돌린 결과가 같음(결정적)을 확인했습니다. 생성된 C++에 일부러 넣은 오류(발사 속도 절반)는 바로 "다름"으로 잡힙니다.
- `--call 함수@프레임`: 부모 C++가 게임 중에 부르는 이벤트(BlueprintImplementableEvent 등)를 기본 인자로 실행합니다. `--probe-offset X:Y:Z`: 프로브가 떨어질 위치.
- 한계: 액터만 대상입니다(위젯·애니메이션은 아직). 무작위 값이나 실제 시간을 쓰는 로직은 차이로 나올 수 있습니다. 시나리오가 건드리지 않는 경로(특정 입력, 다른 액터와의 상호작용)는 검증되지 않습니다.

현재 범위: 액터·컴포넌트·위젯·애니메이션 블루프린트의 변수, 컴포넌트(SCS), BindWidget 위젯, 이벤트 디스패처, 함수, 엔진 이벤트·커스텀 이벤트·컴포넌트 바운드 이벤트, Construction Script(`OnConstruction`), 분기·시퀀스·스위치·캐스트·For/ForEach/While·IsValid·FlipFlop·DoOnce, 배열 함수(Length, Get, Add, Contains …).

엔진과 같은 방식으로 옮기는 것:
- **지연(latent) 액션**(Delay, RetriggerableDelay, MoveComponentTo …): 호출은 그대로 두고, Completed 뒤의 흐름을 별도 UFUNCTION(`Latent_<노드>`)으로 만들어 latent 액션 매니저가 부르게 합니다. UUID는 블루프린트 컴파일러와 같은 노드 GUID 해시라, 대기 중 재호출 무시 같은 동작이 같습니다. 이벤트 인자는 멤버로 복사해 나중에 읽어도 블루프린트 프레임처럼 남아 있습니다. `Move`/`Stop`처럼 실행 핀이 여러 개인 노드(ExpandEnumAsExecs)는 들어온 핀에 맞는 값을 넘깁니다. 루프 안의 지연은 블루프린트에 남습니다.
- **타임라인**: 블루프린트가 만들던 것(`UBlueprintGeneratedClass::CreateTimelineComponent`)과 같은 순서로 `OnConstruction`에서 컴포넌트를 만듭니다. 길이·자동 재생·반복·복제·틱 그룹·시간 팽창 무시, 커브 키(탄젠트까지 저장된 값 그대로), 이벤트 트랙, Update/Finished가 같습니다. 외부 커브 에셋은 하드 참조로 유지합니다. 타임라인을 재생하는 코드가 하나라도 블루프린트에 남으면 타임라인도 남습니다. rebase는 타임라인 템플릿과 트랙 속성을 C++ 멤버로 옮기고 이름을 다시 연결합니다.
- **입력 이벤트**(InputAction, InputKey, InputAxis, Enhanced Input): 엔진이 블루프린트 입력을 묶는 자리(폰 `SetupPlayerInputComponent`, 플레이어 컨트롤러 `SetupInputComponent`, 그 밖의 액터 `EnableInput`)에서 같은 규칙(부모 바인딩 덮어쓰기, 입력 소비, 일시정지 중 실행)으로 묶습니다. Key·ActionValue 같은 출력은 블루프린트의 임시 변수처럼 멤버에 둡니다.
- **부모의 BlueprintNativeEvent**: `_Implementation` 오버라이드로 옮깁니다(이벤트와 함수 그래프 모두).
- **부모의 BlueprintImplementableEvent**: C++에서는 오버라이드할 수 없으므로 본문을 `<이벤트>Native` 함수로 옮기고, 블루프린트에는 그 이벤트와 C++ 호출 노드 하나만 남깁니다(rebase가 연결). 부모 C++가 이벤트를 부르는 경로는 그대로입니다.

블루프린트에 남는 것: 복제 함수, 블루프린트 매크로·구조체·열거형을 쓰는 코드, AddComponent 노드, 터치·벡터 축 입력, AnimGraph(상태 머신), 비헤이비어 트리 노드 블루프린트(계획 중). 비헤이비어 트리 에셋 자체는 데이터라 옮길 대상이 아닙니다.

rebase 안전장치: 블루프린트에 남는 실행 흐름이 C++로 옮긴 노드로 이어지면(그 뒤 코드가 조용히 끊기므로) 저장하지 않습니다.

### 에이전트 안내 (`agent`)

연결하면 Claude Code와 GitHub Copilot이 dwookit가 만든 자료를 알고 시작하도록 안내 파일을 둡니다. 모두 git exclude에 들어가 `git status`에 보이지 않고, `uninstall`이 지웁니다.

| 파일 | 누가 읽나 | 내용 |
|---|---|---|
| `__dwookit/context/AGENT.md` | 둘 다 | 추적 블루프린트 현황(바뀐 것 표시), 읽는 순서(index → 개요 → parts → json), 큰 작업은 싼 서브에이전트(Haiku 등)에 부분별 요약을 맡기고 그 요약으로 설계할 것, 결과물은 C++(BlueprintCallable 등) 또는 정해진 형식의 에디터 체크리스트, "설명 없이 변경 목록만", 확인 명령. 연결·update·analyze·bp export 때 다시 씀 |
| `CLAUDE.local.md` | Claude Code | 관리 블록 하나: `@__dwookit/context/AGENT.md`, `@.github/instructions/project.instructions.md`. 이미 있는 파일은 블록만 넣고 빼며(인코딩·줄바꿈 유지), git에 커밋된 파일은 건드리지 않음 |
| `.github/instructions/dwookit.instructions.md` | Copilot (VS Code) | `applyTo: "**"`, AGENT.md를 먼저 읽으라는 안내 |
| `.github/instructions/project.instructions.md` | 둘 다 | **사용자 지침**. 빈 파일(머리말만)로 만들어 두고 다시 쓰지 않습니다. 네이밍, 폴더 구조, "Tick 금지" 같은 규칙을 적으면 AGENT.md보다 우선합니다. `uninstall`은 내용이 있으면 `%LOCALAPPDATA%\dwookit\uninstalled\<프로젝트-시각>\instructions\`에 백업한 뒤 지웁니다. 팀과 공유하려면 `git add -f`로 커밋하세요(커밋된 파일은 지우지 않음) |

`agent off`는 안내 파일을 지우고 이후에도 쓰지 않습니다(사용자 지침은 남김). `agent on`으로 되돌립니다. `agent status`는 파일별 상태를 보여 줍니다.

### 지침서·스킬 library (창: 홈 › AI 지침·스킬)

팀 규칙 같은 **지침서**(에이전트가 항상 읽는 규칙)와 **스킬**(필요할 때 불러 쓰는 작업 절차)을 dwookit 폴더의 `library/`에 올려 두고, 프로젝트마다 골라 적용합니다. 대상 에이전트는 **Claude Code와 GitHub Copilot 두 가지뿐**이며, 프로젝트마다 둘 중 무엇에 적용할지 고릅니다. `library/`는 git에서 제외되어 dwookit 저장소에 섞이지 않습니다.

| 단계 | 창 | 명령 |
|---|---|---|
| 올리기 | 지침서 올리기(.md, .txt), 스킬 폴더 올리기(SKILL.md가 있는 폴더), ZIP·SKILL.md 올리기 | `dwookit.bat library add-instruction <파일>`, `library add-skill <폴더 \| SKILL.md \| zip>` |
| 연결 전 고르기 | 체크한 항목과 에이전트가 이 PC의 기본값으로 저장됨 | `library defaults --instructions a,b --skills x --targets claude,copilot` |
| 연결하며 적용 | 연결하고 적용 (도구 설치·연결만 하기도 기본값을 적용) | `__dwookit.bat connect --instructions a,b --skills x --targets claude,copilot` |
| 연결 후 바꾸기 | 체크를 바꾸고 이 프로젝트에 적용 | `__dwookit.bat agent use ...` (뺀 항목은 프로젝트에서 지움) |
| 연결 해제 | 연결 해제 | `uninstall`이 dwookit가 넣은 지침서·스킬을 모두 지움. library는 남음 |

- 올릴 때 검사: 지침서는 256 KB 이하의 Markdown·텍스트. 스킬 이름은 소문자·숫자·하이픈(64자 이하)으로 바꾸고 폴더 이름과 맞춥니다(Copilot은 다르면 읽지 않음). SKILL.md에 `name`·`description`이 없으면 채웁니다(두 에이전트 모두 description으로 스킬을 고름). 스킬은 파일 200개·20 MB 이하, ZIP 안의 `..`·절대 경로는 거부합니다.
- 프로젝트에 넣는 곳: Claude Code는 `CLAUDE.local.md` 관리 블록에 `@__dwookit/context/instructions/<이름>.md`를 더하고 스킬을 `.claude/skills/<이름>/`에, Copilot은 `.github/instructions/dwookit-<이름>.instructions.md`(지침서 앞머리의 `applyTo`, 없으면 `"**"`)와 `.github/skills/<이름>/`에 둡니다.
- 같은 이름의 폴더·파일이 이미 있고 dwookit 표식(`.dwookit-managed`, 관리 주석)이 없거나 git에 커밋되어 있으면 건드리지 않고 알려 줍니다. 넣은 경로는 `__dwookit/context/installed.json`에 적어 git exclude에 더하므로 `git status`에 보이지 않습니다.
- `DWOOKIT_LIBRARY` 환경 변수로 library 위치를 바꿀 수 있습니다(팀 공유 폴더 등).

전체 목록과 계획 중인 도구(Insights 내보내기, PerfReportTool 리포트, 정적 분석 등)는 `tools/catalog.json` 또는 `__dwookit.bat tools`로 확인합니다. 카탈로그 항목은 `tool` 값(lens, statemonitor, canvas, forge)으로 묶입니다.

## DwoBuilder (Jenkins, 폐쇄망 지원)

**로컬 빌드**: Jenkins 없이, DwoBuilder를 설치하지 않아도 씁니다. 창에서는 DwoBuilder > 로컬 빌드.

| 명령 | 단계 | 내용 |
|---|---|---|
| `compile` | B1 컴파일 | `Engine/Build/BatchFiles/Build.bat <프로젝트>Editor Win64 Development`. 오류·경고를 파일·줄 단위로 집계. Blueprint 전용 프로젝트는 건너뜀(통과). fail: 종료 코드 ≠ 0 또는 오류 |
| `package` | P1 패키지 | `RunUAT BuildCookRun -cook -stage -pak -archive` (C++ 프로젝트는 `-build`). 결과물은 실행 폴더의 `build/`. fail: `BUILD SUCCESSFUL` 없음, warn: 쿡 오류가 있지만 결과물 생성 |

**자동 빌드**: 
내 PC에서만 쓰는 Jenkins를 설치해, git 이벤트나 외부 요청이 들어오면 **C++ 컴파일 → DwoLens 검사 → 패키지 빌드**를 자동으로 실행합니다. DwoStateMonitor처럼 설치·제거하지만, Jenkins는 프로젝트가 아니라 PC에 하나(`%LOCALAPPDATA%\dwookit\ci`)만 설치되고 프로젝트마다 작업(job)이 하나씩 생깁니다. 프로젝트 폴더에는 아무것도 추가되지 않습니다(git 훅은 `.git/hooks`에 들어갑니다). 빌드 기록이 커질 수 있으므로 다른 디스크에 두려면 사용자 환경 변수 `DWOOKIT_CI_HOME`(예: `setx DWOOKIT_CI_HOME D:\dwookit-ci`)을 설정한 뒤 설치합니다.

| 단계 | 하는 일 |
|---|---|
| 1. 번들 만들기 (인터넷 PC, 한 번) | `dwookit.bat ci-bundle` → `bundles/dwookit-ci-bundle-<버전>.zip`. Jenkins LTS(war, SHA-256 확인), Eclipse Temurin 21 JRE(체크섬 확인), 플러그인(`workflow-aggregator`, `junit`과 의존성)을 묶고 파일마다 SHA-256을 `manifest.json`에 기록 |
| 2. 설치 (폐쇄망 PC) | 번들을 `bundles/`에 두거나 `dwookit.bat ci-install --bundle <zip>`. 체크섬을 확인한 뒤 설치하고 시작합니다. 네트워크에 접속하지 않고 관리자 권한도 필요 없습니다. 창에서는 런처의 **PC 구성요소 > Jenkins (DwoBuilder)** `설치`. 프로젝트에 DwoBuilder 도구를 설치할 때 Jenkins가 없으면 자동으로 함께 설치합니다 |
| 3. 프로젝트에 설치 | 런처의 도구 목록에서 **DwoBuilder** `설치` (`__dwookit.bat tool install builder`, 예전 명령 `ci-job`). Jenkins 파이프라인 작업과 git 훅(`post-commit`, `post-merge`, `pre-push`, `reference-transaction`)을 설치합니다. 기존 훅(git-lfs 등)은 `<훅>.dwookit-orig`로 보존되어 먼저 실행됩니다. 프로젝트 이름이 한글이거나 같은 프로젝트를 두 곳에 받아 둔 경우 작업 이름 뒤에 경로 기반 8자리 식별자가 붙어 서로 겹치지 않습니다 |

**트리거** (기본값, `ci.triggers`에서 변경)

| 이벤트 | 기본 브랜치 | 단계 |
|---|---|---|
| 내가 원격으로 push | `develop` | 컴파일 + DwoLens + 패키지 |
| merge (현재 브랜치) | `develop` | 컴파일 + DwoLens |
| 브랜치 생성 | `feature/*` | 컴파일 + DwoLens |
| 커밋 | `develop`, `feature/*` | DwoLens |
| 외부 명령·HTTP | - | 컴파일 + DwoLens + 패키지 |

- push는 내 PC의 `pre-push` 기록과 원격 추적 브랜치 갱신이 일치할 때만 셉니다. 다른 사람의 push를 `fetch`로 받아 온 것은 빌드하지 않습니다.
- 원격 서버에서 PR을 merge한 것은 내 PC에서 감지할 수 없습니다. 이때는 `ci-trigger`나 HTTP로 빌드를 요청합니다.
- 외부 이벤트 주입: `__dwookit.bat ci-trigger --reason "QA 요청" --stages compile,lens`(단계는 쉼표·공백·`+`로 구분), 또는 HTTP `POST http://127.0.0.1:8098/job/<작업>/buildWithParameters?REASON=...&COMPILE=true&LENS=true&PACKAGE=false` (기본 인증: `secrets.json`의 사용자와 API 토큰).
- 같은 작업이 이미 대기열에 있으면 새 요청은 그 빌드에 합쳐집니다. 한 번에 한 빌드만 실행합니다.
- Jenkins가 꺼져 있으면 훅이나 `ci-trigger`가 자동으로 시작합니다. 훅이 무엇을 판단했는지는 `__dwookit/ci/hook.log`에 남습니다.

**보관 개수**: Jenkins는 최근 `ci.keepBuilds`(기본 30)개 빌드만 남기고, 넘으면 가장 오래된 빌드부터 하나씩 지웁니다(첨부 결과는 `ci.keepArtifacts`개). Jenkins 기본값과 달리 마지막 성공 빌드도 예외 없이 이 개수에 포함됩니다. 패키지 결과물은 DwoLens 실행 기록에 최근 `retention.packages`(기본 3)개만 남습니다.

**결과 보기**: 런처에서 DwoBuilder를 `열기`하면 나오는 화면에서 빌드 목록·결과·실시간 로그를 보고, `지금 빌드`로 단계를 골라 실행합니다. 같은 내용을 Jenkins 화면(`http://127.0.0.1:8098/`, 계정은 `계정 복사`)에서도 볼 수 있고, 각 빌드에는 JUnit 추세와 DwoLens 리포트가 첨부됩니다. DwoLens 검사가 fail이면 빌드는 UNSTABLE, 컴파일·패키지 실패는 FAILURE입니다.

**다른 PC에 설치할 때**: 따로 설치하거나 설정할 것은 Python 3.10 이상(dwookit 공통 요구 사항)뿐입니다. Java·Jenkins·플러그인은 번들에 들어 있고, 관리자 권한·서비스 등록·방화벽 허용·인터넷이 필요 없습니다. Python이 없거나 Microsoft Store 바로 가기만 있으면 설치 전에 그렇게 알려 줍니다. 8098 포트를 다른 프로그램이 쓰고 있으면 `ci-install --port 8099`처럼 다른 포트로 설치합니다. 한국어 Windows(코드 페이지 949)에서 사용자 이름·dwookit 폴더·프로젝트 경로에 한글이나 공백이 있어도 동작합니다(Jenkins 단계는 경로를 스크립트에 넣지 않고 환경 변수로 넘깁니다).

**보안**: Jenkins는 `127.0.0.1`에서만 열리고(원격 에이전트 포트도 열지 않음), 설치할 때 만든 무작위 비밀번호의 계정으로만 접근합니다. 사용 통계 전송과 업데이트 사이트 접속은 꺼 둡니다(업데이트 사이트를 등록하지 않으므로 Jenkins가 외부로 연결하지 않습니다).

**제거**: `__dwookit.bat ci-job --remove`는 그 프로젝트의 작업과 훅만, `dwookit.bat ci-remove`는 Jenkins와 모든 작업·훅을 지웁니다(`--keep-data`면 빌드 기록을 `%LOCALAPPDATA%\dwookit\uninstalled`로 옮김). 프로젝트를 `uninstall`하면 그 프로젝트의 작업과 훅도 함께 지워집니다. 제거 후에는 Jenkins 프로세스·포트·서비스·예약 작업이나 프로젝트의 훅·CI 기록(`__dwookit/ci`)이 남지 않습니다.

## 설정

`config/default.json`이 DwoLens 기본값입니다. 프로젝트별로 바꾸려면 `__dwookit/config.json`에 같은 구조로 필요한 키만 적습니다.

```json
{
  "naming": { "forbidAssetsAtContentRoot": false },
  "verify": { "automationFilters": ["Project.", "MyGame."] },
  "perf": { "map": "/Game/Maps/L_Main", "budgetMs": 16.6 },
  "smoke": { "exclude": ["/Game/Developers/*", "/Game/Test/*"], "maxErrors": 3 },
  "retention": { "runsPerCheck": 20, "packages": 2 },
  "ci": { "keepBuilds": 50, "triggers": { "push": { "branches": ["develop", "release/*"] }, "commit": { "branches": [] } } }
}
```

## 구성

| 경로 | 역할 |
|---|---|
| `dwookit.bat` | 시작점: 폴더 등록(`DWOOKIT_HOME`), 창 열기, 끌어다 놓은 프로젝트 열기, 명령줄 실행 |
| `tools/project-launcher.bat` | 연결할 때 프로젝트에 `__dwookit.bat`으로 복사되는 원본. `home.txt`의 설치 폴더에서 `dwookit.ps1` 실행, 제거되면 스스로 삭제 |
| `tests/selftest.bat` | 개발용 전체 검증 |
| `tools/dwookit.ps1` | 명령 실행, 엔진 호출, 실행 기록 (`run.json`), dwo-Lens 1.x 이전 |
| `tools/forge.ps1`, `tools/lens.ps1`, `tools/legacy-project-launcher.bat` | dwo-Forge·dwo-Lens 1.x 실행 파일 호환 (dwookit.ps1로 넘김) |
| `tools/lens_analyze.py` | DwoLens: raw JSON → 통합 그래프, SQLite, 다이어그램, 게이트, HTML 리포트 |
| `tools/lens_quality.py` | DwoLens: 이름 규칙, 미사용 에셋, BP 복잡도, DwoStateMonitor 감지 |
| `tools/lens_logs.py` | DwoLens: UE 로그·크래시 파서 |
| `tools/lens_verify.py` | DwoLens: 검증·로그 결과 판정과 리포트 |
| `tools/lens_runtime.py` | DwoLens: 맵 부팅·성능 CSV·용량·트레이스 결과 판정과 리포트 |
| `tools/lens_build.py` | 빌드 검사: 컴파일·패키지 로그 → 오류 목록, 결과, 리포트 |
| `tools/lens_export.py` | 실행 결과 → JUnit, SARIF 2.1.0 (이전 실행 대비 new/unchanged) |
| `tools/ci.ps1`, `tools/dwookit_ci.py` | DwoBuilder: 오프라인 번들, Jenkins 설치·실행, 프로젝트 작업, git 훅과 트리거 판단, 보관 개수 |
| `tools/vendor/mermaid.min.js` | 리포트 다이어그램용 Mermaid 10.9.1 (MIT). 리포트가 인터넷 없이 그려지도록 실행 기록 폴더로 복사 |
| `tools/catalog.json` | 도구 레지스트리 (세 도구별로 묶음) |
| `gui/`, `tools/gui.ps1` | ImGui 제어판 (Dear ImGui 1.91.9b docking, nlohmann/json 3.11.3, MIT). DwoStateMonitor 뷰어, DwoCanvas 화면 포함 |
| `config/default.json` | DwoLens 규칙과 기준값 |
| `plugin/DwoLensInspector` | DwoLens 추출 Commandlet (읽기 전용, 패키지를 저장하지 않음) |
| `plugin/DwoStateMonitor` | DwoStateMonitor 런타임 플러그인 |
| `plugin/DwoCanvas`, `canvas/` | DwoCanvas 플러그인, 검사기(`canvas/python`), 어댑터, 설계 문서 |

플러그인은 엔진 버전과 소스 해시별로 `%LOCALAPPDATA%\dwookit\plugin-cache`에 한 번만 빌드되고 여러 프로젝트가 재사용합니다.

## 결과 파일 (`__dwookit/data/runs/<id>/`)

| 파일 | 내용 |
|---|---|
| `run.json` | 실행 ID, 검사, 단계별 결과·소요 시간, dwookit 버전 |
| `out/report.html` | 검사별 리포트 |
| `out/graph.json`, `out/index.db` | 심볼·관계 (status, evidence 포함) |
| `out/class_uml.puml` / `.mmd` | 클래스 다이어그램 |
| `out/features.json` | 클래스별 기능(진입점)과 각 기능의 단계 (`lens.features/1`, 아키텍처의 기능 보기와 리포트의 기능 섹션) |
| `out/class_uml.xmi` | UML 2 클래스 모델(XMI 2.1). Visual Paradigm의 `File > Import > XMI`(또는 `scripts\ImportXMI.bat -project <.vpp> -file <.xmi>`), Enterprise Architect, MagicDraw, Papyrus에서 가져옵니다. 클래스는 `/Game` 폴더, `C++/<모듈>`, `External/<모듈>` 패키지로 나뉘고 속성·함수·상속·참조가 모두 들어갑니다. 다이어그램은 가져온 뒤 패키지를 끌어다 놓아 만듭니다 |
| `out/findings.json`, `out/quality.json`, `out/statemonitor.json` | 분석 결과와 품질 점검 |
| `out/verify.json`, `out/logs.json` | 검증·로그 결과 |
| `out/smoke.json`, `out/perf.json` (+`perf.csv`), `out/sizes.json`, `out/trace.json` (+`trace.utrace`) | 맵 부팅·성능·용량·트레이스 결과 |
| `out/compile.json`, `out/package.json`, `raw/compile.log`, `raw/package.log`, `build/` | 컴파일·패키지 결과와 전체 로그, 패키지 결과물 |
| `out/junit.xml`, `out/results.sarif` | CI용 결과 (모든 검사) |
