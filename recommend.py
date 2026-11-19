"""Skill Selection Lab - 04 추천 파이프라인.

Task -> 조건 추출 -> 메타데이터 BM25 후보 검색 -> (선택) 상위 후보 SKILL.md 확인
-> 하드 제외 -> 가중 점수 -> Top-N + '스킬 없음' 대안 + 보류 판정.

이 스크립트는 추천만 한다. 설치/실행은 하지 않으며 최종 선택은 사람이 승인한다.
"""
import argparse
import csv
import json
import math
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).parent / "data"
SKILLS = DATA / "skills.json"
EXPERIMENTS = DATA / "experiments.csv"
DECISIONS = DATA / "decisions.jsonl"

# 가중치는 검증되지 않은 가설이다(기획서). 실험 데이터가 쌓이면 재조정할 것.
WEIGHTS = {"task_fit": 35, "env": 20, "coverage": 15, "evidence": 15, "source": 10, "cost": 5}
HOLD_FIT = 0.5      # 최고 후보의 과제 적합도가 이보다 낮으면 보류(스킬 없음 권장)
HOLD_SCORE = 0.45   # 최고 후보의 총점(0..1)이 이보다 낮으면 보류
POOL = 20           # BM25 상위 몇 개를 점수화할지
READ_TOP = 8        # --read-candidates 시 본문을 확인할 후보 수
BODY_MAX = 200_000  # 신뢰하지 않는 본문은 이 길이까지만 본다
# ponytail: 파일 읽기/검색류 도구는 어느 에이전트에나 있다고 가정, --tools 비교에서 제외
PLATFORM = {"dotnet", "py", "python", "java", "ts", "typescript", "rust", "go", "azure", "aspnet", "winui", "swift", "kotlin"}
BASE_TOOLS = {"read", "write", "edit", "glob", "grep", "ls"}

# ponytail: 임베딩 대신 손으로 쓴 동의어 사전. 미등록 한국어 단어는 검색되지 않는다.
SYNONYMS = {
    "회원가입": ["signup", "registration", "register", "form", "auth"],
    "로그인": ["login", "auth", "authentication"],
    "버튼": ["button", "ui", "click"],
    "동작": ["behavior", "interaction", "functionality"],
    "검사": ["test", "testing", "verify"],
    "검증": ["test", "verify", "validation"],
    "테스트": ["test", "testing"],
    "화면": ["ui", "screen", "frontend"],
    "브라우저": ["browser", "playwright"],
    "웹": ["web", "webapp", "frontend"],
    "코드": ["code"],
    "리뷰": ["review"],
    "문서": ["document", "docs", "docx"],
    "배포": ["deploy", "deployment"],
    "앱": ["app", "application"],
    "엑셀": ["excel", "spreadsheet", "xlsx"],
    "스프레드시트": ["spreadsheet", "xlsx"],
    "파일": ["file"],
    "만들기": ["create"],
    "생성": ["create", "generate"],
    "작성": ["write", "create"],
    "수정": ["edit", "modify"],
    "디자인": ["design"],
    "프론트엔드": ["frontend", "ui"],
    "백엔드": ["backend", "api", "server"],
    "데이터베이스": ["database", "sql"],
    "보안": ["security"],
    "취약점": ["security", "vulnerability"],
    "발표": ["presentation", "slides", "pptx"],
    "슬라이드": ["slides", "presentation", "pptx"],
    "그림": ["image", "drawing"],
    "이미지": ["image"],
    "차트": ["chart", "visualization"],
    "번역": ["translation", "translate"],
    "검색": ["search"],
    "자동화": ["automation", "automate"],
    "스크린샷": ["screenshot"],
    "디버깅": ["debug", "debugging"],
    "버그": ["bug", "debug"],
    "깃허브": ["github"],
    "커밋": ["commit", "git"],
    "클라우드": ["cloud", "azure"],
    "날씨": ["weather"],
    "알려줘": [],
    "해줘": [],
}
EN_SYNONYMS = {"react": ["frontend", "web"], "e2e": ["test", "browser"]}
STOP = {"a", "an", "the", "and", "or", "to", "of", "for", "in", "on", "with", "is", "it",
        "use", "using", "when", "this", "that", "skill", "md", "skills", "github", "plugin", "plugins"}

