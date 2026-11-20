"""
C1 路由：自动评价 / 求小红花 / 收货致谢

`/care` 页面 + 6 个接口。核心是「跑一轮」：它只读待评价列表 + 串行写，
每次写动作之间随机等待 40–120 秒，所以前端必须按"长任务"处理（轮询状态，
不要卡在请求里等）。
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any, Dict, List

from fastapi import APIRouter, Body, BackgroundTasks
from fastapi.responses import HTMLResponse
from loguru import logger

import care_message
import receipt_reminder

router = APIRouter()
STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"

# 正在跑的账号（防止重复点"跑一轮"把同一批订单写两遍）
_RUNNING: Dict[str, bool] = {}


@router.get("/care", response_class=HTMLResponse)
async def care_page():
    f = STATIC_DIR / "care.html"
    if f.exists():
        return HTMLResponse(f.read_text(encoding="utf-8"))
    return HTMLResponse("<h3>care.html 缺失</h3>", status_code=404)


@router.get("/api/care/status")
async def care_status() -> Dict[str, Any]:
    try:
        return {"ok": True, **care_message.stats(30),
                "running": list(_RUNNING.keys())}
    except Exception as e:
        logger.error(f"读取交易后动作状态失败: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/care/config")
async def care_get_config(cookie_id: str = "") -> Dict[str, Any]:
    """
    读配置。带 cookie_id 时返回**该账号实际生效的配置**（全局 + 覆盖），
    同时把三个来源分开列出来，前端才能说清"这个值是继承的还是自己设的"。

    为什么三者都要返回：只给最终值的话，用户看到求花是关的，无法判断
    是自己关的（全局改也不影响）还是全局本来就关（全局一开就跟着开）。
    """
    try:
        cid = str(cookie_id or "").strip()
        global_cfg = care_message.get_config()
        effective = care_message.get_account_config(cid) if cid else global_cfg
        overrides = (care_message._account_overrides().get(cid) or {}) if cid else {}
        return {"ok": True, "scope": "account" if cid else "global",
                "cookie_id": cid,
                "config": effective,
                "global": global_cfg,
                "overrides": overrides,
                "inherited": [k for k in care_message.PER_ACCOUNT_KEYS if k not in overrides]}
    except Exception as e:
        logger.error(f"读取交易后动作配置失败: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/care/pending")
async def care_pending(cookie_id: str = "") -> Dict[str, Any]:
    """只读：看看平台认为哪些单待评价。演练模式下用户就是靠这个确认链路通不通。"""
    cookie_str = _cookie_str(cookie_id)
    if not cookie_str:
        return {"ok": False, "error": "该账号没有可用 Cookie"}
    got = care_message.fetch_pending(cookie_str)
    if not got.get("ok"):
        return {"ok": False, "kind": got.get("kind"), "error": got.get("error")}
    items = []
    for row in got.get("items") or []:
        items.append({
            "trade_id": care_message.extract_trade_id(row),
            "title": _pick(row, ("title", "itemTitle", "auctionTitle")),
            "buyer": _pick(row, ("buyerNick", "buyerName", "nick")),
            "amount": _pick(row, ("price", "payAmount", "amount")),
        })
    return {"ok": True, "total": got.get("total"), "items": items}


def _pick(row: Dict[str, Any], keys) -> str:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v)[:60]
    return ""


def _cookie_str(cookie_id: str) -> str:
    if not cookie_id:
        return ""
    try:
        from db_manager import db_manager
        info = db_manager.get_cookie_details(cookie_id) or {}
        return str(info.get("value") or "")
    except Exception:
        return ""


@router.post("/api/care/config")
async def care_config(patch: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """
    保存配置。body 里带 `cookie_id` 时写该账号的覆盖，不带则写全局默认。

    为什么两个都要有：全局是"新账号的初值"，账号覆盖是"这个账号自己的选择"。
    只做账号级会让新账号无从继承；只做全局就是原来的一开全开。
    """
    try:
        allowed = set(care_message.DEFAULT_CONFIG.keys())
        clean = {k: v for k, v in (patch or {}).items() if k in allowed}
        cookie_id = str((patch or {}).get("cookie_id") or "").strip()
        # 账号级只允许覆盖"作用到买家身上"的键，其余（限流/延时）仍走全局，
        # 避免某个账号把间隔调到 0 秒把整个 IP 拖下水。
        if cookie_id:
            dropped = [k for k in clean if k not in care_message.PER_ACCOUNT_KEYS]
            for k in dropped:
                clean.pop(k, None)
            if dropped:
                logger.info(f"账号级配置忽略（只走全局）：{dropped}")
        if "rate_template" in clean and clean["rate_template"] not in care_message.DEFAULT_TEMPLATES:
            return {"ok": False, "error": f"未知评价模板：{clean['rate_template']}"}
        if "daily_cap" in clean:
            n = int(clean["daily_cap"])
            if n < 0 or n > 200:
                return {"ok": False, "error": "daily_cap 建议 0–200"}
            clean["daily_cap"] = n
        if "max_per_run" in clean:
            n = int(clean["max_per_run"])
            if n < 1 or n > 100:
                return {"ok": False, "error": "max_per_run 建议 1–100"}
            clean["max_per_run"] = n
        dmin = int(clean.get("delay_seconds_min", care_message.get_config()["delay_seconds_min"]))
        dmax = int(clean.get("delay_seconds_max", care_message.get_config()["delay_seconds_max"]))
        if dmin < 0 or dmax < dmin or dmax > 3600:
            return {"ok": False, "error": "间隔要求 0 ≤ 下限 ≤ 上限 ≤ 3600"}
        if "on_complete_delay_seconds" in clean:
            n = int(clean["on_complete_delay_seconds"])
            if n < 0 or n > 3600:
                return {"ok": False, "error": "确认收货后的等待要求 0–3600 秒"}
            clean["on_complete_delay_seconds"] = n
        cfg = care_message.save_config(clean, cookie_id=cookie_id)
        return {"ok": True, "config": cfg, "scope": "account" if cookie_id else "global",
                "cookie_id": cookie_id}
    except ValueError as e:
        return {"ok": False, "error": f"参数不是数字：{e}"}
    except Exception as e:
        logger.error(f"保存交易后动作配置失败: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/care/preview")
async def care_preview(body: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """只渲染文案，不查接口、不写库。"""
    cfg = care_message.get_config()
    merged = dict(cfg)
    if body.get("rate_template") in care_message.DEFAULT_TEMPLATES:
        merged["rate_template"] = body["rate_template"]
    if isinstance(body.get("custom_feedback"), str):
        merged["custom_feedback"] = body["custom_feedback"]
    if isinstance(body.get("flower_text"), str):
        merged["flower_text"] = body["flower_text"]
    return {"ok": True,
            "feedback": care_message.render_feedback(merged),
            "flower": care_message.render_flower(merged)}


@router.get("/api/care/reminder/config")
async def reminder_config_get() -> Dict[str, Any]:
    return {"ok": True, "config": receipt_reminder.get_config(),
            "send_enabled": receipt_reminder.SEND_ENABLED}


@router.post("/api/care/reminder/config")
async def reminder_config_save(body: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    try:
        return {"ok": True, "config": receipt_reminder.save_config(body)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/care/reminder-preview")
async def reminder_preview(cookie_id: str = "", limit: int = 1000) -> Dict[str, Any]:
    """
    收货提醒只读预演：谁该提醒、谁被跳过、为什么。**不发送任何消息。**
    真发要等真实探针确认回执语义后再打开（与发货回执共用同一单）。
    """
    try:
        return receipt_reminder.scan(cookie_id=cookie_id, limit=limit)
    except Exception as e:
        logger.error(f"收货提醒预演失败: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/care/reminder-recent")
async def reminder_recent(limit: int = 100) -> Dict[str, Any]:
    try:
        return {"ok": True, "records": receipt_reminder.recent(limit=limit)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/care/reminder-run")
async def reminder_run(body: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """
    真发入口——本轮**故意不开放**。返回预演结果，防止有人把它当发送按钮。
    """
    result = receipt_reminder.scan(cookie_id=str(body.get("cookie_id") or ""))
    return {"ok": False,
            "error": "收货提醒的真发尚未启用：发送回执语义未验证，先只做预演；"
                     "请用「预演」查看候选，等真实探针验收后再开。",
            "preview": result}


@router.post("/api/care/run")
async def care_run(background: BackgroundTasks,
                   body: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """
    跑一轮。因为内部有 40–120 秒的随机等待，放进后台任务，立刻返回。
    前端轮询 /api/care/status 看结果。
    """
    cookie_id = str(body.get("cookie_id") or "").strip()
    order_ids: List[str] = [str(x) for x in (body.get("order_ids") or []) if str(x).strip()]
    if not cookie_id:
        return {"ok": False, "error": "cookie_id 必填"}

    cfg = care_message.get_config()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "未启用（先在上面打开总开关）"}
    if not (cfg.get("auto_rate") or cfg.get("ask_flower") or cfg.get("thanks_message")):
        return {"ok": False, "error": "评价/小红花/感谢消息三个动作都没开"}

    if _RUNNING.get(cookie_id):
        return {"ok": False, "error": "该账号已有一轮在跑，等它结束"}

    cookie_str = _cookie_str(cookie_id)
    if not cookie_str:
        return {"ok": False, "error": "该账号没有可用 Cookie"}

    _RUNNING[cookie_id] = True

    def _job():
        try:
            r = care_message.run_once(cookie_id, cookie_str, order_ids or None)
            logger.info(f"交易后动作一轮完成：{r.get('counts')} stopped={r.get('stopped')}")
        except Exception as e:
            logger.error(f"交易后动作一轮异常: {e}")
        finally:
            _RUNNING.pop(cookie_id, None)

    background.add_task(_job)
    return {"ok": True, "message": "已开始，去下面看记录（每单之间会等 40–120 秒）"}


@router.post("/api/care/stop")
async def care_stop(body: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """
    手动"停"只清运行标记。真正的中断靠 `stop_on_risk` + 后端不排队——
    已经发出的单笔请求收不回来，但不会再启动下一单。
    """
    cookie_id = str(body.get("cookie_id") or "").strip()
    if cookie_id:
        _RUNNING.pop(cookie_id, None)
        return {"ok": True, "message": f"已清除 {cookie_id} 的运行标记"}
    _RUNNING.clear()
    return {"ok": True, "message": "已清除全部运行标记"}
