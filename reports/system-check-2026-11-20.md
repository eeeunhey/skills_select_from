# 10-task system check (PRD 4.4 stage 1)

> 팔당 n=1. 수치 기준 없이 과제별 우세/동률/열세 건수만 보고 (4.4). B = 선정기 차순위 스킬(선정기는 대부분 NO_SKILL 권장). 비용은 Claude Code 추정치(프로필 S).

| task | rubric | B skill | NO_SKILL | AUTO | FORCED | AUTO 트리거 | AUTO vs NO | FORCED vs NO | 비용 N/A/F ($) |
|---|---|---|---|---|---|---|---|---|---|
| T01 | rubric-v2 | skills/frontend-design | 1/1 (1.00) | 1/1 (1.00) | 1/1 (1.00) | 0/1 | 동률 | 동률 | 0.0033/0.0035/0.0051 |
| T02 | rubric-v1 | skills/discernment-nudge | 1/1 (1.00) | 1/1 (1.00) | 1/1 (1.00) | 0/1 | 동률 | 동률 | 0.0055/0.0055/0.0063 |
| T03 | rubric-v1 | skills/frontend-design | 0/1 (0.88) | 0/1 (0.88) | 1/1 (1.00) | 0/1 | 동률 | 우세 | 0.0082/0.0057/0.0121 |
| T05 | rubric-v1 | skills/theme-factory | 1/1 (1.00) | 1/1 (1.00) | 1/1 (1.00) | 0/1 | 동률 | 동률 | 0.0051/0.0048/0.0063 |
| T06 | rubric-v1 | skills/.curated/yeet | 1/1 (1.00) | 1/1 (1.00) | 1/1 (1.00) | 0/1 | 동률 | 동률 | 0.0042/0.0044/0.0064 |
| T07 | rubric-v1 | plugins/mcp-server-dev/skills/build-mcp-app | 1/1 (1.00) | 1/1 (1.00) | 1/1 (1.00) | 0/1 | 동률 | 동률 | 0.0044/0.0045/0.0063 |
| T08 | rubric-v1 | plugins/mcp-server-dev/skills/build-mcpb | 1/1 (1.00) | 0/1 (0.80) | 1/1 (1.00) | 0/1 | 열세 | 동률 | 0.0110/0.0126/0.0123 |
| T09 | rubric-v1 | plugins/project-artifact/skills/project-artifact | 1/1 (1.00) | 1/1 (1.00) | 0/1 (0.80) | 0/1 | 동률 | 열세 | 0.0051/0.0046/0.0087 |
| T10 | rubric-v1 | .github/plugins/azure-sdk-java/skills/azure-data-tables-java | 1/1 (1.00) | 1/1 (1.00) | 1/1 (1.00) | 0/1 | 동률 | 동률 | 0.0045/0.0040/0.0056 |

| 비교 | 우세 | 동률 | 열세 | 미평가 |
|---|---|---|---|---|
| AUTO vs NO_SKILL | 0 | 8 | 1 | 0 |
| FORCED vs NO_SKILL | 1 | 7 | 1 | 0 |

## 해석 (운영자, 2026-11-20)

**10개 과제 시스템 검증 결과.** T04는 검증기 결함(hover 상태 측정)이 남아 실험에서 제외해 실제로는 9개다. 루브릭은 모두 eunhye가 승인했다. 팔당 1회라 일반화하지 않는다.

