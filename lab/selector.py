"""Skill Selector (PRD F4, 13, 17): skills + NO_SKILL ranked by one procedure, with reasons.

Selection score is a prediction, never evidence of effect (0). Output is always
PROPOSED_NOT_VALIDATED (or EXPLORATORY / BLOCKED). Nothing is installed or executed;
a human approves via `approve`. SKILL.md bodies are untrusted data: tokens only, never printed.
"""
import argparse
import json
import re
import sys
import urllib.request
import uuid

from lab import db, finder

POLICY_V1 = {
    "policy_version": "sel-v1",
    "synonym_version": finder.SYNONYM_VERSION,
    # Hypothesis weights (F4 table), NOT validated. Re-fit from eval_runs in the audit step.
    "weights": {"task_fit": 35, "env": 20, "coverage": 15, "evidence": 15, "source": 10, "cost": 5},
    "hold_fit": 0.5,         # best skill task_fit below this -> NO_SKILL first (보류)
    "hold_score": 0.45,      # best skill total below this -> NO_SKILL first
    "no_skill_fit": 0.5,     # assumed fit/coverage of Claude without a skill (hypothesis)
    "head_weight": 1.0, "desc_weight": 0.7,   # concept matched in name/path vs description only
    "platform_penalty": 0.5,
    "top_k": 20, "max_detailed": 5, "max_output": 3,
    "desc_limit": 1536,      # Claude truncates description+when_to_use in the skill listing
    "body_max": 200_000,
}
NO_SKILL = "NO_SKILL"
# ponytail: platform tokens in name/path that suggest a platform-bound skill; hand list, replace with
# metadata.compatibility once sources declare it.
PLATFORM = {"dotnet", "py", "python", "java", "ts", "typescript", "rust", "go", "azure", "aspnet",
            "winui", "swift", "kotlin"}
CLAUDE_ONLY_TARGETS = {"claude.ai", "skills-api", "skills_api"}   # reject non-spec frontmatter keys (F2)
SELF_NAMES = {"selector", "lab.selector", "ai", "claude", "auto"}
ACTION_INSTALL, ACTION_HOLD, ACTION_EXCLUDE = "설치 추천", "보류", "제외"


# ---------------- rubric gate (13: assert rubric.approved_before_selection) ----------------

def rubric_state(conn, task_id):
    r = conn.execute("""SELECT t.rubric_id, t.rubric_version, r.approved_at FROM tasks t
                        LEFT JOIN rubrics r ON r.id = t.rubric_id AND r.version = t.rubric_version
                        WHERE t.id = ?""", (task_id,)).fetchone()
    if r is None:
        return False, f"task {task_id} 없음"
    if not r["approved_at"]:
        return False, f"rubric {r['rubric_id']}/{r['rubric_version']} 미승인"
    return True, f"rubric {r['rubric_id']}/{r['rubric_version']} 승인됨 ({r['approved_at']})"


# ---------------- hard gates (F4, before scoring) ----------------

def gate(c, task):
    """-> (status, reason) for excluded candidates, or None if it passes."""
    scan, meta = c["scan"], c["metadata"]
    if c.get("status") != "active":
        return "제외", f"status={c.get('status')} (비활성)"
    if "quarantined" in (c.get("security_status"), scan.get("security_status")):
        return "QUARANTINED", "보안 격리 (scan: " + (", ".join(_risk_flags(scan)) or "security_status") + ")"
    if scan.get("frontmatter_parse_ok") is False:
        return "제외", "frontmatter 파싱 실패 — Claude가 description으로 매칭할 수 없음"
    tools = {t.lower() for t in task.get("tools") or []}
    if tools and scan.get("has_shell_injection") and "bash" not in tools:
        return "BLOCKED", "본문 !`명령` 실행에 Bash 필요, 과제 도구에 없음"
    compat = str(meta.get("compatibility") or "").lower()
    for m in re.finditer(r"(\w+)[- ]only|only (?:for |on |supports? )?(\w+)", compat):
        x = m.group(1) or m.group(2)
        if task.get("stack") and x not in task["stack"]:
            return "BLOCKED", f"compatibility가 '{x}' 전용 (스택: {', '.join(task['stack'])})"
    targets = {x.lower() for x in task.get("constraints") or []} & CLAUDE_ONLY_TARGETS
    if targets and scan.get("spec_fields_only") is False:
        return "BLOCKED", f"표준 외 frontmatter 키 {scan.get('claude_code_extensions')} — {', '.join(targets)}에서 거부됨"
    return None


