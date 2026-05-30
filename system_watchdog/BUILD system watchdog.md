# BUILD: system_watchdog.py — Health check watchdog for Discord

## 🎯 Mục tiêu

Một Python script standalone, chạy bằng Hermes cron job (mode `no_agent=True`) mỗi **5 phút**. Kiểm tra trạng thái services, disk, memory, load. Chỉ post lên Discord `#system-status` khi state **thay đổi** — im lặng nếu mọi thứ ổn.

**Mode `no_agent=True`:** stdout của script = message gửi Discord. Stdout rỗng = không post (im lặng). Script tự build text Discord, KHÔNG gọi LLM, KHÔNG tốn token.

**Triết lý watchdog:** Không spam. Chỉ nói khi có chuyện. Nếu 9router up liên tục 24h → 0 messages. Nếu 9router down → 1 message. Nếu 9router up lại → 1 message. Đó là tất cả.

---

## 🖥 Environment

- **OS:** Ubuntu 24.04 arm64
- **User:** `ubuntu` (home: `/home/ubuntu`)
- **Python:** 3.11 (system) — stdlib only, KHÔNG cần external deps
- **PEP 668 enforced** → không cài thêm package
- **Hermes install dir:** `/home/ubuntu/.hermes/`
- **Script location bắt buộc:** `/home/ubuntu/.hermes/scripts/system_watchdog.py`
  (Hermes cron tool yêu cầu script nằm relative trong `~/.hermes/scripts/`)
- **Run permission:** `chmod +x system_watchdog.py`
- **Shebang:** `#!/usr/bin/env python3`
- **State file:** `/home/ubuntu/.hermes/scripts/watchdog_state.json` (script tự quản lý)

---

## 🔍 Checks to perform

### 1. Services (up/down)

**9router:**
bash
curl -sf -m 3 http://localhost:20218/v1/models > /dev/null
- Exit 0 = up
- Exit non-zero = down
- Timeout 3s (tránh block script)

**hermes-gateway:**
bash
systemctl --user is-active hermes-gateway.service
- stdout "active" = up
- Anything else (inactive, failed, unknown) = down

**hermes-listener:**
bash
systemctl --user is-active hermes-listener.service
- Cùng logic

### 2. Disk usage
python
Parse output of: df -P /
Hoặc dùng os.statvfs("/") — stdlib, không cần subprocess
import os
st = os.statvfs("/")
total = st.f_blocks * st.f_frsize
free = st.f_bavail * st.f_frsize (1/8)
used_pct = round((1 - free / total) * 100, 1)
**Thresholds:**
- ≥80% → ⚠️ warning (post 1 lần khi cross)
- ≥90% → 🚨 critical (post 1 lần khi cross)
- Tụt về <80% → ✅ recovered (post 1 lần)

### 3. Memory usage
python
Parse /proc/meminfo
with open("/proc/meminfo") as f:
    info = {}
    for line in f:
        parts = line.split()
        if parts[0] in ("MemTotal:", "MemAvailable:"):
            info[parts[0].rstrip(":")] = int(parts[1])  # kB
total = info["MemTotal"]
available = info["MemAvailable"]
used_pct = round((1 - available / total) * 100, 1)
**Thresholds:**
- ≥85% → ⚠️ warning
- ≥95% → 🚨 critical
- Tụt về <85% → ✅ recovered

### 4. Load average
python
load1, load5, load15 = os.getloadavg()
**Threshold:** load5 > 2.0 trên máy 2 vCPU (tức >1.0 per core) → ⚠️ warning. Tụt về <1.5 → recovered.

Lưu ý: VPS hiện tại 2 vCPU. Hardcode `CPU_COUNT = 2` hoặc `os.cpu_count()`.

---

## 💾 State file

Path: `/home/ubuntu/.hermes/scripts/watchdog_state.json`

**Format:**
json
{
  "services": {
    "9router": {"status": "up", "since": "2026-05-30T07:00:00Z"},
    "hermes-gateway": {"status": "up", "since": "2026-05-30T07:00:00Z"},
    "hermes-listener": {"status": "up", "since": "2026-05-30T07:00:00Z"}
  },
  "disk": {
    "pct": 47.2,
    "threshold_state": "normal"
  },
  "memory": {
    "pct": 41.5,
    "threshold_state": "normal"
  },
  "load": {
    "load5": 0.03,
    "threshold_state": "normal"
  },
  "last_check": "2026-05-30T07:00:00Z",
  "last_full_report": "2026-05-30T06:00:00Z"
}
**`threshold_state` values:** `"normal"`, `"warning"`, `"critical"`

