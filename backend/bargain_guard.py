"""
AI 议价护栏（B3）
================

**这个模块存在的唯一理由：让"底价"变成代码里的数，而不是提示词里的一句话。**

改造前的实现（`ai_reply_engine.generate_reply`）是这样议价的：把
「最大优惠百分比 10%、最大优惠金额 100 元、最多 3 轮」写进 prompt，然后
让模型自由发挥。这不叫护栏——模型说"给你便宜 200"照样发出去，代码层
一个校验都没有。提示词只能"建议"，不能"阻止"。

所以这里做四件提示词做不到的事：

1. **算底价**：`floor = list - min(list×pct, amount)`，就是"两个上限同时生效"
   的意思（10% 和 100 元取更严的那个）。若商品配了成本价，再夹一层。
2. **解析买家出价**：把「130」「130 行不行」「少 20」「打八折」「抹个零」都
   归一成一个数字，再算砍幅落在哪一档。
3. **给梯度而不是让模型猜**：按砍幅分四档，每档给一个**确定的**还价上限
   和一句话术方向。模型只负责把话术说得像人，价格由这里定。
4. **回读模型输出**：模型写完之后再扫一遍回复里的价格，只要低于底价就
   改写成"最低 X 元"，改不了就整条拦掉转人工。**这一层才是真正的保险丝**，
   因为模型可以不听话，但发出去的字必须先过这里。

**关于"15–40% 是常态"这件事（148 份文档的实测）**：
`102/103` 航海手册 6.4 节写得很直白——「一般是 160 多的价格，直接问你 130
或者 100 行不行」，也就是买家一开口就砍 19%–38%。所以：

- 15–40% 这一档**不是异常，是常态**，话术必须能接住，不能一句"不议价"把人赶走；
- 但我们自己的默认底价（10% / 100 元取严）比这个**紧得多**——160 元的货
  只能让 16 元，买家开口 130 会被直接拒。
- 这不一定错（薄利品类本来就该守），但如果**每一单都拒**，那是在用护栏
  把生意挡在门外。所以 `diagnose_floor()` 会把这个矛盾算给用户看：
  "你的底价对应 10% 让步，而市场常态是 19–38%，按现在的设置 8 成砍价会被拒"。
  **我不替你放宽，但必须让你知道自己在拒绝什么。**

**安全姿态**：默认关闭（`enabled=False`），第 31 天才建议开。开启后也只
在 AI 回复路径上生效，不主动降价、不主动私聊、不改商品价格。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

DB_FILE = Path(os.environ.get("BARGAIN_DB") or (DATA_DIR / "bargain_guard.db"))
CONFIG_FILE = Path(os.environ.get("BARGAIN_CONFIG") or (DATA_DIR / "bargain_config.json"))

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,          # 第 31 天才建议开
    "max_rounds": 3,           # 同一会话最多议价几轮
    "max_discount_percent": 10,
    "max_discount_amount": 100,
    "escalate_on_exceed": True,  # 超界转人工（不自动拒绝死）
    "floor_absolute": 0,       # 全局绝对底价（0=不启用），低于它一律不卖
}

# 砍幅分档（相对标价）。档位边界来自 148 份文档的实战口径：
#   ≤5%   象征性砍价，可以让，成交率最高
#   5–15% 正常砍价
#   15–40% 买家习惯性开口（文档原话：「160 多直接问你 130 或 100」），必须接住
#   >40%  屠龙刀，拒
TIERS: List[Dict[str, Any]] = [
    {"code": "tiny",  "lo": 0.00, "hi": 0.05, "label": "象征性砍价",
     "policy": "可以让，但别一次让到底", "max_concede": 0.05},
    {"code": "normal", "lo": 0.05, "hi": 0.15, "label": "正常砍价",
     "policy": "小幅让步，强调成色/配件/包邮", "max_concede": 0.10},
    {"code": "common", "lo": 0.15, "hi": 0.40, "label": "习惯性开口（最常见）",
     "policy": "先接住再小步让，别一句不议价把人赶走", "max_concede": 0.10},
    {"code": "hard",  "lo": 0.40, "hi": 99.0, "label": "屠龙刀",
     "policy": "不接，礼貌拒绝并给替代方案（包邮/多买优惠）", "max_concede": 0.0},
]

# 数字后面跟这些单位时**不是**价格，别误判（"3 天发货""6 张图""9 成新"）
NON_PRICE_UNITS = ("天", "日", "小时", "分钟", "秒", "张", "个", "件", "次", "年", "月",
                   "折", "成新", "成色", "分", "克", "斤", "公斤", "kg", "g", "ml", "寸", "米",
                   "cm", "mm", "w", "v", "mah", "gb", "tb", "核", "线程", "代", "版")

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


# ---------------------------------------------------------------- 存储

def _conn() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bargain_sessions (
            chat_id    TEXT NOT NULL,
            cookie_id  TEXT NOT NULL,
            item_id    TEXT NOT NULL,
            list_price REAL,
            best_offer REAL,
            last_reply_price REAL,
            rounds     INTEGER DEFAULT 0,
            escalated  INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, cookie_id, item_id)
        )""")
    # 每次判定都留痕，方便事后看"到底让了多少、拒了多少"
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bargain_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            chat_id TEXT, cookie_id TEXT, item_id TEXT,
            list_price REAL, offer REAL, cut_ratio REAL, tier TEXT,
            action TEXT, reply_price REAL, note TEXT
        )""")
    conn.commit()
    return conn


def get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k in DEFAULT_CONFIG:
                if k in raw:
                    cfg[k] = raw[k]
    except Exception:
        pass
    return cfg


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_config()
    for k, v in (patch or {}).items():
        if k in DEFAULT_CONFIG and v is not None:
            cfg[k] = v
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return cfg


def get_session(chat_id: str, cookie_id: str, item_id: str) -> Dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM bargain_sessions WHERE chat_id=? AND cookie_id=? AND item_id=?",
            (chat_id, cookie_id, str(item_id))).fetchone()
        if row:
            return dict(row)
        return {"chat_id": chat_id, "cookie_id": cookie_id, "item_id": str(item_id),
                "list_price": None, "best_offer": None, "last_reply_price": None,
                "rounds": 0, "escalated": 0}
    finally:
        conn.close()


def _upsert_session(s: Dict[str, Any]) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO bargain_sessions(chat_id,cookie_id,item_id,list_price,best_offer,"
            "last_reply_price,rounds,escalated,updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id,cookie_id,item_id) DO UPDATE SET "
            "list_price=excluded.list_price,best_offer=excluded.best_offer,"
            "last_reply_price=excluded.last_reply_price,rounds=excluded.rounds,"
            "escalated=excluded.escalated,updated_at=excluded.updated_at",
            (s["chat_id"], s["cookie_id"], s["item_id"], s.get("list_price"),
             s.get("best_offer"), s.get("last_reply_price"), int(s.get("rounds") or 0),
             1 if s.get("escalated") else 0,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def _log(**kw) -> None:
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO bargain_log(at,chat_id,cookie_id,item_id,list_price,offer,"
                "cut_ratio,tier,action,reply_price,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kw.get("chat_id", ""),
                 kw.get("cookie_id", ""), str(kw.get("item_id", "")), kw.get("list_price"),
                 kw.get("offer"), kw.get("cut_ratio"), kw.get("tier", ""),
                 kw.get("action", ""), kw.get("reply_price"), str(kw.get("note") or "")[:300]))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------- 解析

def _cn_to_int(text: str) -> Optional[int]:
    """把「一百三」「八十」「十五」这类简体中文数字转成整数。只处理口语常见形态。"""
    if not text:
        return None
    # 纯阿拉伯数字直接走
    if text.isdigit():
        return int(text)
    total = 0
    section = 0
    num = 0
    for ch in text:
        if ch in CN_NUM:
            n = CN_NUM[ch]
            if n == 10:
                num = (num or 1) * 10
            else:
                num = n
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        elif ch == "千":
            section += (num or 1) * 1000
            num = 0
        elif ch == "万":
            total += (section + num) * 10000
            section = 0
            num = 0
        else:
            return None
    return total + section + num if (total or section or num) else None


def _clean_idiom(text: str) -> str:
    """「一百三」=130 而不是 103 —— 口语里省略的是末位。"""
    m = re.fullmatch(r"([一二两三四五六七八九])百([一二三四五六七八九])", text)
    if m:
        return str(CN_NUM[m.group(1)] * 100 + CN_NUM[m.group(2)] * 10)
    return text


def parse_offer(message: str, list_price: Optional[float] = None) -> Dict[str, Any]:
    """
    从买家消息里抠出"他想要多少钱"。返回 offer 为 None 表示没提具体数字。

    支持：130 / 130块 / ¥130 / 130元 / 少20 / 便宜20 / 打8折 / 八折 / 一百三
    明确不做：多件总价、分期、以旧换新（这些该走人工）
    """
    if not message:
        return {"offer": None, "kind": None, "raw": ""}
    msg = str(message).strip()
    low = msg.lower()

    # 1) 折扣：「打八折」「8折」「8.5折」「85折」
    #    「85折」是中文口语写法，等于 8.5 折（85% of 标价），不是 85 折。
    #    正则要贪婪到 1–2 位整数，否则会被吃成「5折」，价格差一倍。
    m = re.search(r"([0-9]{1,2}(?:\.[0-9])?)\s*折", msg)
    if m and list_price:
        try:
            zhe = float(m.group(1))
            if 10 <= zhe < 100:      # 「85折」→ 8.5
                zhe = zhe / 10.0
            if 0 < zhe < 10:
                return {"offer": round(float(list_price) * zhe / 10.0, 2),
                        "kind": "discount", "raw": m.group(0)}
        except ValueError:
            pass
    m = re.search(r"([一二两三四五六七八九])折", msg)
    if m and list_price:
        n = CN_NUM.get(m.group(1))
        if n:
            return {"offer": round(float(list_price) * n / 10.0, 2),
                    "kind": "discount", "raw": m.group(0)}

    # 2) 减免：「少20」「便宜 20」「便宜一百」「能不能减 30」
    #    放在"中文数字"前面，因为「便宜一百」里的"一百"是**减免额**不是出价。
    m = re.search(r"(?:少|便宜|减|让|降)\s*([0-9]{1,5})\s*(?:元|块|块钱)?", msg)
    if m and list_price:
        try:
            d = float(m.group(1))
            if 0 < d < float(list_price):
                return {"offer": round(float(list_price) - d, 2),
                        "kind": "delta", "raw": m.group(0)}
        except ValueError:
            pass
    m = re.search(r"(?:少|便宜|减|让|降)\s*([一二两三四五六七八九十百千]{1,6})", msg)
    if m and list_price:
        d = _cn_to_int(_clean_idiom(m.group(1)))
        if d and 0 < d < float(list_price):
            return {"offer": round(float(list_price) - d, 2),
                    "kind": "delta", "raw": m.group(0)}

    # 3) 中文数字 + 货币单位：「一百三」「八十块」
    m = re.search(r"([一二两三四五六七八九十百千]{1,6})\s*(?:元|块钱|块)", msg)
    if m:
        v = _cn_to_int(_clean_idiom(m.group(1)))
        if v:
            return {"offer": float(v), "kind": "price", "raw": m.group(0)}

    # 3b) 裸中文数字：「一百三」不带单位（价格区间内才认，避免把「三成新」当价格）
    if list_price:
        lp0 = float(list_price)
        for m in re.finditer(r"([一二两三四五六七八九十百千]{2,6})", msg):
            tail = msg[m.end():m.end() + 2].strip()
            if any(tail.startswith(u) for u in NON_PRICE_UNITS):
                continue
            v = _cn_to_int(_clean_idiom(m.group(1)))
            if v and lp0 * 0.2 <= v <= lp0 * 2.0 and v != lp0:
                return {"offer": float(v), "kind": "bare", "raw": m.group(0)}

    # 3) 带货币符号/单位的绝对价
    for pat in (r"[¥￥]\s*([0-9]{1,6}(?:\.[0-9]{1,2})?)",
                r"([0-9]{1,6}(?:\.[0-9]{1,2})?)\s*(?:元|块钱|块|rmb|RMB)"):
        m = re.search(pat, msg)
        if m:
            try:
                v = float(m.group(1))
                if v > 0:
                    return {"offer": v, "kind": "price", "raw": m.group(0)}
            except ValueError:
                pass

    # 4) 中文数字 + 单位
    m = re.search(r"([一二两三四五六七八九十百千]{1,6})\s*(?:元|块钱|块)", msg)
    if m:
        v = _cn_to_int(_clean_idiom(m.group(1)))
        if v:
            return {"offer": float(v), "kind": "price", "raw": m.group(0)}

    # 5) 裸数字：必须是"像价格"的（在标价的 20%–200% 之间），且后面不跟非价格单位
    if list_price:
        lp = float(list_price)
        for m in re.finditer(r"(?<![\d.])([0-9]{2,6})(?:\.[0-9]{1,2})?(?![\d])", msg):
            tail = msg[m.end():m.end() + 3].strip().lower()
            if any(tail.startswith(u) for u in NON_PRICE_UNITS):
                continue
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if lp * 0.2 <= v <= lp * 2.0 and v != lp:
                return {"offer": v, "kind": "bare", "raw": m.group(0)}

    return {"offer": None, "kind": None, "raw": ""}


def cut_ratio(offer: Optional[float], list_price: Optional[float]) -> Optional[float]:
    if not offer or not list_price or float(list_price) <= 0:
        return None
    return max(0.0, min(1.0, (float(list_price) - float(offer)) / float(list_price)))


def tier_of(ratio: Optional[float]) -> Dict[str, Any]:
    if ratio is None:
        return {"code": "none", "label": "没提出价", "policy": "正常介绍商品，不主动提降价",
                "max_concede": 0.0}
    for t in TIERS:
        if t["lo"] <= ratio < t["hi"]:
            return t
    return TIERS[-1]


# ---------------------------------------------------------------- 底价

def compute_floor(list_price: float, cfg: Optional[Dict[str, Any]] = None,
                  item_floor: Optional[float] = None) -> Dict[str, Any]:
    """
    算这件商品的真实底价。

    两个上限**同时生效**（取更严的那个）：
        pct_cap    = list × (1 - max_discount_percent/100)
        amount_cap = list - max_discount_amount
    再与「绝对底价」和「商品成本价」取更高的那个 —— 宁可少赚也不亏本。
    """
    c = cfg or get_config()
    lp = float(list_price or 0)
    if lp <= 0:
        return {"ok": False, "error": "标价无效", "floor": None}

    pct = max(0.0, min(100.0, float(c.get("max_discount_percent") or 0)))
    amt = max(0.0, float(c.get("max_discount_amount") or 0))
    pct_cap = lp * (1 - pct / 100.0)
    amount_cap = lp - amt
    floor = max(pct_cap, amount_cap)     # 更严的那个
    binding = "百分比上限" if pct_cap >= amount_cap else "金额上限"

    absolute = float(c.get("floor_absolute") or 0)
    if absolute > 0 and absolute > floor:
        floor = absolute
        binding = "全局绝对底价"
    if item_floor and float(item_floor) > floor:
        floor = float(item_floor)
        binding = "商品成本价"

    return {"ok": True, "list_price": lp, "floor": round(floor, 2),
            "max_discount": round(lp - floor, 2),
            "max_discount_ratio": round((lp - floor) / lp, 4) if lp else 0,
            "binding": binding,
            "pct_cap": round(pct_cap, 2), "amount_cap": round(amount_cap, 2)}


def diagnose_floor(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    把"我们的底价"和"市场常态砍幅"摆在一起，让用户看清自己在拒绝什么。

    **这不是建议放宽**，是防止用户以为"设了 10% 就是正常议价"——
    文档里的常态是 19–38%，两者差一个量级。
    """
    c = cfg or get_config()
    pct = float(c.get("max_discount_percent") or 0)
    amt = float(c.get("max_discount_amount") or 0)
    # 用一个典型的 160 元商品（文档里的原例）做演示
    demo = 160.0
    pct_cap = demo * (1 - pct / 100.0)
    amount_cap = demo - amt
    floor = max(pct_cap, amount_cap)
    allowed_ratio = (demo - floor) / demo

    market_lo, market_hi = 0.19, 0.38      # 160 → 130 / 160 → 100
    tiers_hit = []
    for t in TIERS:
        if t["lo"] >= allowed_ratio:
            tiers_hit.append(t["label"])
    # 用档位宽度做粗估：常态档（15–40%）里，能成交的比例
    if allowed_ratio >= market_hi:
        verdict = "底价宽于市场常态，多数砍价能接住"
    elif allowed_ratio >= market_lo:
        verdict = "底价落在市场常态区间内，属于紧但合理"
    elif allowed_ratio >= 0.05:
        verdict = "底价明显紧于市场常态，习惯性开口会被大量拒绝"
    else:
        verdict = "底价几乎不让价，等于不做议价"

    return {
        "demo_price": demo,
        "allowed_ratio": round(allowed_ratio, 3),
        "allowed_amount": round(demo - floor, 2),
        "market_typical": f"{market_lo:.0%}–{market_hi:.0%}",
        "verdict": verdict,
        "note": ("148 份文档里买家的习惯性开口是砍 19%–38%（原例：160 多的货直接问 130 或 100）。"
                 "按当前设置，砍幅超过 "
                 f"{allowed_ratio:.0%} 就会被拒。"),
        "tight": allowed_ratio < market_lo,
    }


