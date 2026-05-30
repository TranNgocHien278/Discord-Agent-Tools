# BUILD: cost-report.py — Daily token cost report for Discord

## 🎯 Mục tiêu

Một Python script standalone, chạy bằng Hermes cron job (mode `no_agent=True`) mỗi ngày 23:00 UTC. Parse log file của Hermes Agent, tính token cost theo từng model, gửi báo cáo lên Discord channel `#cost-report`.

**Mode `no_agent=True`:** stdout của script = message gửi Discord. Stdout rỗng = không post. Script tự build text Discord, KHÔNG gọi LLM, KHÔNG tốn token.

---

## 🖥 Environment

- **OS:** Ubuntu 24.04 arm64
- **User:** `ubuntu` (home: `/home/ubuntu`)
- **Python:** 3.11 (system) — KHÔNG cần venv, dùng stdlib only nếu được
- **PEP 668 enforced** → không cài thêm package nếu không cần
- **Hermes install dir:** `/home/ubuntu/.hermes/`
- **Script location bắt buộc:** `/home/ubuntu/.hermes/scripts/cost-report.py`
  (Hermes cron tool yêu cầu script nằm relative trong `~/.hermes/scripts/`)
- **Run permission:** `chmod +x cost-report.py`
- **Shebang:** `#!/usr/bin/env python3`

---

## 📂 Data sources (input)

### Source 1: `/home/ubuntu/.hermes/logs/agent.log`

Log file rotating, ghi liên tục bởi Hermes Agent. Pattern cần parse:

2026-05-29 16:32:02,286 INFO [20260529_155501_3ac588c3] agent.conversation_loop: API call #10: model=Claude-Opus provider=custom in=9308 out=20 total=9328 latency=4.5s
Regex gợi ý:
python
TOKEN_RE=*
    r'^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}),\d+ '
    r'INFO [(?P<session>[^]]+)] '
    r'agent.conversation_loop: API call #(?P<n>\d+): '
    r'model=(?P<model>\S+) provider=(?P<provider>\S+) '
    r'in=(?P<in_tok>\d+) out=(?P<out_tok>\d+) total=(?P<total>\d+) '
    r'latency=(?P<latency>[\d.]+)s'
)
```

Ngày target:** mặc định = "hôm nay" theo timezone hệ thống (UTC trên VPS này). Cho phép override qua arg --date YYYY-MM-DD để backfill.

Source 2:
``` (1/7)
`/home/ubuntu/.hermes/scripts/cost_pricing.json`

File pricing (script tự đọc, nếu không có dùng default hardcoded). User maintain.

**Format:**
json
{
  "_comment": "Prices in USD per 1,000,000 tokens. Update when providers change pricing.",
  "default": { "in": 1.0, "out": 3.0 },
  "models": {
    "Claude-Opus":    { "in": 15.0, "out": 75.0 },
    "Claude-Sonnet":  { "in": 3.0,  "out": 15.0 },
    "Claude-Haiku":   { "in": 0.25, "out": 1.25 },
    "Gemini_Flash":   { "in": 0.075, "out": 0.30 },
    "Gemini_Pro":     { "in": 1.25, "out": 5.0 },
    "GPT-5.1":        { "in": 5.0,  "out": 15.0 },
    "GPT-5.2":        { "in": 5.0,  "out": 15.0 },
    "GPT-5.3-Spark":  { "in": 5.0,  "out": 15.0 },
    "GPT-5.3-High":   { "in": 5.0,  "out": 15.0 },
    "GPT-5.5":        { "in": 5.0,  "out": 15.0 },
    "Deepseek":       { "in": 0.27, "out": 1.10 },
    "Qwen":           { "in": 0.20, "out": 0.60 },
    "Kimi":           { "in": 0.30, "out": 1.00 },
    "GLM":            { "in": 0.30, "out": 1.00 },
    "Minimax":        { "in": 0.20, "out": 0.60 }
  }
}
Nếu model không có trong pricing → dùng `default`. Log warning ra stderr (cron sẽ nuốt stderr, không lên Discord) để ý thấy có model mới chưa map giá.

---

## 🧮 Computation

### Daily totals (cho ngày target)
- Total API calls
- Total tokens (in + out)
- Total cost USD

### Per-model breakdown
Cho mỗi model trong ngày:
- Số calls
- Input tokens / output tokens
- Cost (in_cost + out_cost)
- Sort by cost descending

### Monthly projection
- Tính số ngày đã qua trong tháng (đến và bao gồm ngày target)
- Tính tổng cost từ đầu tháng đến ngày target (parse log từng dòng có `date` thuộc tháng đó)
- Average daily cost của tháng đến hiện tại = total / days_so_far
- Projection = avg_daily × days_in_month

