# Discord-Agent-Tools

Bộ scripts standalone hỗ trợ Hermes agent trên VPS, gửi notification về Discord. Mỗi tool là 1 thư mục con với spec markdown + script Python (stdlib only, không cần external deps).

## Tools

| Tool | Status | Mô tả |
|------|--------|-------|
| `system_watchdog/` | ✅ Built | Health check VPS (services, disk, memory, load), post `#system-status` chỉ khi state đổi |
| `report_cost/` | ✅ Built | Daily token cost report (parse `agent.log`), post `#cost-report` mỗi 23:00 UTC |

---

## system_watchdog — Deploy lên VPS

**Target:** Ubuntu 24.04 arm64, user `ubuntu`, Hermes installed tại `/home/ubuntu/.hermes/`.

### 1. Copy script lên VPS

```bash
scp system_watchdog/system_watchdog.py ubuntu@<vps-host>:/home/ubuntu/.hermes/scripts/
```

### 2. Set permission

```bash
ssh ubuntu@<vps-host> 'chmod +x /home/ubuntu/.hermes/scripts/system_watchdog.py'
```

### 3. Test manual

```bash
ssh ubuntu@<vps-host>

# Lần 1: in heartbeat full status, tạo watchdog_state.json
python3 /home/ubuntu/.hermes/scripts/system_watchdog.py

# Lần 2: stdout rỗng (im lặng — không có gì thay đổi)
python3 /home/ubuntu/.hermes/scripts/system_watchdog.py
```

Nếu lần 2 vẫn ra output → có gì đó wrong, kiểm tra state file:

```bash
cat /home/ubuntu/.hermes/scripts/watchdog_state.json
```

### 4. Đăng ký Hermes cron job

Qua Hermes cronjob tool, register với:

- **Schedule:** `*/5 * * * *` (mỗi 5 phút)
- **Mode:** `no_agent=True` (stdout = Discord message, không gọi LLM)
- **Channel ID:** `1509957355549495431` (#system-status)
- **Script:** `system_watchdog.py`

### 5. Verify

- Trigger thủ công vài lần → Discord im lặng (không có gì đổi).
- Stop tạm 1 service (ví dụ `systemctl --user stop hermes-listener`) → cron lần kế post `🚨 hermes-listener DOWN`.
- Start lại → cron lần kế post `✅ hermes-listener UP (recovered)` kèm downtime.

---

## Behavior summary (system_watchdog)

- **Im lặng 99% thời gian.** Chỉ post khi state thay đổi (service up↔down, disk/memory/load cross threshold).
- **Heartbeat:** mỗi 6 giờ post 1 dòng `💚 System OK` để confirm script vẫn sống. Disable bằng env `WATCHDOG_NO_HEARTBEAT=1`.
- **Thresholds:**
  - Disk: warning ≥80%, critical ≥90%
  - Memory: warning ≥85%, critical ≥95%
  - Load (5min): warning > 2.0 trên 2-core (>1.0/core), recover < 1.5
- **State file:** `/home/ubuntu/.hermes/scripts/watchdog_state.json`, atomic write (`.tmp` + rename).
- **Multiple changes cùng lúc:** gom thành 1 message (vd: 2 services cùng down → 1 block `🚨 Services DOWN`).
- **Output cap:** <2000 chars (Discord message limit).

## Constraints (system_watchdog)

- Stdlib only (`subprocess`, `os`, `json`, `pathlib`, `datetime`)
- Không sudo, không network call (Hermes cron framework lo việc post Discord)
- Không in debug ra stdout — chỉ message hoặc rỗng. Errors → stderr.
- Exit 0 luôn (trừ state dir missing hoặc permission denied → exit 1)

## Local test (system_watchdog)

Override state file path bằng env var để test trên máy dev:

```bash
export WATCHDOG_STATE_FILE=/tmp/watchdog_state.json
python3 system_watchdog/system_watchdog.py
```

Lưu ý: trên máy local, `9router` và `hermes-*.service` sẽ probe fail — bình thường, đó là test logic flow chứ không phải test thật. Production behavior chỉ verify được trên VPS.

---

## report_cost — Deploy lên VPS

**Target:** Cùng VPS với system_watchdog. Đọc log file `/home/ubuntu/.hermes/logs/agent.log` (hỗ trợ luôn rotated `.gz` siblings) và tính cost theo pricing template.

### 1. Copy script + pricing lên VPS

```bash
scp report_cost/cost-report.py     ubuntu@<vps-host>:/home/ubuntu/.hermes/scripts/
scp report_cost/cost_pricing.json  ubuntu@<vps-host>:/home/ubuntu/.hermes/scripts/
```

### 2. Set permission

```bash
ssh ubuntu@<vps-host> 'chmod +x /home/ubuntu/.hermes/scripts/cost-report.py'
```

### 3. Test manual

```bash
ssh ubuntu@<vps-host>

# Dry-run: skip monthly section, in nhanh
python3 /home/ubuntu/.hermes/scripts/cost-report.py --dry-run

# Hôm nay (UTC)
python3 /home/ubuntu/.hermes/scripts/cost-report.py

# Backfill 1 ngày cụ thể
python3 /home/ubuntu/.hermes/scripts/cost-report.py --date 2026-05-29
```

Verify:
- Output <2000 chars: `python3 /home/ubuntu/.hermes/scripts/cost-report.py | wc -c`
- Exit code = 0: `python3 /home/ubuntu/.hermes/scripts/cost-report.py; echo $?`
- Stderr không leak vào stdout (cron chỉ gửi stdout lên Discord)

### 4. Đăng ký Hermes cron job

Qua Hermes cronjob tool, register với:

- **Schedule:** `0 23 * * *` (23:00 UTC mỗi ngày)
- **Mode:** `no_agent=True`
- **Channel ID:** `1510160002747207690` (#cost-report)
- **Script:** `cost-report.py`

---

## Behavior summary (report_cost)

- **Output:** 1 message duy nhất gồm daily totals, per-model breakdown (ASCII table trong code block), monthly projection, top spender hint.
- **Comparison vs hôm qua:** 🟢 giảm, 🟡 tăng <20%, 🔴 tăng >20% (hoặc "new activity" nếu hôm qua = 0).
- **Pricing fallback:** model không có trong `cost_pricing.json` → dùng `default` (in $1/1M, out $3/1M), warn ra stderr.
- **Pricing file missing:** dùng built-in defaults (cùng giá trị với template), warn stderr, exit 0.
- **Pricing file malformed:** exit 2 với error stderr (cron sẽ gửi error alert).
- **No data ngày target:** output ngắn `No API activity today`.
- **Truncation:** nếu output >1900 chars, giảm dần per-model rows (10 → 7 → 5 → 3) cho đến khi vừa.
- **Rotated logs:** tự động đọc thêm `agent.log.1`, `agent.log.2.gz`, ... cùng thư mục.

## Constraints (report_cost)

- Stdlib only (`re`, `json`, `gzip`, `argparse`, `calendar`, `pathlib`, `datetime`)
- Stateless — không ghi file nào
- Stdout phải <2000 chars
- ANSI escape codes trong log line được strip trước khi parse
- `--dry-run` skip monthly parse → chạy <100ms ngay cả với log lớn

## Local test (report_cost)

Override paths bằng env vars để test trên máy dev:

```bash
export COSTREPORT_LOG_FILE=/tmp/agent.log         # log fixture
export COSTREPORT_PRICING_FILE=./cost_pricing.json
python3 report_cost/cost-report.py --date 2026-05-29 --dry-run
```
