# Live Lens

상태: 구현 (2026-11-24). DwoLens가 언리얼 에디터 안에서 원본 데이터를 모으고, 에디터를 닫으면 백그라운드에서 가공해 결과를 쌓습니다. 분석(`analyze`)을 따로 실행하거나 에디터를 닫고 기다릴 필요가 없습니다.

## 1. 흐름

```
에디터 실행 ──────────────────────────────────────────── 에디터 종료 ──── (에디터 프로세스 끝) ──────────────
DwoLensLiveSubsystem                                       OnShutdownPostPackagesSaved
  events.jsonl에 바로 기록 (5초마다, 비정상 종료에도 남음)     수집 시점에 따라 raw/ 수집
  저장·삭제·이름 변경·열기·BP 컴파일·맵 열기·PIE·로그 오류      session.json 기록
  ('저장할 때마다'면 저장한 BP를 바로 saves/NNNN/에)            live-ingest 시작 (숨김, 분리) ──▶ 에디터가 닫힐 때까지 대기
                                                                                                    lens_live.py merge   → live/base (전체 원본 캐시)
                                                                                                    lens_analyze.py      → data/runs/run-… (source: live)
                                                                                                    lens_live.py accumulate → changes.json, history.jsonl,
                                                                                                                              runtime.json, 보관 정리
                                                                                                    게임 흐름 다시 계산, 추적 BP 의사코드·설명, AGENT.md
                                                                                                    logs (세션 로그·크래시)
```

- 플러그인: `plugin/DwoLensInspector/Source/DwoLensInspector/Private/DwoLensLive.cpp` (`UDwoLensLiveSubsystem`, 에디터 서브시스템). 수집은 기존 commandlet(`UDwoLensInspectorCommandlet::Main`)을 에디터 안에서 그대로 호출하므로 결과 형식이 `analyze`와 같습니다. `-LensPackages=<파일>`(그 블루프린트만), `-LensSkipAssets`(에셋 목록은 종료 때) 인자가 추가되었습니다.
- 가공: `tools/lens_live.py`, `dwookit.ps1`의 `live`, `live-ingest`.
- 결과는 보통 분석 실행(run)과 같아서 대시보드, 아키텍처, 게임 흐름, 문제, 리포트가 그대로 보여 줍니다. 실행 기록에서 `collect (Live Lens, …)`로 구분됩니다.

## 2. 설정 (`__dwookit/live-lens.json`, 창: DwoLens › 대시보드 › Live Lens, 명령: `live on|off|status|config|rebuild`)

| 항목 | 값 | 설명 |
|---|---|---|
| 수집 시점 `timing` | `changes` (기본) | 세션 중에는 기록만, 종료할 때 바뀐 블루프린트만 수집. QA 프로젝트 실측 종료 지연 0.06초 |
| | `exit` | 종료할 때마다 전체 수집. 가장 확실, 종료 지연이 프로젝트 크기에 비례 (QA 4.2초, Lyra 규모는 수십 초 이상 예상) |
| | `save` | 블루프린트를 저장할 때마다 그것만 바로 수집(에셋 목록은 종료 때). 비정상 종료에도 남지만 저장마다 약간 느려짐 |
| 쌓는 방식 `keep` | `latest` | 최신 Live 실행 하나만 남김 |
| | `history` (기본) | 실행마다 결과와 변경 기록(`out/changes.json`, `live/history.jsonl`)을 남기고, 원본(raw)은 최신 것만 |
| | `snapshot` | 실행마다 원본까지 전부 남김 (용량 큼) |
| 모을 것 `collect` | `structure` | 클래스·블루프린트·에셋 참조 (게임 흐름·기능·파일 분석의 원본) |
| | `logs` | 로그 경고·오류(세션 중 기록) + 종료 후 `logs` 검사(새 오류·크래시) |
| | `pie` | PIE 실행: 맵, 시간, 실제로 존재한 프로젝트 클래스(액터·컴포넌트 수) → `live/runtime.json`, 게임 흐름 노드에 "플레이에서 확인" |
| | `work` | 연 에셋, 저장한 에셋, 열린 맵 |

