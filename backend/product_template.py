"""
商品模板 + 批量发布（A5）

为什么要有：同一类虚拟商品（比如"考研资料-XX 学校 XX 专业"）每次上架要重填
标题/描述/价格/运费/图片，50 个就是 50 次手工。模板把不变量固化，变量用
{{占位符}} 填，一次导入几十行，队列按限流逐条发。

四条硬边界（照抄闲管家/平台容忍度，不突破）：
1. 发布 1 次/60s/账号；**单账号日发布上限 20 条**（DAILY_PUBLISH_CAP）。
   注意：rate_limiter 的 200 是所有写操作的总兜底，不是发布配额——平台对
   「短时间大批量上架/下架」有降权记录，实操文档给的量级是 5–20 条/天。
2. 夜间 23:00–08:00 不发（批量尤其不能在半夜偷偷跑）。
3. 任何一条进 submission_unknown 立刻整批暂停 —— 平台可能已收到，继续发
   只会造成重复上架。必须人工回读后手动恢复。
4. 渲染结果先过 publish_guard：BLOCK 词命中直接拒绝建批，WARN 词只提示。

默认 dry_run=True：只做参数渲染与校验，不真发。用户确认无误后再关掉。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 可用 TEMPLATE_DB 覆盖（测试用），避免测试写到用户真实模板库
DB_PATH = Path(os.environ.get("TEMPLATE_DB")
               or (PROJECT_ROOT / "data" / "template_library.db"))

MAX_BATCH_ROWS = 50          # 单批上限（对齐闲管家 batchCreate ≤50）
MAX_CONCURRENT_JOBS = 1      # 同时只允许一个批次在跑，避免自己撞自己
BATCH_INTERVAL = 60          # 两条之间最少 60 秒（平台容忍度，不突破）
BATCH_WAIT_MAX = 100         # 单条最多排队等 100 秒，超过就暂停交人工
# 单账号每日真实发布上限。平台共识是「一天发太猛会降权」，多个独立来源给出
# 的上限在 5–20 条之间（新号 5 条以上铺量、老号 8–10 条算正常）。这里取 20，
# 比 rate_limiter 的 200 保守一个数量级——200 是所有写操作的总兜底，不是发布配额。
DAILY_PUBLISH_CAP = 20
FAILURE_POLICIES = ("skip", "pause")
DEFAULT_FAILURE_POLICY = "skip"
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_一-龥]{1,24})\s*\}\}")

_lock = threading.Lock()
_running: Dict[str, str] = {}   # job_id -> "running"（进程内互斥）


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS templates (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            title_tpl    TEXT NOT NULL DEFAULT '',
            desc_tpl     TEXT NOT NULL DEFAULT '',
            price        REAL DEFAULT 0,
            quantity     INTEGER DEFAULT 1,
            delivery     TEXT DEFAULT '无需邮寄',
            post_price   REAL DEFAULT 0,
            material_id  INTEGER DEFAULT 0,
            category     TEXT DEFAULT '',
            tags         TEXT DEFAULT '',
            note         TEXT DEFAULT '',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batch_jobs (
            id           TEXT PRIMARY KEY,
            template_id  INTEGER NOT NULL,
            cookie_id    TEXT DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'pending',
            dry_run      INTEGER DEFAULT 1,
            failure_policy TEXT NOT NULL DEFAULT 'skip',
            total        INTEGER DEFAULT 0,
            done         INTEGER DEFAULT 0,
            failed       INTEGER DEFAULT 0,
            skipped      INTEGER DEFAULT 0,
            current      TEXT DEFAULT '',
            error        TEXT DEFAULT '',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batch_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      TEXT NOT NULL,
            seq         INTEGER NOT NULL,
            vars_json   TEXT DEFAULT '{}',
            title       TEXT DEFAULT '',
            price       REAL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'pending',
            item_id     TEXT DEFAULT '',
            error       TEXT DEFAULT '',
            failure_kind TEXT DEFAULT '',
            updated_at  TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_batch_items_job
            ON batch_items(job_id, seq);
    """)
    # 老库不能重建表，升级时只补列。已有批次默认沿用旧的“普通失败后继续”
    # 行为，避免升级当天改变仍在排队的任务。
    import migrate
    migrate.ensure_columns(conn, "batch_jobs", {
        "failure_policy": "TEXT NOT NULL DEFAULT 'skip'",
    }, commit=False)
    migrate.ensure_columns(conn, "batch_items", {
        "failure_kind": "TEXT DEFAULT ''",
    }, commit=False)
    conn.commit()
    return conn


