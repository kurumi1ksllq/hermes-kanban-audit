"""kanban 审计 CLI —— 纯包装层。

所有指标一律取自 engine.compute_all();本文件不重算、不复制任何判定逻辑,
也不直接碰任何数据库(只读约束见需求书-cli.md §5)。
"""
import argparse
import json
import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import (  # noqa: E402
    KANBAN_DB,
    MAIN_STATE_DB,
    PROFILES_DIR,
    _resolve_independently,
    compute_all,
    run_selftest,  # noqa: F401  —— 契约要求可 import,本 CLI 不自跑 engine 自检
)


# --------------------------------------------------------------- 表格工具


def _dw(text):
    """显示宽度:东亚宽/全角(W/F)记 2,其余记 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _flat(value):
    """单元格取值:换行压成空格;None / 空串 / 空列表 → '-'。"""
    if value is None or value == []:
        return "-"
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text if text else "-"


def _clip(text, limit):
    """按显示宽度截断到 limit,超出时末尾补 '…'(占 1 列宽)。"""
    if limit is None or _dw(text) <= limit:
        return text
    out, width = "", 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if width + cw > limit - 1:
            break
        out += ch
        width += cw
    return out + "…"


def _pad(text, width):
    return text + " " * (width - _dw(text))


def table_lines(title, headers, rows, limits=None):
    """渲染一张表:标题行 / 表头 / 横线 / 数据行。不含上下空行(由调用方补)。"""
    limits = limits or [None] * len(headers)
    cells = [
        [_clip(_flat(c), lim) for c, lim in zip(row, limits, strict=False)] for row in rows
    ]
    widths = [_dw(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _dw(cell))
    lines = [title, "  ".join(_pad(h, w) for h, w in zip(headers, widths, strict=False))]
    lines.append("-" * (sum(widths) + 2 * (len(widths) - 1)))
    if not cells:
        lines.append("(无)")
    else:
        lines.extend(
            "  ".join(_pad(c, w) for c, w in zip(row, widths, strict=False)) for row in cells
        )
    return lines


# --------------------------------------------------------------- 三个视图


def issues_lines(data):
    """--view issues:异常清单,保持引擎原有顺序(已按 severity 降序)。"""
    issues = data["issues"]
    rows = [
        [
            it.get("type", ""),
            it.get("severity", ""),
            it.get("task_id", ""),
            (it.get("evidence") or ["-"])[0],
        ]
        for it in issues
    ]
    lines = table_lines(
        f"问题清单({len(issues)} 条)",
        ["类型", "severity", "卡id", "一句话证据"],
        rows,
        [None, None, None, 60],
    )
    lines.append(f"共 {len(issues)} 条")
    return lines


def agents_lines(data):
    """--view agents:专家画像表,保持引擎顺序(按 profile 升序)。"""
    agents = data["agents"]
    rows = [
        [
            a.get("profile", ""),
            a.get("cards", 0),
            "{:.1f}%".format(a.get("completion_rate", 0.0) * 100),
            "{:.1f}%".format(a.get("cache_reuse", 0.0) * 100),
            "{:.1f}".format(a.get("per_call_output", 0.0)),
            "{:.4f}".format(a.get("cost_usd", 0.0)),
        ]
        for a in agents
    ]
    return table_lines(
        f"专家画像({len(agents)} 个 profile)",
        ["profile", "卡数", "完成率", "复用度", "每轮产出", "成本"],
        rows,
    )


def gaps_lines(data):
    """--view gaps:缺口清单,保持引擎顺序(已按 priority/source/what 排好)。"""
    gaps = data["gaps"]
    rows = [
        [
            g.get("what", ""),
            ",".join(g.get("who") or []),
            g.get("affected_cards", 0),
            g.get("action", ""),
        ]
        for g in gaps
    ]
    return table_lines(
        f"缺口清单({len(gaps)} 条)",
        ["缺什么", "谁受影响", "卡数", "建议动作"],
        rows,
        [50, 20, None, 40],
    )


def summary_lines(data):
    """默认(无参):人读摘要。"""
    counts = data["counts"]
    meta = data["meta"]
    gaps = data["gaps"]
    prio = {"高": 0, "中": 0, "低": 0}
    for g in gaps:
        if g.get("priority") in prio:
            prio[g["priority"]] += 1

    lines = [
        "kanban 审计摘要 · {}".format(data["generated_at"]),
        "运行 {running} · 待办 {todo} · 阻塞 {blocked} · 完成 {done} · 总卡 {total}".format(**counts),
        "run 会话解析:{}/{} = {}".format(
            meta["runs_with_session"], meta["runs_total"], meta["session_resolution_rate"]
        ),
        "",
    ]
    lines += issues_lines(data)
    lines.append("")
    lines += agents_lines(data)
    lines.append("")
    lines.append(
        "缺口:{} 条(高 {} / 中 {} / 低 {})  ——  用 --view gaps 看明细".format(
            len(gaps), prio["高"], prio["中"], prio["低"]
        )
    )
    return lines


# --------------------------------------------------------------- 自检


def cli_selftest():
    """--selftest:解析成功率 + missing 明细,与 engine 独立复算对账。"""
    data = compute_all()
    meta = data["meta"]
    total, with_sess, _reasons = _resolve_independently(
        KANBAN_DB, MAIN_STATE_DB, PROFILES_DIR
    )
    rate = round(with_sess / total, 4) if total else 0.0

    lines = [
        "CLI 自检: worker_session_id 解析",
        "  runs_total={} runs_with_session={} session_resolution_rate={}".format(
            meta["runs_total"], meta["runs_with_session"], meta["session_resolution_rate"]
        ),
    ]
    missing = meta["missing"]
    lines.append(f"  missing({len(missing)}):")
    for item in missing:
        lines.append(
            "    run#{} {} reason={}".format(item["run_id"], item["task_id"], item["reason"])
        )

    consistent = (
        meta["runs_total"] == total
        and meta["runs_with_session"] == with_sess
        and meta["session_resolution_rate"] == rate
    )
    lines.append(
        "  与 engine.compute_all()['meta'] 一致: {}".format("OK" if consistent else "MISMATCH")
    )
    zero_missing = not any(x.get("reason") != "no worker_session_id" for x in missing)
    lines.append(
        "  零缺失断言(所有 missing 的 reason 均为 no worker_session_id): {}".format(
            "OK" if zero_missing else "FAIL"
        )
    )
    ok = consistent and zero_missing
    lines.append("SELFTEST PASS" if ok else "SELFTEST FAIL")
    print("\n".join(lines))
    return 0 if ok else 1


# --------------------------------------------------------------- 入口


def main(argv=None):
    parser = argparse.ArgumentParser(description="kanban 审计 CLI(包装 engine)")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON(喂 cron/机器)")
    parser.add_argument(
        "--view", choices=["issues", "agents", "gaps"], help="只看某个视图"
    )
    parser.add_argument("--selftest", action="store_true", help="跑 CLI 自检")
    args = parser.parse_args(argv)

    if args.selftest:
        return cli_selftest()
    if args.json:
        print(json.dumps(compute_all(), ensure_ascii=False, indent=2))
        return 0

    data = compute_all()
    if args.view == "issues":
        block = issues_lines(data)
    elif args.view == "agents":
        block = agents_lines(data)
    elif args.view == "gaps":
        block = gaps_lines(data)
    else:
        print("\n".join(summary_lines(data)))
        return 0
    print("")
    print("\n".join(block))
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
