#!/usr/bin/env python3
"""system_watchdog — Hermes system health watchdog.

Run by Hermes cron job (no_agent=True) every 5 minutes.
Stdout -> Discord #system-status. Empty stdout = silent (all OK).

Checks: services (9router, hermes-gateway, hermes-listener), disk, memory, load.
Only posts when state changes or periodic heartbeat (default every 6h).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────────────
# WATCHDOG_STATE_FILE override is for local testing; production uses the default.
STATE_FILE = Path(os.environ.get(
    "WATCHDOG_STATE_FILE",
    "/home/ubuntu/.hermes/scripts/watchdog_state.json",
))
CPU_COUNT = os.cpu_count() or 2
HEARTBEAT_INTERVAL_HOURS = 6
NO_HEARTBEAT = os.environ.get("WATCHDOG_NO_HEARTBEAT") == "1"

DISK_WARN = 80.0
DISK_CRIT = 90.0
MEM_WARN = 85.0
MEM_CRIT = 95.0
LOAD_WARN_PER_CORE = 1.0      # load5 > CPU_COUNT * this  -> warning
LOAD_RECOVER_PER_CORE = 0.75  # hysteresis: recover when load5 drops below this

ROUTER_URL = "http://localhost:20218/v1/models"
ROUTER_TIMEOUT_SEC = 3
SUBPROC_TIMEOUT = 5

MAX_DISCORD_LEN = 1900  # safety margin under 2000 char limit


# ─── Service checks ───────────────────────────────────────────────────────────

def check_9router() -> tuple[bool, str]:
    """Return (is_up, detail). Detail is short reason when down."""
    try:
        r = subprocess.run(
            ["curl", "-sf", "-m", str(ROUTER_TIMEOUT_SEC), ROUTER_URL],
            capture_output=True, timeout=SUBPROC_TIMEOUT,
        )
        if r.returncode == 0:
            return True, "ok"
        return False, f"curl exit {r.returncode}"
    except subprocess.TimeoutExpired:
        return False, "curl timeout"
    except FileNotFoundError:
        return False, "curl missing"


def check_systemd_service(name: str) -> tuple[bool, str]:
    """Return (is_up, status_string)."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", f"{name}.service"],
            capture_output=True, timeout=SUBPROC_TIMEOUT, text=True,
        )
        status = (r.stdout or "").strip() or (r.stderr or "").strip() or "unknown"
        return r.returncode == 0, status
    except subprocess.TimeoutExpired:
        return False, "systemctl timeout"
    except FileNotFoundError:
        return False, "systemctl missing"


# ─── Resource checks ──────────────────────────────────────────────────────────

def check_disk() -> tuple[float, int, int, int]:
    """Return (used_pct, used_bytes, total_bytes, free_bytes)."""
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    used_pct = round((1 - free / total) * 100, 1)
    return used_pct, used, total, free


def check_memory() -> tuple[float, int, int, int]:
    """Return (used_pct, used_kb, total_kb, available_kb)."""
    info: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            key = parts[0].rstrip(":")
            if key in ("MemTotal", "MemAvailable"):
                info[key] = int(parts[1])
    total = info["MemTotal"]
    available = info["MemAvailable"]
    used = total - available
    used_pct = round((1 - available / total) * 100, 1)
    return used_pct, used, total, available


def check_load() -> tuple[float, float, float]:
    return os.getloadavg()


# ─── State management ────────────────────────────────────────────────────────

