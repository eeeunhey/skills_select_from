# Skill Selection Lab (PRD v1.3 구현)

어떤 상황에서 어떤 외부 AI 스킬이 **실제로** 도움이 되는지 판단하고, 그 판단의 근거를 독립적으로 검증하는 실험실.
기준 문서: [Skill_Selection_Lab_PRD_v1.3.md](Skill_Selection_Lab_PRD_v1.3.md). 구현 순서는 PRD 25.1을 따른다.

> **선정 점수는 예측, 테스트 결과는 증거.** 모든 결과는 사람이 독립 검토하기 전까지 *내부 예비 실험*이다.

## 구현 현황 (PRD 25.1)
| 단계 | 내용 | 상태 |
|---|---|---|
| 0 | 정답 기준 T01–T10 (`rubrics/`, `fixtures/react-demo`, 외부 검증기), 실행 프로필 | 완료. base는 실패, 참조해는 통과 (`benchmarks/ground-truth-matrix.json`). **루브릭 10개는 사람 승인 대기** |
| 1 | 비교 실험 하네스 NO_SKILL / AUTO / FORCED + 토큰 원장 | 완료. `claude -p` 실측 스모크 테스트 통과(추정 $0.0036). 실제 비교 실험은 루브릭 승인 뒤 진행 |
| 2 | Collector + Catalog + 정적 보안 검사 | 완료. 5개 수집원, 341개 스킬 (플러그인 251 / 단독 90, quarantined 82) |
| 3 | Finder(FTS5) + Selector + NO_SKILL + 근거 리포트 + 사람 승인 | 완료 |
| 4 | Selection Audit + 정책 비교 (labeled / holdout) | 완료. [reports/audit-2026-11-19.md](reports/audit-2026-11-19.md). sel-v2는 채택하지 않음 |
| 5 | 야간 워커 축소판 (SQLite 큐, 체크포인트, 05:00 KST 마감, 한도 대기) | 완료. RES-01·03·04·06·07·08·15·17 테스트 |
| 6 | 대시보드, 알림, REST API, 다중 워커 | **범위 밖.** PRD 기준으로 실험량이 CLI로 감당 안 될 때 착수 |

## 빠른 시작
```bash
pip install -r requirements.txt
(cd fixtures/react-demo && npm install)        # 외부 검증기(Playwright, 시스템 Chrome 사용)
python -m pytest -q                            # 오프라인 테스트, Claude/GitHub 호출 없음
python -m lab                                  # 명령 목록
```

### 표준 흐름
```bash
python -m lab collect --export data/skills.json                     # 수집 (LLM 없음)
python -m lab rubrics status                                        # 루브릭 승인 상태
python -m lab rubrics approve T01 rubric-v1 --by <사람 이름>          # ① 사람: 기준 동결
python -m lab select "React 버튼 이동을 브라우저로 확인" --stack react --tools Read,Bash,Write,Edit --task-id T01 --save
python -m lab select approve <decision_id> <skill_id|NO_SKILL> --by <사람 이름>   # ② 사람: 선정 승인
python -m lab eval plan --task T01 --b <skill_id> --c <skill_id> --runs 3
python -m lab eval execute --experiment <id> --dry-run              # 비용 없이 확인
python -m lab eval execute --experiment <id>                        # ③ 실제 실행 (구독/API 사용량 소모)
python -m lab eval report --experiment <id>
python -m lab audit --out reports/audit-<date>.md                   # 선정 정책 감사
python -m lab runtime enqueue-experiment <id> --by <사람 이름>         # 야간 실행 큐 (05:00 KST 마감)
python -m lab runtime run --executor eval                           # OS 스케줄러에서 실행
```

## 사람이 결정해야 하는 것
1. **루브릭 승인 (T01–T10):** 승인 전에는 선정과 실험이 `BLOCKED`/`EXPLORATORY`로 막힌다. 승인 전에 `.claude/agents/rubric-reviewer.md` 서브에이전트로 검토를 받을 수 있다.
2. **`flagged` 후보 검토:** 예를 들어 `webapp-testing`은 번들 `scripts/`가 있어 `flagged`다. 실험의 B/C 팔은 `WAITING_SECURITY_APPROVAL`이 되고, 검토한 뒤 `--i-reviewed-this-candidate`를 붙여야 실행된다.
3. **실행 프로필:** 이 PC는 구독 로그인(S)이라 비용은 항상 *추정*이다. 평가 실험은 API 키(K)를 권장한다(PRD 26.1). 구독의 한도 초과 자동 과금(usage credits) 설정은 직접 꺼야 한다.
4. **예산:** `config/budgets.yaml`의 USD 상한은 NO_SKILL 기준선을 실측한 뒤 채운다. 그 전에는 실행 횟수 상한(30회/야간)만 적용된다.
5. **Claude 프로젝트 설정:** [docs/claude-settings.example.json](docs/claude-settings.example.json)을 `.claude/settings.json`으로 복사해 적용한다. 권한 규칙이라 자동으로 쓰지 않았다.

## 구조
| 경로 | 역할 (PRD 17.1) |
|---|---|
| `lab/collector.py`, `lab/scan.py` | Collector, Security Gate (F1, F2) |
| `lab/finder.py`, `lab/selector.py` | Task Analyst, Finder, Selector (F3, F4) |
| `lab/rubrics.py`, `rubrics/`, `fixtures/`, `benchmarks/` | Benchmark Builder (F0). 선정 로직과 다른 세션에서 작성 |
| `lab/evaluator.py`, `lab/claude_run.py` | Executor, Independent Evaluator (F5) |
| `lab/ledger.py` | Evidence Recorder, 토큰 원장 (F8) |
| `lab/audit.py` | Selection Audit, Failure Analyst (F6, F7) |
| `lab/budget.py`, `lab/runtime.py` | Budget Guard, 야간 워커 (F9) |
| `migrations/` | SQLite 스키마. 버전 기록은 append-only, 승인된 루브릭은 트리거로 동결 |
| `data/lab.db` | 기록 시스템(git으로 버전 관리). `data/artifacts/`(실행 로그)는 로컬에만 저장 |
| `docs/` | 런타임, 평가, 보안, 토큰, 야간 실행, 복구, 역할 정책 |

## 알려진 한계
- 라벨 20개와 루브릭 10개는 모두 `draft-unreviewed`다. 독립된 사람 평가자가 없다(PRD A.3).
- `claude plugin eval` 엔진은 신뢰 확인(`--trust-plugin`)과 `--scaffold`가 필요해서 검토 플래그 뒤에 두었다. 기본 엔진은 `claude -p`다. FORCED 팔은 plugin eval 안에서 실행할 수 없다.
- 선정 정책의 개선 가설 3개(개념 희석, NO_SKILL 상수 기준선, 범용 vs 특화 스킬)는 감사 리포트에 있다.
- 주입 신호 탐지와 부정 표현 처리는 정규식 휴리스틱이다(`ponytail:` 주석에 상한을 적어 둠).
- 매일 수집은 `.github/workflows/collect-skills.yml`(KST 07:17)이 한다. GitHub 예약 실행은 지연되거나 누락될 수 있어, 48시간 넘게 성공이 없으면 경고한다.