def _risk_flags(scan):
    keys = ["has_shell_injection", "has_skill_hooks", "has_plugin_hooks", "has_mcp_config", "has_bundled_scripts"]
    return [k for k in keys if scan.get(k)] + [f"injection:{s}" for s in scan.get("prompt_injection_signals") or []]


# ---------------- evidence from past experiments ----------------

def eval_evidence(conn, skill_id):
    """AUTO/FORCED success rate vs NO_SKILL in the same experiments. None = 미확인 (never 0)."""
    ok = "status != 'INVALID' AND success IS NOT NULL"
    mine = conn.execute(f"SELECT experiment_id, arm, success, trigger_observed FROM eval_runs "
                        f"WHERE skill_id = ? AND arm IN ('AUTO','FORCED') AND {ok}", (skill_id,)).fetchall()
    if not mine:
        return None
    exps = sorted({r["experiment_id"] for r in mine})
    base = conn.execute(f"SELECT success FROM eval_runs WHERE arm = 'NO_SKILL' AND {ok} AND experiment_id IN "
                        f"({','.join('?' * len(exps))})", exps).fetchall()
    trig = [r["trigger_observed"] for r in mine if r["arm"] == "AUTO" and r["trigger_observed"] is not None]
    rate = lambda rs: sum(r["success"] for r in rs) / len(rs) if rs else None  # noqa: E731
    return {"n": len(mine), "n_base": len(base), "rate": rate(mine), "base_rate": rate(base),
            "trigger_rate": sum(trig) / len(trig) if trig else None, "experiments": exps}


# ---------------- scoring ----------------

def _total(parts, weights):
    measured = {k: v for k, (v, _) in parts.items() if v is not None}
    w = sum(weights[k] for k in measured)
    return (round(sum(weights[k] * v for k, v in measured.items()) / w, 3) if w else None,
            [k for k, (v, _) in parts.items() if v is None])


