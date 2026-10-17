"""kanban 审计 · 纯计算引擎(v0.1)

只读打开 kanban 库与各 profile 的会话库,算出设计文档 §4 的全部指标,
输出一份结构化 JSON。后端(plugin_api.py)与 CLI(kanban_audit.py)都 import 本模块。

红线:
- 所有库一律 `mode=ro` 打开;不写任何库、不建表、不改配置。
- `worker_session_id` 只走 `task_runs.metadata` 三级回退,不用 `tasks.session_id`。
- 纯函数(cache_reuse 等)不碰 IO。
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import sqlite3
import statistics
import sys

# ------------------------------------------------------------------ 路径常量

def _default_hermes_home() -> str:
    """HERMES_HOME 优先取环境变量;否则按平台默认。

    Windows: %LOCALAPPDATA%\\hermes;其它平台: ~/.hermes。
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        return env
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "hermes")
    return os.path.join(os.path.expanduser("~"), ".hermes")


HERMES_HOME = _default_hermes_home()
KANBAN_DB = os.path.join(HERMES_HOME, "kanban.db")
MAIN_STATE_DB = os.path.join(HERMES_HOME, "state.db")
PROFILES_DIR = os.path.join(HERMES_HOME, "profiles")

CACHE_WARN_CAP = 0.92        # §4.1.1:告警线 = min(该专家历史中位数, 0.92)
RERUN_MIN_RUNS = 3           # §4.4:「反复失败」= 同卡 runs ≥ 3
COST_ALERT_FACTOR = 2.0      # §4.4:「成本异常」= 单卡成本 > 全局均值 × 2
GAP_NEGATION_RE = re.compile(r"无缺口|没缺口|无缺失|零缺口|不缺")
# 修订 3:规则 2 只认两种明确句式(需求书 §4.1),其余普通叙述一律不判。
#   a) 「(不/无)需要安装 X」——负向 lookbehind 挡掉「无需安装」;
#   b) 「缺/缺少/缺失/没有 … <能力名词>」——能力名词表硬约束。
# 旧裸关键词(不可用 / 无法使用 / 无法安装 / 装不上 / 跑不起来 / 用不了)与
# 无名词约束的 `缺|没有 + 任意 2~16 字` 分支全部删除:它们正是 16 条假阳性的来源。
# `依赖` 不进名词表:会误命中「无第三方依赖」这类好话。
# 修订 4(P2):两处 `\s*` 改 `[ \t]*`。`\s` 可跨换行,而 `_gap_phrase` 在换行处切,
# 于是评论 `缺\nCLI` 会退化成 what="缺"(合规但无用)。收紧到空格/制表符后
# 该输入直接不命中,不会产出半截短语。
GAP_COMMENT_RE = re.compile(
    r"(?<!无)(?<!不)需要安装[ \t]*[^\s,，。;；:：、]{1,40}"
    r"|(?:缺|缺少|缺失|没有)[ \t]*[^\s,，。;；:：、]{0,30}?[ \t]*"
    r"(?:工具集|工具|MCP|技能|CLI|插件|环境|软件包)"
)
# 修订 3:去掉裸 `缺口清单` 备选(会命中讲引擎自身的元描述评论);
# `条` ≠ `处`,且 `处`↔`缺口` 之间允许 ≤6 个非空白字(覆盖「共 6 处具体缺口」)。
GAP_SELFASSESS_RE = re.compile(r"\d{1,3}\s*处\s*[^\s,，。;；:：、]{0,6}?(?:不足|缺口)")

ISSUE_TYPES = (
    "分配错配",
    "反复失败",
    "流程阻塞",
    "token异常",
    "耗时异常",
    "成本异常",
    "历史退化",
)

# ---------------------------------------------------------------- 纯函数(无 IO)


def cache_reuse(i, c):
    """上下文复用度 = c / (c + i);分母 0 返回 0.0。"""
    i = i or 0
    c = c or 0
    d = c + i
    return c / d if d else 0.0


def thinking_ratio(o, r):
    """思考占比 = r / (o + r);分母 0 返回 0.0。"""
    o = o or 0
    r = r or 0
    d = o + r
    return r / d if d else 0.0


def effective_output(i, c, o):
    """有效产出率 = o / (i + c);分母 0 返回 0.0。"""
    i = i or 0
    c = c or 0
    o = o or 0
    d = i + c
    return o / d if d else 0.0


def per_call_output(o, a):
    """每轮产出 = o / a;分母 0 返回 0.0。"""
    o = o or 0
    a = a or 0
    return o / a if a else 0.0


def tool_density(t, a):
    """工具密度 = t / a;分母 0 返回 0.0。"""
    t = t or 0
    a = a or 0
    return t / a if a else 0.0


_SEVERITY_BASE = {
    "分配错配": 80,
    "反复失败": 75,
    "流程阻塞": 70,
    "token异常": 50,
    "耗时异常": 40,
    "成本异常": 35,
    "历史退化": 20,
}