# ---------------------------------------------------------------- 决策

def decide(message: str, list_price: float, chat_id: str, cookie_id: str,
           item_id: str, cfg: Optional[Dict[str, Any]] = None,
           item_floor: Optional[float] = None) -> Dict[str, Any]:
    """
    给出这一轮的议价决策。**返回的是约束，不是最终话术**——话术由模型生成，
    价格由这里定。

    action:
      no_offer  买家没提价 → 正常介绍，不主动降价
      accept    出价 ≥ 底价，直接答应
      counter   出价 < 底价，还一个价（给到 floor 为止，不一次到底）
      reject    屠龙刀（砍幅 >40%）
      escalate  轮数用尽 / 已经让到底，转人工
    """
    c = cfg or get_config()
    lp = float(list_price or 0)
    sess = get_session(chat_id, cookie_id, item_id)
    if sess.get("list_price") is None and lp:
        sess["list_price"] = lp
    rounds = int(sess.get("rounds") or 0)

    f = compute_floor(lp, c, item_floor=item_floor)
    if not f["ok"]:
        return {"ok": False, "action": "escalate", "error": f.get("error")}

    parsed = parse_offer(message, lp)
    offer = parsed["offer"]
    ratio = cut_ratio(offer, lp)
    tier = tier_of(ratio)
    floor = f["floor"]
    max_rounds = int(c.get("max_rounds") or 3)

    action = "no_offer"
    target: Optional[float] = None
    note = ""
    escalate = False

    if offer is None:
        action = "no_offer"
        note = "买家没提具体数字，别主动降价"
        target = None
    elif rounds >= max_rounds:
        action = "escalate"
        escalate = True
        target = floor
        note = f"已议价 {rounds} 轮（上限 {max_rounds}），不再自动谈"
    elif offer >= floor:
        action = "accept"
        target = offer
        note = f"出价 {offer:g} ≥ 底价 {floor:g}，可以答应"
    elif tier["code"] == "hard":
        action = "reject"
        target = floor
        note = f"砍幅 {ratio:.0%} 属屠龙刀，不接"
    else:
        action = "counter"
        # 让步阶梯：从上一轮的报价往下走一档，而不是每次都从标价重算。
        # 每次重算会让"第 3 轮还价 144"这种明显不合理的结果出现（前面已经报过 144 了）。
        last = sess.get("last_reply_price")
        base = float(last) if last is not None else lp
        step = max(1.0, round((base - floor) / 2.0, 2))    # 每次走完剩下空间的一半
        target = max(floor, round(base - step, 2))
        target = max(floor, min(round(max(target, offer), 2), base))

        if last is not None and abs(float(last) - target) < 0.01:
            # 已经贴着底价了，再报同一个数没意义 → 把话说明白 + 转人工
            action = "escalate"
            escalate = True
            target = floor
            note = f"已到底价 {floor:g}，不再重复报价"
        elif abs(target - floor) < 0.01:
            note = (f"出价 {offer:g} < 底价 {floor:g}，还价 {target:g}"
                    f"（档位：{tier['label']}；这已是底价，下一轮不再让）")
        else:
            note = f"出价 {offer:g} < 底价 {floor:g}，还价 {target:g}（档位：{tier['label']}）"

    result = {
        "ok": True, "action": action, "tier": tier["code"], "tier_label": tier["label"],
        "policy": tier["policy"], "offer": offer, "offer_kind": parsed["kind"],
        "cut_ratio": round(ratio, 4) if ratio is not None else None,
        "list_price": lp, "floor": floor, "target_price": target,
        "max_discount": f["max_discount"], "binding": f["binding"],
        "rounds": rounds, "max_rounds": max_rounds,
        "escalate": escalate, "note": note,
    }

    # 记账：轮数只在"买家认真还价"时加，没提数字不算一轮
    if offer is not None:
        sess["rounds"] = rounds + 1
    if offer is not None and (sess.get("best_offer") is None or offer > float(sess["best_offer"])):
        sess["best_offer"] = offer
    if target:
        sess["last_reply_price"] = target
    sess["escalated"] = 1 if escalate else sess.get("escalated") or 0
    _upsert_session(sess)

    _log(chat_id=chat_id, cookie_id=cookie_id, item_id=item_id, list_price=lp,
         offer=offer, cut_ratio=result["cut_ratio"], tier=tier["code"],
         action=action, reply_price=target, note=note)
    return result