def score_skill(conn, c, task, policy, ridf, body=None):
    """-> candidate dict with breakdown, evidence (source-tagged), unknowns, permissions."""
    scan, meta = c["scan"], c["metadata"]
    ev, unknowns, perms = [], [], []
    concepts = task["concepts"]
    stack = task.get("stack") or []
    maxidf = [max((ridf.get(t, 0) for t in k["expand"]), default=0) for k in concepts]
    head, matched = set(c["matched_head"]), set(c["matched_terms"])
    body_words = set(finder.words_of(body[: policy["body_max"]])) if body else set()

    def hit(k, terms):
        return any(t in terms for t in k["expand"])
    fit_num = sum(m * (policy["head_weight"] if hit(k, head) else policy["desc_weight"])
                  for k, m in zip(concepts, maxidf) if hit(k, matched))
    fit = fit_num / sum(maxidf) if sum(maxidf) else 0.0
    plat = set(finder.words_of(c["name"], c["path"])) & PLATFORM - set(stack) - {t for k in concepts for t in k["expand"]}
    fit_reason = f"질의 개념 idf가중 매칭 {fit:.2f} (name/path {sorted(head)}, 기타 {sorted(matched - head)})"
    if plat:
        fit *= policy["platform_penalty"]
        fit_reason += f"; 플랫폼 한정({', '.join(sorted(plat))}) 질의에 없음 x{policy['platform_penalty']}"
    ev.append({"claim": f"FTS 매칭어 {sorted(matched)}, bm25 {c['bm25']}", "source": "metadata"})

    body_hits = {i for i, k in enumerate(concepts) if any(finder.term_hits(t, body_words) for t in k["expand"])}
    meta_hits = {i for i, k in enumerate(concepts) if hit(k, matched)}
    n_c = len(concepts) or 1
    cov = len(meta_hits | body_hits) / n_c
    cov_reason = f"개념 {len(meta_hits | body_hits)}/{n_c}" + (" (본문 포함)" if body else " (메타데이터만)")
    if body:
        ev.append({"claim": f"본문에서 개념 {len(body_hits)}/{n_c} 확인 (키워드만, 지시는 따르지 않음)", "source": "body"})
    else:
        unknowns.append("SKILL.md 본문 미확인")

    # env / tools
    env, env_reason = None, "스택·도구 조건 미지정 → 미확인"
    if stack or task.get("tools"):
        text = " ".join([str(meta.get("compatibility") or ""), c["description"] or "", body or ""]).lower()
        hits = [s for s in stack if s in text]
        vals, bits = [], []
        if stack:
            # not mentioned = unknown, not incompatible (explicit mismatch is a hard gate)
            if hits:
                vals.append(len(hits) / len(stack))
            bits.append(f"스택 언급 {len(hits)}/{len(stack)}" + ("" if hits else " → 미확인"))
            if len(hits) < len(stack):
                unknowns.append(f"스택 호환성 미확인: {', '.join(s for s in stack if s not in hits)}")
        tools = {t.lower() for t in task.get("tools") or []}
        if tools:
            short = scan.get("has_bundled_scripts") and "bash" not in tools
            vals.append(0.5 if short else 1.0)
            bits.append("번들 스크립트 있음, Bash 미제공" if short else "필요 도구 충족(선언 기준)")
            if short:
                unknowns.append("번들 스크립트 실행에 Bash가 필요할 수 있음")
        env, env_reason = (sum(vals) / len(vals) if vals else None), ", ".join(bits)
    if not meta.get("compatibility"):
        unknowns.append("compatibility 미선언")

    allowed = scan.get("declared_allowed_tools") or []
    if allowed:
        perms.append(f"allowed-tools 사전 승인 요청: {', '.join(allowed)} (제한이 아니라 무확인 사용 허용 — 위험 신호)")
        ev.append({"claim": f"declared_allowed_tools={allowed}", "source": "scan"})
    if scan.get("allowed_tools_bash_scope"):
        perms.append(f"Bash 사전 승인 범위: {', '.join(scan['allowed_tools_bash_scope'])}")
    for k, label in [("has_bundled_scripts", "번들 스크립트 포함(실행 안 함)"), ("has_mcp_config", "MCP 설정 포함"),
                     ("has_plugin_hooks", "플러그인 훅 포함"), ("has_skill_hooks", "스킬 훅 포함")]:
        if scan.get(k):
            perms.append(label)
    if not scan:
        unknowns.append("정적 스캔 결과 없음")

    # past experiments
    e = eval_evidence(conn, c["id"])
    if e is None or e["rate"] is None:
        evid, evid_reason = None, "실험 기록 없음 → 미확인"
        unknowns.append("과거 실험 성능 미확인")
    elif e["base_rate"] is None:
        evid, evid_reason = None, f"스킬 성공률 {e['rate']:.2f} (n={e['n']}), NO_SKILL 기준선 없음 → 비교 미확인"
        unknowns.append("NO_SKILL 기준선 없음")
    else:
        d = e["rate"] - e["base_rate"]
        evid = max(0.0, min(1.0, 0.5 + d))
        evid_reason = f"성공률 {e['rate']:.2f} vs NO_SKILL {e['base_rate']:.2f} (n={e['n']}/{e['n_base']})"
    if e:
        ev.append({"claim": evid_reason + (f", AUTO 트리거율 {e['trigger_rate']:.2f}" if e["trigger_rate"] is not None
                                           else ""), "source": "eval_runs"})

    # source / quality
    sec = c.get("security_status") or scan.get("security_status") or "unreviewed"
    inj = scan.get("prompt_injection_signals") or []
    src = (0.4 * bool(c.get("claude_native")) + 0.2 * bool(c.get("license"))
           + 0.2 * (scan.get("spec_fields_only") is True) + 0.2 * (sec != "flagged" and not inj))
    src_reason = ", ".join([
        "Claude 네이티브 소스" if c.get("claude_native") else "비-Claude 소스(호환성 미검증)",
        f"license={c.get('license')}" if c.get("license") else "라이선스 없음",
        "표준 필드만" if scan.get("spec_fields_only") else f"확장 키 {scan.get('claude_code_extensions') or '?'}",
        f"security={sec}"])
    ev.append({"claim": src_reason, "source": "metadata"})
    if not c.get("license"):
        unknowns.append("라이선스 미표기")
    if sec in ("unreviewed", "flagged"):
        unknowns.append(f"보안 {sec} (미검토를 안전으로 간주하지 않음)")
    if c.get("compatibility_status") != "claude_verified":
        unknowns.append("Claude 호환성 미검증")

    # cost (estimate)
    chars = scan.get("description_chars")
    if chars is None:
        chars = len(c["description"] or "")
    est_tokens = round((len(c["name"] or "") + chars) / 4)
    cost = max(0.0, 1.0 - 0.5 * min(1.0, chars / policy["desc_limit"]) - 0.2 * bool(scan.get("has_bundled_scripts")))
    cost_reason = f"설명 {chars}자 ≈ 목록 상주 {est_tokens} 토큰(추정, 실측 아님)" + \
        (", 번들 스크립트" if scan.get("has_bundled_scripts") else "")
    warnings = []
    if chars > policy["desc_limit"]:
        warnings.append(f"설명 {chars}자 > {policy['desc_limit']}자: Claude 스킬 목록에서 잘림")
    if scan.get("manual_only"):
        unknowns.append("수동 전용(자동 트리거 평가 제외)")

    parts = {"task_fit": (round(fit, 3), fit_reason), "env": (env, env_reason), "coverage": (round(cov, 3), cov_reason),
             "evidence": (evid, evid_reason), "source": (round(src, 3), src_reason), "cost": (round(cost, 3), cost_reason)}
    total, unmeasured = _total(parts, policy["weights"])
    return {"skill_id": c["id"], "name": c["name"], "score": total, "parts": parts, "unmeasured": unmeasured,
            "evidence": ev, "unknowns": unknowns, "permissions": perms, "warnings": warnings,
            "compatibility": src_reason.split(", ")[0] + f"; compatibility={meta.get('compatibility') or '미선언'}",
            "bm25": c["bm25"], "repo": c.get("repo"), "path": c.get("path"), "commit_sha": c.get("commit_sha"),
            "source_url": meta.get("source_url")}