def load_state() -> dict:
    """Return state dict. Empty dict on missing/corrupt file. Exit 1 on permission error."""
    if not STATE_FILE.exists():
        return {}
    try:
        raw = STATE_FILE.read_text()
    except OSError as e:
        print(f"watchdog: cannot read state file {STATE_FILE}: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"watchdog: state file corrupt ({e}); treating as first run", file=sys.stderr)
        return {}


def save_state(state: dict) -> None:
    """Atomic write: write tmp + rename."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.rename(tmp, STATE_FILE)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def classify(pct: float, warn: float, crit: float) -> str:
    if pct >= crit:
        return "critical"
    if pct >= warn:
        return "warning"
    return "normal"


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_duration(now: datetime, since_iso: str | None) -> str:
    if not since_iso:
        return "?"
    try:
        since = parse_iso(since_iso)
    except ValueError:
        return "?"
    secs = int((now - since).total_seconds())
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    rem = mins % 60
    return f"{hours}h {rem}m" if rem else f"{hours}h"


def fmt_short_time(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        dt = parse_iso(iso)
    except ValueError:
        return iso[:19].replace("T", " ")
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def fmt_bytes_gb(b: int) -> str:
    return f"{b / 1024**3:.1f}G"


def fmt_kb_size(kb: int) -> str:
    """Format kB value compactly. >=1G -> 1.57G; else -> 234M."""
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.2f}G"
    return f"{kb / 1024:.0f}M"


# ─── Diff logic ───────────────────────────────────────────────────────────────

def diff_services(
    old: dict,
    new: dict[str, tuple[bool, str]],
    now: datetime,
) -> list[str]:
    """Emit messages for service up/down transitions. Group simultaneous downs."""
    messages: list[str] = []
    newly_down: list[tuple[str, str, str | None]] = []  # (name, detail, prev_since_iso)
    newly_up: list[tuple[str, str | None]] = []         # (name, prev_since_iso)

    for name, (is_up, detail) in new.items():
        old_entry = old.get(name, {})
        old_status = old_entry.get("status")
        new_status = "up" if is_up else "down"
        if old_status is None:
            continue  # first time we see this service -> initial heartbeat covers it
        if old_status == new_status:
            continue
        if new_status == "down":
            newly_down.append((name, detail, old_entry.get("since")))
        else:
            newly_up.append((name, old_entry.get("since")))

    if len(newly_down) == 1:
        name, detail, prev_since = newly_down[0]
        prev_dur = fmt_duration(now, prev_since)
        prev_short = fmt_short_time(prev_since)
        prev_str = (
            f"up since {prev_short} ({prev_dur})" if prev_since else "unknown"
        )
        if name == "9router":
            probe_line = f"Probe: curl {ROUTER_URL} failed ({detail})"
        else:
            probe_line = f"systemctl: {detail}"
        messages.append(
            f"🚨 {name} DOWN\n"
            f"{probe_line}\n"
            f"Time: {fmt_time(now)}\n"
            f"Previous: {prev_str}"
        )
    elif len(newly_down) > 1:
        lines = []
        for name, detail, _ in newly_down:
            tag = (
                f"probe failed ({detail})" if name == "9router"
                else f"systemctl: {detail}"
            )
            lines.append(f"{name} — {tag}")
        messages.append(
            "🚨 Services DOWN\n"
            + "\n".join(lines)
            + f"\n\nTime: {fmt_time(now)}"
        )

    for name, prev_since in newly_up:
        downtime = fmt_duration(now, prev_since)
        prev_short = fmt_short_time(prev_since)
        line = (
            f"Down for: ~{downtime} (since {prev_short})"
            if prev_since else "Down for: unknown"
        )
        messages.append(
            f"✅ {name} UP (recovered)\n"
            f"{line}\n"
            f"Time: {fmt_time(now)}"
        )

    return messages


def _threshold_head(label: str, new_state: str, new_pct: float, old_pct: float | None) -> str:
    pct_was = f" (was {old_pct}%)" if old_pct is not None else ""
    if new_state == "critical":
        return f"🚨 {label} usage: {new_pct}% — CRITICAL"
    if new_state == "warning":
        return f"⚠️ {label} usage: {new_pct}%{pct_was}"
    return f"✅ {label} recovered: {new_pct}%{pct_was}"


def diff_disk(old: dict, pct: float, used: int, total: int, free: int) -> str | None:
    old_state = old.get("threshold_state")
    new_state = classify(pct, DISK_WARN, DISK_CRIT)
    if old_state is None or old_state == new_state:
        return None
    head = _threshold_head("Disk", new_state, pct, old.get("pct"))
    body = [f"Used: {fmt_bytes_gb(used)} / {fmt_bytes_gb(total)} • Free: {fmt_bytes_gb(free)}"]
    if new_state == "critical":
        body.append("Action needed: cleanup logs, docker images, old sessions")
    elif new_state == "warning":
        body.append(f"Threshold: {int(DISK_WARN)}% crossed")
    return head + "\n" + "\n".join(body)


def diff_memory(old: dict, pct: float, used_kb: int, total_kb: int, avail_kb: int) -> str | None:
    old_state = old.get("threshold_state")
    new_state = classify(pct, MEM_WARN, MEM_CRIT)
    if old_state is None or old_state == new_state:
        return None
    head = _threshold_head("Memory", new_state, pct, old.get("pct"))
    body = f"Used: {fmt_kb_size(used_kb)} / {fmt_kb_size(total_kb)} • Available: {fmt_kb_size(avail_kb)}"
    return head + "\n" + body


def classify_load(load5: float, prev_state: str | None) -> str:
    """Hysteresis: warn when above WARN; once warning, stay warning until below RECOVER."""
    warn_thr = CPU_COUNT * LOAD_WARN_PER_CORE
    recover_thr = CPU_COUNT * LOAD_RECOVER_PER_CORE
    if prev_state == "warning":
        return "warning" if load5 > recover_thr else "normal"
    return "warning" if load5 > warn_thr else "normal"


def diff_load(old: dict, load1: float, load5: float, load15: float) -> str | None:
    old_state = old.get("threshold_state")
    new_state = classify_load(load5, old_state)
    if old_state is None or old_state == new_state:
        return None
    l1, l5, l15 = round(load1, 2), round(load5, 2), round(load15, 2)
    if new_state == "warning":
        head = f"⚠️ Load high: {l5} (5min avg, {CPU_COUNT} cores)"
    else:
        head = f"✅ Load recovered: {l5} (5min avg, {CPU_COUNT} cores)"
    return f"{head}\nLoad avg: {l1} / {l5} / {l15} (1m/5m/15m)"


# ─── Heartbeat ────────────────────────────────────────────────────────────────

def should_heartbeat(state: dict, now: datetime) -> bool:
    last_iso = state.get("last_full_report")
    if not last_iso:
        return True
    try:
        last = parse_iso(last_iso)
    except ValueError:
        return True
    return (now - last) >= timedelta(hours=HEARTBEAT_INTERVAL_HOURS)


def render_heartbeat(
    services: dict[str, tuple[bool, str]],
    services_state: dict,
    disk_pct: float, disk_used: int, disk_total: int,
    mem_pct: float, mem_used_kb: int, mem_total_kb: int,
    load5: float,
    now: datetime,
) -> str:
    lines = [f"💚 System OK — {now.strftime('%Y-%m-%d %H:%M UTC')}"]
    for name, (is_up, _) in services.items():
        since = services_state.get(name, {}).get("since")
        dur = fmt_duration(now, since)
        status = "up" if is_up else "down"
        lines.append(f"{name}: {status} ({dur})")
    lines.append(f"Disk: {int(round(disk_pct))}% ({fmt_bytes_gb(disk_used)}/{fmt_bytes_gb(disk_total)})")
    lines.append(f"Memory: {int(round(mem_pct))}% ({fmt_kb_size(mem_used_kb)}/{fmt_kb_size(mem_total_kb)})")
    lines.append(f"Load: {round(load5, 2)}")
    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

def run() -> int:
    now = datetime.now(timezone.utc)
    state = load_state()

    services_new: dict[str, tuple[bool, str]] = {
        "9router": check_9router(),
        "hermes-gateway": check_systemd_service("hermes-gateway"),
        "hermes-listener": check_systemd_service("hermes-listener"),
    }
    disk_pct, disk_used, disk_total, disk_free = check_disk()
    mem_pct, mem_used_kb, mem_total_kb, mem_avail_kb = check_memory()
    load1, load5, load15 = check_load()

    # Compute new per-service state (carry over `since` when status unchanged).
    new_services_state: dict[str, dict] = {}
    old_services = state.get("services", {})
    for name, (is_up, _) in services_new.items():
        new_status = "up" if is_up else "down"
        old_entry = old_services.get(name, {})
        if old_entry.get("status") == new_status:
            since = old_entry.get("since", now.isoformat())
        else:
            since = now.isoformat()
        new_services_state[name] = {"status": new_status, "since": since}

    # Diffs.
    messages: list[str] = []
    messages.extend(diff_services(old_services, services_new, now))
    msg = diff_disk(state.get("disk", {}), disk_pct, disk_used, disk_total, disk_free)
    if msg:
        messages.append(msg)
    msg = diff_memory(state.get("memory", {}), mem_pct, mem_used_kb, mem_total_kb, mem_avail_kb)
    if msg:
        messages.append(msg)
    msg = diff_load(state.get("load", {}), load1, load5, load15)
    if msg:
        messages.append(msg)

    # Heartbeat: only when no diffs and 6h elapsed (or first run).
    last_full_report = state.get("last_full_report")
    if not messages and not NO_HEARTBEAT and should_heartbeat(state, now):
        messages.append(render_heartbeat(
            services_new, new_services_state,
            disk_pct, disk_used, disk_total,
            mem_pct, mem_used_kb, mem_total_kb,
            load5, now,
        ))
        last_full_report = now.isoformat()

    # Update + save state.
    new_state = {
        "services": new_services_state,
        "disk": {
            "pct": disk_pct,
            "threshold_state": classify(disk_pct, DISK_WARN, DISK_CRIT),
        },
        "memory": {
            "pct": mem_pct,
            "threshold_state": classify(mem_pct, MEM_WARN, MEM_CRIT),
        },
        "load": {
            "load5": load5,
            "threshold_state": classify_load(load5, state.get("load", {}).get("threshold_state")),
        },
        "last_check": now.isoformat(),
    }
    if last_full_report:
        new_state["last_full_report"] = last_full_report
    save_state(new_state)

    if messages:
        output = "\n\n".join(messages)
        if len(output) > MAX_DISCORD_LEN:
            output = output[: MAX_DISCORD_LEN - 20] + "\n\n[... truncated]"
        print(output)
    return 0


def main() -> int:
    if not STATE_FILE.parent.exists():
        print(f"watchdog: state dir {STATE_FILE.parent} not found", file=sys.stderr)
        return 1
    try:
        return run()
    except SystemExit:
        raise
    except Exception as e:
        print(f"watchdog: unhandled error: {e}", file=sys.stderr)
        return 0  # don't break cron loop on transient errors


if __name__ == "__main__":
    sys.exit(main())
