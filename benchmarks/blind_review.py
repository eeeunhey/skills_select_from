"""Blinded human review of the human-graded requirements (PRD 2.3, 18.4): T04-R3, T08-R4, T09-R4.

  python benchmarks/blind_review.py build  --campaign exp-T__-20261120T1349 --out reports/expert-reviews/blind-2026-11-21
  python benchmarks/blind_review.py import --key data/blind-keys/blind-2026-11-21.json --scores <scores.json> --reviewer <name>

build: one item per run, shuffled with a recorded seed. Items show only screenshots / a code diff under anonymous ids:
no arm, skill, run id or score. The key (item -> run) goes to data/blind-keys/ (gitignored) so the reviewer is not shown it.
import: stores the scores. A requirement's eval_checks row turns pass/fail only when the rubric's own threshold is met
(T04-R3: median >= 4 over >= 2 reviewers, scores > 1 apart -> DISPUTED). Otherwise it stays 'unevaluated' and the
score is kept as evidence. T08-R4 and T09-R4 define no threshold, so they stay unevaluated with scores attached.
"""
import argparse
import base64
import difflib
import html
import json
import random
import secrets
import shutil
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab import db  # noqa: E402

FIXTURE = ROOT / "fixtures" / "react-demo"
KEYS = ROOT / "data" / "blind-keys"
ITEMS = {"T04": "R3", "T08": "R4", "T09": "R4"}
ANCHORS = {
    "T04": ["버튼이 화면마다 눈에 띄게 다름(같은 역할인데 색·모양·크기 다름)", "불일치 버튼 여러 개", "눈에 띄는 예외 1개",
            "사소한 차이만(간격 등)", "같은 역할 버튼의 스타일이 화면 간 구분 불가"],
    "T08": ["375px에서 내용이 깨지거나 읽을 수 없음", "여러 곳이 겹치거나 잘림", "한 곳이 눈에 띄게 어색함",
            "사소한 어색함만", "두 폭 모두 깔끔하게 읽힘"],
    "T09": ["불필요한 변경이 많고 읽기 어려움", "변경이 과하거나 혼란스러움", "보통", "작고 읽기 쉬움, 사소한 군더더기",
            "최소한의 변경으로 명확하게 하나로 합침"],
}
QUESTION = {"T04": "버튼 스타일이 화면들 사이에서 얼마나 일관적인가?",
            "T08": "375px(모바일)과 1280px(데스크톱)에서 읽기 쉽고 깨짐이 없는가?",
            "T09": "중복 검증 로직을 하나로 합친 변경이 작고 읽기 쉬운가?"}


