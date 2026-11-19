"""Task Analyst (rule-based) + Finder (PRD F3, 17.1): FTS5 bm25 retrieval and dedup. No LLM.

Finder only retrieves and collapses duplicates; hard gates and scoring live in lab.selector
so excluded candidates are still reported with a reason.
"""
import json
import math
import re

SYNONYM_VERSION = "syn-v1"

# ponytail: hand-written KO->EN map instead of embeddings (F3: add vectors only when they beat this baseline).
# Korean words missing here are reported as unmapped_ko and only searched verbatim.
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
KNOWN_STACKS = {"react", "vue", "angular", "svelte", "nextjs", "node", "python", "django", "flask",
                "java", "kotlin", "swift", "go", "rust", "dotnet", "typescript", "excel", "azure"}
# (task_type, trigger words, deliverable). First match is task_type; deliverables accumulate.
TASK_RULES = [
    ("testing", ["검사", "검증", "테스트", "test", "qa", "e2e"], "테스트 실행 결과/검증 리포트"),
    ("code_review", ["리뷰", "review"], "리뷰 코멘트"),
    ("debugging", ["디버깅", "버그", "debug", "bug"], "원인 분석/수정"),
    ("document_creation", ["엑셀", "문서", "발표", "슬라이드", "xlsx", "docx", "pptx", "pdf"], "파일 산출물"),
    ("creation", ["만들기", "생성", "작성", "create", "generate"], "생성 산출물"),
    ("information", ["알려줘", "날씨", "뭐야"], "정보 답변"),
]
INPUT_WORDS = {"코드": "소스 코드", "앱": "애플리케이션", "파일": "파일", "버튼": "UI 요소",
               "회원가입": "회원가입 흐름", "repo": "저장소", "url": "URL"}