**State transitions that trigger output:**
- Service: `up → down`, `down → up`
- Disk: `normal → warning`, `warning → critical`, `critical → normal`, `warning → normal`
- Memory: same as disk
- Load: `normal → warning`, `warning →
 (2/8)
normal`

**First run (no state file):** treat everything as "unknown" → check all, save state, post initial status report (full).

**Atomic write:** write to `.tmp` then `os.rename()` — tránh corrupt nếu script bị kill giữa chừng.

---

## 📤 Output format (Discord message)

**Discord formatting rules (BẮT BUỘC):**
- KHÔNG dùng markdown table (`|---|---|`)
- Dùng bullet lists, bold labels, hoặc ASCII trong code blocks
- Tối ưu Discord mobile: line ngắn, không layout ngang

### Khi có state change → post message

**Service down:**

🚨 9router DOWN
Probe: curl http://localhost:20218/v1/models failed
Time: 2026-05-30 07:15:00 UTC
Previous: up since 2026-05-30 06:00:00 UTC (1h 15m)
**Service recovered:**

✅ 9router UP (recovered)
Down for: ~10 min (since 07:05)
Time: 2026-05-30 07:15:00 UTC
**Multiple services down (gom 1 message):**

🚨 Services DOWN
9router — probe failed
hermes-gateway — systemctl: inactive

Time: 2026-05-30 07:15:00 UTC
**Disk warning:**

⚠️ Disk usage: 82.3% (was 79.1%)
Used: 15.6G / 19G • Free: 3.4G
Threshold: 80% crossed
**Disk critical:**

🚨 Disk usage: 91.2% — CRITICAL
Used: 17.3G / 19G • Free: 1.7G
Action needed: cleanup logs, docker images, old sessions
**Disk recovered:**

✅ Disk recovered: 76.4% (was 82.3%)
Used: 14.5G / 19G • Free: 4.5G
**Memory warning:**

⚠️ Memory usage: 87.2% (was 83.1%)
Used: 1.57G / 1.8G • Available: 234M
**Load warning:**

⚠️ Load high: 2.34 (5min avg, 2 cores)
Load avg: 2.34 / 1.89 / 1.12 (1m/5m/15m)
### Khi KHÔNG có state change → stdout rỗng (im lặng)

Script print nothing → cron framework không gửi message → Discord im lặng. Đây là behavior mong muốn 99% thời gian.

### Periodic full report (optional, mỗi 6 giờ)

Nếu `last_full_report` > 6 giờ trước VÀ mọi thứ OK → post 1 heartbeat ngắn:

💚 System OK — 2026-05-30 12:00 UTC
9router: up (6h)
hermes-gateway: up (6h) (3/8)
hermes-listener: up (6h)
Disk: 47% (8.6G/19G)
Memory: 41% (747M/1.8G)
Load: 0.03
Mục đích: confirm script vẫn chạy, không phải "im lặng vì script chết". Nếu không thấy heartbeat >6h → biết có vấn đề.

**Disable heartbeat:** nếu env var `WATCHDOG_NO_HEARTBEAT=1` → skip periodic report (chỉ post khi state change).

---

## 🏗 Code structure
python
#!/usr/bin/env python3
"""system_watchdog — Hermes system health watchdog.

Run by Hermes cron job (no_agent=True) every 5 minutes.
Stdout → Discord #system-status. Empty stdout = silent (all OK).

Checks: services (9router, gateway, listener), disk, memory, load.
Only posts when state changes or periodic heartbeat (6h).
"""
from future import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

─── Config ───
STATE_FILE = Path("/home/ubuntu/.hermes/scripts/watchdog_state.json")
CPU_COUNT = os.cpu_count() or 2
HEARTBEAT_INTERVAL_HOURS = 6
NO_HEARTBEAT = os.environ.get("WATCHDOG_NO_HEARTBEAT") == "1"

Thresholds
DISK_WARN = 80.0
DISK_CRIT = 90.0
MEM_WARN = 85.0
MEM_CRIT = 95.0
LOAD_WARN_PER_CORE = 1.0  # load5 > CPU_COUNT * this = warning

