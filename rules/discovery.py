"""Locating the user and company rule directories.

Tier 1 (project) is found beside the markdown source and is handled by the
caller. Tiers 2 and 3 live outside the project entirely, so they need a
resolution chain:

    tier 3 (company)   --company-rules PATH
                    -> $NARRAOKE_COMPANY_RULES
                    -> "company_rules_dir" in config.json

    tier 2 (user)      --user-rules PATH
                    -> $NARRAOKE_USER_RULES
                    -> "user_rules_dir" in config.json
                    -> ${XDG_CONFIG_HOME:-~/.config}/narraoke/rules.d/

This flag -> env-var -> config -> default chain mirrors `_hf_cache_root` in
tts_engine.py, so it matches a convention already in the codebase.

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

CONFIG_DIR_NAME = "narraoke"
CONFIG_FILE_NAME = "config.json"
USER_RULES_DIR_NAME = "rules.d"

ENV_COMPANY_RULES = "NARRAOKE_COMPANY_RULES"
ENV_USER_RULES = "NARRAOKE_USER_RULES"


class CompanyRulesMissing(RuntimeError):
    """A company rules directory was configured but does not exist."""


def config_home() -> Path:
    """`$XDG_CONFIG_HOME` if set, else `~/.config`."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg)
    return Path.home() / ".config"


def config_path() -> Path:
    """Where the app config file lives."""
    return config_home() / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def default_user_rules_dir() -> Path:
    """The tier-2 location when nothing overrides it."""
    return config_home() / CONFIG_DIR_NAME / USER_RULES_DIR_NAME


def _expand(value: str) -> Path:
    """Expand `~` and environment variables in a configured path."""
    return Path(os.path.expandvars(os.path.expanduser(value.strip())))


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
    candidates: list[tuple[str, str]] = []
    if flag:
        candidates.append(("--company-rules", flag))
    env_value = os.environ.get(ENV_COMPANY_RULES, "").strip()
    if env_value:
        candidates.append((f"${ENV_COMPANY_RULES}", env_value))
    configured = config.get("company_rules_dir")
    if isinstance(configured, str) and configured.strip():
        candidates.append((str(config_path()), configured))

    if not candidates:
        return None  # unconfigured is fine — the tier is simply empty

    source, raw = candidates[0]
    path = _expand(raw)
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
    for raw in (
        flag,
        os.environ.get(ENV_USER_RULES, "").strip() or None,
        (config.get("user_rules_dir")
         if isinstance(config.get("user_rules_dir"), str) else None),
    ):
        if raw and raw.strip():
            path = _expand(raw)
            return path if path.is_dir() else None

    default = default_user_rules_dir()
    return default if default.is_dir() else None


def rule_files_in(directory: Path) -> list[Path]:
    """Every `*.json` in *directory*, in sorted order.

    Sorted so a `10-`/`20-` numeric prefix convention fixes application order.
    Rule order is semantics, so this must never depend on filesystem order.
    """
    return sorted(p for p in directory.glob("*.json") if p.is_file())
