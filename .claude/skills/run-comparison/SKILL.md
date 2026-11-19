---
name: run-comparison
description: Standard Skill Selection Lab comparison run (NO_SKILL vs AUTO vs FORCED) for one task with a frozen rubric. Manual only - it spends subscription/API usage.
disable-model-invocation: true
---

# 비교 실험 표준 절차 (PRD F5, 4.4, 26.2)

비용이 드는 작업이다. 사람이 `/run-comparison`으로 직접 부를 때만 실행한다. 각 단계에서 막히면 우회하지 말고 멈춘 뒤 보고한다.

## 0. 사전 조건 (하나라도 안 되면 멈춘다)
1. 과제의 루브릭이 승인돼 있어야 한다(`rubrics.approved_at`). 승인이 안 됐으면 plan이 거부한다. 루브릭을 대신 승인하지 않는다.
2. `claude --version`을 확인하고 `docs/claude-runtime.md`와 대조한다. 버전이 다르면 형식을 다시 확인한다.
3. 프로필을 확인한다. `config/runtime.yaml`의 `claude.auth_profile`(S/K). S라면 계정의 추가 사용량 설정을 사람이 확인했는지 묻는다.
4. 후보의 `security_status`를 확인한다. `quarantined`이면 실행하지 않는다. `flagged`이면 사람이 검토하기 전까지 BLOCKED다.
5. `config/budgets.yaml` 상한을 확인한다. USD 상한이 null이면 실행 횟수 상한만 걸린다.

## 1. 계획 (비용 없음)
```bash
python -m lab.evaluator plan --task T01 --b <skill_id 또는 file:path/SKILL.md> [--c <skill_id>] [--runs 3]
```
- 모델은 전체 ID만 쓴다(`claude-sonnet-5-5`). 별칭은 거부된다.
- 실행 순서는 seed로 섞이고 seed가 기록된다. 출력된 `experiment_id`를 적어 둔다.

## 2. 미리 보기 (비용 없음)
```bash
python -m lab.evaluator execute --experiment <id> --dry-run
```

## 3. 실행 (비용 발생)
```bash
python -m lab.evaluator execute --experiment <id> [--limit N]
```
- 기본 엔진은 `claude_p`다. 실행마다 fixture를 새로 복사한 임시 폴더를 쓰고, 프로젝트 설정만 읽으며, MCP는 쓰지 않는다. stream-json 전체를 `data/artifacts/<run_id>.jsonl`에 저장한다.
- 에이전트가 끝나면 외부 검증기(`fixtures/<fixture>/verify/run.mjs`)가 채점한다. 에이전트가 보고한 테스트 결과는 증거가 아니다.
- `WAITING_QUOTA_RESET` / `WAITING_RATE_LIMIT` / `WAITING_BUDGET_APPROVAL` / `FAILED_PERMANENT`가 나오면 루프는 즉시 멈춘다. 해당 실행은 `UNEVALUATED`로 남고 나중에 다시 실행된다. 상한을 올리거나 키를 바꾸지 않는다.
- `--engine plugin_eval`은 `--trust-plugin --scaffold --allow-tools`가 필요하다. 사람이 후보를 검토했을 때만 `--i-reviewed-this-candidate`를 붙인다. 에이전트가 스스로 붙이지 않는다.

## 4. 리포트
```bash
python -m lab.evaluator report --experiment <id>
```
읽는 법:
- 트리거 실패(AUTO에서 Skill 호출 없음, 점수 동일)와 효과 없음(FORCED도 차이 없음)은 따로 센다.
- 과제 10개 단계에서는 우세·동률·열세만 말한다. %p 임계값으로 우위를 주장하지 않는다. 일반화는 보류한다.
- 비용은 Claude Code 클라이언트 추정치다. 프로필 S에서는 실청구와 대조할 수 없다.
- `INVALID`는 격리가 깨진 실행이다(개인 스킬이나 CLAUDE.md가 섞임). 비교에서 빼고 원인을 고친다.

## 5. 기록
실패한 실행도 지우지 않는다(DB 트리거가 막는다). 리포트 원문과 experiment_id를 결과 보고에 붙인다.