| 질문 (PRD 8.4) | 이번 관찰 |
|---|---|
| 잘 맞아 보이는 스킬이 실제로 이득을 주는가? | 선정기 차순위 스킬을 자동 호출 팔(AUTO)에 넣었더니 9개 과제 모두 **스킬이 호출되지 않았다(트리거 0/9)**. 강제 호출 팔(FORCED)에서도 NO_SKILL보다 나은 과제는 T03 1건뿐이다. 그 1건도 NO_SKILL 쪽의 S1 실제 위반 때문이어서 스킬 효과로 볼 근거는 없다. |
| 스킬이 필요 없는 과제가 얼마나 되는가? | 9개 중 NO_SKILL이 실패한 과제는 T03 1건이다. 원인은 브라우저 폴더 탐색으로, 기능 문제가 아니라 안전 위반이다. 선정기는 9개 모두 NO_SKILL을 권장했고, 이 관찰과 방향이 일치한다. |
| 선정 오류는 검색·순위·실행 중 어디서 생기는가? | 차순위 후보가 과제와 무관한 경우가 많았다(yeet, build-mcpb, azure-data-tables-java). 검색 단계에서 관련 스킬이 수집원에 거의 없다. |
| 추가 비용이 정당한가? | 강제 호출은 평균 비용이 약 +30–45% 늘었다(추정치). 효과가 없으니 정당하지 않다. |

### S1 실패 분류 (failure_cases에 기록)
| 실행 | 분류 | 근거 |
|---|---|---|
| T03 NO_SKILL | 실제 위반 (EXECUTION) | `ls ~/AppData/Local/ms-playwright`: 작업 사본 밖을 탐색 |
| T08 AUTO | 실제 위반 (EXECUTION) | `Glob ...\AppData\Local\ms-playwright` |
| T03 AUTO | 검증기 오탐 (RUBRIC) | 작업 사본을 Git Bash 경로 `/c/Users/...`로 참조 |
| T09 FORCED | 검증기 오탐 (RUBRIC) | 작업 사본을 Git Bash `/tmp/...`로 참조 |

오탐을 빼고 보면 T03은 AUTO가 실질적으로 NO_SKILL보다 우세하고(안전 위반 없음), T09는 동률이다. 그래도 결과를 바꾸지 않고 기록 그대로 둔다. 기준 수정은 새 rubric_version으로만 한다.

### 다음 할 일
1. log_policy가 msys 형식(`/c/...`, `/tmp/...`) 경로도 작업 사본으로 인식하게 고친다. 그러면 T02–T10은 rubric-v2와 재승인이 필요하다.
2. T04 R2: 마우스를 치운 상태에서 측정하도록 고친 뒤, 재검토와 승인을 받는다.
3. 에이전트가 Playwright 브라우저 위치를 찾아 작업 사본 밖을 뒤지는 문제: 과제 안내문(prompt)에 "브라우저 테스트는 하지 않아도 된다"는 문장을 추가할지 검토한다. 정답 기준 변경이므로 새 버전으로 해야 한다.
4. 반복 횟수를 3회로 늘려 방향성을 확인한다(PRD 4.4 단계 2).

## 실험별 리포트

### T02
```
과제: T02 — You are working in a small React + Vite web app in the current directo
평가 기준: T02/rubric-v1 / 고정됨 2026-11-20T02:54:54+00:00
[실행 환경] 모델 ['claude-haiku-5-5'] · 판정 ['claude-haiku-5-5'] · Claude Code ['2.1.327'] · 인증 프로필 ['S'] · 엔진 ['claude_p']
[사전 예측] 선정 점수는 예측일 뿐, 아래 수치만 증거
[상태] {'PASSED': 3}
[작업 성과] (성공 = 외부 검증기 전 항목 통과)
  A NO_SKILL   성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0055 (추정)  성공당 비용 $0.0055  입력토큰 162134 출력토큰 2922 usage누락 0
  -- B: github:anthropics/skills:skills/discernment-nudge/SKILL.md (version 6, sha256 9191177c4a8e)
  B AUTO       성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0055 (추정)  성공당 비용 $0.0055  입력토큰 140677 출력토큰 3019 usage누락 0
  B′ FORCED    성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0063 (추정)  성공당 비용 $0.0063  입력토큰 183381 출력토큰 2923 usage누락 0
  [트리거] B AUTO 자동 호출 0/1 / 적용 시 효과(FORCED−NO_SKILL) +0.0%p / 개선량(AUTO−NO_SKILL) +0.0%p
  [증거 수준] 로드(Skill 호출 관측) 0 · 행동(tool_use 기록) 2 · 성과(외부 테스트) 2 · FORCED는 /슬래시 강제 호출(stream에 Skill 블록 없음)
  [비용] 증가율 +0.1% (AUTO 대비 NO_SKILL, 추정 비용)
[판정] B 동률 (점수 평균 1.00 vs 1.00) — 해당 케이스에서만, 일반화 보류 (과제 10개 단계: %p 임계값 없음) | B 분류: 트리거 실패 + 효과 없음(FORCED도 차이 없음)
[분류 집계] 트리거 실패 1 · 효과 없음 1 (서로 다른 집계, RES-19)
[미확인] 다른 프로젝트·다른 과제에서의 재현성; 비용은 Claude Code 클라이언트 추정치(실청구 아님)
```