# ---------------------------------------------------------------- 模板

def extract_placeholders(*texts: str) -> List[str]:
    found: List[str] = []
    seen = set()
    for t in texts:
        for m in PLACEHOLDER_RE.finditer(str(t or "")):
            k = m.group(1).strip()
            if k and k not in seen:
                seen.add(k)
                found.append(k)
    return found


def render(text: str, variables: Dict[str, Any]) -> str:
    """把 {{键}} 换成变量值；缺的键原样保留（便于人工发现漏填）。"""
    def sub(m):
        k = m.group(1).strip()
        if k in variables and variables[k] is not None:
            return str(variables[k])
        return m.group(0)
    return PLACEHOLDER_RE.sub(sub, str(text or ""))


def create_template(data: Dict[str, Any]) -> Dict[str, Any]:
    name = str(data.get("name") or "").strip()[:80]
    title_tpl = str(data.get("title_tpl") or "").strip()
    if not name:
        return {"ok": False, "error": "模板名称不能为空"}
    if not title_tpl:
        return {"ok": False, "error": "标题模板不能为空"}
    if len(title_tpl) > 200:
        return {"ok": False, "error": "标题模板过长（建议 ≤30 字，闲鱼标题有长度限制）"}
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO templates(name,title_tpl,desc_tpl,price,quantity,delivery,"
            "post_price,material_id,category,tags,note,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, title_tpl, str(data.get("desc_tpl") or ""),
             float(data.get("price") or 0), int(data.get("quantity") or 1),
             str(data.get("delivery") or "无需邮寄"), float(data.get("post_price") or 0),
             int(data.get("material_id") or 0), _json_or_empty(data.get("category")),
             str(data.get("tags") or "").strip()[:200],
             str(data.get("note") or "").strip()[:500], _now(), _now()),
        )
        conn.commit()
        return {"ok": True, "id": cur.lastrowid,
                "placeholders": extract_placeholders(title_tpl, str(data.get("desc_tpl") or ""))}
    finally:
        conn.close()


def _json_or_empty(v: Any) -> str:
    if not v:
        return ""
    if isinstance(v, str):
        return v.strip()
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return ""


def update_template(template_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "模板不存在"}
        fields = ("name", "title_tpl", "desc_tpl", "price", "quantity", "delivery",
                  "post_price", "material_id", "category", "tags", "note")
        sets, args = [], []
        for f in fields:
            if f not in data:
                continue
            v = data[f]
            if f == "category":
                v = _json_or_empty(v)
            elif f in ("price", "post_price"):
                v = float(v or 0)
            elif f in ("quantity", "material_id"):
                v = int(v or 0)
            sets.append(f"{f}=?")
            args.append(v)
        if not sets:
            return {"ok": True, "id": template_id, "changed": 0}
        sets.append("updated_at=?")
        args.append(_now())
        args.append(template_id)
        conn.execute(f"UPDATE templates SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
        return {"ok": True, "id": template_id, "changed": 1}
    finally:
        conn.close()


def delete_template(template_id: int) -> Dict[str, Any]:
    conn = _conn()
    try:
        conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
        conn.commit()
        return {"ok": True, "id": template_id}
    finally:
        conn.close()


def get_template(template_id: int) -> Dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "模板不存在"}
        return {"ok": True, "template": _row_to_template(row)}
    finally:
        conn.close()


