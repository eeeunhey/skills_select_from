# Skill Selection Lab (MVP v0.1)

공개 레포의 AI 스킬(SKILL.md)을 매일 수집하고, 작업에 맞는 스킬을 근거와 함께 추천한 뒤, A/B/C 실험으로 실제 효과를 기록·분석한다.
기획서: `Skill_Selection_Lab_기획서.docx`

## 구성
| 파일 | 역할 (기획서 섹션) |
|---|---|
| `collector.py` | 03 수집: Git Trees API → SKILL.md 탐지 → YAML 파싱 → blob SHA 비교 → `data/skills.json`, `data/reports/YYYY-MM-DD.md`, `data/runs.jsonl` |
| `.github/workflows/daily-collect.yml` | 매일 KST 07:17 (UTC 22:17) 실행 + 수동 실행, 결과 커밋 |
| `recommend.py` | 04 선정: 메타데이터 BM25 + 한영 동의어 → 필수 제외 → 가중 점수 → Top-3 + 스킬 없음/보류 |
| `experiments.py` | 05 검증: A(스킬 없음)/B(1위)/C(차순위) 실험 계획·기록·리포트(md+html), 실패코드 R1–R6 |
| `data/sources.json` | 수집원 (anthropics, openai, vercel-labs, microsoft `/skills`) |

## 사용 흐름
```bash
pip install -r requirements.txt
python collector.py                                   # 수집 (GITHUB_TOKEN 있으면 사용)
python recommend.py "React 회원가입 버튼 동작 검사" --stack react --tools bash,browser --read-candidates --save
python recommend.py --approve 0 github:anthropics/skills:skills/webapp-testing/SKILL.md   # 사람 승인 (또는 none)
python experiments.py plan --from-decision 0 --task-type button_flow --task "..." --model <model> --runs 3
python experiments.py add --experiment-id <id> --task-type button_flow --task "..." --arm A --model <model> --run 1 --passed 7 --total 10
python experiments.py report                          # data/reports/experiments.md / .html
```
테스트: `python tests/test_collector.py && python tests/test_recommend.py && python tests/test_experiments.py`

## GitHub에 올리기
새 레포에 이 폴더를 push → Actions 탭에서 `daily-collect` 수동 실행 → `data/skills.json` 확인. 토큰은 Actions 기본 `GITHUB_TOKEN` secret만 사용하며 파일에 기록하지 않는다.

## 보안 원칙 (07)
- 외부 SKILL.md는 신뢰할 수 없는 데이터: `yaml.safe_load`로만 파싱, 본문은 키워드 매칭에만 쓰고 출력·실행하지 않는다.
- 외부 `scripts/`·의존성은 실행하지 않는다. 추천은 사람 승인이 필요하며 자동 설치·권한 부여 없음.
- 수집 실패한 레포의 기존 기록은 "삭제"로 바꾸지 않는다. 마지막 성공 후 48시간이 지나면 경고한다.

## 알려진 한계 / 다음 단계
- 가중치(35/20/15/15/10/5)와 보류 임계값은 검증 전 가설이다. 실험 데이터가 쌓이면 다시 맞춘다.
- 다국어 임베딩이 없어서 동의어 사전에 없는 한국어는 놓친다. 범용 "코드 리뷰" 스킬이 현재 수집원에 없어 그 결과가 약하다.
- `--deny-network` 판정은 넓게 잡는다(`npx`, `pip install` 등 언급만 있어도 제외).
- 실험 실행 자체(샌드박스)는 범위 밖이다. 결과는 사람이 기록한다. 자동 제안하는 실패코드는 R4/R2뿐이다.
- 범위 밖: Skills.sh API(OIDC 필요), 수동 북마크 수집원(`sources.json`의 `manual` 자리만 있음), Streamlit/Next.js UI.