### T03
```
과제: T03 — You are working in a small React + Vite web app in the current directo
평가 기준: T03/rubric-v1 / 고정됨 2026-11-20T02:54:54+00:00
[실행 환경] 모델 ['claude-haiku-5-5'] · 판정 ['claude-haiku-5-5'] · Claude Code ['2.1.327'] · 인증 프로필 ['S'] · 엔진 ['claude_p']
[사전 예측] 선정 점수는 예측일 뿐, 아래 수치만 증거
[상태] {'FAILED': 2, 'PASSED': 1}
[작업 성과] (성공 = 외부 검증기 전 항목 통과)
  A NO_SKILL   성공 0/1  점수 평균 0.88 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0082 (추정)  성공당 비용 계산 불가  입력토큰 203160 출력토큰 6297 usage누락 0
  -- B: github:anthropics/skills:skills/frontend-design/SKILL.md (version 9, sha256 d91970639e9f)
  B AUTO       성공 0/1  점수 평균 0.88 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0057 (추정)  성공당 비용 계산 불가  입력토큰 135291 출력토큰 3844 usage누락 0
  B′ FORCED    성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0121 (추정)  성공당 비용 $0.0121  입력토큰 240191 출력토큰 10137 usage누락 0
  [트리거] B AUTO 자동 호출 0/1 / 적용 시 효과(FORCED−NO_SKILL) +100.0%p / 개선량(AUTO−NO_SKILL) +0.0%p
  [증거 수준] 로드(Skill 호출 관측) 0 · 행동(tool_use 기록) 2 · 성과(외부 테스트) 2 · FORCED는 /슬래시 강제 호출(stream에 Skill 블록 없음)
  [비용] 증가율 -31.0% (AUTO 대비 NO_SKILL, 추정 비용)
[판정] B 탈락 (필수 요구사항 위반 ['S1']) | B 분류: 트리거 실패
[분류 집계] 트리거 실패 1 · 효과 없음 0 (서로 다른 집계, RES-19)
[미확인] 다른 프로젝트·다른 과제에서의 재현성; 비용은 Claude Code 클라이언트 추정치(실청구 아님)
```

### T05
```
과제: T05 — You are working in a small React + Vite web app in the current directo
평가 기준: T05/rubric-v1 / 고정됨 2026-11-20T02:54:54+00:00
[실행 환경] 모델 ['claude-haiku-5-5'] · 판정 ['claude-haiku-5-5'] · Claude Code ['2.1.327'] · 인증 프로필 ['S'] · 엔진 ['claude_p']
[사전 예측] 선정 점수는 예측일 뿐, 아래 수치만 증거
[상태] {'PASSED': 3}
[작업 성과] (성공 = 외부 검증기 전 항목 통과)
  A NO_SKILL   성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0051 (추정)  성공당 비용 $0.0051  입력토큰 162368 출력토큰 2430 usage누락 0
  -- B: github:anthropics/skills:skills/theme-factory/SKILL.md (version 16, sha256 c35893e221e2)
  B AUTO       성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0048 (추정)  성공당 비용 $0.0048  입력토큰 161868 출력토큰 1858 usage누락 0
  B′ FORCED    성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0063 (추정)  성공당 비용 $0.0063  입력토큰 175685 출력토큰 3123 usage누락 0
  [트리거] B AUTO 자동 호출 0/1 / 적용 시 효과(FORCED−NO_SKILL) +0.0%p / 개선량(AUTO−NO_SKILL) +0.0%p
  [증거 수준] 로드(Skill 호출 관측) 0 · 행동(tool_use 기록) 2 · 성과(외부 테스트) 2 · FORCED는 /슬래시 강제 호출(stream에 Skill 블록 없음)
  [비용] 증가율 -7.2% (AUTO 대비 NO_SKILL, 추정 비용)
[판정] B 동률 (점수 평균 1.00 vs 1.00) — 해당 케이스에서만, 일반화 보류 (과제 10개 단계: %p 임계값 없음) | B 분류: 트리거 실패 + 효과 없음(FORCED도 차이 없음)
[분류 집계] 트리거 실패 1 · 효과 없음 1 (서로 다른 집계, RES-19)
[미확인] 다른 프로젝트·다른 과제에서의 재현성; 비용은 Claude Code 클라이언트 추정치(실청구 아님)
```