def prompt_block(d: Dict[str, Any]) -> str:
    """把决策变成塞进 prompt 的硬约束块。模型看到的是"只能报到这个价"，不是"尽量"。"""
    if not d.get("ok"):
        return ""
    tier_label = d.get("tier_label") or d.get("tier") or "未知"
    lines = [
        "【议价硬约束——以下数字由系统计算，不得违反】",
        f"- 标价：{float(d['list_price']):g} 元",
        f"- 绝对底价：{float(d['floor']):g} 元（低于此价一律不卖，不许说「可以商量」）",
        f"- 本轮最多报到：{float(d.get('target_price') or d['floor']):g} 元",
        f"- 买家砍幅：{(d.get('cut_ratio') or 0):.0%}（{tier_label}）",
        f"- 本轮策略：{d.get('policy') or ''}",
    ]
    if d["action"] == "accept":
        lines.append(f"- 买家出价已达标，直接答应 {d['offer']:g} 元，不要再往下压")
    elif d["action"] == "reject":
        lines.append("- 这个砍幅不接受。礼貌拒绝，可给「包邮」或「多买优惠」作为替代，"
                     "不要报任何低于底价的数字")
    elif d["action"] == "escalate":
        lines.append("- 议价轮数已用尽。告知这是最终价，再谈需要人工，不要继续让价")
    elif d["action"] == "counter":
        lines.append(f"- 只能还价到 {d['target_price']:g} 元，不许一次让到底，"
                     "也不许报比这更低的数")
    lines.append("- 禁止主动提出降价、包邮抵扣、赠品折现；只有买家问了才回应")
    return "\n".join(lines)


