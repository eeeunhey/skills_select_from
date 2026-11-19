# Claude 실행 환경 (PRD 26) — 확인 기록

확인일 2026-11-19. 이 머신에서 `claude --version`, `claude -p --help`, `claude plugin eval --help`, 공식 문서
(code.claude.com/docs/en/plugin-evals, /headless)와 실제 실행 4회로 확인했다. 버전이 바뀌면 다시 확인하고 이 문서를 고친다.

## 프로필과 버전

| 항목 | 값 |
|---|---|
| 인증 프로필 | **S** (구독 로그인). init 메시지 `apiKeySource: "none"` |
| 권장 | 평가 실행은 **K**(API 키), 26.1 참고. K로 바꾸면 `config/runtime.yaml`의 `claude.auth_profile`을 바꾼다 |
| Claude Code | **2.1.327** (`min_claude_code_version` 2.1.302 이상) |
| 과제 모델 | `claude-sonnet-5-5` (config `task_model`) |
| 보조·판정 모델 | `claude-haiku-5-5` (config `aux_model`, `judge_model`) |
| 별칭 | `sonnet`·`opus`·`haiku`·`fable`·`claude-sonnet`처럼 버전 없는 이름은 `lab.evaluator.check_model_id`가 거부한다 |

**사람이 직접 확인할 것:** 구독 계정의 추가 사용량(usage credits / extra usage) 설정은 코드로 읽을 수 없다.
claude.ai 계정 설정에서 꺼져 있는지 확인한다(config `subscription_usage_credits: off`). 실제 스트림의
`rate_limit_event`에는 `overageStatus: "rejected"`, `overageDisabledReason: "org_level_disabled"`가 찍혔다.
꺼져 있으면 한도에 닿았을 때 종량제로 넘어가지 않고 막힌다(RES-17).

## 표준 명령 (확인함)

단일 실행. `lab.claude_run.run_p`가 만드는 명령이고, cwd는 `fixtures/<fixture>`를 새로 복사한 임시 디렉터리다
(node_modules·verify 제외, 저장소 밖):

```bash
claude -p "<프롬프트>" --model claude-sonnet-5-5 \
  --output-format stream-json --verbose \
  --permission-prompts none \
  --setting-sources project --strict-mcp-config --no-session-persistence \
  --settings '{"disableSkillShellExecution": true}' \
  --max-turns 10 --allowedTools "Read,Glob,Grep,Edit,Write,Skill" \
  [--plugin-dir candidates/<slug>]   # AUTO/FORCED만
  [--max-budget-usd <per_call_max_usd>]   # budgets.yaml 값이 null이면 붙이지 않는다
  > data/artifacts/<run_id>.jsonl
```

- FORCED 팔은 프롬프트 앞에 `/<plugin>:<skill> `를 붙인다. 실제로 확인함: `/demo:say-pong`을 넣으니 스킬 본문이 펼쳐져
  `PONG`이 나왔다. **이때 stream에는 `Skill` tool_use 블록이 없다.** 그래서 FORCED의 `trigger_observed`는 NULL이다.
  (스킬이 로드되지 않은 세션에서는 모델이 "command isn't installed"라고 답한다.)
