"""Static check of one SKILL.md (PRD F2 v1.3 fields, 10, RES-16). Reads text only; never executes anything.

Collected text is untrusted DATA: frontmatter goes through yaml.safe_load, the body is only regex-matched.
"""
import re

import yaml

SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
DESC_LIMIT = 1536  # Claude Code truncates description + when_to_use beyond this
SCRIPT_EXT = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".bat", ".cmd", ".rb", ".pl")
SHELL_INJECTION = re.compile(r"!`[^`\n]+`|^\s*```!", re.M)  # !`cmd` runs in the shell before Claude reads the skill
INJECTION_SIGNALS = {
    "ignore_instructions": r"\b(ignore|disregard|forget)\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)",
    "ssh_keys": r"~/\.ssh\b|\bid_(rsa|ed25519)\b",
    "dotenv_read": r"\b(cat|source|read|open|type|less|head)\s+\S*\.env\b",
    "pipe_to_shell": r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b",
    "send_secrets": r"\b(send|upload|post|exfiltrat\w*|transmit)\s+(the\s+|your\s+|all\s+|any\s+)*(secrets?|credentials?|api[ _-]?keys?|private[ _-]?keys?|passwords?|tokens?)\b",
}
# ponytail: naive regexes, a lexer/LLM review is the upgrade when false positives hurt


def parse_frontmatter(text):
    """-> (meta dict, parse_ok). Missing or broken frontmatter -> ({}, False)."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, False
    end = text.find("\n---", 3)
    if end == -1:
        return {}, False
    try:
        meta = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}, False
    return (meta, True) if isinstance(meta, dict) else ({}, False)


def _tools(v):
    if isinstance(v, str):
        return re.findall(r"[\w*.-]+(?:\([^)]*\))?", v)
    if isinstance(v, list):
        return [str(x) for x in v]
    return []


def scan(text, files=(), plugin_files=()):
    """text: SKILL.md; files: paths relative to the skill folder; plugin_files: relative to the plugin root (if any)."""
    meta, ok = parse_frontmatter(text)
    tools = _tools(meta.get("allowed-tools"))
    desc = len(str(meta.get("description") or "")) + len(str(meta.get("when_to_use") or ""))
    near = list(files) + list(plugin_files)
    r = {
        "frontmatter_parse_ok": ok,
        "spec_fields_only": ok and set(meta) <= SPEC_KEYS,
        "claude_code_extensions": sorted(str(k) for k in meta if k not in SPEC_KEYS),
        "description_chars": desc,
        "truncated_in_claude": desc > DESC_LIMIT,
        "has_bundled_scripts": any(f.startswith("scripts/") or f.lower().endswith(SCRIPT_EXT) for f in files),
        "has_shell_injection": bool(SHELL_INJECTION.search(text)),
        "has_skill_hooks": bool(meta.get("hooks")),
        "declared_allowed_tools": tools,
        "allowed_tools_bash_scope": [t for t in tools if t == "Bash" or t.startswith("Bash(")],
        "has_mcp_config": any(f.rsplit("/", 1)[-1] == ".mcp.json" for f in near),
        "has_plugin_hooks": any(f == "hooks/hooks.json" or f.endswith(".claude/settings.json") for f in near),
        "manual_only": meta.get("disable-model-invocation") is True,
        "prompt_injection_signals": [k for k, p in INJECTION_SIGNALS.items() if re.search(p, text, re.I)],
    }
    if r["has_shell_injection"] or r["has_skill_hooks"] or r["has_plugin_hooks"] or r["has_mcp_config"] \
            or r["prompt_injection_signals"]:
        r["security_status"] = "quarantined"
    elif r["has_bundled_scripts"] or r["allowed_tools_bash_scope"]:
        r["security_status"] = "flagged"
    else:
        r["security_status"] = "unreviewed"  # unchecked is not safe (F2): never 'approved' automatically
    return r