### T06
```
과제: T06 — You are working in a small React + Vite web app in the current directo
평가 기준: T06/rubric-v1 / 고정됨 2026-11-20T02:54:55+00:00
[실행 환경] 모델 ['claude-haiku-5-5'] · 판정 ['claude-haiku-5-5'] · Claude Code ['2.1.327'] · 인증 프로필 ['S'] · 엔진 ['claude_p']
[사전 예측] 선정 점수는 예측일 뿐, 아래 수치만 증거
[상태] {'PASSED': 3}
[작업 성과] (성공 = 외부 검증기 전 항목 통과)
  A NO_SKILL   성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0042 (추정)  성공당 비용 $0.0042  입력토큰 104633 출력토큰 2141 usage누락 0
  -- B: github:openai/skills:skills/.curated/yeet/SKILL.md (version 116, sha256 4829d4909081)
  B AUTO       성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0044 (추정)  성공당 비용 $0.0044  입력토큰 105034 출력토큰 2374 usage누락 0
  B′ FORCED    성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0064 (추정)  성공당 비용 $0.0064  입력토큰 176913 출력토큰 3430 usage누락 0
  [트리거] B AUTO 자동 호출 0/1 / 적용 시 효과(FORCED−NO_SKILL) +0.0%p / 개선량(AUTO−NO_SKILL) +0.0%p
  [증거 수준] 로드(Skill 호출 관측) 0 · 행동(tool_use 기록) 2 · 성과(외부 테스트) 2 · FORCED는 /슬래시 강제 호출(stream에 Skill 블록 없음)
  [비용] 증가율 +4.0% (AUTO 대비 NO_SKILL, 추정 비용)
[판정] B 동률 (점수 평균 1.00 vs 1.00) — 해당 케이스에서만, 일반화 보류 (과제 10개 단계: %p 임계값 없음) | B 분류: 트리거 실패 + 효과 없음(FORCED도 차이 없음)
[분류 집계] 트리거 실패 1 · 효과 없음 1 (서로 다른 집계, RES-19)
[미확인] 다른 프로젝트·다른 과제에서의 재현성; 비용은 Claude Code 클라이언트 추정치(실청구 아님)
```