- 첫 세션(캐시 없음)은 수집 시점과 관계없이 전체를 모읍니다. `live rebuild`는 캐시를 버려 다음 세션이 다시 전체를 모으게 합니다(캐시는 언제든 다시 만들 수 있는 데이터).
- 켜면 프로젝트에 설치된 플러그인 사본(`Plugins/__DwoLensInspector`, git 제외)의 `EnabledByDefault`만 바꿉니다. `.uproject`는 건드리지 않습니다. 에디터 전용 모듈이라 패키지 빌드에는 들어가지 않습니다.
- 에디터가 비정상 종료되면 다음 에디터 시작 때 그 세션을 `crashed`로 닫고, 저장했던 에셋은 이번 세션의 변경으로 넘겨 종료 때 수집합니다.

## 3. 비슷한 도구의 방식과 비교

조사 출처는 각 항목의 링크입니다. 확인하지 못한 내용은 "미확인"으로 적었습니다.

| 도구 | 수집 시점 | 처리 위치 | 캐시·이력 | 알려진 단점 |
|---|---|---|---|---|
| JetBrains Rider (Unreal) | 블루프린트 인덱싱 설정을 켜면 프로젝트 블루프린트를 읽음. 재인덱싱 방식은 미확인 ([문서](https://www.jetbrains.com/help/rider/2025.2/Unreal_Engine__Blueprints.html)) | IDE 프로세스 | 최신만 | 큰 .uasset에서 메모리·CPU 사용, 대형 프로젝트 캐시 재구축이 오래 걸린다는 사용자 보고 ([포럼](https://rider-support.jetbrains.com/hc/en-us/community/posts/360008627880/comments/360001772180)) |
| Visual Studio 2022 Unreal 통합 | 17.7부터 블루프린트 참조에 플러그인 불필요. 플러그인은 `-run=VisualStudioTools -output=`로 파일에 내보내는 commandlet 방식 ([문서](https://learn.microsoft.com/en-us/visualstudio/gamedev/unreal/get-started/vs-tools-unreal-install), [vc-ue-extensions](https://github.com/microsoft/vc-ue-extensions)) | 별도 실행 | 미확인 | 갱신 방식 미확인 |
| Unreal Asset Registry / DDC | 에디터 시작 시 비동기 수집, 저장 때 태그 기록, 파일 변경 반영 ([문서](https://dev.epicgames.com/documentation/en-us/unreal-engine/asset-registry-in-unreal-engine)) | 에디터 안(백그라운드) | 최신만, 버려도 다시 만들 수 있는 캐시 ([DDC](https://dev.epicgames.com/documentation/en-us/unreal-engine/using-derived-data-cache-in-unreal-engine)) | 느린 OnAssetAdded 처리기가 수집을 늦춤, 손상된 캐시는 지워야 함 |
| Unity AssetDatabase | 에디터에 포커스가 돌아올 때나 명시적으로 새로고침 시 변경 파일 검사·임포트 ([문서](https://docs.unity.com/en-us/engine/6000.0/manual/assets-and-media/asset-database/refreshing)) | 에디터 | 최신 결과물만 | 가져오는 중 파일이 바뀌면 다시 시작 |
| SonarLint / SonarQube | 편집기에서 가벼운 분석, CI에서 무거운 전체·PR 분석 ([문서](https://docs.sonarsource.com/sonarqube-server/10.7/user-guide/sonarlint-connected-mode.md)) | 편집기 + 서버 | 서버가 브랜치·PR 결과 보관 | 편집기에서는 일부 규칙만 |
| PVS-Studio (Unreal) | UBT로 첫 실행 전체, 이후 바뀐 파일만 ([문서](https://pvs-studio.com/en/docs/manual/0043/)) | 빌드 시 | 미확인 | 미확인 |
| clangd 백그라운드 인덱스 | 파일별 조각(*.idx), 다시 시작해도 바뀌지 않은 것은 재사용 ([문서](https://clangd.llvm.org/design/indexing)) | 별도 스레드 | 최신만, 이력 없음 | 대형 프로젝트 첫 인덱싱 수 시간·수 GB |
| Unreal 크래시 리포트 | 세션 중 기록, 크래시 후 별도 프로세스(CrashReportClient)가 처리 ([문서](https://dev.epicgames.com/documentation/en-us/unreal-engine/crash-reporting-in-unreal-engine)) | 별도 프로세스 | 보고서 단위 | - |

Live Lens에 반영한 점:
- **에디터 스레드에서는 기록만 하고 무거운 처리는 종료 뒤 별도 프로세스에서** (Asset Registry의 느린 처리기 경고, Rider 사례, 크래시 리포터 구조). 그래서 기본 수집 시점을 `changes`로 했습니다.
- **첫 실행 전체 + 이후 바뀐 것만** (PVS-Studio, clangd). 캐시가 없으면 전체, 있으면 부분 수집입니다.
- **캐시는 언제든 버리고 다시 만들 수 있게** (DDC, Asset Registry 캐시 손상 사례). `live rebuild`가 그 역할입니다.
- **세션별 변경 이력**은 조사한 도구들에서 찾지 못한 부분입니다(대부분 최신만 유지). 프로젝트가 어떻게 바뀌어 왔는지 보려는 목적이라 기본값으로 두되, 원본은 최신만 남겨 용량을 줄였습니다(`history`). 완전한 스냅샷은 선택(`snapshot`)입니다.
- **저장마다 수집**(`save`)은 Asset Registry가 저장 때 태그를 쓰는 방식과 비슷하지만, 블루프린트를 다시 읽는 일이 에디터 스레드에서 일어나므로 선택 사항으로 두고 해당 블루프린트만 읽도록 제한했습니다.

## 4. DwoStateMonitor와 DwoLens

| | DwoLens | DwoStateMonitor |
|---|---|---|
| 보는 것 | 프로젝트 구조, 기능, 흐름, 품질, 로그 (정적 + Live Lens 세션 기록) | 실행 중인 게임의 값 (필드 단위, 시간에 따라) |
| 모듈 | 에디터 전용 (`Editor`) | 런타임 (`Runtime`), 패키지 게임에도 넣을 수 있음 (`--runtime`) |
| 동작 시점 | 에디터 안(Live Lens), commandlet | PIE·게임 실행 중 TCP로 창에 전송 |

판단: **플러그인은 분리 유지, 제품과 화면은 DwoLens로 편입.**
- 플러그인을 합치면 게임(런타임)에 에디터 전용 코드가 들어가거나, 에디터 전용 플러그인이 게임에서 동작해야 합니다. 둘 다 패키지 빌드 안전성을 해칩니다.
- 사용자가 보기에는 같은 프로젝트를 다른 각도에서 보는 도구라서, 창에서는 **DwoLens › 실행 중 관찰**로 옮겼습니다(사이드바의 별도 도구 항목 제거, 연결 수 표시는 이 화면 항목에 표시). DwoStateMonitor 플러그인 설치·업데이트는 런처의 도구 목록에서 그대로 합니다.
- 연결: 게임 흐름에서 객체를 골라 **실행 중 관찰에 추가**하면 DwoStateMonitor 관찰 목록에 들어가고, Live Lens의 PIE 기록(실제로 존재한 클래스)은 게임 흐름 노드에 "플레이에서 확인"으로 표시됩니다.

## 5. 검증 (QA 프로젝트, UE 5.7.4)

| 세션 | 설정 | 한 일 | 결과 |
|---|---|---|---|
| 1 | changes (첫 세션) | 에디터 시작·종료 | 전체 수집 4.2초, 백그라운드 분석 run 생성, logs run |
| 2 | changes | BP 저장, 에셋 열기, PIE 27초 | 종료 시 1개 패키지 수집 0.06초, PIE 클래스 3개 기록, 이전 run의 raw 삭제(history) |
| 3 | save, latest | BP 3번 저장 | 저장마다 즉시 수집, 종료 0.04초, 이전 Live run 모두 정리 |

자동화: `tests/test_tools.py`의 `LiveLensTests`(병합, 변경 기록, 보관 정리).
