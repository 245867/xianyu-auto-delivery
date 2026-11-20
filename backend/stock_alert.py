"""
卡券低库存预警（拆解 13：嗷嗷鱼 FAQ Q25）
==========================================

**为什么需要它**：虚拟商品的断货代价不在"少卖一单"，在"买家付了钱、你没有卡"——
要么退款加解释，要么纠纷。而卡密是**悄悄用完**的：`data_content` 一行一行被
`consume_batch_data()` 减掉，没有任何地方会喊一声。

嗷嗷鱼的做法：后台每 30 分钟扫一次启用中的「批量数据」卡券，剩余 ≤ 10 条时
推一条提醒，同一渠道 24 小时内只提醒一次。

我们照这个口径做，只改一处去重策略：**按卡券**记"上次提醒时间"，
而不是全局 24 小时。理由是我们的卡券数量少、断货风险集中在主力商品上——
一张主力卡券刚到货又掉到 5 条，和一张冷门卡券剩 8 条，不该抢同一次提醒额度。
同一条卡券仍然 24 小时只提醒一次，避免每 10 分钟刷屏。

阈值不开放配置：10 条是"够撑一两天"的经验值，能配的东西越多，越没人配。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STATE_FILE = Path(os.environ.get("STOCK_ALERT_STATE") or (DATA_DIR / "stock_alert_state.json"))

LOW_STOCK = 10          # 剩余 ≤ 这个数就提醒
ALERT_EVERY_HOURS = 24  # 同一张卡券的提醒间隔


def remaining_of(card: Dict[str, Any]) -> Optional[int]:
    """
    这张卡券还剩多少条。只有 `type='data'`（批量卡密）有"库存"概念；
    text / api / image 是"每次都一样/每次现取"，没有耗尽的问题，返回 None。
    """
    if str((card or {}).get("type") or "") != "data":
        return None
    raw = (card or {}).get("data_content") or ""
    return len([ln for ln in str(raw).split("\n") if ln.strip()])


def _label(card: Dict[str, Any]) -> str:
    name = str(card.get("name") or f"#{card.get('id')}")
    if card.get("is_multi_spec"):
        return f"{name}（{card.get('spec_name') or ''}:{card.get('spec_value') or ''}）"
    return name


def scan(user_id: Optional[int] = None) -> Dict[str, Any]:
    """扫一遍所有启用的批量卡券，返回低于阈值的那些（不发送任何东西）。"""
    try:
        from db_manager import db_manager
        cards = db_manager.get_all_cards(user_id) or []
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "checked": 0, "low": []}

    low: List[Dict[str, Any]] = []
    checked = 0
    for c in cards:
        if not c.get("enabled", True):
            continue
        left = remaining_of(c)
        if left is None:
            continue
        checked += 1
        if left <= LOW_STOCK:
            low.append({"card_id": c.get("id"), "name": _label(c), "remaining": left})
    return {"ok": True, "checked": checked, "low": low, "error": ""}


def _load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        # 状态文件坏了就当没提醒过（宁可多提醒一次，也不能从此不提醒）
        return {}


def _save_state(st: Dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"保存库存预警状态失败（不影响提醒）: {e}")


def maybe_notify(user_id: Optional[int] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    扫 + 按卡券 24 小时去重后推送。永不抛异常（心跳里调用）。
    """
    now = now or datetime.now()
    res = scan(user_id)
    if not res.get("ok"):
        return {"ok": False, "notified": [], "skipped": [], "error": res.get("error")}

    st = _load_state()
    seen = st.get("cards") if isinstance(st.get("cards"), dict) else {}
    cutoff = now - timedelta(hours=ALERT_EVERY_HOURS)

    fresh: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for item in res["low"]:
        key = str(item["card_id"])
        last = str(seen.get(key) or "")
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S") if last else None
        except ValueError:
            last_dt = None
        if last_dt and last_dt > cutoff:
            skipped.append(item)
        else:
            fresh.append(item)

    if fresh:
        lines = [f"· {i['name']}：剩 {i['remaining']} 条" for i in fresh]
        text = "以下卡券库存不足（阈值 %d 条），卖完就无法自动发货了：\n%s" % (
            LOW_STOCK, "\n".join(lines))
        try:
            import notify_service
            notify_service.send_event("risk", "⚠️ 卡券库存不足", text)
        except Exception as e:
            logger.warning(f"库存不足通知发送失败: {e}")
        for i in fresh:
            seen[str(i["card_id"])] = now.strftime("%Y-%m-%d %H:%M:%S")
        st["cards"] = seen
        st["last_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        _save_state(st)
        logger.warning(f"卡券库存不足：{[i['name'] + '=' + str(i['remaining']) for i in fresh]}")

    return {"ok": True, "notified": fresh, "skipped": skipped, "checked": res["checked"],
            "error": ""}


def stats(user_id: Optional[int] = None) -> Dict[str, Any]:
    """给页面/接口用：当前所有批量卡券的剩余条数。"""
    res = scan(user_id)
    return {"ok": res.get("ok", False), "low": res.get("low", []),
            "checked": res.get("checked", 0),
            "threshold": LOW_STOCK, "error": res.get("error", "")}


__all__ = ["LOW_STOCK", "ALERT_EVERY_HOURS", "remaining_of", "scan", "maybe_notify", "stats"]