# ---------------------------------------------------------------- 回读校验

# 一句话里所有数字的候选。贪婪匹配 + 前后不接数字，保证「1600」不会被切成「160」。
_NUM = re.compile(r"(?<![0-9.])[0-9]{1,6}(?:\.[0-9]{1,2})?(?![0-9])")

# 数字左边出现这些词，说明它是**费用**（运费/定金/押金…），不是售价。
# 这一条是血的教训：旧版把「运费10元」当成"报价 10 元低于底价 176.4"，
# 然后改写成「运费176.4元」——比不拦还糟，买家会以为运费要 176。
# 用 search 而不是 $ 锚定，因为「运费我出12元」中间还夹着字。
_FEE_ANY = re.compile(
    r"(?:运费|邮费|快递费|首重|续重|定金|订金|押金|差价|手续费|服务费|"
    r"保价费|安装费|加急费|运费险|税费|顺丰|ems)", re.I
)

# 「减到160」「降到160」是**目标价**，要按报价算；先于减免词判断。
_TARGET_ANY = re.compile(r"(?:减到|降到|便宜到|让到|卖到|算到|做到)")

# 「便宜10元」「少10块」「给你减30」「加8元」里的数字是**让价/加价额**。
# 但「便宜点160」里的 160 是目标价 —— 两者的区别用**量级**判：
# 让价额通常远小于标价，落在标价 20%–200% 之外的当让价额，之内的当报价。
_DELTA_ANY = re.compile(r"(?<![不没别])(?:减|少|便宜|优惠|让|降|返|省|加|另加|多收)")