### Comparison vs hôm trước
- Daily cost ngày target
- Daily cost ngày trước đó (-1 day)
- Delta + %

---

## 📤 Output format (Discord message)

**Discord formatting rules (BẮT BUỘC tuân thủ):**
 (2/7)
- KHÔNG dùng markdown table (`|---|---|`) — Discord không render
- Dùng bullet lists, bold labels, hoặc ASCII tables trong code blocks
- Tối ưu Discord mobile: line ngắn, không layout ngang
- Code block cho data thuần (số, ASCII align), prose cho narrative

### Template

💰 Cost Report — 2026-05-30

Daily Total:
Calls: 197
Tokens: in 2,910,726 / out 32,115 (total 2,942,841)
Cost: $46.07
vs hôm qua ($12.34): 🔴 +$33.73 (+273%)

Per-Model Breakdown:
Model           Calls    Tokens     Cost
Claude-Opus       150  2,500,000   $40.20
Gemini_Flash       30    300,000    $0.18
Deepseek           17    142,841    $0.45


Monthly (May 2026):
Days elapsed: 30/31
Month-to-date: $523.45
Avg/day: $17.45
📈 Projected end-of-month: $540.95

💡 Top spender: Claude-Opus ($40.20 = 87.3% daily)
### Logic chi tiết

- Nếu daily calls = 0 → output ngắn:
  

  💰 Cost Report — 2026-05-30
  No API activity today (0 calls).
  Month-to-date: $XXX.XX (XX days active out of 30)
  
- Comparison vs hôm trước: 🟢 nếu giảm, 🔴 nếu tăng >20%, 🟡 nếu tăng <20%
- Cost format: `$X.XX` cho số <100, `$X,XXX.XX` cho số ≥1000
- Token format: `1,234,567` (comma-separated)
- ASCII table align: tự pad spaces, không dùng markdown pipes

### Stdout discipline
- Chỉ in **1 message duy nhất** ra stdout
- Không in debug/log message
- Errors/warnings → `sys.stderr` (cron không gửi stderr lên Discord)
- Nếu script fail (exit non-zero) → cron framework sẽ tự gửi error alert (đó là behavior của Hermes cron với `no_agent=True`)

---

## 🏗 Code structure
 (3/7)
```python
#!/usr/bin/env python3
"""cost-report — Daily Hermes Agent token cost report.

Run by Hermes cron job (no_agent=True). Stdout → Discord #cost-report.
Usage:
  cost-report.py                  # report for today (UTC)
  cost-report.py --date 2026-05-29  # backfill specific day
  cost-report.py --dry-run        # print but skip month projection (faster)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# Constants
HOME = Path.home()
LOG_FILE = Path("/home/ubuntu/.hermes/logs/agent.log")
PRICING_FILE = Path("/home/ubuntu/.hermes/scripts/cost_pricing.json")

DEFAULT_PRICING = {
    "default": {"in": 1.0, "out": 3.0},
    "models": {
        # ... see pricing table above
    }
}

TOKEN_RE = re.compile(...)  # see regex above

def load_pricing() -> dict: ...
def parse_log(target_date: date) -> list[dict]: ...
def parse_log_month(year: int, month: int, until: date) -> list[dict]: ...
def calc_cost(rec: dict, pricing: dict) -> float: ...
def fmt_money(n: float) -> str: ...
def fmt_int(n: int) -> str: ...
def render_report(today: list, yesterday: list, month_records: list,
                  target: date, pricing: dict) -> str: ...

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()
    pricing = load_pricing()

    today_recs = parse_log(target)
    yesterday_recs = parse_log(target - timedelta(days=1))
    month_recs = (parse_log_month(target.year, target.month, target)
                  if not args.dry_run else [])

    out = render_report(today_recs, yesterday_recs, month_recs, target, pricing)
    print(out)
    return 0

if __name__ == "__main__":
    sys.exit(main())


``` (4/7)
### Performance considerations

- `agent.log` có thể >100MB. Đọc theo dòng (`for line in open(...)`), KHÔNG `read_text()` toàn file.
- Filter date prefix sớm: nếu line không bắt đầu bằng `target_date.isoformat()` (cho daily) hoặc `target.year-target.month-` (cho monthly) → skip trước khi regex match.
- Monthly parse có thể chậm trên file lớn — chấp nhận, mỗi ngày chỉ chạy 1 lần.
- Nếu file rotated (vd `agent.log.1`, `agent.log.2.gz`), check thêm `agent.log.{1..N}` và `*.gz` (gzip.open) — nhưng chỉ nếu cần data của tháng hiện tại.

---

## 🚫 Constraints

