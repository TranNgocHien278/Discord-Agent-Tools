#!/usr/bin/env python3
"""cost-report — Daily Hermes Agent token cost report.

Run by Hermes cron job (no_agent=True). Stdout -> Discord #cost-report.

Usage:
  cost-report.py                       # report for today (UTC)
  cost-report.py --date 2026-05-29     # backfill specific day
  cost-report.py --dry-run             # skip monthly section (faster)
"""
from __future__ import annotations

import argparse
import calendar
import gzip
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────────────

LOG_FILE = Path(os.environ.get(
    "COSTREPORT_LOG_FILE",
    "/home/ubuntu/.hermes/logs/agent.log",
))
PRICING_FILE = Path(os.environ.get(
    "COSTREPORT_PRICING_FILE",
    "/home/ubuntu/.hermes/scripts/cost_pricing.json",
))

MAX_DISCORD_LEN = 1900  # safety margin under 2000 char limit

DEFAULT_PRICING: dict = {
    "default": {"in": 1.0, "out": 3.0},
    "models": {
        "Claude-Opus":    {"in": 15.0, "out": 75.0},
        "Claude-Sonnet":  {"in": 3.0,  "out": 15.0},
        "Claude-Haiku":   {"in": 0.25, "out": 1.25},
        "Gemini_Flash":   {"in": 0.075, "out": 0.30},
        "Gemini_Pro":     {"in": 1.25, "out": 5.0},
        "GPT-5.1":        {"in": 5.0,  "out": 15.0},
        "GPT-5.2":        {"in": 5.0,  "out": 15.0},
        "GPT-5.3-Spark":  {"in": 5.0,  "out": 15.0},
        "GPT-5.3-High":   {"in": 5.0,  "out": 15.0},
        "GPT-5.5":        {"in": 5.0,  "out": 15.0},
        "Deepseek":       {"in": 0.27, "out": 1.10},
        "Qwen":           {"in": 0.20, "out": 0.60},
        "Kimi":           {"in": 0.30, "out": 1.00},
        "GLM":            {"in": 0.30, "out": 1.00},
        "Minimax":        {"in": 0.20, "out": 0.60},
    },
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

TOKEN_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}),\d+ "
    r"INFO \[(?P<session>[^\]]+)\] "
    r"agent\.conversation_loop: API call #(?P<n>\d+): "
    r"model=(?P<model>\S+) provider=(?P<provider>\S+) "
    r"in=(?P<in_tok>\d+) out=(?P<out_tok>\d+) total=(?P<total>\d+) "
    r"latency=(?P<latency>[\d.]+)s"
)

_warned_models: set[str] = set()


# ─── Pricing ──────────────────────────────────────────────────────────────────

def load_pricing() -> dict:
    if not PRICING_FILE.exists():
        print(
            f"cost-report: pricing file {PRICING_FILE} not found, using defaults",
            file=sys.stderr,
        )
        return DEFAULT_PRICING
    try:
        raw = PRICING_FILE.read_text()
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"cost-report: pricing file malformed JSON: {e}", file=sys.stderr)
        sys.exit(2)
    except OSError as e:
        print(f"cost-report: cannot read pricing file: {e}", file=sys.stderr)
        sys.exit(2)
    # Merge with defaults so missing keys still resolve.
    if "default" not in data:
        data["default"] = DEFAULT_PRICING["default"]
    if "models" not in data:
        data["models"] = {}
    return data


def price_for(model: str, pricing: dict) -> tuple[float, float, bool]:
    """Return (in_per_mtok, out_per_mtok, is_default)."""
    m = pricing.get("models", {}).get(model)
    if m and "in" in m and "out" in m:
        return float(m["in"]), float(m["out"]), False
    d = pricing.get("default", {"in": 1.0, "out": 3.0})
    if model not in _warned_models:
        print(
            f"cost-report: no pricing for model {model!r}, using default",
            file=sys.stderr,
        )
        _warned_models.add(model)
    return float(d["in"]), float(d["out"]), True


# ─── Log iteration ────────────────────────────────────────────────────────────