# 「最低120」「给你168」这类没有货币单位、但上下文明确是报价的说法。
_CTX_LEAD = re.compile(
    r"(?:最低|最多|最少|就|给你|算你|收你|要你|一共|总共|现在|改)\s*$"
)

_CURRENCY_LEAD = re.compile(r"[¥￥]\s*$")
_UNIT_TAIL = ("元", "块钱", "块", "rmb")

# 小句边界：同一个数字只受**同一小句**里的定语影响。
# 「运费另算，160给你」里的 160 不受前半句的"运费"污染。
_CLAUSE_BREAK = "，,。.！!？?；;、\n\r\t 　"


def _clause_tail(text: str, pos: int, width: int = 16) -> str:
    """取数字左边、同一小句内的字。"""
    seg = text[max(0, pos - width):pos]
    for i in range(len(seg) - 1, -1, -1):
        if seg[i] in _CLAUSE_BREAK:
            return seg[i + 1:]
    return seg


def _fmt_price(v: float) -> str:
    """176.4 而不是 176.40；176 而不是 176.0。"""
    s = f"{float(v):.2f}".rstrip("0").rstrip(".")
    return s or "0"


def looks_like_price_offer(message: str) -> bool:
    """
    这条消息是不是买家在报价（供 detect_intent 用，纯函数、无依赖）。

    为什么需要它：关键词表只认「便宜/多少钱/最低」这类词，买家最常见的回价
    其实**就是一个数字**——「167」「160吧」「165元」。这些消息原先一律落进
    default 意图，于是 B3 不启动、议价轮数也不计数，模型可以一路让到成本线。

    只认两种形态，避免误吞尺码（2XL）、体重（120-150斤）、算式（1+1=？）：
      - 数字紧跟货币单位，出现在任何位置          → 「165元」「160块包邮吗」
      - 整条以数字开头，后面只剩不超过 6 个汉字   → 「167」「160吧」「150 行不行」「160 拿走」
        但后面跟的是数量词/成色词就排除            → 「3天发货」「9成新」「120-150斤」
    """
    text = (message or "").strip()
    if not text:
        return False
    if re.search(r"[0-9]{1,6}(?:\.[0-9]{1,2})?\s*(?:元|块钱|块)", text):
        return True
    m = re.match(
        r"^\s*[¥￥]?\s*([0-9]{1,6}(?:\.[0-9]{1,2})?)\s*([一-龥]{0,6})\s*"
        r"[吧嘛呢啊哈呀~～!！?？.。,，、]*\s*$", text)
    if not m:
        return False
    tail = m.group(2)
    if any(tail.startswith(u) for u in NON_PRICE_UNITS):
        return False
    return True