def stem(w):
    for suf in ("ing", "es", "ed", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    if len(w) >= 6 and w.endswith("e"):
        w = w[:-1]                       # create -> creat* (creating, creation)
    if len(w) >= 5 and w[-1] == w[-2] and w[-1] not in "aeiouls":
        w = w[:-1]                       # debugg -> debug
    return w


def analyze(query, stack=None, tools=None, constraints=None):
    """Rule-based task draft (17: the analyst never finalizes intent; a human confirms)."""
    q = query.lower()
    concepts, rest = [], q
    for ko, ens in SYNONYMS.items():
        if ko in rest:
            rest = rest.replace(ko, " ")
            if ens:
                concepts.append({"term": ko, "expand": sorted({stem(e) for e in ens})})
    words = [w for w in re.findall(r"[a-z0-9]+", rest) if w not in STOP]
    for w in words:
        concepts.append({"term": w, "expand": sorted({stem(w), *(stem(e) for e in EN_SYNONYMS.get(w, []))})})
    unmapped = re.findall(r"[가-힣]{2,}", rest)
    stack = [s.lower() for s in stack or []]
    inferred = [w for w in words if w in KNOWN_STACKS and w not in stack]
    task_type, deliverables = "general", []
    for t, trig, deliv in TASK_RULES:
        if any(x in q for x in trig):
            task_type = task_type if task_type != "general" else t
            deliverables.append(deliv)
    return {
        "query": query,
        "task_type": task_type,
        "inputs": [v for k, v in INPUT_WORDS.items() if k in q],
        "deliverables": deliverables,
        "stack": stack + inferred,
        "stack_inferred": inferred,
        "tools": [t.strip() for t in tools or [] if t.strip()],
        "constraints": list(constraints or []),
        "concepts": concepts,
        "unmapped_ko": unmapped,
        "analysis_method": "rule-v1 (초안, 사람 확인 필요)",
    }


def search_terms(task):
    terms = {t for c in task["concepts"] for t in c["expand"]}
    return sorted(terms | set(task.get("unmapped_ko") or []))


def fts_term(t):
    """Quote for FTS5 syntax; prefix-match stems of 4+ chars (unicode61 does not stem)."""
    quoted = '"' + t.replace('"', '""') + '"'
    return quoted + "*" if len(t) >= 4 and t.isascii() else quoted


def term_hits(term, words):
    return any(w == term or (len(term) >= 4 and term.isascii() and w.startswith(term)) for w in words)


def words_of(*texts):
    return re.findall(r"[^\W_]+", " ".join(t or "" for t in texts).lower())


def _json(s):
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def idf(conn, terms):
    n = conn.execute("SELECT count(*) FROM skills_fts").fetchone()[0]
    out = {}
    for t in terms:
        df = conn.execute("SELECT count(*) FROM skills_fts WHERE skills_fts MATCH ?", (fts_term(t),)).fetchone()[0]
        out[t] = math.log(1 + (n - df + 0.5) / (df + 0.5))
    return out


def find(conn, task, top_k=20):
    """-> {"terms", "match", "idf", "hits": [candidate...], "dedup_notes": [...]}.

    Each candidate: skill row + metadata/scan dicts + source_claude_native + bm25 + matched terms.
    """
    terms = search_terms(task)
    res = {"terms": terms, "match": "", "idf": {}, "hits": [], "dedup_notes": []}
    if not terms:
        return res
    match = " OR ".join(fts_term(t) for t in terms)
    res["match"], res["idf"] = match, idf(conn, terms)
    # bm25 weights per column: skill_id(unindexed), name x3, description, path, hints
    rows = conn.execute(
        """SELECT f.skill_id, f.hints, bm25(skills_fts, 0, 3.0, 1.0, 1.0, 1.0) AS bm, s.*,
                  v.metadata_json, v.scan_json, v.commit_sha, v.id AS version_id,
                  COALESCE(src.claude_native, 0) AS claude_native
           FROM skills_fts f JOIN skills s ON s.id = f.skill_id
           LEFT JOIN skill_versions v ON v.id = COALESCE(s.current_version_id,
                (SELECT max(id) FROM skill_versions WHERE skill_id = s.id))
           LEFT JOIN sources src ON src.id = s.source_id
           WHERE skills_fts MATCH ? ORDER BY bm LIMIT ?""", (match, top_k * 3)).fetchall()
    cands = []
    for r in rows:
        c = dict(r)
        meta, c["scan"] = _json(c.pop("metadata_json")), _json(c.pop("scan_json"))
        fm = meta.get("frontmatter") if isinstance(meta.get("frontmatter"), dict) else {}
        c["metadata"] = {**fm, **meta}           # frontmatter keys (compatibility, ...) at top level
        c["bm25"] = round(-c.pop("bm"), 4)      # FTS5 bm25 is negative; flip so higher = better
        head = words_of(c["name"], re.sub(r"[/_.-]", " ", c["path"] or ""))
        body = words_of(c["description"], c.get("hints"))
        c["matched_head"] = [t for t in terms if term_hits(t, head)]
        c["matched_terms"] = [t for t in terms if t in c["matched_head"] or term_hits(t, body)]
        cands.append(c)
    # dedup: duplicate_of, then same name+description. Prefer usable rows, then higher bm25.
    cands.sort(key=lambda c: (c["status"] != "active", c["security_status"] == "quarantined", -c["bm25"]))
    seen, kept = {}, []
    for c in cands:
        keys = [k for k in (c.get("duplicate_of"), c["id"],
                            ("nd", (c["name"] or "").strip().lower(), (c["description"] or "").strip().lower()))
                if k]
        rep = next((seen[k] for k in keys if k in seen), None)
        if rep:
            why = "duplicate_of" if c.get("duplicate_of") in (rep["id"], rep.get("duplicate_of")) \
                or rep.get("duplicate_of") == c["id"] else "same name+description"
            res["dedup_notes"].append({"dropped": c["id"], "kept": rep["id"], "reason": why})
            continue
        for k in keys:
            seen[k] = c
        kept.append(c)
    kept.sort(key=lambda c: -c["bm25"])
    res["hits"] = kept[:top_k]
    return res