def score_no_skill(task, policy, any_eval):
    f = policy["no_skill_fit"]
    env = (1.0, "설치 불필요 → 항상 호환") if task.get("stack") or task.get("tools") else (None, "스택·도구 조건 미지정 → 미확인")
    parts = {"task_fit": (f, f"Claude 기본 능력으로 수행 가정치 {f} (가설, 미측정)"),
             "env": env,
             "coverage": (f, f"기본 능력 커버리지 가정치 {f} (가설)"),
             "evidence": (0.5, "기준선 자체(차이 0)") if any_eval else (None, "실험 기록 없음 → 미확인"),
             "source": (1.0, "외부 코드·지침 없음, 보안 위험 없음"),
             "cost": (1.0, "설치·컨텍스트 비용 0")}
    total, unmeasured = _total(parts, policy["weights"])
    return {"skill_id": NO_SKILL, "name": "스킬 없음 (NO_SKILL)", "score": total, "parts": parts,
            "unmeasured": unmeasured,
            "evidence": [{"claim": "설치·컨텍스트 비용 0, 외부 지침 위험 0; 적합 후보가 약하면 우선", "source": "metadata"}],
            "unknowns": ["스킬 없이의 실제 성공률은 실험 전 미확인"], "permissions": [], "warnings": [],
            "compatibility": "항상 호환 (설치 없음)"}


# ---------------- recommend ----------------

