"""Locating the user and company rule directories.

Tier 1 (project) is found beside the markdown source and is handled by the
caller. Tiers 2 and 3 live outside the project entirely, so they need a
resolution chain:

    tier 3 (company)   --company-rules PATH
                    -> $NARRAOKE_COMPANY_RULES
                    -> "company_rules_dir" in narraoke.config.json

    tier 2 (user)      --user-rules PATH
                    -> $NARRAOKE_USER_RULES
                    -> "user_rules_dir" in narraoke.config.json

This flag -> env-var -> config chain mirrors `_hf_cache_root` in
tts_engine.py, so it matches a convention already in the codebase.

**The config file is repo-local**, not `~/.config`: the deliberate choice is
that a checkout should be self-contained, so everything needed to reproduce a
render sits beside the code rather than in a hidden per-user location.

Two files make that work without publishing anyone's paths:

    narraoke.config.json          real, gitignored — your machine's paths
    narraoke.config.example.json  committed — the schema and a worked example

**Relative paths resolve against the config file**, not the working directory.
`"../narraoke-overrides"` therefore means "a sibling of this checkout" no
matter where `narraoke` is invoked from, which is what makes a relative path
safe to write down at all.

**The two tiers fail differently, on purpose.** An unconfigured company tier
is simply empty and silent. But a company path that *is* configured and does
not exist must fail loudly: a silently-absent NDA rule means a client name
gets mispronounced in a delivered video. An absent user tier is always normal
and silent — it holds personal preferences by definition.

Config is JSON, not TOML: `tomllib` is 3.11+ and this project targets 3.10,
so TOML would mean a new dependency. JSON reuses the JSONC comment stripper
the rule loader already has, so comments still work.
"""
from __future__ import annotations

import os
from pathlib import Path

CONFIG_FILE_NAME = "narraoke.config.json"
EXAMPLE_CONFIG_FILE_NAME = "narraoke.config.example.json"

ENV_COMPANY_RULES = "NARRAOKE_COMPANY_RULES"
ENV_USER_RULES = "NARRAOKE_USER_RULES"

# The repo root — this file is rules/discovery.py, so two levels up.
_REPO_ROOT = Path(__file__).resolve().parent.parent


class CompanyRulesMissing(RuntimeError):
    """A company rules directory was configured but does not exist."""


def config_path() -> Path:
    """The repo-local config file. May not exist; that is normal."""
    return _REPO_ROOT / CONFIG_FILE_NAME


def example_config_path() -> Path:
    """The committed example, documenting the schema."""
    return _REPO_ROOT / EXAMPLE_CONFIG_FILE_NAME


def _expand(value: str, base: Path | None = None) -> Path:
    """Expand `~` and env vars; resolve a relative path against *base*.

    Paths from the config file are resolved against the config file's own
    directory rather than the process working directory, so
    `"../narraoke-overrides"` keeps meaning "a sibling of this checkout"
    however narraoke is invoked. Paths from a flag or an env var are left to
    the shell's usual cwd-relative interpretation, which is what a caller
    typing a path expects.
    """
    expanded = Path(os.path.expandvars(os.path.expanduser(value.strip())))
    if base is not None and not expanded.is_absolute():
        return (base / expanded).resolve()
    return expanded


def resolve_company_rules_dir(
    flag: str | None = None,
    config: dict | None = None,
) -> Path | None:
    """Locate the tier-3 directory, or None when the tier is unconfigured.

    Raises `CompanyRulesMissing` when a path *is* configured but absent —
    rendering with a silently-missing company tier is the failure this guards
    against.
    """
    config = config or {}
    config_base = config_path().parent
    # (source label, raw value, base for relative paths)
    candidates: list[tuple[str, str, Path | None]] = []
    if flag:
        candidates.append(("--company-rules", flag, None))
    env_value = os.environ.get(ENV_COMPANY_RULES, "").strip()
    if env_value:
        candidates.append((f"${ENV_COMPANY_RULES}", env_value, None))
    configured = config.get("company_rules_dir")
    if isinstance(configured, str) and configured.strip():
        candidates.append((str(config_path()), configured, config_base))

    if not candidates:
        return None  # unconfigured is fine — the tier is simply empty

    source, raw, base = candidates[0]
    path = _expand(raw, base)
    if not path.is_dir():
        raise CompanyRulesMissing(
            f"company rules directory {str(path)!r} (from {source}) does not "
            f"exist. Rendering without it would silently drop the company "
            f"tier. Fix the path or unset it to run without company rules."
        )
    return path


def resolve_user_rules_dir(
    flag: str | None = None,
    config: dict | None = None,
) -> Path | None:
    """Locate the tier-2 directory, or None when there is nothing to load.

    Absent is normal and silent at every step of the chain — tier 2 holds
    personal preferences, so most machines will not have one.
    """
    config = config or {}
    configured = config.get("user_rules_dir")
    for raw, base in (
        (flag, None),
        (os.environ.get(ENV_USER_RULES, "").strip() or None, None),
        (configured if isinstance(configured, str) else None, config_path().parent),
    ):
        if raw and raw.strip():
            path = _expand(raw, base)
            return path if path.is_dir() else None
    return None


def rule_files_in(directory: Path) -> list[Path]:
    """Every `*.json` in *directory*, in sorted order.

    Sorted so a `10-`/`20-` numeric prefix convention fixes application order.
    Rule order is semantics, so this must never depend on filesystem order.
    """
    return sorted(p for p in directory.glob("*.json") if p.is_file())