─── Service checks ───
def check_9router() -> bool: ...
def check_systemd_service(name: str) -> bool: ...

─── Resource checks ───
def check_disk() -> tuple[float, int, int]: ...  # (pct, used_gb, total_gb)
def check_memory() -> tuple[float, int, int]: ...  # (pct, used_mb, total_mb)
def check_load() -> tuple[float, float, float]: ...  # (1m, 5m, 15m)

─── State management ───
def load_state() -> dict: ...
def save_state(state: dict) -> None: ...  # atomic write

─── Diff logic ───
def diff_services(old: dict, new: dict) -> list[str]: ...
def diff_disk(old: dict, new_pct: float) -> str | None: ...
def diff_memory(old: dict, new_pct: float) -> str | None: ...
def diff_load(old: dict, new_load5: float) -> str | None: ...

─── Heartbeat ─── (4/8)
def should_heartbeat(state: dict, now: datetime) -> bool: ...
def render_heartbeat(services: dict, disk_pct: float, mem_pct: float, load: tuple) -> str: ...

─── Main ───
def main() -> int:
    now = datetime.now(timezone.utc)
    state = load_state()

    # Perform all checks
    services_new = {
        "9router": check_9router(),
        "hermes-gateway": check_systemd_service("hermes-gateway"),
        "hermes-listener": check_systemd_service("hermes-listener"),
    }
    disk_pct, disk_used, disk_total = check_disk()
    mem_pct, mem_used, mem_total = check_memory()
    load1, load5, load15 = check_load()

    # Compute diffs
    messages = []
    messages.extend(diff_services(state.get("services", {}), services_new, now))
    d = diff_disk(state.get("disk", {}), disk_pct, disk_used, disk_total)
    if d: messages.append(d)
    m = diff_memory(state.get("memory", {}), mem_pct, mem_used, mem_total)
    if m: messages.append(m)
    l = diff_load(state.get("load", {}), load5, load1, load15)
    if l: messages.append(l)

    # Heartbeat check
    if not messages and not NO_HEARTBEAT and should_heartbeat(state, now):
        messages.append(render_heartbeat(services_new, disk_pct, mem_pct, (load1, load5, load15)))
        state["last_full_report"] = now.isoformat()

    # Update state
    state["services"] = {
        name: {
            "status": "up" if up else "down",
            "since": state.get("services", {}).get(name, {}).get("since", now.isoformat())
                     if (state.get("services", {}).get(name, {}).get("status") == ("up" if up else "down"))
                     else now.isoformat()
        }
        for name, up in services_new.items()
    }
    state["disk"] = {"pct": disk_pct, "threshold_state": classify(disk_pct, DISK_WARN, DISK_CRIT)}
    state["memory"] = {"pct": mem_pct, "threshold_state": classify(mem_pct, MEM_WARN, MEM_CRIT)} (5/8)
state["load"] = {"load5": load5, "threshold_state": "warning" if load5 > CPU_COUNT * LOAD_WARN_PER_CORE else "normal"}
    state["last_check"] = now.isoformat()

    save_state(state)

    # Output
    if messages:
        print("\n\n".join(messages))
    # else: stdout empty → cron sends nothing

    return 0

def classify(pct: float, warn: float, crit: float) -> str:
    if pct >= crit: return "critical"
    if pct >= warn: return "warning"
    return "normal"

if name == "main":
    sys.exit(main())
---

## 🚫 Constraints

- **KHÔNG** dùng external dependency — stdlib only (`subprocess`, `os`, `json`, `pathlib`, `datetime`)
- **KHÔNG** modify `/home/ubuntu/.hermes/.env`, config.yaml
- **KHÔNG** dùng `sudo` — tất cả check chạy dưới user `ubuntu`
- **KHÔNG** gọi network API (Discord post do cron framework lo)
- **KHÔNG** print debug/log ra stdout — chỉ final message hoặc empty
- Errors/warnings → `sys.stderr`
- Stdout phải <2000 chars (Discord message limit) — nếu nhiều changes cùng lúc, gom gọn
- `subprocess.run()` với `timeout=5` cho mọi external command — tránh hang
- State file phải atomic write (write `.tmp` + `os.rename`)
- Exit 0 luôn (trừ khi state file directory không tồn tại — exit 1)