def recommend(conn, task, policy=POLICY_V1, bodies=None, budget_caps=None, rubric_approved=True, exploratory=False):
    """task: dict from finder.analyze (optionally with "task_id"). Returns a result dict (see render)."""
    bodies = bodies or {}
    base = {"query": task["query"], "task": task, "policy_version": policy["policy_version"],
            "synonym_version": policy.get("synonym_version"), "task_id": task.get("task_id"),
            "approval_required": True, "auto_install": False}
    if task.get("task_id"):
        rubric_ok, rubric_note = rubric_state(conn, task["task_id"])
    else:
        rubric_ok, rubric_note = bool(rubric_approved), "호출자가 rubric 승인 확인" if rubric_approved else "rubric 미승인"
    if not rubric_ok and not exploratory:
        return {**base, "decision": "BLOCKED", "rubric": rubric_note, "ranked": [], "excluded": [],
                "verdict": "BLOCKED", "verdict_reason": f"선정 전 승인된 rubric 필요 ({rubric_note})"}
    decision = "PROPOSED_NOT_VALIDATED" if rubric_ok else "EXPLORATORY"
    if not rubric_ok:
        rubric_note += " → 평가 불가, 탐색용"

    found = finder.find(conn, task, top_k=policy["top_k"])
    excluded, scored = [], []
    for c in found["hits"]:
        g = gate(c, task)
        if g:
            excluded.append({"skill_id": c["id"], "name": c["name"], "status": g[0], "reason": g[1]})
        else:
            scored.append(score_skill(conn, c, task, policy, found["idf"]))
    any_eval = any(s["parts"]["evidence"][0] is not None for s in scored)
    no_skill = score_no_skill(task, policy, any_eval)
    key = lambda x: -(x["score"] or 0)  # noqa: E731
    scored.sort(key=key)
    # detailed review only for the top max_detailed (6.2); body re-read changes coverage/env only
    detailed = scored[: policy["max_detailed"]]
    hits = {c["id"]: c for c in found["hits"]}
    if bodies:
        detailed = [score_skill(conn, hits[s["skill_id"]], task, policy, found["idf"], bodies.get(s["skill_id"]))
                    for s in detailed]
    for s in scored[policy["max_detailed"]:]:
        excluded.append({"skill_id": s["skill_id"], "name": s["name"], "status": "제외",
                         "reason": f"상세 검토 상한 {policy['max_detailed']}개 밖 (점수 {s['score']})"})
    ranked = sorted(detailed + [no_skill], key=key)
    best = next((s for s in ranked if s["skill_id"] != NO_SKILL), None)
    hold = (best is None or best["parts"]["task_fit"][0] < policy["hold_fit"]
            or (best["score"] or 0) < policy["hold_score"] or ranked[0]["skill_id"] == NO_SKILL)
    if hold:
        ranked = [no_skill] + [s for s in ranked if s is not no_skill]
    for i, s in enumerate(ranked, 1):
        s["rank"] = i
        if s["skill_id"] == NO_SKILL:
            s["proposed_action"] = "스킬 없음 권장" if i == 1 else "기준선(비교용)"
            s["reason"] = ("적합 후보가 약해 우선" if i == 1 else "비교 기준선: 비용·위험 0") + \
                ", 설치·컨텍스트 비용 0"
        elif i == 1:
            s["proposed_action"], s["reason"] = ACTION_INSTALL, "비교 범위 내 1위 (선정 점수는 예측, 실측 아님)"
        else:
            s["proposed_action"] = ACTION_HOLD
            s["reason"] = "차순위: 1위와 동시 설치하지 말 것(설명 유사 시 호출 간섭 가설 GT-03), 대안 비교용"
        s["breakdown"] = {k: {"value": v, "weight": policy["weights"][k], "reason": r} for k, (v, r) in s.pop("parts").items()}
        s["decision"] = decision
    out = ranked[: policy["max_output"]]
    if all(s["skill_id"] != NO_SKILL for s in out):
        out.append(no_skill)                                  # NO_SKILL is always shown on the same screen
    if best is None:
        why = "검색·게이트 통과 후보 없음"
    else:
        why = (f"최고 후보 {best['name']}: 과제적합도 {best['breakdown']['task_fit']['value']}, 총점 {best['score']} "
               f"(기준: 적합도≥{policy['hold_fit']}, 총점≥{policy['hold_score']}, NO_SKILL {no_skill['score']})")
    if task.get("unmapped_ko"):
        why += f"; 동의어 사전에 없는 단어: {', '.join(task['unmapped_ko'])}"
    top = out[0]
    return {**base, "decision": decision, "rubric": rubric_note,
            "verdict": "보류/스킬 없음 권장" if hold else f"설치 추천: {top['name']}", "verdict_reason": why,
            "ranked": out, "excluded": excluded,
            "retrieval": {"terms": found["terms"], "match": found["match"], "hits": len(found["hits"]),
                          "dedup_notes": found["dedup_notes"]},
            "guidance": ["후보는 한 번에 하나씩 설치해 비교 (설명이 비슷한 스킬끼리 호출 간섭 가설, GT-03)",
                         "'적게, 맞는 것만': 스킬 목록은 컨텍스트 예산 안에서만 노출됨"],
            "detailed_ids": [s["skill_id"] for s in detailed],
            "budget_caps": budget_caps or {},
            "next_step": next_step(task, ranked, budget_caps, decision)}


