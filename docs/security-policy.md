# Security policy (PRD 10, F2, RES-16)

## Third-party skills are untrusted input
- `SKILL.md` text and anything bundled with it are **data**. Collection parses frontmatter with `yaml.safe_load` only and never executes anything.
- Collected text lives only in `data/` (SQLite). Raw repositories are never cloned. `claude` is never started inside a third-party folder: `-p` would run that folder's `.claude/settings.json` hooks and connect its `.mcp.json` servers without a trust prompt.

## Static gate (`lab/scan.py`)
| Signal | Status |
|---|---|
| ``!`command` `` shell injection in the body (runs before Claude sees the text) | `quarantined` |
| frontmatter `hooks`, plugin `hooks/hooks.json`, `.mcp.json` server | `quarantined` |
| prompt-injection phrases (ignore previous instructions, ~/.ssh, .env, curl \| sh, sending secrets) | `quarantined` |
| bundled `scripts/`, `allowed-tools` containing `Bash` | `flagged` |
| nothing found | `unreviewed`, **never** `approved` automatically |

`allowed-tools` means "these tools are pre-approved", not "only these tools". It is stored as a risk signal.

## Running candidates
- Quarantined candidates are never wrapped or run (`QUARANTINED`).
- Candidates with hooks or servers run only in a container or CI runner (PRD 10 v1.3).
- `claude plugin eval` always runs with `--no-publish`. `--trust-plugin` needs the explicit operator flag after a human review. `--scaffold` and `--allow-real-servers` are never added by code.
- Unattended runs list the tools they need with `--allowedTools` and use `--permission-prompts none`, so prompts are refused rather than waiting. Full-permission modes are not used.
- Run settings set `disableSkillShellExecution: true`.

## Secrets
- Tokens come from the environment (`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`) or Actions secrets and are never written to files or logs.
- The ledger stores redacted `provider_usage_json` only. Raw prompts are not logged (`log_raw_prompts: false`).