def severity_of(issue_type, ctx=None):
    """严重度 0..100:基准分 + ctx 加成,封顶 100。未知类型 = 0。"""
    ctx = ctx or {}
    s = _SEVERITY_BASE.get(issue_type, 0)
    if issue_type == "反复失败" and ctx.get("runs", 0) >= 4:
        s += 5
    if issue_type == "流程阻塞" and ctx.get("block_kind") == "capability":
        s += 15
    return min(100, s)


# ------------------------------------------------------------------ 内部工具


def _open_ro(path):
    """只读打开 sqlite 库。"""
    return sqlite3.connect("file:{}?mode=ro".format(path.replace("\\", "/")), uri=True)


def _parse_json_obj(raw):
    """把 JSON 字符串解析成 dict;NULL / 非法 JSON / 非对象 → None(不抛异常)。"""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", "replace")
        except Exception:
            return None
    if not isinstance(raw, str):
        return None
    try:
        val = json.loads(raw)
    except Exception:
        return None
    return val if isinstance(val, dict) else None


def _worker_session_id(metadata):
    """task_runs.metadata → worker_session_id;取不到返回 None。"""
    d = _parse_json_obj(metadata)
    if d is None:
        return None
    sid = d.get("worker_session_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    return None


def _num(v, default=0.0):
    """把任意值转成 float;转不动(非数值/None)返回 default,不抛异常。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _cost_of(estimated, actual):
    """单 run 成本:estimated 非 NULL/0 → estimated;否则 actual;再 NULL → 0.0。"""
    if estimated:
        return _num(estimated)
    if actual is not None:
        return _num(actual)
    return 0.0


def _r4(x):
    return round(_num(x), 4)


def _p95(walls):
    """最近秩 p95:sorted[min(n-1, ceil(0.95*n)-1)]。"""
    if not walls:
        return 0.0
    s = sorted(walls)
    n = len(s)
    idx = min(n - 1, math.ceil(0.95 * n) - 1)
    return s[idx]


def _gap_phrase(text, m, limit=80):
    """gaps 的「缺什么」取值:从 m.start() 向右取短句,绝不向左取前缀。

    遇句末符(。！？!?)或换行即停;`limit` 处硬截断并补一个 `…`,
    保证返回值长度 ≤ limit 且不含换行。不在 `,`/`、`/`;`/`(` 处停
    (否则会把 `共 13 处不足(工具集 2` 切残)。
    """
    s = (text or "")[m.start():]
    cut = len(s)
    for k, ch in enumerate(s):
        if ch in "。！？!?\n\r":
            cut = k
            break
    phrase = s[:cut].strip()
    if len(phrase) > limit:
        phrase = phrase[: limit - 1].rstrip() + "…"
    return phrase


def _one_line(text, limit=200):
    if not text:
        return ""
    return text[:limit].replace("\n", " ").replace("\r", " ")


# ------------------------------------------------------------------ 主体


def _collect(kanban_db, main_db, profiles_dir):
    """读两库,返回 (tasks, runs, ev_by_task, cm_by_task, meta, baselines)。

    baselines[profile] = 该专家「sessions 表全部有 token 会话」的 c/(c+i) 列表
    (修订 1:基线口径不是 run 口径)。
    """
    kb = _open_ro(kanban_db)
    main_conn = _open_ro(main_db)
    prof_conns = {}

    def prof_conn(profile):
        if profile in prof_conns:
            return prof_conns[profile]
        fp = os.path.join(profiles_dir, profile, "state.db")
        conn = None
        if os.path.isfile(fp):
            try:
                conn = _open_ro(fp)
            except sqlite3.Error:
                conn = None
        prof_conns[profile] = conn
        return conn

    sess_sql = (
        "select input_tokens, cache_read_tokens, output_tokens, reasoning_tokens, "
        "api_call_count, tool_call_count, estimated_cost_usd, actual_cost_usd "
        "from sessions where id = ?"
    )

    def find_session(profile, sid):
        """三级回退:profiles/<profile>/state.db → 主库 state.db → 查不到。"""
        conn = prof_conn(profile)
        if conn is not None:
            try:
                row = conn.execute(sess_sql, (sid,)).fetchone()
            except sqlite3.Error:
                row = None
            if row:
                return row
        try:
            row = main_conn.execute(sess_sql, (sid,)).fetchone()
        except sqlite3.Error:
            row = None
        return row

    tasks = {}
    for tid, title, assignee, status, block_kind in kb.execute(
        "select id, title, assignee, status, block_kind from tasks"
    ):
        tasks[tid] = {
            "task_id": tid,
            "title": title or "",
            "assignee": assignee or "",
            "status": status or "",
            "block_kind": block_kind or "",
        }

    runs = []
    missing = []
    for rid, tid, profile, status, started, ended, outcome, metadata, summary in kb.execute(
        "select id, task_id, profile, status, started_at, ended_at, outcome, metadata, summary "
        "from task_runs order by id"
    ):
        profile = profile or ""
        wall = None
        if started is not None and ended is not None:
            wall = _num(ended) - _num(started)
        rec = {
            "run_id": rid,
            "task_id": tid,
            "profile": profile,
            "status": status or "",
            "outcome": outcome or "",
            "wall": _r4(wall) if wall is not None else 0.0,
            "_wall_raw": wall,
            "has_session": False,
            "input": 0,
            "cache_read": 0,
            "output": 0,
            "reasoning": 0,
            "api_calls": 0,
            "tool_calls": 0,
            "cost": 0.0,
            "cache_reuse": 0.0,
            "thinking_ratio": 0.0,
            "summary": summary or "",
        }
        sid = _worker_session_id(metadata)
        if sid is None:
            missing.append({"run_id": rid, "task_id": tid, "reason": "no worker_session_id"})
        else:
            row = find_session(profile, sid)
            if row is None:
                missing.append({"run_id": rid, "task_id": tid, "reason": "session_not_found"})
            else:
                (i, c, o, r, a, t, est, act) = row
                rec.update(
                    {
                        "has_session": True,
                        "input": _int(i),
                        "cache_read": _int(c),
                        "output": _int(o),
                        "reasoning": _int(r),
                        "api_calls": _int(a),
                        "tool_calls": _int(t),
                        "cost": _cost_of(est, act),
                    }
                )
                rec["cache_reuse"] = _r4(cache_reuse(rec["input"], rec["cache_read"]))
                rec["thinking_ratio"] = _r4(thinking_ratio(rec["output"], rec["reasoning"]))
        runs.append(rec)

    ev_by_task = {}
    for tid, kind, payload, created in kb.execute(
        "select task_id, kind, payload, created_at from task_events"
    ):
        ev_by_task.setdefault(tid, []).append((created, kind or "", payload))

    cm_by_task = {}
    for tid, author, body, created in kb.execute(
        "select task_id, author, body, created_at from task_comments"
    ):
        cm_by_task.setdefault(tid, []).append((created, author or "", body or ""))

    # 修订 1:基线口径 = 该专家 sessions 表全部有 token 会话的中位数。
    # 来源:profiles/<profile>/state.db;该库文件不存在(如 default)→ 回退主库
    # state.db 中 profile_name == <profile> 的 sessions。只取 i+c > 0 的行。
    baselines = {}
    for profile in sorted({r["profile"] for r in runs}):
        conn = prof_conn(profile)
        reuses = []
        if conn is not None:
            try:
                rows = conn.execute(
                    "select input_tokens, cache_read_tokens from sessions "
                    "where ifnull(input_tokens, 0) + ifnull(cache_read_tokens, 0) > 0"
                ).fetchall()
            except sqlite3.Error:
                rows = []
        else:
            # 主库若没有 profile_name 列(结构差异)也不许抛异常,退为无基线。
            try:
                rows = main_conn.execute(
                    "select input_tokens, cache_read_tokens from sessions "
                    "where profile_name = ? "
                    "and ifnull(input_tokens, 0) + ifnull(cache_read_tokens, 0) > 0",
                    (profile,),
                ).fetchall()
            except sqlite3.Error:
                rows = []
        for i, c in rows:
            reuses.append(cache_reuse(_int(i), _int(c)))
        baselines[profile] = reuses

    meta = {
        "kanban_db": kanban_db,
        "runs_total": len(runs),
        "runs_with_session": sum(1 for r in runs if r["has_session"]),
        "missing": missing,
    }
    meta["session_resolution_rate"] = _r4(
        meta["runs_with_session"] / meta["runs_total"] if meta["runs_total"] else 0.0
    )

    kb.close()
    main_conn.close()
    for conn in prof_conns.values():
        if conn is not None:
            conn.close()

    return tasks, runs, ev_by_task, cm_by_task, meta, baselines


def _build_agents(runs, baselines):
    """按 task_runs.profile 聚合;所有数值聚合跳过无 session 的 run。

    返回 (agents, raw):agents 是对外输出(数值 round 4),raw 保留未 round 的
    中位数 / p95,供异常检测按契约原始值比较(round 后比较会在边界翻转)。
    cache_reuse / cache_baseline_min 走修订 1 的会话口径(见 baselines)。
    """
    by_profile = {}
    for r in runs:
        by_profile.setdefault(r["profile"], []).append(r)

    card_run_count = {}
    for r in runs:
        card_run_count[r["task_id"]] = card_run_count.get(r["task_id"], 0) + 1

    agents = {}
    raw = {}
    for profile, pruns in by_profile.items():
        cards = {r["task_id"] for r in pruns}
        n_runs = len(pruns)
        completed = sum(1 for r in pruns if r["outcome"] == "completed")
        blocked = sum(1 for r in pruns if r["outcome"] == "blocked")
        timed_out = sum(1 for r in pruns if r["outcome"] == "timed_out")
        rerun_cards = sum(1 for c in cards if card_run_count.get(c, 0) >= 2)

        sess = [r for r in pruns if r["has_session"]]
        walls = [r["_wall_raw"] for r in sess if r["_wall_raw"] is not None]
        # 修订 1:复用度基线用该专家全部有 token 会话,不用 run 口径。
        session_reuses = baselines.get(profile, [])
        reuse_median = statistics.median(session_reuses) if session_reuses else 0.0
        sum_i = sum(r["input"] for r in sess)
        sum_c = sum(r["cache_read"] for r in sess)
        sum_o = sum(r["output"] for r in sess)
        sum_r = sum(r["reasoning"] for r in sess)
        sum_a = sum(r["api_calls"] for r in sess)
        sum_t = sum(r["tool_calls"] for r in sess)

        agents[profile] = {
            "profile": profile,
            "cards": len(cards),
            "runs": n_runs,
            "completed": completed,
            "completion_rate": _r4(completed / n_runs if n_runs else 0.0),
            "rerun_rate": _r4(rerun_cards / len(cards) if cards else 0.0),
            "blocked_rate": _r4(blocked / n_runs if n_runs else 0.0),
            "timeout_rate": _r4(timed_out / n_runs if n_runs else 0.0),
            "wall_p50": _r4(statistics.median(walls)) if walls else 0.0,
            "wall_p95": _r4(_p95(walls)),
            "cache_reuse": _r4(reuse_median),
            "cache_baseline_min": _r4(min(reuse_median, CACHE_WARN_CAP))
            if session_reuses else 0.0,
            "effective_output": _r4(effective_output(sum_i, sum_c, sum_o)),
            "per_call_output": _r4(per_call_output(sum_o, sum_a)),
            "tool_density": _r4(tool_density(sum_t, sum_a)),
            "thinking_ratio": _r4(thinking_ratio(sum_o, sum_r)),
            "cost_usd": _r4(sum(r["cost"] for r in sess)),
        }
        raw[profile] = {
            "reuse_median": reuse_median,
            "p95": _p95(walls),
        }
    return agents, raw


def _detect_issues(tasks, runs, raw):
    """§4.4 异常检测。raw 用未 round 的中位数 / p95 比较(契约原始值)。"""
    by_card = {}
    for r in runs:
        by_card.setdefault(r["task_id"], []).append(r)

    # 单卡成本(供成本异常用)
    card_cost = {}
    for tid, cruns in by_card.items():
        card_cost[tid] = sum(r["cost"] for r in cruns if r["has_session"])
    positives = [v for v in card_cost.values() if v > 0]
    cost_mean = (sum(positives) / len(positives)) if positives else 0.0
    cost_limit = cost_mean * COST_ALERT_FACTOR

    issues = []

    def add(tid, itype, evidence, ctx=None):
        task = tasks.get(tid, {})
        issues.append(
            {
                "id": f"{tid}:{itype}",
                "type": itype,
                "severity": severity_of(itype, ctx or {}),
                "task_id": tid,
                "title": task.get("title", ""),
                "evidence": evidence,
                "suggestion": _suggest(itype),
            }
        )

    for tid, task in tasks.items():
        cruns = sorted(by_card.get(tid, []), key=lambda r: r["run_id"])

        # 1) 分配错配
        assignee = task["assignee"]
        if assignee:
            bad = [r for r in cruns if r["profile"] != assignee]
            if bad:
                add(
                    tid,
                    "分配错配",
                    [
                        f"assignee={assignee}, run#{r['run_id']} 执行 profile={r['profile']}"
                        for r in bad
                    ],
                )

        # 2) 反复失败
        if len(cruns) >= RERUN_MIN_RUNS:
            add(
                tid,
                "反复失败",
                [
                    f"task_runs 行数={len(cruns)}(run "
                    + ", ".join(f"#{r['run_id']}" for r in cruns)
                    + ")"
                ],
                {"runs": len(cruns)},
            )

        # 3) 流程阻塞
        if task["status"] == "blocked":
            add(
                tid,
                "流程阻塞",
                [f"tasks.status=blocked, block_kind={task['block_kind'] or '(空)'}"],
                {"block_kind": task["block_kind"]},
            )

        # 4) token异常(每卡一条,evidence 列全部命中 run)
        hits = []
        for r in cruns:
            if not r["has_session"]:
                continue
            median = raw.get(r["profile"], {}).get("reuse_median", 0.0)
            base = min(median, CACHE_WARN_CAP)
            if base and r["cache_reuse"] < base:
                hits.append(
                    f"run#{r['run_id']} profile={r['profile']} "
                    f"cache_reuse={r['cache_reuse']:.4f} < 基线={base:.4f}"
                )
        if hits:
            add(tid, "token异常", hits)

        # 5) 耗时异常
        slow = []
        for r in cruns:
            p95 = raw.get(r["profile"], {}).get("p95", 0.0)
            if p95 and r["_wall_raw"] is not None and r["_wall_raw"] > p95:
                slow.append(
                    f"run#{r['run_id']} profile={r['profile']} "
                    f"墙钟={r['wall']:.4f}s > 该专家 p95={p95:.4f}s"
                )
        if slow:
            add(tid, "耗时异常", slow)

        # 6) 成本异常
        total = card_cost.get(tid, 0.0)
        if cost_limit and total > cost_limit:
            add(
                tid,
                "成本异常",
                [
                    f"Σ cost={total:.4f} > 2 × 全局均值 {cost_mean:.4f}(={cost_limit:.4f})"
                ],
            )

    issues.sort(key=lambda x: (-x["severity"], x["task_id"], x["type"]))
    return issues


def _suggest(itype):
    return {
        "分配错配": "核对 assignee 与实跑 profile,改派给对口专家",
        "反复失败": "拆卡或补需求书;先定位卡在哪一步,别再原样重跑",
        "流程阻塞": "按 block_kind 找人解锁:needs_input 补需求、capability 补能力",
        "token异常": "收上下文 / 复用系统提示,先查该轮是否重复读了大文件",
        "耗时异常": "设单轮超时兜底,超时按现状收尾,不要空等",
        "成本异常": "拆小粒度或换档,复核该卡是否烧在无效重试上",
    }.get(itype, "人工复核该卡")


def _detect_gaps(tasks, ev_by_task, cm_by_task, runs_by_card):
    """§4.6/§4.7 被动信号(修订 2:精度优先,宁可漏报不可刷屏)。

    三条规则:①blocked(capability) 事件;②收紧后的评论冒泡 + 否定短语排除;
    ③自评结构化缺口(task_runs.summary / 评论)。
    """
    acc = {}

    def bump(source, what, who, card, action):
        key = (source, what, tuple(sorted(who)))
        item = acc.get(key)
        if item is None:
            acc[key] = {
                "what": what,
                "who": sorted(set(who)),
                "cards": {card},
                "action": action,
                "source": source,
            }
        else:
            item["cards"].add(card)

    # 1) blocked 事件,payload.kind == capability
    for tid, evs in ev_by_task.items():
        assignee = tasks.get(tid, {}).get("assignee", "")
        for _created, kind, payload in evs:
            if kind != "blocked":
                continue
            pd = _parse_json_obj(payload)
            if not pd or pd.get("kind") != "capability":
                continue
            raw_reason = pd.get("reason")
            reason = raw_reason if isinstance(raw_reason, str) and raw_reason else "能力阻塞"
            source = "编制缺口" if "无对口专家" in reason else "能力缺口"
            bump(
                source,
                _one_line(reason, 120),
                [assignee] if assignee else [],
                tid,
                "评估新增对口专家或改派现有专家" if source == "编制缺口"
                else "补齐缺失能力(装依赖 / 补技能 / 修 MCP)后重跑该卡",
            )

    # 2) 评论冒泡(收紧后)+ 否定短语排除
    for tid, cms in cm_by_task.items():
        for _created, author, body in cms:
            body = body or ""
            if GAP_NEGATION_RE.search(body):
                continue
            m = GAP_COMMENT_RE.search(body)
            if not m:
                continue
            bump(
                "能力缺口",
                _gap_phrase(body, m),
                [author] if author else [],
                tid,
                "补齐缺失能力(装依赖 / 补技能 / 修 MCP)后重跑该卡",
            )

    # 3) 自评结构化缺口:task_runs.summary 或 task_comments.body
    for tid, cruns in runs_by_card.items():
        for r in cruns:
            m = GAP_SELFASSESS_RE.search(r.get("summary") or "")
            if not m:
                continue
            bump(
                "能力缺口",
                _gap_phrase(r["summary"], m),
                [r["profile"]] if r["profile"] else [],
                tid,
                "补齐缺失能力(装依赖 / 补技能 / 修 MCP)后重跑该卡",
            )
    for tid, cms in cm_by_task.items():
        for _created, author, body in cms:
            body = body or ""
            m = GAP_SELFASSESS_RE.search(body)
            if not m:
                continue
            bump(
                "能力缺口",
                _gap_phrase(body, m),
                [author] if author else [],
                tid,
                "补齐缺失能力(装依赖 / 补技能 / 修 MCP)后重跑该卡",
            )

    gaps = []
    for item in acc.values():
        n = len(item["cards"])
        # 需求书只要求「priority 取 高/中/低」,没给规则;这是本引擎定的启发式:
        # 缺人(编制缺口)最重;被动信号按影响卡数分档。
        # ponytail: 阈值(3/2)是拍的,有真实数据后再校准。
        if item["source"] == "编制缺口" or n >= 3:
            prio = "高"
        elif n == 2:
            prio = "中"
        else:
            prio = "低"
        gaps.append(
            {
                "what": item["what"],
                "who": item["who"],
                "affected_cards": n,
                "action": item["action"],
                "priority": prio,
                "source": item["source"],
            }
        )
    gaps.sort(key=lambda g: ({"高": 0, "中": 1, "低": 2}[g["priority"]], g["source"], g["what"]))
    return gaps


def _attribution(task, cruns, evs):
    """§4.5 一句话判定,按优先级取第一个命中。"""
    assignee = task["assignee"]
    if assignee and any(r["profile"] != assignee for r in cruns):
        return "分配问题"

    blocked_events = []
    for _created, kind, payload in evs:
        if kind == "blocked":
            pd = _parse_json_obj(payload) or {}
            blocked_events.append(pd)
    if task["status"] == "blocked" and (
        task["block_kind"] == "needs_input"
        or any(pd.get("kind") == "needs_input" for pd in blocked_events)
    ):
        return "需求不清"

    sess = [r for r in sorted(cruns, key=lambda r: r["run_id"]) if r["has_session"]]
    if len(sess) >= 2:
        reads = [r["cache_read"] for r in sess]
        outs = [r["output"] for r in sess]
        inc_read = all(reads[k] > reads[k - 1] for k in range(1, len(reads)))
        noninc_out = all(outs[k] <= outs[k - 1] for k in range(1, len(outs)))
        if inc_read and noninc_out:
            return "上下文膨胀"

    if task["block_kind"] in ("dependency", "capability") or any(
        pd.get("kind") in ("dependency", "capability") for pd in blocked_events
    ):
        return "外部依赖"
    if any(r["outcome"] == "timed_out" for r in cruns):
        return "外部依赖"

    if len(cruns) >= 2 and not any(r["outcome"] == "completed" for r in cruns):
        return "模型能力"
    return ""


def _timeline(evs, cms):
    out = []
    for created, kind, payload in evs:
        pd = _parse_json_obj(payload)
        reason = pd.get("reason") if pd else None
        text = reason if isinstance(reason, str) and reason else _one_line(payload or "")
        out.append({"ts": _r4(created), "kind": kind, "text": text})
    for created, author, body in cms:
        out.append(
            {
                "ts": _r4(created),
                "kind": "comment",
                "text": f"{author}: {_one_line(body, 120)}",
            }
        )
    out.sort(key=lambda x: x["ts"])
    return out


def compute_all(kanban_db=KANBAN_DB, main_db=MAIN_STATE_DB, profiles_dir=PROFILES_DIR):
    """读两库 → 结构化审计 JSON。唯一真源。"""
    tasks, runs, ev_by_task, cm_by_task, meta, baselines = _collect(
        kanban_db, main_db, profiles_dir
    )
    agents, raw = _build_agents(runs, baselines)
    issues = _detect_issues(tasks, runs, raw)

    issues_by_card = {}
    for it in issues:
        issues_by_card.setdefault(it["task_id"], []).append(it)

    by_card = {}
    for r in runs:
        by_card.setdefault(r["task_id"], []).append(r)

    gaps = _detect_gaps(tasks, ev_by_task, cm_by_task, by_card)

    cards = []
    for tid in sorted(tasks):
        task = tasks[tid]
        cruns = sorted(by_card.get(tid, []), key=lambda r: r["run_id"])
        evs = ev_by_task.get(tid, [])
        card_issues = sorted(issues_by_card.get(tid, []), key=lambda x: -x["severity"])
        cards.append(
            {
                "task_id": tid,
                "title": task["title"],
                "assignee": task["assignee"],
                "status": task["status"],
                "runs": [
                    {
                        "run_id": r["run_id"],
                        "profile": r["profile"],
                        "status": r["status"],
                        "outcome": r["outcome"],
                        "wall": _r4(r["wall"]),
                        "has_session": r["has_session"],
                        "input": r["input"],
                        "cache_read": r["cache_read"],
                        "output": r["output"],
                        "reasoning": r["reasoning"],
                        "api_calls": r["api_calls"],
                        "tool_calls": r["tool_calls"],
                        "cost": _r4(r["cost"]),
                        "cache_reuse": r["cache_reuse"],
                        "thinking_ratio": r["thinking_ratio"],
                    }
                    for r in cruns
                ],
                "timeline": _timeline(evs, cm_by_task.get(tid, [])),
                "attribution": _attribution(task, cruns, evs),
                "issues": [it["id"] for it in card_issues],
            }
        )

    counts = {"running": 0, "todo": 0, "blocked": 0, "done": 0, "total": len(tasks)}
    for task in tasks.values():
        if task["status"] in ("running", "todo", "blocked", "done"):
            counts[task["status"]] += 1

    return {
        "generated_at": datetime.datetime.now().astimezone().isoformat(),
        "meta": meta,
        "counts": counts,
        "issues": issues,
        "agents": [agents[p] for p in sorted(agents)],
        "gaps": gaps,
        "cards": cards,
    }


# ------------------------------------------------------------------ 自测


def _resolve_independently(kanban_db, main_db, profiles_dir):
    """自测用:独立重算解析率(不复用 compute_all 内部逻辑)。"""
    kb = _open_ro(kanban_db)
    main_conn = _open_ro(main_db)
    conns = {}
    total = 0
    with_sess = 0
    reasons = []
    for rid, tid, profile, metadata in kb.execute(
        "select id, task_id, profile, metadata from task_runs order by id"
    ):
        total += 1
        d = _parse_json_obj(metadata)
        sid = d.get("worker_session_id") if d else None
        if not (isinstance(sid, str) and sid.strip()):
            reasons.append((rid, tid, "no worker_session_id"))
            continue
        sid = sid.strip()
        profile = profile or ""
        hit = None
        fp = os.path.join(profiles_dir, profile, "state.db")
        if profile not in conns:
            conns[profile] = _open_ro(fp) if os.path.isfile(fp) else None
        if conns[profile] is not None:
            hit = conns[profile].execute("select 1 from sessions where id=?", (sid,)).fetchone()
        if not hit:
            hit = main_conn.execute("select 1 from sessions where id=?", (sid,)).fetchone()
        if hit:
            with_sess += 1
        else:
            reasons.append((rid, tid, "session_not_found"))
    kb.close()
    main_conn.close()
    for c in conns.values():
        if c is not None:
            c.close()
    return total, with_sess, reasons


def run_selftest():
    """跑自测;全过返回 0,任一断言失败返回 1。"""
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)
        return cond

    data = compute_all()

    # ---- ASSERT 1:解析率一致 + 带合法 sid 的 run 零缺失
    total, with_sess, reasons = _resolve_independently(KANBAN_DB, MAIN_STATE_DB, PROFILES_DIR)
    rate = round(with_sess / total, 4) if total else 0.0
    m = data["meta"]
    print("ASSERT 1: 解析率一致")
    print(
        f"  runs_total={m['runs_total']} runs_with_session={m['runs_with_session']} "
        f"session_resolution_rate={m['session_resolution_rate']}"
    )
    print(f"  独立重算: runs_total={total} runs_with_session={with_sess} rate={rate}")
    print(f"  missing({len(m['missing'])}):")
    for item in m["missing"]:
        print(f"    run#{item['run_id']} {item['task_id']} reason={item['reason']}")
    check(m["runs_total"] == total, "runs_total 不一致:{} != {}".format(m["runs_total"], total))
    check(m["runs_with_session"] == with_sess,
          "runs_with_session 不一致:{} != {}".format(m["runs_with_session"], with_sess))
    check(m["session_resolution_rate"] == rate,
          "session_resolution_rate 不一致:{} != {}".format(m["session_resolution_rate"], rate))
    bad_reason = [x for x in m["missing"] if x["reason"] != "no worker_session_id"]
    check(not bad_reason,
          f"存在带合法 worker_session_id 却解析失败的 run:{bad_reason}")
    check(len(m["missing"]) == len(reasons),
          f"missing 条数不一致:{len(m['missing'])} != {len(reasons)}")
    print("ASSERT 1 %s" % ("OK" if not failures else "FAIL"))

    # ---- ASSERT 2:三个已知问题命中
    print("ASSERT 2: 已知问题命中")
    expect = [
        ("t_b08e72f3", "分配错配"),
        ("t_61f3ea7a", "反复失败"),
        ("t_d0f4c3e3", "token异常"),
    ]
    hits = {(it["task_id"], it["type"]) for it in data["issues"]}
    before = len(failures)
    for tid, itype in expect:
        got = (tid, itype) in hits
        print("  {} {} -> {}".format(tid, itype, "OK" if got else "MISS"))
        check(got, f"未命中 issue:{tid}:{itype}")
    if len(failures) != before:
        print(f"  实际 issues({len(data['issues'])}):")
        for it in data["issues"]:
            print(f"    {it['id']} sev={it['severity']}")
    print("ASSERT 2 %s" % ("OK" if len(failures) == before else "FAIL"))

    # ---- ASSERT 3:纯函数样例
    print("ASSERT 3: 纯函数样例")
    samples = [
        ("cache_reuse(1000, 3000)", cache_reuse(1000, 3000), 0.75),
        ("thinking_ratio(750, 250)", thinking_ratio(750, 250), 0.25),
        ("effective_output(1000, 3000, 400)", effective_output(1000, 3000, 400), 0.1),
        ("per_call_output(400, 8)", per_call_output(400, 8), 50.0),
        ("tool_density(16, 8)", tool_density(16, 8), 2.0),
        ('severity_of("分配错配", {})', severity_of("分配错配", {}), 80),
        ('severity_of("流程阻塞", {"block_kind":"capability"})',
         severity_of("流程阻塞", {"block_kind": "capability"}), 85),
        ('severity_of("未知X", {})', severity_of("未知X", {}), 0),
    ]
    before = len(failures)
    for name, got, want in samples:
        ok = got == want
        print("  {} = {} (期望 {}) {}".format(name, got, want, "OK" if ok else "FAIL"))
        check(ok, f"{name} = {got},期望 {want}")
    print("ASSERT 3 %s" % ("OK" if len(failures) == before else "FAIL"))

    # ---- ASSERT 4:gaps 提取(纯函数,喂合成 dict,不碰真实库)
    print("ASSERT 4: gaps 提取(合成输入)")

    def gaps_of(comments=(), summaries=()):
        """用合成 comment / run.summary 跑一遍 _detect_gaps。"""
        tasks = {"t_x": {"task_id": "t_x", "assignee": "probe"}}
        cm = {
            "t_x": [(0.0, "probe", b) for b in comments]
        }
        runs = {
            "t_x": [
                {"run_id": 1, "profile": "probe", "summary": s} for s in summaries
            ]
        }
        return _detect_gaps(tasks, {}, cm, runs)

    cases = [
        # (标号, 期望是否产生, 输入类型, 文本, what 必须包含的串)
        ("N1", False, "c", "A `set_value` / background 点击，没有用前台按键、没有 raise_window。", None),
        ("N2", False, "c", "- 看 `craft-system.md` 的工作流条款:有没有明确写「画布是空的 → 第一件事」", None),
        ("N3", False, "c", "本机没有系统 Python,也不需要;要跑解释器用 `py`。", None),
        ("N4", False, "c", "bash 整体不可用,pi 直接停摆。", None),
        ("N5", False, "c", "无第三方依赖 / 无 GUI 前台依赖", None),
        ("N6", False, "c", "--ref 缺文件/指目录 → rc=1", None),
        ("N7", False, "c", "1 条自评结构化缺口(真实信号,文档 §4.7① 记载「11 张卡全部产出结构化缺口清单」)", None),
        ("P1", True, "c", "这台机器需要安装 jq 才能跑通 jq 管道。", "需要安装 jq"),
        ("P2", True, "c", "缺 sqlite3 CLI,批量查库要手写。", "CLI"),
        ("P3", True, "c", "缺 davinci-resolve MCP,41 个工具全摆设。", None),
        ("P4", True, "s", "自评完成，共 3 处不足（MCP 不足 / 技能缺比价与工商专用 / CLI 缺 jq…）", "处不足"),
        ("P5", True, "s", '自评完成:4 类里 3 类判"不足",共 6 处具体缺口,最关键是音频自检能力为零。', None),
        # P7(P1 修复):长文本 + 含换行,真正走到 §4.3 规则 4 的硬截断分支
        ("P7", True, "s", "自评完成,共 5 处不足:" + "甲" * 200 + "\n第二行正文", None),
        # P8(P2 修复):`缺\nCLI` 不得命中(否则会退化成 what="缺")
        ("P8", False, "c", "缺\nCLI,批量查库要手写。", None),
    ]
    before = len(failures)
    for label, want, kind, text, must in cases:
        g = gaps_of(comments=[text]) if kind == "c" else gaps_of(summaries=[text])
        got = bool(g)
        ok = got == want
        detail = ""
        if ok and want and must:
            hit = any(must in x["what"] for x in g)
            ok = hit
            detail = f" what含'{must}'={'OK' if hit else 'MISS'}"
        print(
            "  {} {} -> {} (期望 {}){}{}".format(
                label, kind, "产生" if got else "不产生",
                "产生" if want else "不产生", detail,
                "" if ok else "  FAIL what={}".format([x["what"] for x in g]),
            )
        )
        check(ok, f"{label} 判定不符:期望{'产生' if want else '不产生'},实得{'产生' if got else '不产生'}")

    # P6:所有产出的 what 长度 ≤ 80 且不含换行,并且**确实有一条走到了硬截断分支**
    # (P1 修复:不这样卡,断言在短输入下恒真,等于没测)
    all_texts = [c[3] for c in cases]
    all_gaps = gaps_of(comments=all_texts) + gaps_of(summaries=all_texts)
    p6_ok = True
    truncated = 0
    for x in all_gaps:
        w = x["what"]
        if len(w) > 80 or "\n" in w or "\r" in w:
            p6_ok = False
            print(f"  P6 FAIL len={len(w)} what={w!r}")
        if len(w) == 80 and w.endswith("…"):
            truncated += 1
    print(
        f"  P6 所有 what ≤ 80 且无换行 -> {'OK' if p6_ok else 'FAIL'}"
        f"(共 {len(all_gaps)} 条,其中截断 {truncated} 条)"
    )
    check(p6_ok, "P6:存在 what 长度 > 80 或含换行")
    check(truncated >= 1, "P6 空转:没有用例触发 limit=80 硬截断分支")
    print("ASSERT 4 %s" % ("OK" if len(failures) == before else "FAIL"))

    if failures:
        print(f"SELFTEST FAIL({len(failures)} 项):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SELFTEST PASS")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="kanban 审计纯计算引擎")
    parser.add_argument("--selftest", action="store_true", help="跑自测")
    args = parser.parse_args()
    if args.selftest:
        sys.exit(run_selftest())
    print(json.dumps(compute_all(), ensure_ascii=False, indent=2))