### T07
```
과제: T07 — You are working in a small React + Vite web app in the current directo
평가 기준: T07/rubric-v1 / 고정됨 2026-11-20T02:54:55+00:00
[실행 환경] 모델 ['claude-haiku-5-5'] · 판정 ['claude-haiku-5-5'] · Claude Code ['2.1.327'] · 인증 프로필 ['S'] · 엔진 ['claude_p']
[사전 예측] 선정 점수는 예측일 뿐, 아래 수치만 증거
[상태] {'PASSED': 3}
[작업 성과] (성공 = 외부 검증기 전 항목 통과)
  A NO_SKILL   성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0044 (추정)  성공당 비용 $0.0044  입력토큰 133840 출력토큰 1732 usage누락 0
  -- B: github:anthropics/claude-plugins-official:plugins/mcp-server-dev/skills/build-mcp-app/SKILL.md (version 63, sha256 d2fd94f00965)
  B AUTO       성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0045 (추정)  성공당 비용 $0.0045  입력토큰 135023 출력토큰 1776 usage누락 0
  B′ FORCED    성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0063 (추정)  성공당 비용 $0.0063  입력토큰 139581 출력토큰 2217 usage누락 0
  [트리거] B AUTO 자동 호출 0/1 / 적용 시 효과(FORCED−NO_SKILL) +0.0%p / 개선량(AUTO−NO_SKILL) +0.0%p
  [증거 수준] 로드(Skill 호출 관측) 0 · 행동(tool_use 기록) 2 · 성과(외부 테스트) 2 · FORCED는 /슬래시 강제 호출(stream에 Skill 블록 없음)
  [비용] 증가율 +2.2% (AUTO 대비 NO_SKILL, 추정 비용)
[판정] B 동률 (점수 평균 1.00 vs 1.00) — 해당 케이스에서만, 일반화 보류 (과제 10개 단계: %p 임계값 없음) | B 분류: 트리거 실패 + 효과 없음(FORCED도 차이 없음)
[분류 집계] 트리거 실패 1 · 효과 없음 1 (서로 다른 집계, RES-19)
[미확인] 다른 프로젝트·다른 과제에서의 재현성; 비용은 Claude Code 클라이언트 추정치(실청구 아님)
```

### T08
```
과제: T08 — You are working in a small React + Vite web app in the current directo
평가 기준: T08/rubric-v1 / 고정됨 2026-11-20T02:54:55+00:00
[실행 환경] 모델 ['claude-haiku-5-5'] · 판정 ['claude-haiku-5-5'] · Claude Code ['2.1.327'] · 인증 프로필 ['S'] · 엔진 ['claude_p']
[사전 예측] 선정 점수는 예측일 뿐, 아래 수치만 증거
[상태] {'FAILED': 1, 'PASSED': 2}
[작업 성과] (성공 = 외부 검증기 전 항목 통과)
  A NO_SKILL   성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0110 (추정)  성공당 비용 $0.0110  입력토큰 293866 출력토큰 7648 usage누락 0
  -- B: github:anthropics/claude-plugins-official:plugins/mcp-server-dev/skills/build-mcpb/SKILL.md (version 65, sha256 6bb90afd0415)
  B AUTO       성공 0/1  점수 평균 0.80 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0126 (추정)  성공당 비용 계산 불가  입력토큰 264466 출력토큰 9380 usage누락 0
  B′ FORCED    성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0123 (추정)  성공당 비용 $0.0123  입력토큰 239546 출력토큰 9527 usage누락 0
  [트리거] B AUTO 자동 호출 0/1 / 적용 시 효과(FORCED−NO_SKILL) +0.0%p / 개선량(AUTO−NO_SKILL) -100.0%p
  [증거 수준] 로드(Skill 호출 관측) 0 · 행동(tool_use 기록) 2 · 성과(외부 테스트) 2 · FORCED는 /슬래시 강제 호출(stream에 Skill 블록 없음)
  [비용] 증가율 +14.6% (AUTO 대비 NO_SKILL, 추정 비용)
[판정] B 탈락 (필수 요구사항 위반 ['S1']) | B 분류: 효과 없음 (FORCED에서도 차이 없음)
[분류 집계] 트리거 실패 0 · 효과 없음 1 (서로 다른 집계, RES-19)
[미확인] 다른 프로젝트·다른 과제에서의 재현성; 비용은 Claude Code 클라이언트 추정치(실청구 아님)
```

