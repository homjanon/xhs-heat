# -*- coding: utf-8 -*-
"""
xhs-heat · retention.py
存储上限与到期清理（只动项目数据目录，绝不越界）

规则（配置在 sources.json 的 storage 段）：
  inbox/            采集件：默认保留 30 天，且总量上限 200MB（超出按最旧优先删）
  docs/data/reports 运行报告：90 天
  docs/push         推送文本：90 天
  docs/daily        每日归档：365 天
  docs/data/agg     聚合快照：400 天（热度分位基线，留最长）

用法：
  python scripts/retention.py            # 按配置清理
  python scripts/retention.py --dry-run  # 只报告会删什么，不动手
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from common import (DAILY_DIR, DATA_DIR, DOCS, INBOX_DIR, REPORT_DIR, load_config,  # noqa: E402
                    log)

SAFE_ROOTS = (INBOX_DIR, REPORT_DIR, DAILY_DIR, os.path.join(DOCS, "push"),
              os.path.join(DATA_DIR, "agg"))


def _files(path, suffix=None):
    if not os.path.isdir(path):
        return []
    out = []
    for f in os.listdir(path):
        if f.startswith("."):
            continue
        full = os.path.join(path, f)
        if not os.path.isfile(full):
            continue
        if suffix and not f.endswith(suffix):
            continue
        out.append(full)
    return out


def _days_old(path):
    return (time.time() - os.path.getmtime(path)) / 86400.0


def _safe_unlink(path):
    """只删白名单目录内的文件，防御性检查，避免手滑。"""
    real = os.path.realpath(path)
    for root in SAFE_ROOTS:
        if real.startswith(os.path.realpath(root) + os.sep):
            os.remove(real)
            return True
    return False


def purge_by_age(path, days, suffix, dry, label):
    removed, freed = 0, 0
    for f in _files(path, suffix):
        if _days_old(f) <= days:
            continue
        size = os.path.getsize(f)
        if not dry and _safe_unlink(f):
            removed += 1
            freed += size
        elif dry:
            removed += 1
            freed += size
            log("    [dry] %s" % os.path.basename(f))
    if removed:
        log("  %-10s 超期删除 %d 个（%.1f MB，阈值 %d 天）" % (label, removed, freed / 1048576.0, days))
    return removed, freed


def purge_by_size(path, max_mb, dry, label):
    files = sorted(_files(path), key=os.path.getmtime)
    total = sum(os.path.getsize(f) for f in files)
    limit = max_mb * 1048576.0
    if total <= limit:
        return 0, 0
    removed, freed = 0, 0
    for f in files:
        if total - freed <= limit:
            break
        size = os.path.getsize(f)
        if not dry and _safe_unlink(f):
            removed += 1
            freed += size
        elif dry:
            removed += 1
            freed += size
            log("    [dry] %s" % os.path.basename(f))
    if removed:
        log("  %-10s 超量删除 %d 个（%.1f MB，上限 %d MB）" % (label, removed, freed / 1048576.0, max_mb))
    return removed, freed


def main(argv=None):
    ap = argparse.ArgumentParser(description="存储上限与到期清理")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config()
    st = cfg.get("storage") or {}
    dry = args.dry_run

    log("存储清理%s" % ("（演练，不删除）" if dry else ""))
    total_removed = total_freed = 0

    r, f = purge_by_age(INBOX_DIR, int(st.get("inbox_days", 30)), None, dry, "inbox")
    total_removed += r
    total_freed += f
    r, f = purge_by_size(INBOX_DIR, int(st.get("inbox_max_mb", 200)), dry, "inbox")
    total_removed += r
    total_freed += f

    for path, key, label, suffix in (
        (REPORT_DIR, "reports_days", "reports", ".json"),
        (os.path.join(DOCS, "push"), "push_days", "push", ".txt"),
        (DAILY_DIR, "daily_days", "daily", ".json"),
        (os.path.join(DATA_DIR, "agg"), "agg_days", "agg", ".json"),
    ):
        r, f = purge_by_age(path, int(st.get(key, 90)), suffix, dry, label)
        total_removed += r
        total_freed += f

    inbox_files = _files(INBOX_DIR, ".gz") + _files(INBOX_DIR, ".jsonl")
    inbox_mb = sum(os.path.getsize(x) for x in inbox_files) / 1048576.0
    log("  ── 结果：删除 %d 个文件，释放 %.1f MB｜inbox 现存 %d 个 / %.1f MB"
        % (total_removed, total_freed / 1048576.0, len(inbox_files), inbox_mb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