def _row_to_template(row: sqlite3.Row) -> Dict[str, Any]:
    cat = None
    if row["category"]:
        try:
            cat = json.loads(row["category"])
        except Exception:
            cat = None
    return {
        "id": row["id"], "name": row["name"],
        "title_tpl": row["title_tpl"], "desc_tpl": row["desc_tpl"],
        "price": row["price"], "quantity": row["quantity"],
        "delivery": row["delivery"], "post_price": row["post_price"],
        "material_id": row["material_id"], "category": cat,
        "tags": row["tags"], "note": row["note"],
        "placeholders": extract_placeholders(row["title_tpl"], row["desc_tpl"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_templates(keyword: str = "") -> Dict[str, Any]:
    conn = _conn()
    try:
        sql = "SELECT * FROM templates"
        args: List[Any] = []
        if keyword:
            sql += " WHERE name LIKE ? OR tags LIKE ? OR note LIKE ?"
            kw = f"%{keyword}%"
            args = [kw, kw, kw]
        sql += " ORDER BY updated_at DESC, id DESC LIMIT 200"
        rows = conn.execute(sql, args).fetchall()
        return {"ok": True, "items": [_row_to_template(r) for r in rows]}
    finally:
        conn.close()


# ---------------------------------------------------------------- 预览渲染

def preview(template_id: int, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """不落库、不发布，只把变量灌进模板，返回渲染结果与校验问题。"""
    t = get_template(template_id)
    if not t.get("ok"):
        return t
    tpl = t["template"]
    need = tpl["placeholders"]
    out, problems = [], []
    for i, row in enumerate((rows or [])[:MAX_BATCH_ROWS], 1):
        vars_ = {str(k): v for k, v in (row or {}).items()}
        title = render(tpl["title_tpl"], vars_)
        desc = render(tpl["desc_tpl"], vars_)
        price = row.get("price")
        try:
            price = float(price if price not in (None, "") else tpl["price"])
        except Exception:
            price = 0.0
        left = [p for p in need if f"{{{{{p}}}}}" in title or f"{{{{{p}}}}}" in desc]
        issues = []
        warnings_tpl: List[str] = []
        if left:
            issues.append("未填变量：" + "、".join(left))
        if not title.strip():
            issues.append("标题为空")
        # 标题长度口径统一交给 publish_guard（30 字上限 + 前 25 字放核心信息）。
        # 此前这里是 `len(title) > 60` 才提示，和 opportunity_rewrite 的 30 字截断矛盾——
        # 改写那边砍到 30，模板这边 40 字算"正常"，用户根本不知道会被截。
        if price <= 0:
            issues.append("价格必须大于 0")
        if not desc.strip():
            issues.append("描述为空（闲鱼要求填写）")
        # 发布前合规闸：模板渲染出来的每一行都要过关，否则批量发布就是把违禁词
        # 一次性提交 50 遍——这比手工发一两条危险得多。所以 BLOCK 级算 problems（拦），
        # WARN 级只进 warnings（提示），绝不把「正品」这种正常二手词判成无效行。
        warnings: List[str] = list(warnings_tpl)
        try:
            from publish_guard import check_publish
            g = check_publish(title, desc)
            if not g["ok"]:
                issues.append("违禁/导流词：" + "、".join(g["block"]))
            warnings.extend(g.get("warnings") or [])
        except Exception:
            # 闸门本身出错不能把预览带崩，但要在结果里留痕
            warnings.append("合规检查未执行（publish_guard 异常）")
        if issues:
            problems.append({"seq": i, "issues": issues})
        out.append({"seq": i, "variables": vars_, "title": title, "desc": desc,
                    "price": price, "issues": issues, "warnings": warnings})
    return {"ok": True, "template": tpl, "rows": out, "problems": problems,
            "row_count": len(out), "valid": len(out) - len(problems)}


# ---------------------------------------------------------------- 批量任务

def create_job(template_id: int, rows: List[Dict[str, Any]], cookie_id: str,
               dry_run: bool = True,
               failure_policy: str = DEFAULT_FAILURE_POLICY) -> Dict[str, Any]:
    failure_policy = str(failure_policy or DEFAULT_FAILURE_POLICY).strip().lower()
    if failure_policy not in FAILURE_POLICIES:
        return {
            "ok": False,
            "error": "failurePolicy 只能是 skip 或 pause",
        }
    if not cookie_id and not dry_run:
        return {"ok": False, "error": "真实发布必须指定账号（cookie_id）"}
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return {"ok": False, "error": "没有要发布的行"}
    if len(rows) > MAX_BATCH_ROWS:
        return {"ok": False, "error": f"单批最多 {MAX_BATCH_ROWS} 行（对齐闲管家批量上限）"}
    if not dry_run:
        # 日配额前置检查：一次性提交 30 行、发到一半被限，比开跑前说清楚更糟。
        import rate_limiter
        today = rate_limiter.daily_count("publish", cookie_id)
        if today >= DAILY_PUBLISH_CAP:
            return {"ok": False,
                    "error": f"该账号今天已发布 {today} 条，达到单日上限 {DAILY_PUBLISH_CAP} 条。"
                             "平台对『短时间大批量上架』有降权记录，建议明天继续。"}
        if today + len(rows) > DAILY_PUBLISH_CAP:
            return {"ok": False,
                    "error": f"本批 {len(rows)} 行 + 今日已发 {today} 条超过单日上限 "
                             f"{DAILY_PUBLISH_CAP} 条，请减少行数或分两天。"}
    pv = preview(template_id, rows)
    if not pv.get("ok"):
        return pv
    if pv["problems"]:
        # 违禁词和「变量没填」是两回事，报错要说清楚是哪一类，否则用户会去反复检查
        # 明明填好了的变量。
        has_block = any("违禁/导流词" in i for p in pv["problems"] for i in p["issues"])
        msg = ("有内容命中平台违禁/导流词，已在发布前拦下（见 problems）"
               if has_block else "有行没填完整，先生成预览修正")
        return {"ok": False, "error": msg, "problems": pv["problems"]}

    job_id = uuid.uuid4().hex[:12]
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO batch_jobs(id,template_id,cookie_id,status,dry_run,"
            "failure_policy,total,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, template_id, cookie_id, "pending", 1 if dry_run else 0,
             failure_policy, len(rows), _now(), _now()),
        )
        for i, r in enumerate(rows, 1):
            rv = next(x for x in pv["rows"] if x["seq"] == i)
            conn.execute(
                "INSERT INTO batch_items(job_id,seq,vars_json,title,price,status) "
                "VALUES (?,?,?,?,?,?)",
                (job_id, i, json.dumps(r, ensure_ascii=False), rv["title"],
                 rv["price"], "pending"),
            )
        conn.commit()
        return {
            "ok": True,
            "job_id": job_id,
            "total": len(rows),
            "dry_run": dry_run,
            "failurePolicy": failure_policy,
        }
    finally:
        conn.close()


def get_job(job_id: str) -> Dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "批次不存在"}
        items = conn.execute(
            "SELECT * FROM batch_items WHERE job_id=? ORDER BY seq", (job_id,)
        ).fetchall()
        job = dict(row)
        job["dry_run"] = bool(job["dry_run"])
        job["failure_policy"] = job.get("failure_policy") or DEFAULT_FAILURE_POLICY
        job["failurePolicy"] = job["failure_policy"]
        job["items"] = [{
            "seq": it["seq"], "title": it["title"], "price": it["price"],
            "status": it["status"], "item_id": it["item_id"], "error": it["error"],
            "failure_kind": it["failure_kind"] or "",
            "updated_at": it["updated_at"],
        } for it in items]
        return {"ok": True, "job": job}
    finally:
        conn.close()


def list_jobs(limit: int = 20) -> Dict[str, Any]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM batch_jobs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(100, limit)),),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["dry_run"] = bool(d["dry_run"])
            d["failure_policy"] = d.get("failure_policy") or DEFAULT_FAILURE_POLICY
            d["failurePolicy"] = d["failure_policy"]
            out.append(d)
        return {"ok": True, "items": out}
    finally:
        conn.close()


