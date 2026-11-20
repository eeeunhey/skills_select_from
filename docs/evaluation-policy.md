# 평가 정책 (Evaluation Policy) — 초안

> **상태: 내부 예비 실험.** 이 저장소의 루브릭(`rubrics/`)과 선정 라벨(`benchmarks/*.jsonl`)은 AI 에이전트(`benchmark-agent`)가 작성한 초안이다. 독립된 사람이 검토하고 승인하기 전에는 어떤 결과도 "검증됨"으로 보고하지 않는다(PRD 16.2, A.3).

## 1. 루브릭 수명주기

```
draft (파일 작성) ──register──> DB 등록 (승인 대기) ──사람이 approve──> frozen
        ^                                                                 │
        └────────── 바꿀 게 있으면 새 rubric_version(rubric-v2 …) ─────────┘
```

- `python -m lab.rubrics register rubrics/*.yaml --author <이름>`: 스키마를 검사하고(요구사항 id 중복 없음, 요구사항마다 verifier 1개, 필수 safety 요구사항 존재, verifier id가 `fixtures/react-demo/verify/cases.json`에 있음, 목표·조건·금지사항·프롬프트에 스킬 이름 없음) `rubrics`(본문 sha256)와 `tasks` 행을 쓴다. `project_fixture.tree_sha256: PENDING_FREEZE`는 이때 fixture 트리 해시(`node_modules` 제외, CRLF→LF 정규화)로 바뀌어 `tasks.fixture_commit`에 저장된다.
- **승인은 사람만 한다**: `python -m lab.rubrics approve T01 rubric-v1 --by <이름>`. 등록 뒤 fixture가 바뀌었으면 승인을 거부한다. 승인 순간부터 DB 트리거가 그 행의 UPDATE·DELETE를 막는다.
- `python -m lab.rubrics check rubrics/*.yaml`: 승인된 루브릭의 파일이나 fixture가 디스크에서 바뀌었으면 오류("create a new rubric_version")를 낸다. 실험 시작 전에 반드시 돌린다.
- 승인은 **후보 선정 전에** 한다(`approved_before_selection`). 실험을 시작한 뒤 결과를 보고 기준을 고치면 그 실험은 무효다.

## 2. 과제 실행과 채점 분리

- 에이전트는 `node fixtures/react-demo/verify/run.mjs prepare --dest <임시폴더>`로 만든 사본에서만 일한다. 사본에는 `verify/`(정답 목록·테스트)가 없다.
- 채점은 에이전트가 끝난 뒤 **에이전트 밖에서** 한다: `node fixtures/react-demo/verify/run.mjs --case T01 --workdir <사본> --log <transcript.jsonl> --json out.json`. 검증기는 사본의 `vite.config.js`가 아니라 fixture 원본 설정으로 앱을 띄운다.
- 에이전트가 "테스트 통과"라고 보고한 것은 성과 증거가 아니다.
- 루브릭이 쓸모 있는지는 `python benchmarks/validate_ground_truth.py`로 확인한다. 손대지 않은 BASE에서는 functional 요구사항이 모두 fail이어야 하고, 참조 해답(`benchmarks/reference-solutions/`, 루브릭 검증용, 에이전트에게 주지 않음)에서는 모두 pass여야 한다. regression 요구사항은 BASE에서 pass여야 한다(이미 되던 동작이 유지되는지 보는 항목이다).

## 3. 증거 수준 (PRD F5)

| 수준 | 의미 | Claude에서 확인하는 것 |
|---|---|---|
| 1. 로드 | 지침을 불러왔다 | 실행 기록의 `Skill` 도구 호출 블록 |
| 2. 행동 | 필수 단계를 실행했다 | `tool_use`/`tool_result` 블록, 중간 산출물 |
| 3. 성과 | 결과가 기준을 충족했다 | 위 외부 검증기의 요구사항별 pass |

세 수준을 섞어 쓰지 않는다. 읽었다는 사실(1)만으로는 효과(3)가 입증되지 않는다.

## 4. 요구사항 결과와 실행 상태

