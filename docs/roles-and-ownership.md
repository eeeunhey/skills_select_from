# Roles and ownership (PRD 16–17, 24)

Roles are capabilities, not head count. One operator may hold several implementation roles, but **approval and independent review are separate acts**.

| Decision | Who does it | Who approves | How the repo enforces it |
|---|---|---|---|
| Rubric and success criteria | QA / domain author (`rubrics/`) | Project owner | `lab.rubrics approve --by` sets `approved_at`; DB trigger freezes it |
| Source registration | Collector config | Project owner | `config/sources.yaml` in git history |
| Skill recommendation | Selector (`lab.selector`) | Operator | `lab.selector approve` only accepts a ranked choice; the selector never approves |
| Running an external skill | Executor (`lab.evaluator`) | User | Quarantined skills refused; `--trust-plugin` needs an operator flag |
| Grading | External verifier + human | Evaluation owner | Agent self-reported tests are not outcome evidence |
| Selection-policy change | Ranking owner | Project owner | `lab.audit` compares policies on the holdout set |
| Budget-cap increase | — | User | Caps only change by editing `config/budgets.yaml` |

## How this repo was built (internal preliminary experiment)
- The ground truth (`rubrics/`, `benchmarks/`, `fixtures/`) and the selector (`lab/finder.py`, `lab/selector.py`) were written by **separate agent sessions**. The selector session was told not to read the ground-truth paths (PRD 27.3).
- That is separation by prompt only. Both sessions ran the same model, so they may share biases (PRD 17, rule 5). All labels are `draft-unreviewed`.
- Until an independent person reviews them, every result is a **내부 예비 실험 (internal preliminary experiment)**, not an independent evaluation (PRD 16.3).