def _set_job(job_id: str, **fields) -> None:
    if not fields:
        return
    conn = _conn()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE batch_jobs SET {sets}, updated_at=? WHERE id=?",
                     list(fields.values()) + [_now(), job_id])
        conn.commit()
    finally:
        conn.close()


def _set_item(job_id: str, seq: int, status: str, item_id: str = "",
              error: str = "", failure_kind: str = "") -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE batch_items SET status=?, item_id=?, error=?, failure_kind=?, "
            "updated_at=? "
            "WHERE job_id=? AND seq=?",
            (status, item_id, error[:500], failure_kind[:80], _now(), job_id, seq),
        )
        conn.commit()
    finally:
        conn.close()


def _count(job_id: str) -> Dict[str, int]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) c FROM batch_items WHERE job_id=? GROUP BY status",
            (job_id,),
        ).fetchall()
        d = {r["status"]: int(r["c"]) for r in rows}
        return {"done": d.get("done", 0), "failed": d.get("failed", 0),
                "skipped": d.get("skipped", 0), "pending": d.get("pending", 0)}
    finally:
        conn.close()


def _count_cols(job_id: str) -> Dict[str, int]:
    """只保留 batch_jobs 里真实存在的列（pending 只是派生量，不能写库）。"""
    c = _count(job_id)
    return {k: v for k, v in c.items() if k in ("done", "failed", "skipped")}


