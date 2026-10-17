"""kanban 审计 · FastAPI 路由(v0.1)

只读包装层:所有指标一律取自 `engine.compute_all()`,本文件不重算、不复制任何
判定逻辑,也不碰任何数据库(不建 sqlite 连接、不写库)。

缓存:整份快照 30s 内存缓存(所有端点共用);`?fresh=1` 绕过。
失败:engine 抛任何异常 → HTTP 503 + `{"ok": false, "error": "<Type>: <msg>"}`,不泄漏 traceback。
"""

from __future__ import annotations

import os
import sys
import threading
import time

# engine.py 在插件根,本文件在 dashboard/ 子目录 —— 原地跑与安装后都要能 import。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # engine 缺失/损坏时不让整个插件加载崩掉,降级到 503 信封
    import engine  # noqa: E402
    _ENGINE_ERROR: BaseException | None = None
    compute_all = engine.compute_all  # noqa: E402
except Exception as _exc:  # noqa: BLE001
    engine = None  # type: ignore[assignment]
    _ENGINE_ERROR = _exc

    def compute_all(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError(f"engine unavailable: {_ENGINE_ERROR}")


from fastapi import APIRouter  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

__all__ = ["router", "compute_all"]

router = APIRouter()

CACHE_TTL_SECONDS = 30.0
_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()


# ------------------------------------------------------------------ 内部


def _snapshot(fresh: bool = False) -> dict:
    """取一份审计快照;30s 内复用缓存,fresh=True 强制重算。"""
    with _lock:
        if engine is None:
            raise RuntimeError(f"engine unavailable: {_ENGINE_ERROR}")
        now = time.monotonic()
        if not fresh and _cache["data"] is not None and (now - _cache["at"]) < CACHE_TTL_SECONDS:
            return _cache["data"]
        data = engine.compute_all()
        _cache["at"] = time.monotonic()
        _cache["data"] = data
        return data


def _fail(exc: BaseException) -> JSONResponse:
    """engine 失败时的统一信封:503 + ok:false + 异常摘要(不含 traceback)。"""
    return JSONResponse(
        status_code=503,
        content={"ok": False, "error": f"{type(exc).__name__}: {exc}"},
    )


def _load(fresh: bool = False) -> dict | JSONResponse:
    """成功返回快照 dict,失败返回 503 信封 —— 调用方 isinstance 分流。"""
    try:
        return _snapshot(fresh)
    except Exception as exc:  # 所有端点统一结构化失败,不裸 500
        return _fail(exc)


# ------------------------------------------------------------------ 端点


@router.get("/health")
def health():
    data = _load()
    if isinstance(data, JSONResponse):
        return data
    meta = data["meta"]
    return {
        "ok": True,
        "generated_at": data["generated_at"],
        "runs_total": meta["runs_total"],
        "runs_with_session": meta["runs_with_session"],
        "session_resolution_rate": meta["session_resolution_rate"],
        "missing": meta["missing"],
    }


@router.get("/summary")
def summary():
    data = _load()
    if isinstance(data, JSONResponse):
        return data
    issue_counts: dict = {}
    for it in data["issues"]:
        issue_counts[it["type"]] = issue_counts.get(it["type"], 0) + 1
    return {
        "counts": data["counts"],
        "generated_at": data["generated_at"],
        "issue_counts": issue_counts,
    }


@router.get("/issues")
def issues(fresh: int = 0):
    data = _load(bool(fresh))
    if isinstance(data, JSONResponse):
        return data
    return {"issues": data["issues"], "generated_at": data["generated_at"]}


@router.get("/agents")
def agents(fresh: int = 0):
    data = _load(bool(fresh))
    if isinstance(data, JSONResponse):
        return data
    return {"agents": data["agents"], "generated_at": data["generated_at"]}


@router.get("/gaps")
def gaps(fresh: int = 0):
    data = _load(bool(fresh))
    if isinstance(data, JSONResponse):
        return data
    return {"gaps": data["gaps"], "generated_at": data["generated_at"]}


@router.get("/card/{task_id}")
def card(task_id: str):
    data = _load()
    if isinstance(data, JSONResponse):
        return data
    found = next((c for c in data["cards"] if c["task_id"] == task_id), None)
    if found is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"card not found: {task_id}"},
        )
    by_id = {it["id"]: it for it in data["issues"]}
    card_issues = [by_id[i] for i in found["issues"] if i in by_id]
    card_issues.sort(key=lambda x: -x["severity"])
    return {
        "task": {k: found[k] for k in ("task_id", "title", "assignee", "status")},
        "timeline": found["timeline"],
        "runs": found["runs"],
        "attribution": found["attribution"] or "",
        "issues": card_issues,
    }