- **KHÔNG** dùng external dependency (httpx, requests, pandas, ...). Stdlib only.
- **KHÔNG** modify `/home/ubuntu/.hermes/.env`, config.yaml, hoặc bất kỳ file Hermes nào ngoài `scripts/cost_pricing.json` (script chỉ đọc log + pricing)
- **KHÔNG** in secrets ra stdout/stderr
- **KHÔNG** gọi network API (Discord post được Hermes cron framework lo)
- **KHÔNG** ghi state file — script stateless
- Stdout phải <2000 chars (Discord message limit) — nếu vượt, truncate per-model breakdown
- Exit 0 nếu OK, exit non-zero nếu lỗi không phục hồi được (file không đọc được, regex không match dòng nào trong tháng — thực ra empty là OK, exit 0)

---

## 🔧 Cron registration (sau khi script xong)

User (hoặc assistant) sẽ chạy:
bash
Place files
chmod +x /home/ubuntu/.hermes/scripts/cost-report.py
pricing file đã có sẵn (xem template trên)
Test manually trước
/home/ubuntu/.hermes/scripts/cost-report.py --dry-run
/home/ubuntu/.hermes/scripts/cost-report.py
/home/ubuntu/.hermes/scripts/cost-report.py --date 2026-05-29

Register cron (qua Hermes cronjob tool — assistant sẽ làm bước này)
Schedule: 0 23 * * *
Channel ID: 1510160002747207690 (#cost-report)
Mode: no_agent=True
Script: cost-report.py
Script không cần biết về cron — chỉ in ra stdout, framework lo phần gửi.

---

## ✅ Acceptance test

Test trên VPS sau khi build:
 (5/7)
1. `python3 /home/ubuntu/.hermes/scripts/cost-report.py --date 2026-05-29` → output đẹp, có per-model breakdown, monthly projection
2. `... --date 2025-01-01` (ngày không có data) → output "No API activity today"
3. `... --date 2026-05-30 --dry-run` → output không có monthly section, chạy <1s
4. Pipe `| wc -c` → output <2000 chars
5. Stdout không có ANSI/control char, không có debug print
6. Exit code = 0 cho mọi case trên
7. ASCII table align đẹp trên Discord mobile (paste vào channel test trước)
8. Format số đúng: `$1,234.56` không phải `$1234.56`, tokens `2,910,726` không phải `2910726`
9. Pricing file missing → vẫn chạy được với default, log warning stderr
10. Pricing file malformed JSON → exit non-zero với error stderr

---

## 💡 Hints / pitfalls

- `agent.log` ghi UTF-8, có khi có ANSI escape codes — strip trước khi regex match (xem `\x1b\[[0-9;]*m`)
- Multi-line log records (JSON dump): chỉ parse lines bắt đầu timestamp prefix, ignore continuation lines
- Timezone: log timestamp là **local time của VPS** (đang là UTC). Đừng convert sang timezone khác — Discord user đọc UTC OK.
- `date.today()` dùng local time → trên VPS UTC = UTC date. OK.
- Đầu tháng: `days_so_far = target.day` (vd target=2026-05-15 → days_so_far=15)
- Last day of month: `import calendar; calendar.monthrange(year, month)[1]`
- Khi monthly cost = 0 (đầu tháng chưa có data) → projection = 0, đừng chia 0
- Comparison vs yesterday: nếu yesterday cost = 0 và today > 0 → hiển thị "🔴 +$X.XX (new activity)" thay vì "+inf%"
- ASCII table align: dùng `f"{name:<15} {calls:>5} {tokens:>10} {cost:>8}"` — KHÔNG dùng markdown
- Test edge case: model name có dấu `-` hoặc `_` (vd `Claude-Opus`, `Gemini_Flash`) — regex `\S+` đã handle

---

## 📝 Kết quả mong muốn

Khi push code lên repo (hoặc đưa cho assistant):
- File `cost-report.py` (path: `/home/ubuntu/.hermes/scripts/cost-report.py`)
- File `cost_pricing.json` (path: `/home/ubuntu/.hermes/scripts/cost_pricing.json`) — pricing template
 (6/7)
- Output thực của 3 lần test:
  - `--date 2026-05-29` (ngày có data)
  - `--date 2025-01-01` (ngày không data)
  - `--dry-run` (skip monthly)
- Note bất kỳ deviation nào khỏi spec này (kèm lý do)

Sau đó assistant sẽ:
1. Verify output format trên Discord (paste test message vào `#cost-report`)
2. Đăng ký cron job qua `cronjob` tool
3. Run trigger 1 lần để confirm end-to-end


---

Đó là toàn bộ context + spec. Build xong bạn gửi code lại, mình sẽ review + đăng ký cron job. (7/7)