def _open_log(path: Path):
    """Yield text lines from a log file (handles .gz)."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def _candidate_log_files() -> list[Path]:
    """Return LOG_FILE plus any rotated siblings (agent.log.1, agent.log.2.gz, ...)."""
    files: list[Path] = []
    if LOG_FILE.exists():
        files.append(LOG_FILE)
    if LOG_FILE.parent.exists():
        for sibling in sorted(LOG_FILE.parent.iterdir()):
            name = sibling.name
            if name == LOG_FILE.name:
                continue
            if name.startswith(LOG_FILE.name + "."):
                files.append(sibling)
    return files


def _iter_lines():
    for f in _candidate_log_files():
        try:
            with _open_log(f) as fh:
                for line in fh:
                    yield line
        except OSError as e:
            print(f"cost-report: cannot read {f}: {e}", file=sys.stderr)


def _clean(line: str) -> str:
    return ANSI_RE.sub("", line.rstrip("\n"))


def parse_log(target_day: date) -> list[dict]:
    """Records whose date == target_day."""
    prefix = target_day.isoformat()
    out: list[dict] = []
    for line in _iter_lines():
        clean = _clean(line)
        if not clean.startswith(prefix):
            continue
        m = TOKEN_RE.match(clean)
        if not m:
            continue
        out.append({
            "date": m["date"],
            "model": m["model"],
            "provider": m["provider"],
            "in": int(m["in_tok"]),
            "out": int(m["out_tok"]),
            "total": int(m["total"]),
        })
    return out


def parse_log_month(year: int, month: int, until: date) -> list[dict]:
    """Records from the 1st of (year, month) through `until` inclusive."""
    month_prefix = f"{year:04d}-{month:02d}-"
    until_iso = until.isoformat()
    out: list[dict] = []
    for line in _iter_lines():
        clean = _clean(line)
        if not clean.startswith(month_prefix):
            continue
        if clean[:10] > until_iso:
            continue
        m = TOKEN_RE.match(clean)
        if not m:
            continue
        out.append({
            "date": m["date"],
            "model": m["model"],
            "in": int(m["in_tok"]),
            "out": int(m["out_tok"]),
            "total": int(m["total"]),
        })
    return out


# ─── Computation ──────────────────────────────────────────────────────────────

def calc_record_cost(rec: dict, pricing: dict) -> float:
    in_p, out_p, _ = price_for(rec["model"], pricing)
    return rec["in"] * in_p / 1_000_000 + rec["out"] * out_p / 1_000_000


def aggregate_by_model(records: list[dict], pricing: dict) -> list[dict]:
    """Return list of {model, calls, in, out, cost} sorted by cost desc."""
    agg: dict[str, dict] = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
    for r in records:
        a = agg[r["model"]]
        a["calls"] += 1
        a["in"] += r["in"]
        a["out"] += r["out"]
        a["cost"] += calc_record_cost(r, pricing)
    rows = [{"model": k, **v} for k, v in agg.items()]
    rows.sort(key=lambda x: x["cost"], reverse=True)
    return rows


def total_cost(records: list[dict], pricing: dict) -> float:
    return sum(calc_record_cost(r, pricing) for r in records)


# ─── Formatting ───────────────────────────────────────────────────────────────

def fmt_money(n: float) -> str:
    if abs(n) >= 1000:
        return f"${n:,.2f}"
    return f"${n:.2f}"


def fmt_int(n: int) -> str:
    return f"{n:,}"


def comparison_line(today: float, yesterday: float) -> str:
    if yesterday <= 0 and today <= 0:
        return f"vs hôm qua ({fmt_money(0)}): no activity"
    if yesterday <= 0:
        return f"vs hôm qua ({fmt_money(0)}): 🔴 +{fmt_money(today)} (new activity)"
    delta = today - yesterday
    pct = delta / yesterday * 100
    if delta < 0:
        emoji = "🟢"
        sign = "-"
        delta_abs = abs(delta)
    else:
        emoji = "🔴" if pct > 20 else "🟡"
        sign = "+"
        delta_abs = delta
    return (
        f"vs hôm qua ({fmt_money(yesterday)}): "
        f"{emoji} {sign}{fmt_money(delta_abs)} ({sign}{abs(pct):.0f}%)"
    )


def render_per_model_table(rows: list[dict], max_rows: int | None = None) -> str:
    """ASCII-aligned table inside a Discord code block."""
    if not rows:
        return ""
    truncated = False
    if max_rows is not None and len(rows) > max_rows:
        rows = rows[:max_rows]
        truncated = True

    name_w = max(len("Model"), max(len(r["model"]) for r in rows))
    header = f"{'Model':<{name_w}}  {'Calls':>5}  {'Tokens':>11}  {'Cost':>9}"
    lines = [header]
    for r in rows:
        tokens = r["in"] + r["out"]
        lines.append(
            f"{r['model']:<{name_w}}  "
            f"{r['calls']:>5}  "
            f"{fmt_int(tokens):>11}  "
            f"{fmt_money(r['cost']):>9}"
        )
    if truncated:
        lines.append("... (truncated)")
    return "```\n" + "\n".join(lines) + "\n```"


def render_report(
    today_recs: list[dict],
    yesterday_recs: list[dict],
    month_recs: list[dict] | None,
    target: date,
    pricing: dict,
) -> str:
    today_cost = total_cost(today_recs, pricing)
    yesterday_cost = total_cost(yesterday_recs, pricing)

    # Header
    parts = [f"💰 Cost Report — {target.isoformat()}"]

    if not today_recs:
        parts.append("")
        parts.append("No API activity today (0 calls).")
        if month_recs is not None:
            month_cost = total_cost(month_recs, pricing)
            active_days = len({r["date"] for r in month_recs})
            days_in_month = calendar.monthrange(target.year, target.month)[1]
            parts.append(
                f"Month-to-date: {fmt_money(month_cost)} "
                f"({active_days} days active out of {days_in_month})"
            )
        return "\n".join(parts)

    # Daily totals
    in_tok = sum(r["in"] for r in today_recs)
    out_tok = sum(r["out"] for r in today_recs)
    total_tok = in_tok + out_tok
    parts.append("")
    parts.append("**Daily Total:**")
    parts.append(f"Calls: {fmt_int(len(today_recs))}")
    parts.append(
        f"Tokens: in {fmt_int(in_tok)} / out {fmt_int(out_tok)} "
        f"(total {fmt_int(total_tok)})"
    )
    parts.append(f"Cost: {fmt_money(today_cost)}")
    parts.append(comparison_line(today_cost, yesterday_cost))

    # Per-model breakdown
    rows = aggregate_by_model(today_recs, pricing)
    parts.append("")
    parts.append("**Per-Model Breakdown:**")
    parts.append(render_per_model_table(rows))

    # Monthly section
    if month_recs is not None:
        month_cost = total_cost(month_recs, pricing)
        days_so_far = target.day
        days_in_month = calendar.monthrange(target.year, target.month)[1]
        avg_daily = month_cost / days_so_far if days_so_far > 0 else 0.0
        projection = avg_daily * days_in_month
        month_name = target.strftime("%B %Y")
        parts.append("")
        parts.append(f"**Monthly ({month_name}):**")
        parts.append(f"Days elapsed: {days_so_far}/{days_in_month}")
        parts.append(f"Month-to-date: {fmt_money(month_cost)}")
        parts.append(f"Avg/day: {fmt_money(avg_daily)}")
        parts.append(f"📈 Projected end-of-month: {fmt_money(projection)}")

    # Top spender hint
    if rows and today_cost > 0:
        top = rows[0]
        share = top["cost"] / today_cost * 100
        parts.append("")
        parts.append(
            f"💡 Top spender: {top['model']} "
            f"({fmt_money(top['cost'])} = {share:.1f}% daily)"
        )

    out = "\n".join(parts)

    # Truncate per-model table if message too long.
    if len(out) > MAX_DISCORD_LEN:
        for cap in (10, 7, 5, 3):
            parts_trunc = list(parts)
            # Find table line and rebuild with cap
            for i, p in enumerate(parts_trunc):
                if p.startswith("```") and "Model" in p:
                    parts_trunc[i] = render_per_model_table(rows, max_rows=cap)
                    break
            out = "\n".join(parts_trunc)
            if len(out) <= MAX_DISCORD_LEN:
                break
        if len(out) > MAX_DISCORD_LEN:
            out = out[: MAX_DISCORD_LEN - 20] + "\n[... truncated]"
    return out


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=str, default=None,
                        help="Target date YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip monthly section (faster)")
    args = parser.parse_args()

    if args.date:
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print(f"cost-report: invalid --date {args.date!r}", file=sys.stderr)
            return 2
    else:
        target = date.today()

    pricing = load_pricing()

    today_recs = parse_log(target)
    yesterday_recs = parse_log(target - timedelta(days=1))
    month_recs = (
        None if args.dry_run
        else parse_log_month(target.year, target.month, target)
    )

    out = render_report(today_recs, yesterday_recs, month_recs, target, pricing)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
