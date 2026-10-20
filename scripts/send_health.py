"""發訊狀況報表（add-persona-authoring-console 第 9 群：發訊配速的監測）。

列出最近 N 小時每個整點的發訊量與各類發訊錯誤次數，再列出最近幾筆錯誤的
原文。小規模實測時看這張表：限速、疑似違規、權杖失效任一欄不是 0，就要停下來查。

用法（容器內執行）：

    docker compose exec app python scripts/send_health.py            # 最近 24 小時
    docker compose exec app python scripts/send_health.py --hours 72
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.channels.send_errors import ALERT_CATEGORIES, CATEGORIES, CATEGORY_LABELS  # noqa: E402
from app.db import repository as repo  # noqa: E402
from app.db.session import db_session  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))


def render(hours: dict[datetime, dict[str, int]], errors: list, window: int) -> str:
    header = ["時間（台北）", "發出"] + [CATEGORY_LABELS[c] for c in CATEGORIES]
    rows = [
        [at.astimezone(TAIPEI).strftime("%m-%d %H:00"), str(c.get("sent", 0))]
        + [str(c.get(cat, 0)) for cat in CATEGORIES]
        for at, c in hours.items()
    ]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(r, widths)) for r in [header, *rows]]

    sent = sum(c.get("sent", 0) for c in hours.values())
    alerts = sum(c.get(cat, 0) for c in hours.values() for cat in ALERT_CATEGORIES)
    peak = max((c.get("sent", 0) for c in hours.values()), default=0)
    lines.insert(0, f"最近 {window} 小時：發出 {sent} 則，單一小時最多 {peak} 則，告警 {alerts} 次")
    lines.insert(1, "")
    if not rows:
        lines.append("（這段時間沒有發訊，也沒有錯誤）")

    if errors:
        lines += ["", "最近的發訊錯誤："]
        for e in errors:
            at = e.occurred_at if e.occurred_at.tzinfo else e.occurred_at.replace(tzinfo=timezone.utc)
            lines.append(
                f"- {at.astimezone(TAIPEI):%m-%d %H:%M}  {CATEGORY_LABELS.get(e.category, e.category)}"
                f"  HTTP {e.http_status}  code {e.error_code}／{e.error_subcode}  session {e.session_id}"
            )
            if e.detail:
                lines.append(f"  {e.detail[:200]}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="發訊狀況報表")
    parser.add_argument("--hours", type=int, default=24, help="往回看幾小時（預設 24）")
    parser.add_argument("--errors", type=int, default=10, help="列出最近幾筆錯誤（預設 10）")
    args = parser.parse_args()

    since = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=args.hours - 1
    )
    with db_session() as db:
        hours = repo.send_activity_since(db, since)
        errors = repo.recent_send_errors(db, args.errors)
        for e in errors:
            db.expunge(e)
    print(render(hours, errors, args.hours))


if __name__ == "__main__":
    main()
