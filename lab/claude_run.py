"""Thin subprocess wrappers around the Claude Code CLI (PRD 26.2). No retries here: Claude Code
retries API errors itself (system/api_retry); the orchestrator classifies the final outcome.

Isolation achievable without logging out profile S (verified on 2.1.327, docs/claude-runtime.md):
  --setting-sources project  -> user settings (enabledPlugins, hooks, permissions) are not read
  --strict-mcp-config        -> no MCP servers unless passed
  --plugin-dir <candidate>   -> only the candidate plugin is added
  --settings {"disableSkillShellExecution": true}
  allowlisted env (clean_env: no parent CLAUDE* session vars, no stray tokens), fresh temp cwd outside any repo.
The leftover risk (personal ~/.claude/skills, CLAUDE.md in ancestors) is detected, not prevented:
lab.ledger.isolation_check on the init message + preflight_leaks() below -> run INVALID.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from lab import ledger

CLAUDE = shutil.which("claude") or "claude"
RUN_SETTINGS = json.dumps({"disableSkillShellExecution": True})


class ClaudeRunError(Exception):
    """Carries what lab.runtime._normalize / ledger.classify_error read."""
    def __init__(self, info, parsed=None, exit_code=None, stderr="", usd=None):
        super().__init__(info.get("error_code") or info.get("state"))
        self.parsed, self.exit_code, self.stderr, self.usd = parsed, exit_code, stderr, usd
        self.retry_after = info.get("retry_after_s")
        self.reset_at = info.get("reset_at")
        self.attempts_used = info.get("attempts_used", 1)


def version():
    out = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    return out.split()[0] if out else None  # "2.1.327 (Claude Code)" -> "2.1.327"


# Allowlist, not denylist: the agent and the verifier (which imports agent-written code under node) must never
# inherit GITHUB_TOKEN / ANTHROPIC_* / AWS_* etc. from the operator's shell.
SAFE_ENV = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA",
            "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "NODE_PATH", "LANG", "PYTHONIOENCODING"}


def minimal_env():
    """Only SAFE_ENV (case-insensitive: Windows env names are)."""
    return {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV}


def clean_env(auth_profile=None):
    """Env for the `claude` CLI: minimal_env() (parent-session CLAUDE* vars are thus dropped) + what auth needs.
    Profile S authenticates from the user config dir (USERPROFILE/APPDATA, optionally CLAUDE_CONFIG_DIR);
    profile K additionally gets ANTHROPIC_API_KEY. No other ANTHROPIC_* / tokens are passed."""
    if auth_profile is None:
        import yaml
        from lab.db import ROOT
        auth_profile = yaml.safe_load((ROOT / "config" / "runtime.yaml").read_text(encoding="utf-8"))["claude"]["auth_profile"]
    keep = {"CLAUDE_CONFIG_DIR"} | ({"ANTHROPIC_API_KEY"} if auth_profile == "K" else set())
    return {**minimal_env(), **{k: v for k, v in os.environ.items() if k.upper() in keep}}


def preflight_leaks(workdir):
    """Personal memory files Claude Code would load for this cwd (init message does not list them)."""
    found = []
    home = Path.home()
    for f in (home / ".claude" / "CLAUDE.md", home / ".claude" / "CLAUDE.local.md"):
        if f.exists():
            found.append(str(f))
    for d in [Path(workdir).resolve(), *Path(workdir).resolve().parents]:
        for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", ".mcp.json"):
            if (d / name).exists() and d != Path(workdir).resolve():
                found.append(str(d / name))
    return found


def run_p(prompt, workdir, model, allowed_tools, artifact_path, plugin_dir=None, max_budget_usd=None,
          max_turns=10, timeout_s=300):
    """claude -p in `workdir`; full stream-json saved to artifact_path (evidence). -> dict."""
    cmd = [CLAUDE, "-p", prompt, "--model", model, "--output-format", "stream-json", "--verbose",
           "--permission-prompts", "none", "--setting-sources", "project", "--strict-mcp-config",
           "--no-session-persistence", "--settings", RUN_SETTINGS, "--max-turns", str(max_turns)]
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]
    if plugin_dir:
        cmd += ["--plugin-dir", str(plugin_dir)]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    Path(artifact_path).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    with open(artifact_path, "x", encoding="utf-8") as out:
        try:
            proc = subprocess.run(cmd, cwd=workdir, stdout=out, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                  text=True, encoding="utf-8", errors="replace", timeout=timeout_s, env=clean_env())
            code, err = proc.returncode, proc.stderr
        except subprocess.TimeoutExpired as e:
            code, err = 143, f"timeout after {timeout_s}s {e.stderr or ''}"
    latency = int((time.monotonic() - t0) * 1000)
    parsed = ledger.read_transcript(artifact_path)
    return {"exit_code": code, "stderr": err[-4000:] if err else "", "latency_ms": latency, "parsed": parsed,
            "artifact": str(artifact_path), "classification": ledger.classify(parsed, code, err)}


def run_plugin_eval(plugin_dir, model, judge_model, runs, json_path, max_cost_usd=None, keep_temp=True,
                    operator_reviewed=False, allow_tools=(), scaffold=False, timeout_s=3600):
    """claude plugin eval <dir> ... --json <path> --no-publish. Gated flags (--trust-plugin, --allow-tools,
    --scaffold) only with operator_reviewed=True (CLI flag --i-reviewed-this-candidate, PRD 10/26.2).
    --allow-real-servers is never passed. Without trust, a --json run is refused (exit 1) - by design."""
    cmd = [CLAUDE, "plugin", "eval", str(plugin_dir), "--runs", str(runs), "--model", model,
           "--judge-model", judge_model, "--no-publish", "--json", str(json_path)]
    if max_cost_usd is not None:
        cmd += ["--max-cost-usd", str(max_cost_usd)]
    if keep_temp:
        cmd += ["--keep-temp"]
    if operator_reviewed:
        cmd += ["--trust-plugin"]
        if scaffold:
            cmd += ["--scaffold"]
        if allow_tools:
            cmd += ["--allow-tools", *allow_tools]  # last: it swallows following words
    elif allow_tools or scaffold:
        raise PermissionError("--allow-tools/--scaffold need --i-reviewed-this-candidate")
    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=timeout_s, env=clean_env())
    result = None
    if Path(json_path).exists():
        result = json.loads(Path(json_path).read_text(encoding="utf-8"))
    return {"exit_code": proc.returncode, "stdout": proc.stdout[-8000:], "stderr": proc.stderr[-8000:],
            "latency_ms": int((time.monotonic() - t0) * 1000), "result": result, "json_path": str(json_path)}