def next_step(task, ranked, caps, decision, decision_id="<decision_id>"):
    if decision != "PROPOSED_NOT_VALIDATED":
        return "rubric 승인 후 다시 선정하세요 (탐색 결과는 평가에 쓰지 않음)."
    skills = [s["skill_id"] for s in ranked if s["skill_id"] != NO_SKILL][:2]
    cmd = ["python -m lab.evaluator plan", f"--decision {decision_id}",
           f"--task-id {task.get('task_id') or '<task_id>'}", "--arms NO_SKILL,AUTO,FORCED"]
    cmd += [f'--candidate "{s}"' for s in skills]
    caps = caps or {}
    if caps.get("per_call_max_usd") is not None:
        cmd.append(f"--max-budget-usd {caps['per_call_max_usd']}")     # -> claude -p --max-budget-usd
    if caps.get("per_suite_max_usd") is not None:
        cmd.append(f"--max-cost-usd {caps['per_suite_max_usd']}")      # -> claude plugin eval --max-cost-usd
    for k in ("max_turns", "timeout_seconds"):
        if caps.get(k) is not None:
            cmd.append(f"--{k.replace('_', '-')} {caps[k]}")
    return " ".join(cmd) + ("" if caps else "   # 예산 상한 없음: Budget Guard 값 필요")


# ---------------- persistence + human approval ----------------

def save(conn, result, task_id=None):
    if result["decision"] == "BLOCKED":
        raise ValueError("BLOCKED 결과는 저장할 결정이 없음")
    did = "dec-" + uuid.uuid4().hex[:12]
    ts = db.now()
    ctx = {k: result.get(k) for k in ("task", "excluded", "retrieval", "verdict", "verdict_reason", "rubric",
                                      "budget_caps", "synonym_version")}
    with db.tx(conn):
        for s in result["ranked"]:
            evidence = {"items": s["evidence"], "permissions": s["permissions"], "warnings": s["warnings"]}
            if s["skill_id"] == NO_SKILL:
                evidence["decision_context"] = ctx
            conn.execute(
                "INSERT INTO recommendations (decision_id, task_id, query, skill_id, rank, score_json, evidence_json,"
                " unknowns_json, decision, policy_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (did, task_id or result.get("task_id"), result["query"],
                 None if s["skill_id"] == NO_SKILL else s["skill_id"], s["rank"],
                 json.dumps({"total": s["score"], "breakdown": s["breakdown"], "unmeasured": s["unmeasured"],
                             "proposed_action": s["proposed_action"]}, ensure_ascii=False),
                 json.dumps(evidence, ensure_ascii=False), json.dumps(s["unknowns"], ensure_ascii=False),
                 result["decision"], result["policy_version"], ts))
    result["decision_id"] = did
    result["next_step"] = next_step(result["task"], result["ranked"], result.get("budget_caps"), result["decision"], did)
    return did