def scan_reply_prices(text: str, list_price: Optional[float] = None) -> List[Dict[str, Any]]:
    """
    把回复里每个数字连同**上下文**判一遍，只留下真正会被买家当成售价的数字。

    返回项含 start/end（便于按位置改写，不用 str.replace 乱替换）和 kind。

    判定顺序（顺序本身就是规则，别调换）：
      费用词 → 跳过      运费10元 / 定金20元 / 补差价15元
      目标价 → 报价      减到160
      让价词 → 跳过      便宜10元 / 少10块 / 给你减30 / 加8元
      语境词 → 报价      最低160 / 给你168
      ¥/￥   → 报价
      元/块  → 报价
      裸数字 → 只有落在标价 20%–200% 才算报价
    """
    t = str(text or "")
    out: List[Dict[str, Any]] = []
    for m in _NUM.finditer(t):
        start, end = m.span()
        try:
            v = float(m.group(0))
        except ValueError:
            continue
        if v <= 0:
            continue

        lead = _clause_tail(t, start)
        right = t[end:end + 3]
        rlow = right.strip().lower()

        # 后面跟的不是钱 → 天数/成色/张数/重量/尺寸，直接排除
        if any(rlow.startswith(u) for u in NON_PRICE_UNITS):
            continue
        # 「120-150」「120~150」是区间（体重/年份/尺码），不是报价
        if re.match(r"\s*[-~至到]\s*[0-9]", right):
            continue

        lp = float(list_price or 0)
        in_band = bool(lp > 0 and lp * 0.2 <= v <= lp * 2.0)

        if _FEE_ANY.search(lead):
            continue                       # 运费/定金/押金… 永远不是售价
        if lead.rstrip().endswith("到") or _TARGET_ANY.search(lead):
            kind = "target"
        elif _DELTA_ANY.search(lead) and not in_band:
            continue                       # 便宜10元 / 少10块 —— 让价额
        elif _CTX_LEAD.search(lead):
            kind = "context"
        elif _CURRENCY_LEAD.search(lead):
            kind = "currency"
        elif any(rlow.startswith(u) for u in _UNIT_TAIL):
            kind = "unit"
        elif in_band:
            kind = "bare"
        else:
            continue

        out.append({"value": v, "start": start, "end": end,
                    "kind": kind, "raw": m.group(0)})
    return out