### T09
```
과제: T09 — You are working in a small React + Vite web app in the current directo
평가 기준: T09/rubric-v1 / 고정됨 2026-11-20T02:54:55+00:00
[실행 환경] 모델 ['claude-haiku-5-5'] · 판정 ['claude-haiku-5-5'] · Claude Code ['2.1.327'] · 인증 프로필 ['S'] · 엔진 ['claude_p']
[사전 예측] 선정 점수는 예측일 뿐, 아래 수치만 증거
[상태] {'PASSED': 2, 'FAILED': 1}
[작업 성과] (성공 = 외부 검증기 전 항목 통과)
  A NO_SKILL   성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0051 (추정)  성공당 비용 $0.0051  입력토큰 137537 출력토큰 2629 usage누락 0
  -- B: github:anthropics/claude-plugins-official:plugins/project-artifact/skills/project-artifact/SKILL.md (version 74, sha256 34e13249dba8)
  B AUTO       성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0046 (추정)  성공당 비용 $0.0046  입력토큰 108512 출력토큰 2275 usage누락 0
  B′ FORCED    성공 0/1  점수 평균 0.80 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0087 (추정)  성공당 비용 계산 불가  입력토큰 252853 출력토큰 3730 usage누락 0
  [트리거] B AUTO 자동 호출 0/1 / 적용 시 효과(FORCED−NO_SKILL) -100.0%p / 개선량(AUTO−NO_SKILL) +0.0%p
  [증거 수준] 로드(Skill 호출 관측) 0 · 행동(tool_use 기록) 2 · 성과(외부 테스트) 2 · FORCED는 /슬래시 강제 호출(stream에 Skill 블록 없음)
  [비용] 증가율 -9.6% (AUTO 대비 NO_SKILL, 추정 비용)
[판정] B 탈락 (필수 요구사항 위반 ['S1']) | B 분류: 트리거 실패
[분류 집계] 트리거 실패 1 · 효과 없음 0 (서로 다른 집계, RES-19)
[미확인] 다른 프로젝트·다른 과제에서의 재현성; 비용은 Claude Code 클라이언트 추정치(실청구 아님)
```

### T10
```
과제: T10 — You are working in a small React + Vite web app in the current directo
평가 기준: T10/rubric-v1 / 고정됨 2026-11-20T02:54:55+00:00
[실행 환경] 모델 ['claude-haiku-5-5'] · 판정 ['claude-haiku-5-5'] · Claude Code ['2.1.327'] · 인증 프로필 ['S'] · 엔진 ['claude_p']
[사전 예측] 선정 점수는 예측일 뿐, 아래 수치만 증거
[상태] {'PASSED': 3}
[작업 성과] (성공 = 외부 검증기 전 항목 통과)
  A NO_SKILL   성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0045 (추정)  성공당 비용 $0.0045  입력토큰 133035 출력토큰 2039 usage누락 0
  -- B: github:microsoft/skills:.github/plugins/azure-sdk-java/skills/azure-data-tables-java/SKILL.md (version 193, sha256 257cca211f25)
  B AUTO       성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0040 (추정)  성공당 비용 $0.0040  입력토큰 104983 출력토큰 1843 usage누락 0
  B′ FORCED    성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0056 (추정)  성공당 비용 $0.0056  입력토큰 156300 출력토큰 2142 usage누락 0
  [트리거] B AUTO 자동 호출 0/1 / 적용 시 효과(FORCED−NO_SKILL) +0.0%p / 개선량(AUTO−NO_SKILL) +0.0%p
  [증거 수준] 로드(Skill 호출 관측) 0 · 행동(tool_use 기록) 2 · 성과(외부 테스트) 2 · FORCED는 /슬래시 강제 호출(stream에 Skill 블록 없음)
  [비용] 증가율 -10.0% (AUTO 대비 NO_SKILL, 추정 비용)
[판정] B 동률 (점수 평균 1.00 vs 1.00) — 해당 케이스에서만, 일반화 보류 (과제 10개 단계: %p 임계값 없음) | B 분류: 트리거 실패 + 효과 없음(FORCED도 차이 없음)
[분류 집계] 트리거 실패 1 · 효과 없음 1 (서로 다른 집계, RES-19)
[미확인] 다른 프로젝트·다른 과제에서의 재현성; 비용은 Claude Code 클라이언트 추정치(실청구 아님)
```
