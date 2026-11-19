"""python -m lab <command> ...  — thin dispatcher over the role modules (PRD 17.1)."""
import importlib
import sys

COMMANDS = {
    "collect": ("lab.collector", "F1/F2: collect SKILL.md metadata + static security scan (no LLM)"),
    "rubrics": ("lab.rubrics", "F0: register / approve (human) / check frozen rubrics"),
    "select": ("lab.selector", "F3/F4: candidates + NO_SKILL with reasons; approve <decision> <choice>"),
    "eval": ("lab.evaluator", "F5: plan / execute / report NO_SKILL vs AUTO vs FORCED"),
    "ledger": ("lab.ledger", "F8: load a stream-json transcript into llm_calls"),
    "audit": ("lab.audit", "F6/M4: score selector policies on labeled/holdout tasks"),
    "runtime": ("lab.runtime", "F9: nightly worker (05:00 KST), status, pause/resume/cancel"),
    "dashboard": ("lab.dashboard", "21.1: static ops dashboard HTML (--out reports/dashboard.html)"),
    "api": ("lab.api", "22.3: local REST API on 127.0.0.1 (POST needs LAB_API_TOKEN)"),
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        print("usage: python -m lab <command> [args]\n")
        for k, (_, d) in COMMANDS.items():
            print(f"  {k:<8} {d}")
        return 0 if not argv or argv[0] in ("-h", "--help") else 2
    mod = importlib.import_module(COMMANDS[argv[0]][0])
    return mod.main(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