- 부모 Claude 세션의 `CLAUDE*` 환경변수(`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, 세션 ID, 메시징 소켓 등)는 지우고
  실행한다(`claude_run.clean_env`). 지우지 않으면 init 메시지에 `entrypoint: claude-vscode`가 찍혔다.

비교 실행 (`lab.claude_run.run_plugin_eval`):

```bash
claude plugin eval candidates/<slug> --runs 3 --model claude-sonnet-5-5 --judge-model claude-haiku-5-5 \
  --no-publish --json data/artifacts/<exp>-B.plugin-eval.json [--max-cost-usd <per_suite_max_usd>] --keep-temp \
  [--trust-plugin --scaffold --allow-tools Write Edit]   # --i-reviewed-this-candidate 일 때만
```

- `--no-publish`는 항상 붙인다. 붙이지 않으면 리포트가 claude.ai에 게시된다.
- `--json` 실행은 질문을 할 수 없으므로 신뢰하지 않은 디렉터리는 exit 1로 거부된다. `--trust-plugin`, `--scaffold`,
  `--allow-tools`는 운영자 플래그 `--i-reviewed-this-candidate`가 있을 때만 붙인다. `--allow-real-servers`와
  `--mocks off`는 붙이지 않는다.

## 확인한 실제 형식 (2.1.327)

**stream-json** 한 줄에 JSON 하나:
- `{"type":"system","subtype":"init"}`: `session_id`, `model`, `cwd`, `tools`, `mcp_servers[{name,status}]`,
  `skills[str]`, `slash_commands`, `agents`, `plugins[{name,path,source,version?}]`, `claude_code_version`,
  `apiKeySource`, `permissionMode`, `plugin_errors?`. 내장 플러그인은 `path: "builtin"`이고, 후보 스킬은
  `"<plugin>:<skill>"`로 보인다. **CLAUDE.md·메모리 필드는 없다.**
- `{"type":"rate_limit_event","rate_limit_info":{status, resetsAt, rateLimitType, overageStatus, unifiedWindows}}`
- `{"type":"system","subtype":"api_retry"}`: `attempt`, `max_retries`, `retry_delay_ms`, `error_status`,
  `error`(`rate_limit`/`overloaded`/`billing_error`/`authentication_failed`/...), `no_response?` (문서 기준)
- `{"type":"assistant","message":{id, model, content[...], usage{...}}, "request_id"}`: 블록마다 한 줄씩 나오고 같은
  `message.id`가 반복된다. 출력 토큰은 자리표시자라서 쓰지 않는다(19.3 규칙 3). `tool_use` 블록은
  `{type:"tool_use", id, name, input}` 형태이고, 스킬 로드는 `name:"Skill"`, `input.skill`로 나온다.
- `{"type":"system","subtype":"thinking_tokens"}`: 추정치다. 원장에는 넣지 않는다.
- 마지막 줄 `{"type":"result"}`: `subtype`(`success`/`error_max_turns`/`error_during_execution`/
  `error_max_budget_usd`), `is_error`, `api_error_status`, `num_turns`, `total_cost_usd`, `duration_ms`,
  `session_id`, `result`, `permission_denials`, `usage`(최상위 루프만), `modelUsage{<model>:{inputTokens,
  outputTokens, cacheReadInputTokens, cacheCreationInputTokens, costUSD, thinkingTokens, costBasis:"list"}}`.
  원장 값은 `modelUsage`에서 읽는다(19.3 규칙 2).

**plugin eval --json** (문서 기준, 1단계에서 실제 실행으로는 확인하지 못함): `schemaVersion: 1`, `partial`,
`partialReason`(`cost_ceiling`/`interrupted`/`auth_failed`), `aggregates{overallScore, casesPassed, casesTotal,
meanDelta}`, `cases[]{name, aggregates{score, delta}, arms{with[], without[]}}`이 있다. run에는 `error`,
`aborted?`, `skippedPaidGraders?`가 있고, 그 밖에 `costUsd`, `durationSeconds`, `claudeVersion`이 있다.
run별 점수·작업 디렉터리·트랜스크립트 필드 이름은 문서에 없다. 그래서 `ingest_plugin_eval`은 키 이름 패턴으로 찾는다.
케이스 형식은 `evals/<case>/prompt.md`(frontmatter `max_turns`, `timeout_seconds`, `allowed_tools`, `runs`,
`plugins`, `model`, `env`) + `graders/<name>.md`(`type: regex|tool_used|tool_order|file_exists|llm|baseline`)
+ `case.yaml`(`schema_version: "1.1"`, `name`, `context.scaffold_script|add_dirs|history_file`)이다.
작업 공간은 빈 디렉터리에서 시작한다. 고정 파일(fixture)은 `scaffold_script`(`--scaffold` 필요)로만 넣을 수 있다.

## 격리: 된 것과 한계

| 막는 것 | 수단 | 확인 |
|---|---|---|
| 사용자 설정의 플러그인·훅·권한 | `--setting-sources project` | 실행해 봄: 설치된 개인 플러그인이 init에 나오지 않음 |
| MCP 서버 | `--strict-mcp-config` | `mcp_servers: []` |
| 스킬 셸 실행(`` !`cmd` ``) | `--settings {"disableSkillShellExecution": true}` + 정적 검사 격리 | 설정은 전달하지만 실행으로는 검증하지 않음 |
| 수집 저장소 폴더의 훅·.mcp.json | cwd를 저장소 밖 임시 복사본으로 둠 | 코드로 보장 |
| 개인 스킬·에이전트·플러그인 유입 | init 메시지 검사(`ledger.isolation_check`) → INVALID | 테스트 RES-18 |
| `~/.claude/CLAUDE.md`, 상위 폴더의 CLAUDE.md | 실행 전 파일 검사(`claude_run.preflight_leaks`) → INVALID | init에 나오지 않으므로 파일 존재로 판정 |

- 임시 `CLAUDE_CONFIG_DIR`/HOME은 **쓰지 않는다.** 그러면 프로필 S의 로그인이 사라진다. 프로필 K라면
  `--bare` + 임시 HOME으로 더 강하게 격리할 수 있다. 다만 `--bare`는 스킬 목록을 모델에 넘기지 않으므로 AUTO 팔에는
  쓸 수 없다(PRD F5).
- `--safe-mode`도 시험했다. 개인 설정은 막지만 `--plugin-dir`로 넣은 플러그인의 스킬까지 꺼져서(`demo:say-pong`이
  목록에 없음) 쓰지 않는다.
- 내장 스킬 허용 목록(`ledger.BUILTIN_SKILLS`)은 2.1.327 기준으로 고정했다. 버전이 오르면 새 내장 스킬 때문에
  실행이 INVALID가 될 수 있다(안전한 쪽 실패). 그때 NO_SKILL 실행의 init 메시지로 목록을 갱신한다.
- `claude plugin eval`은 실행마다 임시 HOME·설정을 쓴다. 다만 플러그인 자체 코드(훅, MCP)를 막는 경계는 아니다(PRD 10).
- 에이전트 작업 디렉터리에는 node_modules가 없다. 외부 검증기(`verify/run.mjs`)가 채점 직전에 fixture의
  node_modules를 junction으로 연결한다. 작업 디렉터리는 증거로 임시 폴더에 남긴다(`eval_runs.notes.workdir`).

## 스모크 테스트 결과 (실제 비용)

| # | 명령 | 결과 | total_cost_usd |
|---|---|---|---|
| 1 | `-p "Reply with the single word OK"` haiku, `--safe-mode` 등 | OK, 개인 플러그인 없음 | 0.00071779 |
| 2 | `/demo:say-pong` + `--plugin-dir` + `--safe-mode` | 스킬 로드 안 됨(safe-mode가 플러그인 스킬을 끔) | 0.00050058 |
| 3 | `/demo:say-pong` + `--plugin-dir`, `--setting-sources project` | PONG (FORCED 작동) | 0.00069279 |
| 4 | `lab.claude_run.run_p` (표준 명령) → `data/artifacts/smoke-claude-p.jsonl` | OK, isolation_ok=1 | 0.00165234 |

합계는 약 **$0.0036**(클라이언트 추정치)이다. 4번을 원장에 매핑한 결과는 `usage_source=provider`,
input 2 + cache write 7,391 + cache read 17,194 = 총 입력 24,587, output 4다.
`claude plugin eval` 스모크는 비용 없이 거부되는 것까지만 확인했다. 작은 데모 플러그인에 `--json --no-publish`로
실행하니 exit 1이 나왔다: "`... is not a trusted plugin directory, and this run cannot stop to ask you about it (no
interactive terminal, or --json / CI) ... pass --trust-plugin`". 지시에 따라 `--trust-plugin`으로 우회하지 않았다.
그래서 `--json` 결과 형식은 아직 실제로 확인하지 못했다. 확인하려면 사람이 터미널에서 한 번 실행해 그 디렉터리를
신뢰하거나, 검토한 뒤 `--i-reviewed-this-candidate`를 붙여 실행하고 `python -m lab.evaluator ingest`로
매핑을 확인한다.