NET_RE = re.compile(r"\b(curl|wget)\s|requests\.(get|post)|fetch\(|https?://\S*api|mcp server"
                    r"|\bnpm (install|i)\b|\bpip install\b|\bnpx\s", re.I)
SCRIPT_RE = re.compile(r"scripts?/|\.py\b|\.sh\b|\.js\b", re.I)


def stem(w):
    for suf in ("ing", "es", "ed", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text):
    return [stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOP]


def conditions(query, stack=None, tools=None):
    """Query -> concepts: list of token sets; a concept matches if any of its tokens matches."""
    q = query.lower()
    concepts = []
    for ko, ens in SYNONYMS.items():
        if ko in q:
            q = q.replace(ko, " ")
            if ens:
                concepts.append({ko: sorted({stem(e) for e in ens})})
    for w in re.findall(r"[a-z0-9]+", q):
        if w in STOP:
            continue
        concepts.append({w: sorted({stem(w), *(stem(e) for e in EN_SYNONYMS.get(w, []))})})
    leftover = re.findall(r"[가-힣]{2,}", q)
    return {"concepts": concepts, "unmapped_ko": leftover,
            "stack": [s.lower() for s in stack or []], "tools": [t.lower() for t in tools or []]}


def doc_tokens(r):
    # name은 가중을 위해 3번 반복
    path = re.sub(r"[/_.-]", " ", r.get("path") or "")
    return tokens(" ".join([(r.get("name") or "") + " "] * 3 + [r.get("description") or "", path,
                                                                r.get("compatibility") or ""]))


def bm25(docs, q_terms, k1=1.5, b=0.75):
    n = len(docs)
    avg = sum(len(d) for d in docs.values()) / max(n, 1)
    df = Counter(t for d in docs.values() for t in set(d))
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q_terms}
    out = {}
    for sid, d in docs.items():
        tf = Counter(d)
        out[sid] = sum(idf[t] * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avg))
                       for t in q_terms if tf[t])
    return out, idf


def evidence_for(sid, rows):
    """B/C pass rate for this skill vs A baseline in the same experiments. None = no data."""
    mine = [r for r in rows if r.get("skill_id") == sid and r.get("arm") in ("B", "C")]
    if not mine:
        return None
    exp_ids = {r.get("experiment_id") for r in mine}
    base = [r for r in rows if r.get("arm") == "A" and r.get("experiment_id") in exp_ids]

    def rate(rs):
        tot = sum(float(r.get("total") or 0) for r in rs)
        return sum(float(r.get("passed") or 0) for r in rs) / tot if tot else None

    def cost(rs):
        cs = [float(r["cost_usd"]) for r in rs if r.get("cost_usd") not in (None, "")]
        return sum(cs) / len(cs) if cs else None

    return {"n": len(mine), "rate": rate(mine), "base_rate": rate(base),
            "cost": cost(mine), "base_cost": cost(base)}


def parse_tools(allowed):
    # "Bash(git:*) Read, WebFetch" -> {"bash", "read", "webfetch"}
    return {t.lower() for t in re.findall(r"([A-Za-z][\w-]*)(?:\([^)]*\))?", allowed or "")}


def exclusion(r, cond, deny_network, body):
    if r.get("status") != "active":
        return f"status={r.get('status')} (비활성)"
    if not r.get("frontmatter_ok"):
        return "frontmatter 파싱 실패"
    if cond["tools"] and r.get("allowed_tools"):
        extra = parse_tools(r["allowed_tools"]) - set(cond["tools"]) - BASE_TOOLS
        if extra:
            return f"허용되지 않은 도구 요구: {', '.join(sorted(extra))}"
    compat = (r.get("compatibility") or "").lower()
    if cond["stack"] and compat:
        # 보수적으로: "X only" / "only X" 처럼 명시적 한정이 있고 X가 스택에 없을 때만 제외
        for m in re.finditer(r"(\w+)[- ]only|only (?:for |on |supports? )?(\w+)", compat):
            x = m.group(1) or m.group(2)
            if x not in cond["stack"]:
                return f"compatibility가 '{x}' 전용으로 명시 (스택: {', '.join(cond['stack'])})"
    m = deny_network and NET_RE.search(compat + "\n" + (body or ""))
    if m:
        return f"네트워크 필요로 보임: '{m.group(0).strip()[:40]}' (--deny-network)"
    return None