def approve(conn, decision_id, choice, approver):
    """Human approval of one ranked option. The selector never approves its own output."""
    if not approver or approver.strip().lower() in SELF_NAMES:
        raise PermissionError("사람 승인자 이름 필요 (selector/AI 자기 승인 불가)")
    rows = conn.execute("SELECT id, skill_id, decision, approved_at FROM recommendations WHERE decision_id = ?",
                        (decision_id,)).fetchall()
    if not rows:
        raise KeyError(f"decision {decision_id} 없음")
    if any(r["approved_at"] for r in rows):
        raise ValueError(f"decision {decision_id} 이미 승인됨")
    if rows[0]["decision"] != "PROPOSED_NOT_VALIDATED":
        raise ValueError(f"{rows[0]['decision']} 결정은 승인 불가 (승인된 rubric 필요)")
    target = None if choice == NO_SKILL else choice
    row = next((r for r in rows if r["skill_id"] == target), None)
    if row is None:
        raise ValueError(f"{choice} 는 이 결정의 후보가 아님: {[r['skill_id'] or NO_SKILL for r in rows]}")
    conn.execute("UPDATE recommendations SET approved_by = ?, approved_at = ? WHERE id = ?", (approver, db.now(), row["id"]))
    return row["id"]


# ---------------- report ----------------

def _fmt(v):
    return "미확인" if v is None else f"{v:.2f}"


def render(res):
    t = res["task"]
    out = [f"질의: {res['query']}",
           f"과제 초안: type={t['task_type']} | 산출물={', '.join(t['deliverables']) or '-'} | "
           f"입력={', '.join(t['inputs']) or '-'} | 스택={', '.join(t['stack']) or '-'}"
           + (f" (추정: {', '.join(t['stack_inferred'])})" if t.get("stack_inferred") else "")
           + f" | 도구={', '.join(t['tools']) or '-'}",
           f"개념: {', '.join(c['term'] + '→' + '/'.join(c['expand']) for c in t['concepts']) or '(없음)'}",
           f"정책: {res['policy_version']} | rubric: {res.get('rubric')} | 결정: {res['decision']}",
           f"판정: {res['verdict']} — {res['verdict_reason']}", ""]
    if res["decision"] == "BLOCKED":
        return "\n".join(out + ["선정 중단: 평가 기준(rubric)을 먼저 승인하세요. (--no-rubric 은 탐색용)"])
    for s in res["ranked"]:
        out.append(f"[{s['rank']}] {s['name']}  ({s['skill_id']})  총점 {_fmt(s['score'])}  → {s['proposed_action']}")
        out.append(f"    선정 이유: {s['reason']}")
        for k, b in s["breakdown"].items():
            out.append(f"      {k:<9}{_fmt(b['value']):>6} x{b['weight']:<3} {b['reason']}")
        if s["unmeasured"]:
            out.append(f"    미측정 항목(총점 계산 제외): {', '.join(s['unmeasured'])}")
        out.append(f"    호환성: {s['compatibility']}")
        out.append(f"    필요한 권한: {'; '.join(s['permissions']) or '선언 없음'}")
        out.append(f"    근거: {' | '.join(e['claim'] + ' [' + e['source'] + ']' for e in s['evidence'])}")
        out.append(f"    미확인 정보: {', '.join(s['unknowns']) or '없음'}")
        if s["warnings"]:
            out.append(f"    경고: {'; '.join(s['warnings'])}")
    if res["excluded"]:
        out.append("\n비선정/제외 이유:")
        capped = [e for e in res["excluded"] if e["reason"].startswith("상세 검토 상한")]
        out += [f"  - [{e['status']}] {e['name']} ({e['skill_id']}): {e['reason']}"
                for e in res["excluded"] if e not in capped]
        if capped:
            out.append(f"  - 상세 검토 상한 밖(점수순 {len(capped)}개): "
                       + ", ".join(f"{e['name']}" for e in capped))
    r = res["retrieval"]
    out.append(f"\n검색: FTS5 bm25, 용어 {len(r['terms'])}개, 히트 {r['hits']}개, 중복 제거 {len(r['dedup_notes'])}건")
    out += [f"  - 중복: {d['dropped']} → {d['kept']} ({d['reason']})" for d in r["dedup_notes"]]
    out += ["", *("※ " + g for g in res["guidance"])]
    out.append(f"\n다음 단계: {res['next_step']}")
    if res.get("decision_id"):
        out.append(f"저장됨: {res['decision_id']} → 승인: python -m lab.selector approve {res['decision_id']} "
                   f"<skill_id|NO_SKILL> --by <이름>")
    out.append("\n※ 결정: " + res["decision"] + " — 선정 점수는 예측이며 효과의 증거가 아님. "
               "사람 승인 필요, 자동 설치·실행 없음.")
    return "\n".join(out)