def extract_reply_prices(reply: str, list_price: Optional[float] = None) -> List[float]:
    """扫回复里所有"明确是价格"的数字，别把"3 天""6 张图""运费10元"当价格。"""
    return [h["value"] for h in scan_reply_prices(reply, list_price=list_price)]


# ---------------------------------------------------------------- 思维链泄漏

_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.S | re.I)
_THINK_OPEN = re.compile(r"<think\b[^>]*>.*\Z", re.S | re.I)

# 这些词只会出现在**我们塞给模型的约束块**里，正常客服话术不会说。
# 命中就整条丢弃 —— 把「议价硬约束 / 本轮最多报到 176.4」发给买家，
# 等于同时暴露了"这是机器人"和我们的底价。
# 注意：不要放「绝对底价」——「这已经是绝对底价了」是正常话术。
_LEAK_MARKERS = ("议价硬约束", "本轮最多报到", "最大议价轮数", "议价设置",
                 "安全边界", "只能还价到", "本轮策略", "禁止主动提出降价")


def scrub_model_output(text: str) -> Dict[str, Any]:
    """
    剥掉内联思维链，并拦下"把提示词原文说给买家"的回复。

    推理模型（R1/QwQ 之类）有时把 CoT 直接写进 content，或用 <think> 包起来。
    我们只在发送前清理，不改模型调用方式 —— 关掉推理能力会连带降低答复质量。
    """
    t = str(text or "")
    cleaned = _THINK_OPEN.sub(" ", _THINK_BLOCK.sub(" ", t))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip("\"'`")
    if not cleaned:
        return {"ok": False, "text": "", "leak": "空回复"}
    hit = next((w for w in _LEAK_MARKERS if w in cleaned), None)
    if hit:
        return {"ok": False, "text": "", "leak": hit}
    return {"ok": True, "text": cleaned, "leak": None}