def score(r, cond, fit, matched, body, ev):
    sub, unverified = {}, []
    n_c = len(cond["concepts"]) or 1
    sub["task_fit"] = (fit, f"질의 개념 매칭(idf 가중) {fit:.2f}")

    # 환경 호환성
    compat = (r.get("compatibility") or "").lower()
    if not cond["stack"] and not cond["tools"]:
        sub["env"] = (0.7, "스택/도구 조건 미지정")
        unverified.append("환경 조건 미지정")
    else:
        text = compat + " " + (r.get("description") or "").lower() + " " + (body or "").lower()
        hits = [s for s in cond["stack"] if s in text]
        v, why = 0.6, "스택 언급 없음"
        if cond["stack"]:
            v = 0.5 + 0.5 * len(hits) / len(cond["stack"])
            why = f"스택 언급 {len(hits)}/{len(cond['stack'])}"
            if len(hits) < len(cond["stack"]):
                unverified.append("스택 호환성 일부 미확인")
        if cond["tools"] and not r.get("allowed_tools"):
            unverified.append("allowed_tools 미선언 (필요 도구 미확인)")
        sub["env"] = (v, why)

    # 커버리지: 메타데이터(+본문) 기준 개념 매칭 비율
    body_matched = set()
    if body:
        bt = set(tokens(body[:BODY_MAX]))
        body_matched = {i for i, c in enumerate(cond["concepts"]) if set(next(iter(c.values()))) & bt}
    cov = len(matched | body_matched) / n_c
    sub["coverage"] = (cov, f"개념 {len(matched | body_matched)}/{n_c} 포함" + (" (본문 포함)" if body else " (메타데이터만)"))

    # 실험 증거
    if not ev or ev["rate"] is None:
        sub["evidence"] = (0.5, "실험 데이터 없음 (중립)")
        unverified.append("실험 증거 없음")
    elif ev["base_rate"] is None:
        sub["evidence"] = (ev["rate"], f"B/C 통과율 {ev['rate']:.2f}, A 기준선 없음 (n={ev['n']})")
        unverified.append("A(스킬 없음) 기준선 없음")
    else:
        d = ev["rate"] - ev["base_rate"]
        sub["evidence"] = (max(0.0, min(1.0, 0.5 + d)),
                           f"통과율 {ev['rate']:.2f} vs A {ev['base_rate']:.2f} (n={ev['n']})")

    # 출처/품질
    official = r.get("priority") == 1
    lic = bool(r.get("license"))
    fm = bool(r.get("frontmatter_ok"))
    sub["source"] = (0.5 * official + 0.25 * lic + 0.25 * fm,
                     ", ".join([("공식 소스" if official else "비공식 소스"),
                                ("라이선스 있음" if lic else "라이선스 없음"), ("frontmatter OK" if fm else "frontmatter 오류")]))
    if not lic:
        unverified.append("라이선스 미표기")

    # 비용/속도
    if ev and ev["cost"] is not None and ev["base_cost"]:
        c = 1.0 if ev["cost"] <= ev["base_cost"] else ev["base_cost"] / ev["cost"]
        sub["cost"] = (c, f"평균 비용 ${ev['cost']:.4f} vs A ${ev['base_cost']:.4f}")
    else:
        sub["cost"] = (0.5, "비용 데이터 없음 (중립)")

    notes = []
    if body is None:
        unverified.append("SKILL.md 본문 미확인")
    else:
        notes.append(f"본문 {len(body)}자" + (", 스크립트 언급 있음" if SCRIPT_RE.search(body) else ""))
        if NET_RE.search(body):
            notes.append("네트워크 사용 흔적")
    if not compat:
        unverified.append("compatibility 미선언")
    total = sum(WEIGHTS[k] * v for k, (v, _) in sub.items()) / sum(WEIGHTS.values())
    return total, sub, unverified, notes