- 요구사항마다 결과는 `pass | fail | unevaluated` 중 하나이고, verifier 종류는 `external_test | log_policy | human`이다(`eval_checks`와 같은 어휘).
- **UNEVALUATED**: 결과를 관찰하지 못했으면 성공으로도 실패로도 세지 않는다. 사람 평가 항목(T04-R3, T08-R4, T09-R4, T10-R3 진단용 휴리스틱)은 사람 점수가 들어오기 전까지 항상 unevaluated다. 대화 기록 없이 채점하면 S1(log_policy)도 unevaluated다. S1이 검사하는 것은 파일 도구(Read/Write/Edit/Glob/Grep)의 경로 인자가 작업 사본 안인지, 웹/MCP 도구 사용, 네트워크·설치 또는 비밀 경로 패턴에 걸리는 셸 명령뿐이다. **알려진 한계**: 이 패턴에 걸리지 않는 셸 명령으로 작업 사본 밖을 읽는 경우(예: `cat ../other/file`)는 잡지 못한다. 사용 한도·rate limit·검증기 인프라 오류(포트 충돌, 리포트 없음)로 끝난 실행은 실패가 아니라 `UNEVALUATED`로 다시 분류하고 다시 돌린다.
- 필수(mandatory) safety 요구사항이 fail이면 다른 점수와 상관없이 그 실행은 탈락 또는 격리다. 필수 safety가 unevaluated이면 그 실행은 `PASSED`가 될 수 없다.
- 실패한 실행도 지우지 않는다(`eval_runs_no_delete`).

## 5. 판정 단계 (PRD 4.4) — 단일 만능 점수 금지

순서: ① 보안·필수 요구사항 위반 → 탈락/격리 ② 데이터 부족 → `INSUFFICIENT_EVIDENCE` ③ 미사용 대비 개선 ④ 비용·지연 ⑤ 일관성·사용자 기준.

| 단계 | 표본 | 쓸 수 있는 판정 |
|---|---|---|
| 시스템 검증 (과제 10개) | 팔당 30회 | **수치 기준 없음.** 과제별 우세/동률/열세 건수와 개별 사례만 보고 |
| 방향성 확인 (과제 30개) | 팔당 90회 이상 | 여러 과제에서 방향이 같은지만 보고(개선 과제 수와 악화 과제 수를 나란히) |
| 우위 주장 | 보류 평가셋 + 통계 검토 | 이 단계에서 %p 임계값을 미리 등록 |

- 지금 저장소(T01–T10)는 첫 번째 단계다. "+N%p 개선" 같은 수치 주장을 하지 않는다.
- `AUTO` 팔에서 `Skill` 호출이 없고 결과가 `NO_SKILL`과 같으면 '효과 없음'이 아니라 **트리거 실패**로 분류한다. `FORCED`에서도 차이가 없을 때만 '효과 없음'이다.
- 비교 범위 안에서 1위라고만 쓴다("최선의 스킬"이라고 쓰지 않는다).

## 6. 눈가림과 사람 평가

- UI·문서 결과(T04, T08 스크린샷 등)는 스킬 이름, 팔(arm), 선정 순위를 가린 상태로 평가하고, A/B/C 제시 순서를 섞는다.
- 평가자는 1–5점 구조화 루브릭을 쓰고 평가자 id와 근거를 남긴다. 사람이 점수를 고치면 고친 이유와 평가자를 기록한다.
- **DISPUTED**: 평가자 사이에 판단이 갈리면 평균만 내지 않는다. 원인과 합의 여부를 저장하고, 합의 전에는 `DISPUTED`(불확실)로 둔다.
- LLM 판정(`llm` grader)은 보조 의견이다. 같은 계열 AI의 의견만으로 정답을 확정하지 않는다. 과제 모델과 판정 모델의 전체 ID를 기록한다.

## 7. 선정 라벨 (`benchmarks/labeled-tasks.jsonl`, `holdout-tasks.jsonl`)

- 줄마다 `relevant_skills`(Recall@K 분모), `hard_negatives`(비슷하지만 범위·환경이 맞지 않는 스킬), `duplicates_elsewhere`(같은 이름, 다른 레포), `acceptable_no_skill`(NO_SKILL 정당성)을 담는다. 2026-11-19 GitHub 트리 스냅샷 기준이다.
- 모든 라벨은 `label_status: draft-unreviewed`다. 사람 검토 후 `reviewed`로 바꾸고 검토자를 남긴다. 라벨이 불완전하면 Recall@K 보고에 그 한계를 함께 쓴다.
- 보류 평가셋(`holdout-tasks.jsonl`)과 `rubrics/`는 Selector 개발 세션에서 읽지 못하게 한다(PRD 27.3). 정책 조정에는 labeled만 쓰고, holdout으로는 최종 확인만 한다.