---

## 🔧 Cron registration (sau khi script xong)
bash
Place + permission
chmod +x /home/ubuntu/.hermes/scripts/system_watchdog.py

Test manually
python3 /home/ubuntu/.hermes/scripts/system_watchdog.py
Lần đầu: sẽ print full status (vì chưa có state file)
Lần 2 ngay sau: stdout rỗng (không có gì đổi)
Register cron (qua Hermes cronjob tool)
Schedule: /5 * * *
Channel ID: 1509957355549495431 (#system-status)
Mode: no_agent=True
Script: system_watchdog.py
---

## ✅ Acceptance test

1. **First run (no state file):** → print full status report (heartbeat format), tạo
 (6/8)
`watchdog_state.json`
2. **Second run (ngay sau):** → stdout rỗng (im lặng)
3. **Stop 9router** (`pkill -f 9router` hoặc stop port) → run script → print `🚨 9router DOWN`
4. **Run lại (9router vẫn down):** → stdout rỗng (đã report rồi)
5. **Start 9router lại** → run script → print `✅ 9router UP (recovered)` với downtime
6. **Fake disk >80%:** edit state file, set `disk.pct=79`, `disk.threshold_state=normal`, rồi mock `os.statvfs` hoặc tạo large file → run → print `⚠️ Disk usage`
7. **Fake disk recovered:** xóa large file → run → print `✅ Disk recovered`
8. **Heartbeat:** edit state file, set `last_full_report` = 7 giờ trước → run → print `💚 System OK`
9. **No heartbeat env:** `WATCHDOG_NO_HEARTBEAT=1 python3 system_watchdog.py` + state 7h old → stdout rỗng
10. **Multiple changes:** stop gateway + 9router cùng lúc → 1 message gom cả 2
11. **State file corrupt:** xóa state file → run → behave như first run (graceful)
12. **State file permission denied:** `chmod 000 watchdog_state.json` → run → exit 1, stderr error
13. **Stdout <2000 chars** cho mọi case
14. **Atomic write:** kill script mid-run (SIGKILL) → state file không bị corrupt (vẫn là version cũ hoặc version mới, không bao giờ partial)

---

## 💡 Hints / pitfalls

- `subprocess.run(["curl", "-sf", "-m", "3", "http://localhost:20218/v1/models"], capture_output=True, timeout=5)` — timeout Python (5s) > timeout curl (3s) để curl tự exit trước
- `systemctl --user is-active` exit code: 0=active, 3=inactive/failed — check `returncode == 0`, KHÔNG parse stdout (có thể thay đổi format)
- `os.statvfs("/")` — dùng `f_bavail` (available to non-root), KHÔNG dùng `f_bfree` (includes reserved blocks)
- `/proc/meminfo` — dùng `MemAvailable` (kernel 3.14+, Ubuntu 24.04 có). KHÔNG dùng `MemFree` (sai lệch lớn do cache/buffers)
- `os.getloadavg()` — returns tuple (1min, 5min, 15min). Dùng 5min cho threshold.
 (7/8)
- First run edge case: nếu 9router đang down lúc first run → state ghi "down", post report. Lần sau nếu vẫn down → im lặng. Đúng behavior.
- `datetime.now(timezone.utc)` — luôn dùng UTC, consistent với log timestamps
- Duration formatting: `since` timestamp → `now - since` → format "Xh Ym" hoặc "Xm" nếu <1h
- Gom multiple changes: nếu >1 service down cùng lúc, gom vào 1 message block thay vì 3 messages riêng
- State file path cùng thư mục với script (`~/.hermes/scripts/`) — đảm bảo directory tồn tại (nó luôn tồn tại vì script nằm đó)

---

## 📝 Kết quả mong muốn

Khi build xong:
- File `system_watchdog.py` (path: `/home/ubuntu/.hermes/scripts/system_watchdog.py`)
- Output thực của acceptance tests 1-9 (paste terminal output)
- Note bất kỳ deviation nào khỏi spec (kèm lý do)

Sau đó assistant sẽ:
1. Review code
2. Đăng ký cron job (`*/5 * * * *`, `no_agent=True`, deliver `#system-status`)
3. Run trigger vài lần để verify im lặng + state change detection


---

Đó là spec cho system_watchdog.py. Build xong gửi lại, mình review + đăng ký cron. (8/8)