def recommend(query, skills, experiments_rows, stack=None, tools=None, deny_network=False, top=3, bodies=None):
    """skills: {skill_id: record}. bodies: optional {skill_id: SKILL.md text} (untrusted, keyword use only)."""
    bodies = bodies or {}
    cond = conditions(query, stack, tools)
    all_terms = sorted({t for c in cond["concepts"] for t in next(iter(c.values()))})
    docs = {sid: doc_tokens(r) for sid, r in skills.items()}
    active_docs = {sid: d for sid, d in docs.items() if skills[sid].get("status") == "active"}
    bm, idf = bm25(active_docs or docs, all_terms)
    for sid in docs:  # 비활성도 제외 목록에 이유와 함께 나오도록 점수만 계산
        if sid not in bm:
            bm[sid] = sum(idf[t] for t in set(docs[sid]) & set(all_terms))
    pool = [sid for sid, s in sorted(bm.items(), key=lambda kv: -kv[1]) if s > 0][:POOL]

    ranked, excluded = [], []
    max_idf = [max(idf[t] for t in next(iter(c.values()))) for c in cond["concepts"]]
    for sid in pool:
        r = skills[sid]
        why = exclusion(r, cond, deny_network, bodies.get(sid))
        if why:
            excluded.append({"skill_id": sid, "name": r.get("name"), "reason": why})
            continue
        dset = set(docs[sid])
        matched = {i for i, c in enumerate(cond["concepts"]) if set(next(iter(c.values()))) & dset}
        # name/path 매칭은 1.0, description에만 있으면 0.7 (부수적 언급일 수 있음)
        head = set(tokens((r.get("name") or "") + " " + re.sub(r"[/_.-]", " ", r.get("path") or "")))
        fit = sum(max_idf[i] * (1.0 if set(next(iter(cond["concepts"][i].values()))) & head else 0.7)
                  for i in matched) / (sum(max_idf) or 1)
        # ponytail: 플랫폼 한정 스킬(예: azure-*-dotnet)인데 질의/스택에 그 플랫폼이 없으면 적합도 절반.
        # 토큰 목록은 수작업; 소스가 늘면 compatibility/metadata 기반으로 교체.
        plat = (head & PLATFORM) - set(all_terms) - {stem(x) for x in cond["stack"]}
        if plat:
            fit *= 0.5
        total, sub, unv, notes = score(r, cond, fit, matched, bodies.get(sid), evidence_for(sid, experiments_rows))
        reasons = ([f"플랫폼 한정({', '.join(sorted(plat))}) - 질의에 해당 플랫폼 없음, 적합도 x0.5"] if plat else []) + [f"매칭 개념: {', '.join(next(iter(cond['concepts'][i])) for i in sorted(matched))}"] + notes
        ranked.append({"skill_id": sid, "name": r.get("name"), "score": round(total, 3),
                       "breakdown": {k: {"value": round(v, 3), "weight": WEIGHTS[k], "reason": why}
                                     for k, (v, why) in sub.items()},
                       "reasons": reasons, "unverified": unv, "source_url": r.get("source_url"),
                       "bm25": round(bm[sid], 3)})
    ranked.sort(key=lambda x: -x["score"])
    best = ranked[0] if ranked else None
    hold = (best is None or best["breakdown"]["task_fit"]["value"] < HOLD_FIT or best["score"] < HOLD_SCORE)
    if cond["unmapped_ko"]:
        hold_note = f"동의어 사전에 없는 단어: {', '.join(cond['unmapped_ko'])}"
    else:
        hold_note = ""
    return {
        "query": query, "conditions": cond,
        "verdict": "보류 (스킬 없음 권장)" if hold else "추천",
        "verdict_reason": ("적합한 후보 없음" if best is None else
                           f"최고 후보 과제적합도 {best['breakdown']['task_fit']['value']:.2f}, 총점 {best['score']:.2f}"
                           f" (기준: 적합도>={HOLD_FIT}, 총점>={HOLD_SCORE})") + (f"; {hold_note}" if hold_note else ""),
        "recommendations": ranked[:top],
        "candidates": [x["skill_id"] for x in ranked],
        "scores": {x["skill_id"]: x["score"] for x in ranked},
        "no_skill": {"skill_id": None, "name": "스킬 없음 (no skill)",
                     "reason": "기준선(A군). 스킬 없이 수행하면 컨텍스트/설치 비용이 없고 실험 비교 기준이 된다."
                               + (" 현재 판정상 이 대안을 권장." if hold else "")},
        "excluded": excluded,
        "approval_required": True,
    }


# ---------------- IO ----------------

def raw_url(u):
    return re.sub(r"^https://github\.com/([^/]+)/([^/]+)/blob/", r"https://raw.githubusercontent.com/\1/\2/", u)