# ---------------- CLI ----------------

def fetch_body(c, limit):
    """Raw SKILL.md at the pinned commit. Untrusted DATA: used for keyword tokens only, never printed."""
    url = c["metadata"].get("source_url") or ""
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/blob/(.+)", url)
    if m:
        url = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}"
    elif c.get("repo") and c.get("commit_sha") and c.get("path"):
        url = f"https://raw.githubusercontent.com/{c['repo']}/{c['commit_sha']}/{c['path']}"
    else:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read(limit).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - failed fetch stays "본문 미확인"
        print(f"[warn] 본문 가져오기 실패 {c['id']}: {e}", file=sys.stderr)
        return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "approve":
        ap = argparse.ArgumentParser(prog="lab.selector approve")
        ap.add_argument("decision_id")
        ap.add_argument("choice", help="skill_id 또는 NO_SKILL")
        ap.add_argument("--by", required=True)
        ap.add_argument("--db", default=str(db.DB_PATH))
        a = ap.parse_args(argv[1:])
        conn = db.connect(a.db)
        try:
            approve(conn, a.decision_id, a.choice, a.by)
        except (PermissionError, KeyError, ValueError) as e:
            sys.exit(f"승인 실패: {e}")
        print(f"{a.decision_id}: {a.choice} 승인됨 (by {a.by})")
        return
    ap = argparse.ArgumentParser(prog="lab.selector", description="스킬 선정 (사람 승인 필요, 자동 설치 없음)")
    ap.add_argument("query")
    ap.add_argument("--stack", default="")
    ap.add_argument("--tools", default="")
    ap.add_argument("--constraints", default="", help="예: claude.ai,skills-api")
    ap.add_argument("--task-id")
    ap.add_argument("--no-rubric", action="store_true", help="rubric 미승인 탐색 (EXPLORATORY)")
    ap.add_argument("--read-candidates", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--caps", default="", help='Budget Guard 상한 JSON, 예: {"per_call_max_usd":0.5}')
    ap.add_argument("--db", default=str(db.DB_PATH))
    a = ap.parse_args(argv)
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
    conn = db.connect(a.db)
    task = finder.analyze(a.query, split(a.stack), split(a.tools), split(a.constraints))
    if a.task_id:
        task["task_id"] = a.task_id
    kw = dict(budget_caps=json.loads(a.caps) if a.caps else None, rubric_approved=False, exploratory=a.no_rubric)
    res = recommend(conn, task, **kw)
    if a.read_candidates and res.get("detailed_ids"):
        hits = {c["id"]: c for c in finder.find(conn, task, POLICY_V1["top_k"])["hits"]}
        bodies = {sid: b for sid in res["detailed_ids"]
                  if (b := fetch_body(hits[sid], POLICY_V1["body_max"])) is not None}
        res = recommend(conn, task, bodies=bodies, **kw)
    if a.save and res["decision"] != "BLOCKED":
        save(conn, res, a.task_id)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(res))
    if res["decision"] == "BLOCKED":
        sys.exit(2)


if __name__ == "__main__":
    main()