def _night_ban() -> Tuple[bool, str]:
    now = datetime.now()
    if 23 <= now.hour or now.hour < 8:
        return False, "夜间时段（23:00–08:00）禁止发布，批次已暂停；请白天恢复"
    return True, ""


def _pending_rows(job_id: str) -> List[sqlite3.Row]:
    conn = _conn()
    try:
        return conn.execute(
            "SELECT * FROM batch_items WHERE job_id=? AND status='pending' ORDER BY seq",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()


def _row_idempotency_key(job: Dict[str, Any], row: sqlite3.Row) -> str:
    import idempotency
    return idempotency.make_key(
        "batch_publish",
        str(job.get("cookie_id") or ""),
        row["title"],
        row["price"],
    )


def _unresolved_submissions(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    找出继续执行前必须人工核对的行。

    `failure_kind` 记录当前代码产生的未知提交；幂等账本再兜一层老数据：
    进程若在 claim 后、写回 batch_items 前退出，行仍可能是 pending/failed，
    但账本已经是 pending 或 submission_unknown。此时继续跑可能重复上架。
    """
    import idempotency
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM batch_items WHERE job_id=? AND status IN ('pending','failed') "
            "ORDER BY seq",
            (job["id"],),
        ).fetchall()
    finally:
        conn.close()

    blocked: List[Dict[str, Any]] = []
    for row in rows:
        key = _row_idempotency_key(job, row)
        record = idempotency.get(key) or {}
        ledger_status = str(record.get("status") or "")
        failure_kind = str(row["failure_kind"] or "")
        if (
            failure_kind == "submission_unknown"
            or ledger_status in ("pending", "submission_unknown")
        ):
            blocked.append({
                "seq": int(row["seq"]),
                "title": str(row["title"] or ""),
                "status": str(row["status"] or ""),
                "failure_kind": failure_kind,
                "ledger_status": ledger_status,
            })
    return blocked


def run_job(job_id: str, max_items: int = 0) -> Dict[str, Any]:
    """
    同步执行一个批次（调用方负责放到后台线程）。
    每行：取 rate_limiter 配额 → 夜间检查 → 发布 → 写回结果。
    任何 submission_unknown 立即整批暂停。
    """
    import rate_limiter
    from db_manager import db_manager

    with _lock:
        if _running:
            return {"ok": False, "error": "已有批次在跑，等它结束（避免自己撞自己）"}
        _running[job_id] = "running"
    try:
        jinfo = get_job(job_id)
        if not jinfo.get("ok"):
            return jinfo
        job = jinfo["job"]
        if job["status"] in ("done", "cancelled"):
            return {"ok": False, "error": "该批次已结束"}
        dry = bool(job["dry_run"])
        cookie_id = job["cookie_id"]
        failure_policy = str(
            job.get("failure_policy") or DEFAULT_FAILURE_POLICY
        ).strip().lower()
        if failure_policy not in FAILURE_POLICIES:
            failure_policy = DEFAULT_FAILURE_POLICY

        unresolved = _unresolved_submissions(job)
        if unresolved:
            _set_job(
                job_id,
                status="paused",
                error="存在提交结果未知或中断的发布，必须先逐行人工核对后重置队列",
            )
            return {
                "ok": False,
                "error": "存在提交结果未知或中断的发布，队列未继续",
                "requires_verification": True,
                "unknown_items": unresolved,
                "job": get_job(job_id)["job"],
            }

        _set_job(job_id, status="running", error="")
        processed = 0
        for row in _pending_rows(job_id):
            if max_items and processed >= max_items:
                _set_job(job_id, status="paused", error="已达本轮处理上限")
                break
            ok, reason = _night_ban()
            if not ok and not dry:
                _set_job(job_id, status="paused", error=reason)
                break

            if not dry:
                if db_manager.is_account_automation_silent(cookie_id):
                    _set_job(job_id, status="paused", error="账号处于静默模式，已暂停批量发布")
                    break
                import idempotency
                key = idempotency.make_key("batch_publish", cookie_id, row["title"],
                                           row["price"])
                # B5 顺序：**只读预检 → 取配额 → 原子 claim**。
                # 同一行被重复触发时，先排队 100 秒才发现"已提交过"是纯浪费。
                pre = idempotency.precheck(key)
                if pre.get("blocked"):
                    _set_item(job_id, row["seq"], "failed", "",
                              "上一次相同内容结果未知，需人工回读后再发",
                              failure_kind="submission_unknown")
                    _set_job(job_id, status="paused",
                             error="检测到 submission_unknown，整批暂停，请人工核对")
                    break
                if pre.get("exists"):
                    _set_item(job_id, row["seq"], "skipped", "", "同内容已提交过")
                    _set_job(job_id, **_count_cols(job_id))
                    continue

                # 批量发布本来就该是"一分钟一条"的节奏：排不上就等，不要一遇限流就停，
                # 否则 50 行要人工点 50 次。等太久（>100s，说明别处还在写）才交人工。
                ok_q = rate_limiter.wait("publish", cookie_id, max_wait=BATCH_WAIT_MAX,
                                         limit=1, window=BATCH_INTERVAL)
                if not ok_q:
                    _set_job(job_id, status="paused",
                             error="发布限流等待超过 100 秒（可能另有写操作在跑），批次已暂停")
                    break
                if db_manager.is_account_automation_silent(cookie_id):
                    _set_job(job_id, status="paused", error="账号处于静默模式，已暂停批量发布")
                    break
                claim = idempotency.claim(key, "publish", note=row["title"][:80])
                if claim.get("blocked"):
                    _set_item(job_id, row["seq"], "failed", "",
                              "上一次相同内容结果未知，需人工回读后再发",
                              failure_kind="submission_unknown")
                    _set_job(job_id, status="paused",
                             error="检测到 submission_unknown，整批暂停，请人工核对")
                    break
                if not claim.get("claimed"):
                    _set_item(job_id, row["seq"], "skipped", "", "同内容已提交过")
                    _set_job(job_id, **_count_cols(job_id))
                    continue
            else:
                key = ""

            try:
                if dry:
                    time.sleep(0.05)
                    _set_item(job_id, row["seq"], "done", "", "dry-run 未真实提交")
                else:
                    res = _publish_one(job, row)
                    _set_item(job_id, row["seq"], "done",
                              str(res.get("itemId", "")), "")
                    try:
                        idempotency.finish(key, "done", note=str(res.get("itemId", ""))[:80])
                    except Exception:
                        pass
            except Exception as e:
                msg = str(e)[:300]
                unknown = ("结果未知" in msg) or (getattr(e, "submission_status", "") == "submission_unknown")
                failure_kind = "submission_unknown" if unknown else (
                    "risk" if _is_risk(e) else "failed"
                )
                _set_item(
                    job_id,
                    row["seq"],
                    "failed",
                    "",
                    msg,
                    failure_kind=failure_kind,
                )
                if not dry:
                    try:
                        if unknown:
                            idempotency.finish(key, "submission_unknown", note=msg[:200])
                        else:
                            idempotency.release(key)
                    except Exception:
                        pass
                if unknown:
                    _set_job(job_id, status="paused",
                             error="发布结果未知（平台可能已收到），整批暂停，请人工回读")
                    break
                if _is_risk(e):
                    _set_job(job_id, status="paused", error=f"触发风控：{msg[:120]}")
                    break
                if failure_policy == "pause":
                    _set_job(
                        job_id,
                        status="paused",
                        error=f"第 {row['seq']} 行普通失败；failurePolicy=pause，已停止后续行",
                    )
                    break
            finally:
                processed += 1
                _set_job(job_id, **_count_cols(job_id))

        c = _count(job_id)
        jinfo = get_job(job_id)
        still_paused = jinfo.get("ok") and jinfo["job"]["status"] == "paused"
        if not still_paused:
            final = "done" if c["pending"] == 0 else "paused"
            _set_job(job_id, status=final, **_count_cols(job_id))
        return {"ok": True, "job": get_job(job_id)["job"]}
    finally:
        with _lock:
            _running.pop(job_id, None)


def _is_risk(e: Exception) -> bool:
    try:
        from opportunity_publisher import PublishRiskError, PublishAuthError, PublishPausedError
        return isinstance(e, (PublishRiskError, PublishAuthError, PublishPausedError))
    except Exception:
        txt = str(e)
        return any(k in txt for k in ("RGV587", "风控", "登录", "TOKEN"))


def _publish_one(job: Dict[str, Any], row: sqlite3.Row) -> Dict[str, Any]:
    """真正走一次发布（复用 opportunity_api 的取 cookie 与发布链路）。"""
    from config import config
    from opportunity_publisher import publish_item
    import material_library as matlib

    t = get_template(int(job["template_id"]))
    if not t.get("ok"):
        raise RuntimeError("模板已不存在")
    tpl = t["template"]

    vars_ = json.loads(row["vars_json"] or "{}")
    title = render(tpl["title_tpl"], vars_)
    desc = render(tpl["desc_tpl"], vars_)
    if not desc.strip():
        desc = title

    images: List[str] = []
    if tpl["material_id"]:
        mp = matlib.material_paths(int(tpl["material_id"]))
        if mp.get("ok"):
            images = mp["paths"]
    if not images:
        raise RuntimeError("模板没有可用图片：请绑定素材库素材（至少 1 张图）")

    # 取 cookie：与 opportunity_api 同源
    from opportunity_api import _resolve_cookie
    cookie_str = _resolve_cookie(job["cookie_id"])
    if not cookie_str:
        raise RuntimeError("账号 Cookie 不存在，请重新登录")

    item_data = {
        "title": title, "desc": desc, "imageUrls": images,
        "price": float(row["price"] or tpl["price"] or 0),
        "quantity": int(tpl["quantity"] or 1),
        "delivery": tpl["delivery"] or "无需邮寄",
        "postPrice": float(tpl["post_price"] or 0),
    }
    if tpl.get("category"):
        item_data["category"] = tpl["category"]
    return publish_item(cookie_str, item_data,
                        str(config.get("APP_CONFIG.app_key", "")), cookie_id=job["cookie_id"])


def resume_job(job_id: str) -> Dict[str, Any]:
    j = get_job(job_id)
    if not j.get("ok"):
        return j
    if j["job"]["status"] == "done":
        return {"ok": False, "error": "批次已完成"}
    if j["job"]["status"] == "cancelled":
        return {"ok": False, "error": "已取消的批次不能直接恢复"}
    if j["job"]["status"] == "running":
        with _lock:
            active = job_id in _running
        if active:
            return {"ok": False, "error": "该批次正在执行，不能同时恢复"}
    unresolved = _unresolved_submissions(j["job"])
    if unresolved:
        return {
            "ok": False,
            "error": "存在提交结果未知或中断的发布，必须先逐行人工核对",
            "requires_verification": True,
            "unknown_items": unresolved,
        }
    _set_job(job_id, status="pending", error="")
    return {"ok": True, "job_id": job_id}


def reset_job_unknown(job_id: str, seq: int, outcome: str, reason: str,
                      matched_item_id: str = "") -> Dict[str, Any]:
    """
    人工核对后释放批次中的单条未知/中断提交。

    一次只处理一行：
    - confirmed     平台已有 -> 该行 done，禁止重发
    - not_received  平台没有 -> 该行回 pending，可继续排队
    - still_unknown 尚未核清 -> 保持阻断
    """
    j = get_job(job_id)
    if not j.get("ok"):
        return j
    job = j["job"]
    seq = int(seq)
    item = next((it for it in job.get("items") or [] if int(it["seq"]) == seq), None)
    if item is None:
        return {"ok": False, "error": "批次行不存在"}
    if item["status"] in ("done", "skipped"):
        return {"ok": False, "error": "已完成或已跳过的行不需要核对"}

    import idempotency
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM batch_items WHERE job_id=? AND seq=?",
            (job_id, seq),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "批次行不存在"}
    key = _row_idempotency_key(job, row)
    record = idempotency.get(key) or {}
    ledger_status = str(record.get("status") or "")
    if not ledger_status and item.get("failure_kind") != "submission_unknown":
        return {"ok": False, "error": "该行没有提交结果未知记录，普通失败无需重置队列"}

    # pending 只允许从“当前没有活动 worker”的批次人工核对。仍在运行的批次
    # 不能由另一个请求改写其幂等账本。
    with _lock:
        active = job_id in _running
    verified = idempotency.verify_unknown(
        key,
        outcome,
        reason=reason,
        matched_item_id=matched_item_id,
        allow_pending=(ledger_status == "pending" and not active),
    )
    if not verified.get("ok"):
        return verified

    if outcome == "confirmed":
        _set_item(
            job_id,
            seq,
            "done",
            matched_item_id,
            "人工核对：平台已有该提交，已按完成处理",
            failure_kind="",
        )
    elif outcome == "not_received":
        _set_item(
            job_id,
            seq,
            "pending",
            "",
            "人工核对：平台未收到，允许重新排队",
            failure_kind="",
        )
    else:
        _set_item(
            job_id,
            seq,
            "failed",
            "",
            "人工核对：结果仍未确认，继续保持阻断",
            failure_kind="submission_unknown",
        )

    _set_job(job_id, status="paused", **_count_cols(job_id))
    return {
        "ok": True,
        "job_id": job_id,
        "seq": seq,
        **verified,
        "remaining_unknown": _unresolved_submissions(get_job(job_id)["job"]),
        "job": get_job(job_id)["job"],
    }


def cancel_job(job_id: str) -> Dict[str, Any]:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE batch_items SET status='skipped', error='批次取消' "
            "WHERE job_id=? AND status='pending'", (job_id,))
        conn.commit()
    finally:
        conn.close()
    c = _count(job_id)
    _set_job(job_id, status="cancelled", **_count_cols(job_id))
    return {"ok": True, "job_id": job_id, **c}


def stats() -> Dict[str, Any]:
    conn = _conn()
    try:
        t = conn.execute("SELECT COUNT(*) c FROM templates").fetchone()["c"]
        j = conn.execute("SELECT COUNT(*) c FROM batch_jobs").fetchone()["c"]
        return {"ok": True, "templates": int(t), "jobs": int(j),
                "max_batch_rows": MAX_BATCH_ROWS,
                "running": list(_running.keys())}
    finally:
        conn.close()