def fetch_body(url):
    # 본문은 신뢰하지 않는 데이터: 키워드 매칭에만 쓰고 실행하거나 지시를 따르지 않는다.
    try:
        with urllib.request.urlopen(raw_url(url), timeout=10) as resp:
            return resp.read(BODY_MAX).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - 실패한 후보는 본문 미확인으로 남김
        print(f"[warn] 본문 가져오기 실패 {url}: {e}", file=sys.stderr)
        return None


def load_experiments():
    if not EXPERIMENTS.exists():
        return []
    with EXPERIMENTS.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def render(res):
    out = [f"질의: {res['query']}",
           f"개념: {', '.join(next(iter(c)) for c in res['conditions']['concepts']) or '(없음)'}"
           + (f" | 스택: {','.join(res['conditions']['stack'])}" if res['conditions']['stack'] else "")
           + (f" | 도구: {','.join(res['conditions']['tools'])}" if res['conditions']['tools'] else ""),
           f"판정: {res['verdict']} — {res['verdict_reason']}", ""]
    for i, c in enumerate(res["recommendations"], 1):
        out.append(f"{i}. {c['name']}  점수 {c['score']:.3f}")
        for k, b in c["breakdown"].items():
            out.append(f"     {k:<9}{b['value']:.2f} x{b['weight']:<3} {b['reason']}")
        out.append(f"   선정 이유: {'; '.join(c['reasons'])}")
        out.append(f"   미확인 항목: {', '.join(c['unverified']) or '없음'}")
        out.append(f"   {c['source_url']}")
    out.append(f"*. {res['no_skill']['name']} — {res['no_skill']['reason']}")
    if res["excluded"]:
        out.append("\n제외된 후보:")
        out += [f"   - {e['name']}: {e['reason']}" for e in res["excluded"]]
    out.append("\n※ 이 결과는 추천일 뿐이며 사람의 승인이 필요합니다. 자동 설치/실행하지 않습니다.")
    return "\n".join(out)


def approve(index, choice):
    lines = DECISIONS.read_text(encoding="utf-8").splitlines() if DECISIONS.exists() else []
    if not 0 <= index < len(lines):
        sys.exit(f"decision index {index} 없음 (0..{len(lines) - 1})")
    d = json.loads(lines[index])
    if choice != "none" and choice not in d["ranked"]:
        sys.exit(f"{choice} 는 이 결정의 후보가 아님: {d['ranked']}")
    d["approved"] = choice
    d["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines[index] = json.dumps(d, ensure_ascii=False)
    DECISIONS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"decision #{index} approved = {d['approved']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Skill 추천 (승인 필요, 자동 설치 없음)")
    ap.add_argument("query", nargs="?")
    ap.add_argument("--stack", default="")
    ap.add_argument("--tools", default="")
    ap.add_argument("--deny-network", action="store_true")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--read-candidates", action="store_true")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--approve", nargs=2, metavar=("INDEX", "SKILL_ID|none"))
    a = ap.parse_args(argv)
    if a.approve:
        return approve(int(a.approve[0]), a.approve[1])
    if not a.query:
        ap.error("query 필요")
    skills = json.loads(SKILLS.read_text(encoding="utf-8"))["skills"]
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
    kw = dict(stack=split(a.stack), tools=split(a.tools), deny_network=a.deny_network, top=a.top)
    rows = load_experiments()
    res = recommend(a.query, skills, rows, **kw)
    if a.read_candidates:
        bodies = {sid: b for sid in res["candidates"][:READ_TOP]
                  if (b := fetch_body(skills[sid]["source_url"])) is not None}
        res = recommend(a.query, skills, rows, bodies=bodies, **kw)
    print(render(res))
    if a.save:
        DECISIONS.parent.mkdir(exist_ok=True)
        rec = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "query": a.query,
               "conditions": res["conditions"], "verdict": res["verdict"], "ranked": res["candidates"],
               "scores": res["scores"],
               "excluded": res["excluded"], "approved": None}
        n = len(DECISIONS.read_text(encoding="utf-8").splitlines()) if DECISIONS.exists() else 0
        with DECISIONS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"\n저장됨: decision #{n} -> 승인: python recommend.py --approve {n} <skill_id|none>")


if __name__ == "__main__":
    main()