def enforce(reply: str, d: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    最后一道保险丝：模型写完的回复必须过这里。

    - 出现低于底价的价格 → 改写为底价（保留句子结构，只换数字）
    - 改写不成就整条拦掉（ok=False），调用方应转人工或换默认话术
    - 出现"还能再便宜"「可以商量」这类**没报数但暗示可让**的话 → 也拦
    """
    if not d.get("ok"):
        return {"ok": False, "reply": "", "violations": ["决策无效"], "rewritten": False}
    c = cfg or get_config()
    text = str(reply or "").strip()
    if not text:
        return {"ok": False, "reply": "", "violations": ["空回复"], "rewritten": False}

    floor = float(d["floor"])
    violations: List[str] = []
    rewritten = False

    # 按**位置**改写，不用 str.replace：
    #   - replace 会把「160」误伤到「1600」里；
    #   - 同一句话出现两个价格时，replace 会把两个都换掉。
    # 从后往前改，前面的下标才不会失效。
    hits = scan_reply_prices(text, list_price=d.get("list_price"))
    bad = [h for h in hits if h["value"] < floor - 0.01]
    if bad:
        violations.append(
            "报价低于底价：" + "、".join(f"{_fmt_price(h['value'])}" for h in bad)
            + f" < {_fmt_price(floor)}")
        for h in sorted(bad, key=lambda x: -x["start"]):
            text = text[:h["start"]] + _fmt_price(floor) + text[h["end"]:]
        rewritten = True

    # 隐含可让价的话术（没报数但等于承诺）
    # 连后面的「点/儿/一点」一起换掉，否则会留下「已经是实价点」这种残句
    SOFT = ["还能再便宜", "可以再优惠", "价格好商量", "还能少", "可以商量",
            "再给你让", "最低可以到", "随便给"]
    for w in SOFT:
        if w in text:
            violations.append(f"含暗示可让价的话术：「{w}」")
            text = re.sub(re.escape(w) + r"(?:点儿|一点|点|儿|一下)?", "已经是实价了", text)
            rewritten = True

    # 重新扫一遍，还有低于底价的就整条拦
    still_bad = [h["value"] for h in
                 scan_reply_prices(text, list_price=d.get("list_price"))
                 if h["value"] < floor - 0.01]
    if still_bad:
        _log(chat_id=d.get("chat_id", ""), cookie_id=d.get("cookie_id", ""),
             item_id=d.get("item_id", ""), list_price=d.get("list_price"),
             offer=d.get("offer"), cut_ratio=d.get("cut_ratio"), tier=d.get("tier"),
             action="blocked", reply_price=None,
             note="改写后仍低于底价：" + str(still_bad))
        return {"ok": False, "reply": "", "violations": violations + [f"改写失败，仍含 {still_bad}"],
                "rewritten": rewritten}

    if violations:
        _log(chat_id=d.get("chat_id", ""), cookie_id=d.get("cookie_id", ""),
             item_id=d.get("item_id", ""), list_price=d.get("list_price"),
             offer=d.get("offer"), cut_ratio=d.get("cut_ratio"), tier=d.get("tier"),
             action="rewritten", reply_price=floor, note="；".join(violations))

    return {"ok": True, "reply": text, "violations": violations, "rewritten": rewritten,
            "floor": floor}


# ---------------------------------------------------------------- 状态

def stats(days: int = 30) -> Dict[str, Any]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT action, COUNT(*) n FROM bargain_log WHERE at >= date('now', ?) "
            "GROUP BY action", (f"-{int(days)} days",)).fetchall()
        by_action = {r["action"]: r["n"] for r in rows}
        sess = conn.execute("SELECT COUNT(*) n FROM bargain_sessions").fetchone()["n"]
        esc = conn.execute("SELECT COUNT(*) n FROM bargain_sessions WHERE escalated=1").fetchone()["n"]
        recent = [dict(r) for r in conn.execute(
            "SELECT * FROM bargain_log ORDER BY id DESC LIMIT 20").fetchall()]
    finally:
        conn.close()
    total = sum(by_action.values()) or 1
    return {
        "config": get_config(),
        "diagnosis": diagnose_floor(),
        "sessions": int(sess), "escalated": int(esc),
        "by_action": by_action,
        "accept_rate": round(by_action.get("accept", 0) / total, 3),
        "reject_rate": round(by_action.get("reject", 0) / total, 3),
        "recent": recent,
    }


__all__ = ["parse_offer", "cut_ratio", "tier_of", "compute_floor", "diagnose_floor",
           "decide", "prompt_block", "enforce", "extract_reply_prices",
           "scan_reply_prices", "looks_like_price_offer", "scrub_model_output",
           "get_config", "save_config", "get_session", "stats",
           "TIERS", "DEFAULT_CONFIG"]