def diff_text(workdir):
    out = []
    for f in sorted((Path(workdir) / "src").rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(workdir).as_posix()
        base = FIXTURE / rel
        a = base.read_text(encoding="utf-8").replace("\r\n", "\n").splitlines() if base.exists() else []
        b = f.read_text(encoding="utf-8").replace("\r\n", "\n").splitlines()
        if a != b:
            out += list(difflib.unified_diff(a, b, f"base/{rel}", f"changed/{rel}", lineterm=""))
    return "\n".join(out) or "(변경 없음)"


def build(campaign, out):
    conn = db.connect()
    out = ROOT / out
    (out / "img").mkdir(parents=True, exist_ok=True)
    items = []
    for task, req in ITEMS.items():
        for r in conn.execute("SELECT run_id, notes FROM eval_runs WHERE experiment_id LIKE ? AND task_id=? AND status IN"
                              " ('PASSED','FAILED') ORDER BY run_id", (campaign + "%", task)).fetchall():
            arts = sorted(ROOT.glob(f"data/artifacts/{r['run_id']}.a*.verify-artifacts"))
            item = {"task": task, "req": req, "run_id": r["run_id"], "images": [], "diff": None}
            if task == "T09":
                wd = json.loads(r["notes"] or "{}").get("workdir")
                if not wd or not Path(wd).exists():
                    continue
                item["diff"] = diff_text(wd)
            else:
                shots = sorted((arts[-1] / "screenshots").glob("*.png")) if arts else []
                if not shots:
                    continue
                for s in shots:
                    name = secrets.token_hex(6) + ".png"
                    shutil.copyfile(s, out / "img" / name)
                    item["images"].append({"file": f"img/{name}", "label": s.stem.replace("_", " ")})
            items.append(item)
    seed = secrets.randbits(32)
    random.Random(seed).shuffle(items)
    key = {"seed": seed, "campaign": campaign, "items": {}}
    for i, it in enumerate(items, 1):
        it["id"] = f"{it['task']}-{i:02d}"     # task stays visible (different questions); run identity does not
        key["items"][it["id"]] = {"run_id": it["run_id"], "task": it["task"], "req": it["req"]}
    KEYS.mkdir(parents=True, exist_ok=True)
    (KEYS / f"{out.name}.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
    (out / "index.html").write_text(render(items), encoding="utf-8")
    print(f"{len(items)} items -> {out / 'index.html'}  (key: data/blind-keys/{out.name}.json — do not open before scoring)")


def render(items):
    css = """:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--card:#fff;--accent:#2f5d8a}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#9c9b95;--line:#33332f;--card:#1f1f1d;--accent:#8fb3d9}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,'Malgun Gothic',sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 120px}h1{font-size:22px;margin:0 0 4px}
.lead{color:var(--muted);margin:0 0 24px}.item{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 20px}
.item h2{font-size:16px;margin:0 0 6px}.q{margin:0 0 12px}.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}
figure{margin:0}figure img{width:100%;border:1px solid var(--line);border-radius:6px;cursor:zoom-in}figcaption{font-size:12px;color:var(--muted)}
pre{overflow:auto;max-height:420px;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px;font-size:12.5px}
.scale{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}.scale label{border:1px solid var(--line);border-radius:6px;padding:6px 10px;cursor:pointer;flex:1 1 160px;font-size:13px}
.scale input{margin-right:6px}.scale label:has(input:checked){border-color:var(--accent);outline:1px solid var(--accent)}
textarea{width:100%;box-sizing:border-box;margin-top:8px;min-height:44px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px}
.bar{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;justify-content:center}
button{background:var(--accent);color:var(--bg);border:0;border-radius:6px;padding:9px 16px;font-weight:600;cursor:pointer}
.d-add{color:#2e7d32}.d-del{color:#c62828}dialog{max-width:95vw;border:0;padding:0;background:transparent}dialog img{max-width:95vw;max-height:92vh}"""
    body = []
    for it in items:
        if it["diff"] is not None:
            lines = "".join(f'<span class="{"d-add" if l.startswith("+") else "d-del" if l.startswith("-") else ""}">'
                            f"{html.escape(l)}</span>\n" for l in it["diff"].splitlines())
            media = f"<pre>{lines}</pre>"
        else:
            media = '<div class="shots">' + "".join(
                f'<figure><img loading="lazy" src="{im["file"]}" alt="{html.escape(im["label"])}"><figcaption>{html.escape(im["label"])}'
                f"</figcaption></figure>" for im in it["images"]) + "</div>"
        scale = "".join(f'<label><input type="radio" name="{it["id"]}" value="{n}">{n} — {html.escape(a)}</label>'
                        for n, a in enumerate(ANCHORS[it["task"]], 1))
        body.append(f'<section class="item" id="{it["id"]}"><h2>{it["id"]}</h2><p class="q">{QUESTION[it["task"]]}</p>{media}'
                    f'<div class="scale">{scale}</div><textarea data-note="{it["id"]}" placeholder="메모 (선택)"></textarea></section>')
    js = """const ids=[...document.querySelectorAll('section.item')].map(s=>s.id);
function collect(){const o={};for(const id of ids){const c=document.querySelector(`input[name="${id}"]:checked`);
const n=document.querySelector(`textarea[data-note="${id}"]`).value.trim();if(c||n)o[id]={score:c?+c.value:null,note:n}}return o}
function upd(){const n=Object.values(collect()).filter(x=>x.score).length;document.getElementById('cnt').textContent=`${n}/${ids.length} 채점`}
document.addEventListener('change',upd);
document.getElementById('dl').onclick=()=>{const b=new Blob([JSON.stringify(collect(),null,1)],{type:'application/json'});
const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='blind-scores.json';a.click()};
const dlg=document.getElementById('z');document.addEventListener('click',e=>{if(e.target.matches('figure img')){dlg.querySelector('img').src=e.target.src;dlg.showModal()}});
dlg.onclick=()=>dlg.close();upd();"""
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Blind Review</title><style>{css}</style></head><body><main><h1>눈가림 평가</h1>"
            '<p class="lead">각 항목은 서로 다른 실행 결과입니다. 어떤 조건(스킬 사용 여부)에서 나온 결과인지는 숨겨져 있습니다. '
            "기준대로 1–5점을 고르고, 다 끝나면 아래 버튼으로 <b>blind-scores.json</b>을 저장하세요. 이미지를 누르면 확대됩니다.</p>"
            + "".join(body) +
            '</main><div class="bar"><span id="cnt"></span><button id="dl">점수 저장 (JSON)</button></div>'
            f'<dialog id="z"><img alt=""></dialog><script>{js}</script></body></html>')


def import_scores(key_path, scores_path, reviewer):
    key = json.loads(Path(key_path).read_text(encoding="utf-8"))
    scores = json.loads(Path(scores_path).read_text(encoding="utf-8"))
    store = ROOT / "reports" / "expert-reviews" / f"{Path(key_path).stem}-{reviewer}.json"
    store.write_text(json.dumps({"reviewer": reviewer, "key": key_path, "scores": scores}, ensure_ascii=False, indent=1), encoding="utf-8")
    conn = db.connect()
    # all reviewers' files for this key -> per item list of scores
    per = {}
    for f in (ROOT / "reports" / "expert-reviews").glob(f"{Path(key_path).stem}-*.json"):
        for iid, v in json.loads(f.read_text(encoding="utf-8"))["scores"].items():
            if v.get("score"):
                per.setdefault(iid, []).append(v["score"])
    changed = 0
    for iid, meta in key["items"].items():
        s = per.get(iid, [])
        if not s:
            continue
        verdict, why = "unevaluated", f"scores {s} (reviewers {len(s)})"
        if meta["task"] == "T04":
            if len(s) >= 2 and max(s) - min(s) > 1:
                why += " -> DISPUTED"
            elif len(s) >= 2:
                verdict = "pass" if statistics.median(s) >= 4 else "fail"
            else:
                why += " -> needs >= 2 reviewers (rubric)"
        else:
            why += " -> rubric defines no pass threshold"
        conn.execute("UPDATE eval_checks SET result=?, evidence_uri=? WHERE run_id=? AND requirement_id=? AND verifier='human'",
                     (verdict, f"{store.relative_to(ROOT).as_posix()} :: {iid}: {why}", meta["run_id"], meta["req"]))
        changed += 1
    print(f"stored {store.name}; updated {changed} human checks")
    return per


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--campaign", required=True)
    b.add_argument("--out", required=True)
    i = sub.add_parser("import")
    i.add_argument("--key", required=True)
    i.add_argument("--scores", required=True)
    i.add_argument("--reviewer", required=True)
    a = ap.parse_args(argv)
    build(a.campaign, a.out) if a.cmd == "build" else import_scores(a.key, a.scores, a.reviewer)


if __name__ == "__main__":
    main()
