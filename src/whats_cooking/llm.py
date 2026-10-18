"""Calling Claude through the Claude Code CLI (`claude -p`) on your subscription, behind a budget guard.

On the Pro plan, `claude -p` draws from the same usage limits as your chats. The guard measures
every call in API-equivalent dollars (what the same tokens would cost on the API, as reported by
the CLI): a consistent yardstick for "how much of my usage did this take". Caps live in
config/budget.yaml.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from whats_cooking.config import Settings
from whats_cooking.db import DB, now_iso

log = logging.getLogger(__name__)

TIERS = ["haiku", "sonnet", "opus"]

DEFAULTS = {
    "daily_cap_usd": 1.00,
    "weekly_cap_usd": 5.00,
    "downgrade_at": 0.70,          # fraction of the weekly cap: from here, use one model tier lower
    "skip_at": 0.90,               # from here, only purposes listed in `reserved_for` may call Claude
    "reserved_for": ["weekly"],
    "models": {"brief": "sonnet", "dedupe": "haiku", "problems": "sonnet", "longgame": "sonnet", "weekly": "opus"},
    "per_call_cap_usd": {"brief": 0.60, "dedupe": 0.05, "problems": 0.40, "longgame": 0.30, "weekly": 1.50},
    "timeout_seconds": 600,
    "auth": "subscription",        # 'subscription' (never bill an API key) | 'api_key' (deliberately use API credits)
}

# Environment variables that would make Claude Code bill an API key / other provider instead of the subscription.
_BILLING_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY")

_LIMIT_MARKERS = ("usage limit", "rate limit", "limit reached", "rate_limit", "too many requests", "overloaded")


@dataclass
class LLMResult:
    status: str                   # 'ok' | 'limited' | 'error' | 'skipped'
    model: str
    data: dict | None = None      # structured output (when a schema was given)
    text: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class BudgetGuard:
    def __init__(self, settings: Settings, db: DB):
        self.cfg = {**DEFAULTS, **(settings.budget or {})}
        self.cfg["models"] = {**DEFAULTS["models"], **(self.cfg.get("models") or {})}
        self.cfg["per_call_cap_usd"] = {**DEFAULTS["per_call_cap_usd"], **(self.cfg.get("per_call_cap_usd") or {})}
        self.db = db

    def spent(self, hours: float) -> float:
        since = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = self.db.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE ts >= ?", (since,)).fetchone()
        return float(row[0])

    def plan(self, purpose: str, requested_model: str | None = None) -> tuple[str | None, float, list[str]]:
        """Decide (model, per-call cap) for a call, or (None, 0, reasons) if it must be skipped."""
        notes: list[str] = []
        model = requested_model or self.cfg["models"].get(purpose, "sonnet")
        cap = float(self.cfg["per_call_cap_usd"].get(purpose, 0.25))
        day, week = self.spent(24), self.spent(24 * 7)
        weekly_cap, daily_cap = float(self.cfg["weekly_cap_usd"]), float(self.cfg["daily_cap_usd"])
        used = week / weekly_cap if weekly_cap else 0.0

        if used >= self.cfg["skip_at"] and purpose not in self.cfg["reserved_for"]:
            return None, 0.0, [f"weekly usage at {used:.0%} of cap (${week:.2f}/${weekly_cap:.2f}); reserved for {self.cfg['reserved_for']}"]
        if week + min(cap, 0.01) > weekly_cap:
            return None, 0.0, [f"weekly cap reached (${week:.2f}/${weekly_cap:.2f})"]
        if day + min(cap, 0.01) > daily_cap and purpose not in self.cfg["reserved_for"]:
            return None, 0.0, [f"daily cap reached (${day:.2f}/${daily_cap:.2f})"]
        if used >= self.cfg["downgrade_at"] and model in TIERS and model != TIERS[0]:
            lower = TIERS[TIERS.index(model) - 1]
            notes.append(f"weekly usage at {used:.0%}: using {lower} instead of {model}")
            model = lower
        # Never let one call exceed what's left today/this week.
        remaining = weekly_cap - week if purpose in self.cfg["reserved_for"] else min(daily_cap - day, weekly_cap - week)
        cap = max(0.01, min(cap, remaining))
        return model, cap, notes

    def record(self, purpose: str, r: LLMResult, usage: dict | None = None, duration_ms: int | None = None) -> None:
        usage = usage or {}
        with self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO llm_calls (ts, purpose, model, status, input_tokens, output_tokens, cache_read_tokens,
                                       cache_creation_tokens, cost_usd, duration_ms, error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (now_iso(), purpose, r.model, r.status, r.input_tokens, r.output_tokens,
                 int(usage.get("cache_read_input_tokens") or 0), int(usage.get("cache_creation_input_tokens") or 0),
                 r.cost_usd, duration_ms, r.error),
            )


def claude_binary() -> str | None:
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude")


def child_env(auth: str) -> dict[str, str]:
    """Environment for the claude subprocess. In subscription mode, API-key variables are removed."""
    env = dict(os.environ)
    if auth == "subscription":
        for k in _BILLING_ENV:
            env.pop(k, None)
    return env


def check_auth(binary: str, auth: str, env: dict[str, str]) -> str | None:
    """Return a reason to refuse the call, or None if the login is what config/budget.yaml allows.

    Subscription mode requires a first-party Claude login with no API key from any source
    (environment, settings files or an apiKeyHelper), as reported by `claude auth status`.
    """
    if auth != "subscription":
        return None
    try:
        p = subprocess.run([binary, "auth", "status", "--json"], capture_output=True, text=True,
                           encoding="utf-8", timeout=60, env=env)
        status = json.loads(p.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return f"could not verify Claude login ({exc})"
    if not status.get("loggedIn"):
        return "Claude Code is not logged in (run `claude` to log in, or set CLAUDE_CODE_OAUTH_TOKEN)"
    if status.get("apiKeySource"):
        return (f"an API key is configured ({status['apiKeySource']}); refusing so the API is never billed. "
                "Remove it, or set auth: api_key in config/budget.yaml to use API credits on purpose")
    if status.get("apiProvider", "firstParty") != "firstParty":
        return f"Claude Code is set to a third-party provider ({status.get('apiProvider')}); refusing"
    return None


def call_claude(settings: Settings, db: DB, purpose: str, prompt: str, system: str,
                schema: dict | None = None, model: str | None = None, runner=None) -> LLMResult:
    """Run one budget-guarded `claude -p` call. `runner` is injectable for tests."""
    guard = BudgetGuard(settings, db)
    chosen, cap, notes = guard.plan(purpose, model)
    if chosen is None:
        r = LLMResult("skipped", model or guard.cfg["models"].get(purpose, "?"), error="; ".join(notes), notes=notes)
        guard.record(purpose, r)
        log.warning("Claude call for %s skipped: %s", purpose, r.error)
        return r

    binary = claude_binary()
    if runner is None and not binary:
        r = LLMResult("error", chosen, error="claude CLI not found (install Claude Code or set CLAUDE_BIN)", notes=notes)
        guard.record(purpose, r)
        return r
    auth = guard.cfg.get("auth", "subscription")
    env = child_env(auth)
    if runner is None and (refusal := check_auth(binary, auth, env)):
        r = LLMResult("error", chosen, error=refusal, notes=notes)
        guard.record(purpose, r)
        log.error("Claude call for %s refused: %s", purpose, refusal)
        return r

    args = [
        binary or "claude", "-p",
        "--model", chosen,
        "--output-format", "json",
        "--system-prompt", system,
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--max-budget-usd", f"{cap:.2f}",
    ]
    if schema:
        args += ["--json-schema", json.dumps(schema)]
    log.info("Calling Claude (%s, cap $%.2f) for %s: %d prompt chars", chosen, cap, purpose, len(prompt))
    try:
        out = runner(args, prompt, int(guard.cfg["timeout_seconds"])) if runner else \
            _run(args, prompt, int(guard.cfg["timeout_seconds"]), env)
    except subprocess.TimeoutExpired:
        r = LLMResult("error", chosen, error="timed out", notes=notes)
        guard.record(purpose, r)
        return r
    r, usage, duration = parse_cli_output(out, chosen)
    r.notes = notes + r.notes
    guard.record(purpose, r, usage, duration)
    if not r.ok:
        log.warning("Claude call for %s: %s (%s)", purpose, r.status, (r.error or "")[:200])
    return r


def _run(args: list[str], prompt: str, timeout: int, env: dict[str, str]) -> tuple[int, str, str]:
    # Prompt goes over stdin: Windows caps command lines at ~32k characters.
    p = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def parse_cli_output(out: tuple[int, str, str], model: str) -> tuple[LLMResult, dict, int | None]:
    code, stdout, stderr = out
    try:
        j = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        j = {}
    usage = j.get("usage") or {}
    cost = float(j.get("total_cost_usd") or 0.0)
    model_usage = j.get("modelUsage") or {}
    in_tok = sum(int(v.get("inputTokens") or 0) for v in model_usage.values()) or int(usage.get("input_tokens") or 0)
    out_tok = sum(int(v.get("outputTokens") or 0) for v in model_usage.values()) or int(usage.get("output_tokens") or 0)
    # Claude Code may also run a small model for side tasks; attribute the call to the requested model.
    used_model = next((k for k in model_usage if model.lower() in k.lower()), None) or max(
        model_usage, key=lambda k: model_usage[k].get("outputTokens") or 0, default=model)
    message = " ".join(str(x) for x in (j.get("result"), j.get("subtype"), stderr) if x)

    if j and not j.get("is_error") and j.get("subtype") == "success":
        return (LLMResult("ok", used_model, data=j.get("structured_output"), text=j.get("result") or "",
                          cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok),
                usage, j.get("duration_ms"))
    status = "limited" if (j.get("api_error_status") == 429 or any(m in message.lower() for m in _LIMIT_MARKERS)) else "error"
    if j.get("subtype") == "error_max_budget_usd":
        status, message = "error", f"hit per-call cap: {message}"
    err = (message or f"exit code {code}").strip()[:1000]
    return (LLMResult(status, used_model, cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok, error=err),
            usage, j.get("duration_ms"))
