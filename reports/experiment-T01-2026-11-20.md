# T01 첫 실측 비교 실험 (2026-11-20)

> 루브릭 T01 rubric-v2: eunhye 승인. 과제당 1회 실행(팔당 n=1), 시스템 검증 단계라 일반화하지 않음 (PRD 4.4). 실행 로그 원본은 data/artifacts/(로컬)에 있음.

```
과제: T01 — You are working in a small React + Vite web app in the current directo
평가 기준: T01/rubric-v2 / 고정됨 2026-11-20T02:17:21+00:00
[실행 환경] 모델 ['claude-haiku-5-5'] · 판정 ['claude-haiku-5-5'] · Claude Code ['2.1.327'] · 인증 프로필 ['S'] · 엔진 ['claude_p']
[사전 예측] 선정 점수는 예측일 뿐, 아래 수치만 증거
[상태] {'PASSED': 3}
[작업 성과] (성공 = 외부 검증기 전 항목 통과)
  A NO_SKILL   성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0033 (추정)  성공당 비용 $0.0033  입력토큰 103371 출력토큰 889 usage누락 0
  -- B: github:anthropics/skills:skills/frontend-design/SKILL.md (version 9, sha256 d91970639e9f)
  B AUTO       성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0035 (추정)  성공당 비용 $0.0035  입력토큰 104966 출력토큰 975 usage누락 0
  B′ FORCED    성공 1/1  점수 평균 1.00 표준편차 0.00  미평가 0 무효 0  평균비용 $0.0051 (추정)  성공당 비용 $0.0051  입력토큰 149002 출력토큰 1507 usage누락 0
  [트리거] B AUTO 자동 호출 0/1 / 적용 시 효과(FORCED−NO_SKILL) +0.0%p / 개선량(AUTO−NO_SKILL) +0.0%p
  [증거 수준] 로드(Skill 호출 관측) 0 · 행동(tool_use 기록) 2 · 성과(외부 테스트) 2 · FORCED는 /슬래시 강제 호출(stream에 Skill 블록 없음)
  [비용] 증가율 +5.1% (AUTO 대비 NO_SKILL, 추정 비용)
[판정] B 동률 (점수 평균 1.00 vs 1.00) — 해당 케이스에서만, 일반화 보류 (과제 10개 단계: %p 임계값 없음) | B 분류: 트리거 실패 + 효과 없음(FORCED도 차이 없음)
[분류 집계] 트리거 실패 1 · 효과 없음 1 (서로 다른 집계, RES-19)
[미확인] 다른 프로젝트·다른 과제에서의 재현성; 비용은 Claude Code 클라이언트 추정치(실청구 아님)
```

## 이전 시도 (rubric-v1, exp-T01-20261119T195459-76e1)

3팔 모두 R1·R2는 통과했으나 S1(log_policy)이 오탐으로 실패했다. 작업 사본이 AppData\Local\Temp 아래에 있어서 생긴 오탐이다. 결과는 삭제하지 않고 failure_cases에 RUBRIC으로 기록했고, rubric-v2에서 고쳤다.
