"""
交易后自动动作（C1）：自动评价 / 求小红花 / 收货致谢
=====================================================

**先纠正上一版的一个错误结论。**
上一版我写"评价和小红花都没有接口、做不了"——**那是错的**，用户反馈他在闲鱼真实
交易里遇到过自动评价，是对的。我漏查了闭源项目。核实来源：

- `najiuwanan/xianyu-Plus`（Java/Spring）`RateService.java`：
  - 待评价列表 `mtop.taobao.idle.merchant.rate.list` v1.0，
    `data={"pageNumber":1,"rowsPerPage":50,"queryType":"ORDER","rateSearchParam":{"sellerRateStatus":"5"}}`
  - 提交评价 `mtop.taobao.idle.rate.create` **v4.0**，
    `data={"tradeId":<订单号>,"rate":1,"feedback":<文案>,"createOrAppend":0}`
- 同一个仓库 `RedFlowerService.java`：
  - 求小红花 `mtop.taobao.idlemessage.red.flower` v1.0，
    `data={"orderId":<订单号>,"channel":"list"}`

三个接口都在 `h5api.m.goofish.com`，走我们已有的 md5 签名
（`sign = md5(token & t & appKey & data)`），和 `sold_out_relist._mtop` 完全同构。

---

**这一版为什么必须"先查列表、再提交"**

`rate.create` 的 `tradeId` 到底是哪个 ID（订单号 / 交易号 / 子单号），没有公开文档，
两个仓库里都只能看到"把列表里的 id 原样传回去"。所以本模块**不猜**：

1. 先调 `rate.list` 拿"平台认为待评价"的订单集合；
2. 目标订单**必须在这个集合里**，否则不提交——平台说不能评就是不能评；
3. 提交时用**列表里那一行提取出来的 id**，不是我们自己数据库里的 order_id。

这样即使两个 ID 体系不同也不会评错单。多花的是一次列表请求，换来的是不会把
A 订单的好评写到 B 订单上。

**错误语义（从上游抄来的真实报错，全部原样兼容）：**
| 平台返回 | 我们的处理 |
|---|---|
| `SUCCESS` | sent |
| 含「已评价/已经评价/评价过了/重复评价」 | **视为成功**（说明之前发成功过，幂等目标已达成） |
| 含「未完成的交易不允许评价」「交易未完成」 | waiting，不重试，等确认收货 |
| 含「超过30天的订单不允许评价」「不支持评价」「无评价资格」 | skipped（永久跳过） |
| 含 `timeout/超时` | **unknown**，不重试——超时不等于失败，可能已受理 |
| `FAIL_SYS_TOKEN_EXOIRED` / `SESSION_EXPIRED` | auth，交给调用方刷新 cookie 后重试一次 |
| `RGV587` / `USER_VALIDATE` / `你被挤爆啦` | risk，**立即停止本轮所有动作** |

**风控是这里最贵的失败。** 评价和小红花都是写操作，`rate.create` 还是 v4.0 的敏感接口。
所以三条硬约束：串行、每个动作之间有随机间隔（默认 40–120 秒）、
**任一动作触发风控就停掉本轮剩下的全部订单**（不是只停这一单）。

**和其他模块的关系**：走统一 `rate_limiter` 的 `write` scope（1 次/60 秒兜底）。
这里不额外抢 `delivery` 的配额——评价发生在确认收货之后，和发货不同时。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlite_runtime import connect_sqlite
from platform_guard import (
    PlatformGuardBlocked,
    admit as guard_admit,
    observe as guard_observe,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

DB_FILE = Path(os.environ.get("CARE_DB") or (DATA_DIR / "care_message.db"))
CONFIG_FILE = Path(os.environ.get("CARE_CONFIG") or (DATA_DIR / "care_config.json"))
# 按账号覆盖（评价/求花/致谢三个开关及其文案）。与全局配置分开存：
# 全局是"新账号的初值"，这里是"这个账号自己的选择"。
ACCOUNT_CONFIG_FILE = Path(os.environ.get("CARE_ACCOUNT_CONFIG")
                           or (DATA_DIR / "care_accounts.json"))

MTOP_HOST = "https://h5api.m.goofish.com"
TOKEN_COOKIE_NAME = "_m_h5_tk"

# 三个真实接口（版本号是硬要求：rate.create 必须 4.0，1.0 不存在）
#
# 注意：还有一条 `mtop.taobao.idle.merchant.rate.create` v1.0，是**卖家 PC 站**
# （origin: seller.goofish.com）的**批量**版，body 用 `tradeIdList: [...]`。
# 两个都是真的，不要因为看到另一个就以为这个错了。我们跑在 H5 站（goofish.com），
# 用单笔版；批量版要换 origin，混用会 403/风控。
RATE_LIST_API = "mtop.taobao.idle.merchant.rate.list"
RATE_LIST_VERSION = "1.0"
RATE_CREATE_API = "mtop.taobao.idle.rate.create"
RATE_CREATE_VERSION = "4.0"
RATE_CREATE_API_BATCH = "mtop.taobao.idle.merchant.rate.create"  # 未启用，仅记录
RATE_CREATE_VERSION_BATCH = "1.0"
RED_FLOWER_API = "mtop.taobao.idlemessage.red.flower"
RED_FLOWER_VERSION = "1.0"

# 超时不是失败：平台可能已受理。核验次数上限（参考 xianyu-Plus 的 2 次）
MAX_TIMEOUT_RECHECKS = 2

# `waiting`（交易未完成）**不是终态**。
#
# 闲管家 2025.11.03 的更新日志里有一条「修复求/收小红花偶尔不触发问题」——
# 这正是我们原来的形状：`claim()` 是 INSERT OR IGNORE，只要行存在就再也不发。
# 一单在买家确认收货前被扫到，平台回"未完成的交易"，我们落 waiting，然后这单
# **永远**不会再试。触发与否取决于"那一轮扫描恰好早于还是晚于确认收货"，
# 表现就是"偶尔不触发"，而且永远查不出来。
#
# 现在：waiting 行过了冷却期可以被重新 claim（attempts 累加，到上限转人工）。
WAITING_RETRY_HOURS = 6
WAITING_MAX_ATTEMPTS = 6

# 列表里可能出现的 id 字段名（不同页面版本不一样，按优先级取）
TRADE_ID_KEYS = ("bizOrderId", "tradeId", "orderId", "id")
TRADE_ID_NESTS = ("order", "orderInfo", "tradeInfo", "bizOrder", "data", "item")

DEFAULT_TEMPLATES: Dict[str, str] = {
    "thanks": "宝贝描述相符，发货快，沟通顺畅，下次有需要还来～",
    "thanks_digital": "东西好用，激活顺利，卖家讲得很清楚，值得推荐～",
    "thanks_fast": "发货很快，包装完好，和描述一致，好评～",
}

DEFAULT_FLOWER_TEXT = "东西收到啦，用着挺好，谢谢～"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,            # 总开关，默认关
    "dry_run": True,             # 默认演练：只查不写
    "auto_rate": False,          # 自动评价
    "rate_template": "thanks",
    "custom_feedback": "",       # 非空则优先于模板
    "ask_flower": False,         # 求小红花（注意：这是给买家的骚扰面，谨慎开）
    "flower_text": "",           # 空则用 DEFAULT_FLOWER_TEXT
    "thanks_message": False,     # 收货后发一条感谢消息（C1 原来那部分）
    "delay_seconds_min": 40,     # 每次写动作之间的随机间隔下限
    "delay_seconds_max": 120,
    "on_complete_delay_seconds": 90,  # 确认收货后等多久再动手（躲"秒评"特征）
    "max_per_run": 20,           # 单轮最多处理几单，防跑飞
    "daily_cap": 30,
    "stop_on_risk": True,        # 任一动作触发风控就停本轮
}


# ---------------------------------------------------------------- 存储

def _conn() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS care_actions (
            idem_key   TEXT PRIMARY KEY,      -- action:order:template
            action     TEXT NOT NULL,         -- rate | flower | message
            order_id   TEXT NOT NULL,
            cookie_id  TEXT NOT NULL,
            buyer_id   TEXT DEFAULT '',
            item_id    TEXT DEFAULT '',
            trade_id   TEXT DEFAULT '',       -- 列表里提取出来的真实 id
            status     TEXT NOT NULL,         -- claimed|sent|dry_run|skipped|waiting|failed|unknown
            note       TEXT DEFAULT '',
            attempts   INTEGER DEFAULT 0,     -- waiting 重试次数（到上限转人工）
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_care_a_day ON care_actions(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_care_a_order ON care_actions(order_id)")
    # 超时核验次数 / waiting 重试次数（老库补列，幂等）
    try:
        import migrate
        migrate.ensure_columns(conn, "care_actions", {"rechecks": "INTEGER DEFAULT 0",
                                                     "attempts": "INTEGER DEFAULT 0"},
                               commit=False)
    except Exception:
        pass
    conn.commit()
    return conn


def _read_global() -> Dict[str, Any]:
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


# 按账号覆盖：只允许覆盖"会作用到买家身上"的那几个动作开关与文案，
# 不覆盖限流/延时类（那属于账号自身的风控特征，改全局更安全，也避免一个账号
# 把间隔调到 0 秒把整个 IP 拖下水）。
PER_ACCOUNT_KEYS = (
    "auto_rate", "rate_template", "custom_feedback",
    "ask_flower", "flower_text",
    "thanks_message",
)

# 读不到账号配置时的安全默认：三个动作全关。
# 这些动作都直接作用到买家（评价不可撤销、求花与致谢都会发消息），拿不准就别做。
_INTERACTION_OFF = {"auto_rate": False, "ask_flower": False, "thanks_message": False}


def get_account_config(cookie_id: str) -> Dict[str, Any]:
    """
    取某个账号的完整配置 = 全局默认 + 该账号的覆盖。

    为什么必须按账号：这三个动作对不同账号的经营策略未必一致
    （比如小号只做评价不打扰买家，主号才求花）。全局开关意味着一开全开，
    而误开的代价由买家承担 —— 评价不可撤销，求花是实打实的消息骚扰。

    迁移语义（照 super-butler 的做法）：**账号没有覆盖记录时继承全局值**，
    所以升级不会让已经在跑的账号突然停下；账号被主动关闭时才写入显式 False。
    """
    cfg = _read_global()
    cid = str(cookie_id or "").strip()
    if not cid:
        return cfg
    over = _account_overrides().get(cid) or {}
    for k in PER_ACCOUNT_KEYS:
        if k in over and over[k] is not None:
            cfg[k] = over[k]
    return cfg


_ACCOUNT_CACHE: Dict[str, Any] = {"mtime": None, "data": {}}


def _account_overrides() -> Dict[str, Dict[str, Any]]:
    """
    读按账号覆盖表。缓存按文件 mtime 失效——轮询每轮要读好几次配置，
    每次都开文件在 30 个账号 × 每 10 分钟的场景下是没必要的 IO。
    """
    try:
        st = ACCOUNT_CONFIG_FILE.stat()
        mtime = st.st_mtime_ns
    except FileNotFoundError:
        _ACCOUNT_CACHE["mtime"] = None
        _ACCOUNT_CACHE["data"] = {}
        return {}
    except Exception:
        return _ACCOUNT_CACHE.get("data") or {}

    if _ACCOUNT_CACHE.get("mtime") == mtime:
        return _ACCOUNT_CACHE.get("data") or {}

    data: Dict[str, Dict[str, Any]] = {}
    try:
        raw = json.loads(ACCOUNT_CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for cid, over in raw.items():
                if isinstance(over, dict):
                    data[str(cid)] = {k: v for k, v in over.items() if k in PER_ACCOUNT_KEYS}
    except Exception:
        data = {}
    _ACCOUNT_CACHE["mtime"] = mtime
    _ACCOUNT_CACHE["data"] = data
    return data


def save_account_config(cookie_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    写某个账号的覆盖。**写显式值**——包括 False。
    写 False 与"没有记录"是不同的：前者表示"这个账号我明确关了"，
    后者表示"跟着全局走"。混在一起会让"关掉一个账号"在全局变动时被重新打开。
    """
    cid = str(cookie_id or "").strip()
    if not cid:
        raise ValueError("cookie_id 不能为空")
    all_over = dict(_account_overrides())
    cur = dict(all_over.get(cid) or {})
    for k, v in (patch or {}).items():
        if k in PER_ACCOUNT_KEYS and v is not None:
            cur[k] = v
    all_over[cid] = cur
    ACCOUNT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNT_CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(all_over, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ACCOUNT_CONFIG_FILE)   # 原子替换：避免轮询读到写了一半的文件
    _ACCOUNT_CACHE["mtime"] = None     # 强制下次重读
    return get_account_config(cid)


def get_config(cookie_id: str = "") -> Dict[str, Any]:
    """兼容旧调用：不带 cookie_id 时返回全局配置。"""
    if cookie_id:
        return get_account_config(cookie_id)
    return _read_global()


def save_config(patch: Dict[str, Any], cookie_id: str = "") -> Dict[str, Any]:
    """写全局配置；带 cookie_id 时写该账号的覆盖。"""
    cid = str(cookie_id or "").strip()
    if cid:
        return save_account_config(cid, patch)
    cfg = _read_global()
    for k, v in (patch or {}).items():
        if k in DEFAULT_CONFIG and v is not None:
            cfg[k] = v
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
    except Exception:
        pass
    return cfg


def make_key(action: str, order_id: str, variant: str = "") -> str:
    """一个动作对一个订单只做一次。variant 用于区分不同模板/文案。"""
    return f"{action}:{order_id}:{variant}" if variant else f"{action}:{order_id}"


def claim(action: str, order_id: str, cookie_id: str, variant: str = "",
          buyer_id: str = "", item_id: str = "", trade_id: str = "") -> Dict[str, Any]:
    key = make_key(action, order_id, variant)
    now_dt = datetime.now()
    now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO care_actions(idem_key,action,order_id,cookie_id,buyer_id,"
            "item_id,trade_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, action, str(order_id), cookie_id, str(buyer_id or ""),
             str(item_id or ""), str(trade_id or ""), "claimed", now, now))
        conn.commit()
        if cur.rowcount > 0:
            return {"ok": True, "key": key}

        row = conn.execute(
            "SELECT status,updated_at,COALESCE(attempts,0) AS attempts "
            "FROM care_actions WHERE idem_key=?", (key,)).fetchone()
        if not row:
            return {"ok": False, "key": key, "reason": "占位丢失"}

        st = row["status"]
        # waiting 是"等确认收货"，不是"做过"。过冷却期就再试一次，到上限转人工。
        if st == "waiting":
            att = int(row["attempts"] or 0)
            if att >= WAITING_MAX_ATTEMPTS:
                return {"ok": False, "key": key,
                        "reason": f"已等确认收货 {att} 次仍未成，转人工"}
            try:
                last = datetime.strptime(str(row["updated_at"] or ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                last = now_dt - timedelta(hours=WAITING_RETRY_HOURS + 1)
            if now_dt - last < timedelta(hours=WAITING_RETRY_HOURS):
                return {"ok": False, "key": key,
                        "reason": f"等确认收货（{WAITING_RETRY_HOURS} 小时后重试，已试 {att} 次）"}
            # 带 status 条件，两个进程同时来只有一个能抢到
            upd = conn.execute(
                "UPDATE care_actions SET status='claimed', attempts=COALESCE(attempts,0)+1, "
                "updated_at=? WHERE idem_key=? AND status='waiting'", (now, key))
            conn.commit()
            if upd.rowcount == 1:
                return {"ok": True, "key": key, "retry": "waiting"}
            return {"ok": False, "key": key, "reason": "刚被别的进程重新抢走"}

        return {"ok": False, "key": key, "reason": f"已处理过（{st}）"}
    finally:
        conn.close()


def finish(key: str, status: str, note: str = "") -> None:
    if status not in ("sent", "failed", "dry_run", "skipped", "waiting", "unknown"):
        raise ValueError(f"未知状态：{status}")
    conn = _conn()
    try:
        conn.execute("UPDATE care_actions SET status=?, note=?, updated_at=? WHERE idem_key=?",
                     (status, str(note)[:300],
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key))
        conn.commit()
    finally:
        conn.close()


def release(key: str) -> None:
    """删掉占位，允许重来。只用于"确定没写成功且可以安全重放"的场景。"""
    conn = _conn()
    try:
        conn.execute("DELETE FROM care_actions WHERE idem_key=?", (key,))
        conn.commit()
    finally:
        conn.close()


def day_count(action: str = "", day: Optional[str] = None) -> int:
    """今天真实写成功（sent）的条数。演练/跳过/等待都不占配额。"""
    d = day or datetime.now().strftime("%Y-%m-%d")
    sql = "SELECT COUNT(*) n FROM care_actions WHERE status='sent' AND created_at LIKE ?"
    args: List[Any] = [d + "%"]
    if action:
        sql += " AND action=?"
        args.append(action)
    conn = _conn()
    try:
        row = conn.execute(sql, args).fetchone()
        return int(row["n"] if row else 0)
    finally:
        conn.close()


def recent(limit: int = 40) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM care_actions ORDER BY created_at DESC LIMIT ?",
            (int(limit),)).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------- mtop 底层

def _extract_token(cookie_str: str) -> str:
    """从 Cookie 串里取签名用的 token：`_m_h5_tk` 里第一个 `_` 之前的部分。"""
    for part in str(cookie_str or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        if name.strip() == TOKEN_COOKIE_NAME:
            value = value.strip()
            return value.split("_")[0] if "_" in value else value
    return ""


def _app_key() -> str:
    try:
        from config import config
        return str(config.get("APP_CONFIG.app_key", ""))
    except Exception:
        return ""


def _mtop(cookie_str: str, api: str, data: Any, version: str, spm: str = "",
          app_key: str = "", timeout: int = 20,
          cookie_id: str = "") -> Tuple[Dict[str, Any], Any]:
    """和 sold_out_relist._mtop 同构。返回 (json, response)。

    `cookie_id` 非空时，会把响应头下发的续期 Cookie 回写（见 `_merge_and_persist`）。
    """
    import requests

    token = _extract_token(cookie_str)
    if not token:
        raise RuntimeError(f"Cookie 中缺少 {TOKEN_COOKIE_NAME}")
    key = app_key or _app_key()
    if not key:
        raise RuntimeError("APP_CONFIG.app_key 未配置")

    t = str(int(time.time() * 1000))
    data_val = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    sign = hashlib.md5(f"{token}&{t}&{key}&{data_val}".encode()).hexdigest()

    params = {
        "jsv": "2.7.2", "appKey": key, "t": t, "sign": sign,
        "v": version, "type": "originaljson", "accountSite": "xianyu",
        "dataType": "json", "timeout": "20000", "api": api,
        "sessionOption": "AutoLoginOnly",
    }
    if spm:
        params["spm_cnt"] = spm

    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.goofish.com",
        "referer": "https://www.goofish.com/",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "cookie": str(cookie_str or "").strip(),
    }
    try:
        guard_admit(cookie_str, scope="care_message", write=False, sleep=True)
    except PlatformGuardBlocked as exc:
        raise RuntimeError(str(exc))
    resp = requests.post(f"{MTOP_HOST}/h5/{api}/{version}/", params=params,
                         data={"data": data_val}, headers=headers, timeout=timeout)
    guard_observe(
        cookie_str,
        text=str(getattr(resp, "text", "") or ""),
        status_code=int(getattr(resp, "status_code", 0) or 0),
    )
    if cookie_id:
        _merge_and_persist(cookie_id, cookie_str, resp)

    # 统一解析：把"网关 502 返 HTML"和"接口业务失败"分开。
    # 不分开的后果是临时抖动被当成接口不可用，重试策略跟着错。
    try:
        from mtop_response import parse_mtop_response
        kind, raw, text = parse_mtop_response(resp)
    except Exception:
        kind, raw, text = "", {}, ""
    if kind == "transport":
        raise RuntimeError(text[:160])
    if kind in ("auth", "risk"):
        # 用 ret 文本走既有的 classify_ret（它已覆盖这两种）
        return {"ret": [text]}, resp
    if kind == "ok":
        return raw, resp

    try:
        return resp.json(), resp
    except Exception as e:
        raise RuntimeError(f"响应不是 JSON：{str(e)[:80]}")


def _merge_and_persist(cookie_id: str, cookie_str: str, resp: Any) -> str:
    """
    接住服务端在响应头下发的续期 Cookie 并回写。

    为什么必须做：MTOP 的签名 token 就是 Cookie 里的 `_m_h5_tk`，
    平台在响应里下发新值时如果不写回，我们手里的 token 就一直是旧的 ——
    表现为"偶尔一整天都正常，某天开始全部请求签名失败"，很难归因。

    复用 `polish_service` 里已有的合并/回写实现，不重复造。
    """
    try:
        # 没有账号 ID 就不回写：回写要有明确归属，否则会写坏别的账号的 Cookie。
        if not str(cookie_id or "").strip():
            return cookie_str
        if not getattr(resp, "headers", None) or not resp.headers.get("set-cookie"):
            return cookie_str
        import polish_service
        new_cookie = polish_service._merge_response_cookies(resp, cookie_str)
        if new_cookie and new_cookie != cookie_str:
            polish_service._persist_cookie(cookie_id, new_cookie)
            return new_cookie
    except Exception:
        pass
    return cookie_str


def _ret_text(raw: Dict[str, Any]) -> str:
    ret = raw.get("ret") or []
    return " | ".join(str(x) for x in ret) if isinstance(ret, list) else str(ret)


def classify_ret(text: str) -> str:
    """把平台返回归类。顺序很重要：风控和鉴权必须先判，别被 SUCCESS 以外的杂字干扰。"""
    t = str(text or "")
    up = t.upper()
    for k in ("RGV587", "USER_VALIDATE", "PUNISH", "哎哟喂", "被挤爆", "风控"):
        if k in up or k in t:
            return "risk"
    for k in ("TOKEN_EXOIRED", "TOKEN_EXPIRED", "SESSION_EXPIRED", "ILLEGAL_ACCESS", "未登录"):
        if k in up or k in t:
            return "auth"
    if "SUCCESS" in up:
        return "ok"
    # 已评价：说明之前发成功过，幂等目标已达成
    for k in ("已评价", "已经评价", "评价过了", "重复评价", "already rate"):
        if k in t or k in t.lower():
            return "already"
    # 未完成：等确认收货，不重试
    for k in ("未完成的交易", "交易未完成", "未完成交易"):
        if k in t:
            return "waiting"
    # 永久不可评
    for k in ("超过30天", "超出30天", "不支持评价", "无评价资格"):
        if k in t:
            return "skipped"
    if "timeout" in t.lower() or "超时" in t:
        return "unknown"
    return "error"


# ---------------------------------------------------------------- 待评价列表

def _as_map(v: Any) -> Optional[Dict[str, Any]]:
    if isinstance(v, dict):
        return {str(k): x for k, x in v.items()}
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            return json.loads(v)
        except Exception:
            return None
    return None


def _find_first(src: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    if not isinstance(src, dict):
        return None
    for k in keys:
        if k in src:
            return src[k]
    return None


def _unwrap_page(data: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
    """列表结构在不同页面版本里套的层不一样，往下找三层直到出现 items 类字段。"""
    base = ("items", "itemList", "orderList", "records", "list")
    if not isinstance(data, dict) or depth >= 3:
        return data if isinstance(data, dict) else {}
    if _find_first(data, base) is not None:
        return data
    for k in ("module", "result", "model", "content", "data"):
        nested = _as_map(data.get(k))
        if nested is not None:
            got = _unwrap_page(nested, depth + 1)
            if _find_first(got, base) is not None:
                return got
    return data


def extract_trade_id(item: Dict[str, Any], depth: int = 0) -> str:
    """从列表的一行里挖出平台认的订单 id。挖不到就返回空字符串，调用方不提交。"""
    if not isinstance(item, dict) or depth > 3:
        return ""
    for k in TRADE_ID_KEYS:
        v = item.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() != "null":
            return s
    for k in TRADE_ID_NESTS:
        got = extract_trade_id(_as_map(item.get(k)) or {}, depth + 1)
        if got:
            return got
    return ""


def buyer_rate_verdict(row: Optional[Dict[str, Any]]) -> str:
    """
    按平台待评价列表的买家评价字段判定能否回评。

    契约（用户提供的脱敏样本）：
    - buyerRateStatus: "4" 买家已评价；"5" 待买家评价
    - rateResult:      "1" 好评；"0" 中评；"-1" 差评

    返回 good / bad / pending / unknown。只有 good 允许回评。
    """
    if not isinstance(row, dict):
        return "unknown"
    buyer_status = str(row.get("buyerRateStatus") or "").strip()
    result = str(row.get("rateResult") or "").strip()
    if buyer_status == "5":
        return "pending"
    if buyer_status != "4":
        return "unknown"
    if result == "1":
        return "good"
    if result in ("0", "-1"):
        return "bad"
    return "unknown"


def fetch_pending(cookie_str: str, page: int = 1, rows: int = 50,
                  app_key: str = "", cookie_id: str = "") -> Dict[str, Any]:
    """读"待我评价"的订单。只读，不产生任何写动作。"""
    payload = {
        "pageNumber": int(page),
        "rowsPerPage": int(rows),
        "queryType": "ORDER",
        "rateSearchParam": {"sellerRateStatus": "5"},
    }
    try:
        raw, _ = _mtop(cookie_str, RATE_LIST_API, payload, RATE_LIST_VERSION,
                       app_key=app_key, cookie_id=cookie_id)
    except Exception as e:
        return {"ok": False, "kind": "network", "error": str(e)[:160], "items": []}

    text = _ret_text(raw)
    kind = classify_ret(text)
    if kind != "ok":
        return {"ok": False, "kind": kind, "error": text[:200], "items": []}

    data = _as_map(raw.get("data")) or {}
    container = _unwrap_page(data)
    raw_items = _find_first(container, ("items", "itemList", "orderList", "records", "list"))
    items: List[Dict[str, Any]] = []
    if isinstance(raw_items, list):
        items = [{str(k): v for k, v in it.items()} for it in raw_items if isinstance(it, dict)]
    total = _find_first(container, ("totalCount", "total", "count"))
    try:
        total_n = int(total) if total is not None else len(items)
    except Exception:
        total_n = len(items)
    return {"ok": True, "items": items, "total": total_n, "ret": text}


def find_in_pending(cookie_str: str, order_id: str, max_pages: int = 10,
                    app_key: str = "", cookie_id: str = "") -> Dict[str, Any]:
    """确认某订单是否在"待评价"集合里；在的话带回平台那一行的 id。"""
    scanned = 0
    for page in range(1, max_pages + 1):
        got = fetch_pending(cookie_str, page=page, app_key=app_key, cookie_id=cookie_id)
        if not got.get("ok"):
            return got
        items = got.get("items") or []
        for it in items:
            tid = extract_trade_id(it)
            # 两种可能：平台行的 id 就是订单号，或者行里另存了订单号
            if tid == str(order_id) or str(it.get("orderId") or "") == str(order_id) \
                    or str(it.get("bizOrderId") or "") == str(order_id):
                return {"ok": True, "found": True, "trade_id": tid, "row": it}
        scanned += len(items)
        if len(items) < 50 or scanned >= int(got.get("total") or 0):
            break
    return {"ok": True, "found": False, "trade_id": "", "row": None,
            "reason": "订单不在平台待评价列表里（可能还没确认收货，或已评过）"}


# ---------------------------------------------------------------- 三个动作

def submit_rate(cookie_str: str, trade_id: str, feedback: str,
                app_key: str = "", cookie_id: str = "") -> Dict[str, Any]:
    """提交好评。rate=1 是好评；createOrAppend=0 表示新建而不是追评。"""
    payload = {
        "tradeId": str(trade_id),
        "rate": 1,
        "feedback": str(feedback or "")[:500],
        "createOrAppend": 0,
    }
    try:
        raw, _ = _mtop(cookie_str, RATE_CREATE_API, payload, RATE_CREATE_VERSION,
                       app_key=app_key, cookie_id=cookie_id)
    except Exception as e:
        return {"ok": False, "kind": "network", "error": str(e)[:160]}
    text = _ret_text(raw)
    return {"ok": classify_ret(text) == "ok", "kind": classify_ret(text),
            "ret": text, "data": raw.get("data")}


def ask_flower(cookie_str: str, order_id: str, text: str = "",
               app_key: str = "", cookie_id: str = "") -> Dict[str, Any]:
    """求小红花。text 非空时带上（部分版本接受，不认就忽略）。"""
    payload: Dict[str, Any] = {"orderId": str(order_id), "channel": "list"}
    if text:
        payload["content"] = str(text)[:200]
    try:
        raw, _ = _mtop(cookie_str, RED_FLOWER_API, payload, RED_FLOWER_VERSION,
                       app_key=app_key, cookie_id=cookie_id)
    except Exception as e:
        return {"ok": False, "kind": "network", "error": str(e)[:160]}
    t = _ret_text(raw)
    return {"ok": classify_ret(t) == "ok", "kind": classify_ret(t), "ret": t}


# ---------------------------------------------------------------- 话术

def render_feedback(cfg: Optional[Dict[str, Any]] = None) -> str:
    c = cfg or get_config()
    custom = str(c.get("custom_feedback") or "").strip()
    if custom:
        return custom[:500]
    tpl = str(c.get("rate_template") or "thanks")
    return DEFAULT_TEMPLATES.get(tpl) or DEFAULT_TEMPLATES["thanks"]


def render_flower(cfg: Optional[Dict[str, Any]] = None) -> str:
    c = cfg or get_config()
    return str(c.get("flower_text") or "").strip() or DEFAULT_FLOWER_TEXT


# ---------------------------------------------------------------- 超时核验

def list_unknown(action: str = "rate", limit: int = 20) -> List[Dict[str, Any]]:
    """取出"结果未知"的单：这些是请求超时、平台可能已经受理的。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM care_actions WHERE status='unknown' AND action=?"
            " AND COALESCE(rechecks,0) < ? ORDER BY updated_at ASC LIMIT ?",
            (action, MAX_TIMEOUT_RECHECKS, int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _bump_recheck(key: str) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE care_actions SET rechecks=COALESCE(rechecks,0)+1,"
                     " updated_at=? WHERE idem_key=?",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key))
        conn.commit()
    finally:
        conn.close()


def recheck_unknown(cookie_str: str, app_key: str = "", cookie_id: str = "") -> Dict[str, Any]:
    """
    超时回读核验：**不重发，只读列表**。

    评价是不可撤销的写操作。超时后如果直接重发，最坏情况是同一单被评两次
    （平台侧会报"已评价"，但更糟的是可能触发风控）。正确做法是先看平台
    当前还认不认这单是"待评价"：

      - 已经不在待评价列表 → 说明上一次其实提交成功了 → 改判 sent
      - 还在列表里         → 上次没写进去 → 改判 failed（可安全重来）
      - 读列表本身失败      → 什么都不改，下次再核（最多 2 次）

    这就是 AutoJs6 的 `SmartClick.verify`：动作之后必须验证结果，
    不能靠"没报错"当成功。
    """
    out: Dict[str, Any] = {"ok": True, "checked": 0, "promoted": 0,
                           "reverted": 0, "kept": 0}
    rows_unknown = list_unknown("rate")
    if not rows_unknown:
        return out

    # 一次性把"平台当前认为待评价"的整张表读下来。**必须确认读到最后一页**，
    # 否则第 2 页上的订单会被误判成"已提交"。
    ids: set = set()
    complete = False
    scanned = 0
    for page in range(1, 11):
        got = fetch_pending(cookie_str, page=page, rows=50, app_key=app_key,
                            cookie_id=cookie_id)
        if not got.get("ok"):
            out["ok"] = False
            out["error"] = got.get("error")
            return out     # 列表读不到就整轮放弃，核验次数不涨
        items = got.get("items") or []
        for it in items:
            tid = extract_trade_id(it)
            if tid:
                ids.add(tid)
            for k in ("bizOrderId", "orderId", "tradeId", "id"):
                v = str(it.get(k) or "").strip()
                if v:
                    ids.add(v)
        scanned += len(items)
        total = int(got.get("total") or 0)
        if len(items) < 50 or (total and scanned >= total):
            complete = True
            break

    for row in rows_unknown:
        key = str(row.get("idem_key") or "")
        order_id = str(row.get("order_id") or "")
        if not key or not order_id:
            continue
        out["checked"] += 1

        hit = order_id in ids or (row.get("trade_id") and str(row["trade_id"]) in ids)
        if hit:
            # 还在待评价列表 = 上次确实没写进去，可以安全重来
            finish(key, "failed", "超时核验：仍在待评价列表，上次未受理")
            out["reverted"] += 1
        elif complete:
            # 整表确认过，确实消失了 = 上次其实提交成功
            finish(key, "sent", "超时核验：已不在待评价列表，判定为已提交成功")
            out["promoted"] += 1
        else:
            # 列表没读全，不能下结论。次数 +1，涨满 MAX_TIMEOUT_RECHECKS 就停
            _bump_recheck(key)
            out["kept"] += 1

    return out


# ---------------------------------------------------------------- 一轮处理

def run_once(cookie_id: str, cookie_str: str = "", order_ids: Optional[List[str]] = None,
             app_key: str = "", sender=None, sleep_fn=None) -> Dict[str, Any]:
    """
    跑一轮。order_ids 为空时自动拉"待评价"列表里的单。

    返回 {"ok", "actions": [...], "stopped": 原因}
    stopped 非空表示本轮被中断（风控/鉴权），调用方应该停下来。
    """
    # 按账号取配置：同一个进程要为多个账号跑，用全局配置就是"一开全开"。
    cfg = get_account_config(cookie_id)
    if not cfg.get("enabled"):
        return {"ok": False, "error": "未启用", "actions": [], "stopped": ""}

    if not str(cookie_str or "").strip():
        try:
            from db_manager import db_manager
            info = db_manager.get_cookie_details(cookie_id) or {}
            cookie_str = info.get("value") or ""
        except Exception as e:
            return {"ok": False, "error": f"读取 Cookie 失败：{e}", "actions": [], "stopped": ""}
    if not str(cookie_str or "").strip():
        return {"ok": False, "error": "该账号没有可用 Cookie", "actions": [], "stopped": ""}

    dry = bool(cfg.get("dry_run"))
    do_rate = bool(cfg.get("auto_rate"))
    do_flower = bool(cfg.get("ask_flower"))
    do_msg = bool(cfg.get("thanks_message"))
    if not (do_rate or do_flower or do_msg):
        return {"ok": True, "actions": [], "stopped": "",
                "note": "三个动作都没开"}

    do_sleep = sleep_fn or time.sleep
    max_per_run = max(1, int(cfg.get("max_per_run") or 20))
    dmin = max(0, int(cfg.get("delay_seconds_min") or 0))
    dmax = max(dmin, int(cfg.get("delay_seconds_max") or dmin))

    actions: List[Dict[str, Any]] = []
    stopped = ""

    # 0) 先把上次超时、结果未知的单回读核验掉。这一步只读不写，
    #    放最前面：核验完才知道哪些单真的还需要重做。
    recheck_info: Dict[str, Any] = {}
    if do_rate and not dry:
        try:
            recheck_info = recheck_unknown(cookie_str, app_key=app_key,
                                           cookie_id=cookie_id)
        except Exception as e:
            recheck_info = {"ok": False, "error": str(e)[:160]}

    # 目标订单集合
    targets: List[Dict[str, Any]] = []
    if order_ids:
        for oid in order_ids[:max_per_run]:
            targets.append({"order_id": str(oid)})
    else:
        got = fetch_pending(cookie_str, app_key=app_key, cookie_id=cookie_id)
        if not got.get("ok"):
            kind = got.get("kind")
            return {"ok": False, "error": got.get("error"), "actions": [],
                    "stopped": kind if kind in ("risk", "auth") else "",
                    "hint": "读待评价列表失败"}
        for row in (got.get("items") or [])[:max_per_run]:
            targets.append({"order_id": str(extract_trade_id(row) or ""), "row": row})

    for i, tgt in enumerate(targets):
        oid = str(tgt.get("order_id") or "")
        if not oid:
            actions.append({"order_id": "", "action": "-", "status": "skipped",
                            "note": "列表行里挖不出订单 id"})
            continue

        # 1) 评价
        if do_rate and day_count("rate") < int(cfg.get("daily_cap") or 30):
            actions.append(_one_rate(cfg, cookie_str, oid, tgt.get("row"),
                                     app_key=app_key, dry=dry, cookie_id=cookie_id))
            st = actions[-1].get("status")
            # 风控/鉴权必须中断整轮：继续写只会把账号推到更深的惩罚里，
            # 而且后面每一单都会以同样的方式失败（只是白白多打几次接口）。
            if st == "risk_stop":
                stopped = "risk"
                break
            if st == "auth":
                stopped = "auth"
                break
        # 2) 小红花
        if do_flower and day_count("flower") < int(cfg.get("daily_cap") or 30):
            actions.append(_one_flower(cfg, cookie_str, oid, app_key=app_key,
                                       dry=dry, cookie_id=cookie_id))
            st = actions[-1].get("status")
            if st == "risk_stop":
                stopped = "risk"
                break
            if st == "auth":
                stopped = "auth"
                break
        # 3) 感谢消息
        if do_msg and sender is not None:
            actions.append(_one_message(cfg, cookie_str, oid, sender=sender, dry=dry))

        # 串行 + 随机间隔（最后一个不用等）
        if i < len(targets) - 1 and dmax > 0:
            do_sleep(random.uniform(dmin, dmax))

    # 风控要记到**跨进程的**熔断状态里（拆解 3）。原来只是 break 出这一轮：
    # 十分钟后定时任务醒来，照样往同一个账号上写，等于每隔十分钟去撞一次墙。
    if stopped == "risk":
        secs = 0
        try:
            import rate_limiter as _rl
            secs = _rl.trip_circuit(cookie_id, reason="评价/小红花触发风控")
            logger.warning(f"【{cookie_id}】已熔断该账号的写操作 {_rl.CIRCUIT_MINUTES} 分钟")
        except Exception as _ce:
            logger.error(f"【{cookie_id}】记录熔断状态失败: {_ce}")
        # 熔断同时要让用户知道：灯还亮着但所有写操作都已停，不说一声就等于静默停工。
        try:
            from notify_service import notify_risk_trip
            notify_risk_trip(cookie_id, "评价/小红花", "平台返回风控或访问受限", secs)
        except Exception:
            pass

    return {"ok": True, "actions": actions, "stopped": stopped,
            "counts": _count_by_status(actions), "recheck": recheck_info}


def _count_by_status(actions: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for a in actions:
        s = str(a.get("status") or "?")
        out[s] = out.get(s, 0) + 1
    return out


def _one_rate(cfg: Dict[str, Any], cookie_str: str, order_id: str,
              row: Optional[Dict[str, Any]] = None,
              app_key: str = "", dry: bool = False,
              cookie_id: str = "") -> Dict[str, Any]:
    tpl = str(cfg.get("rate_template") or "thanks")
    key = make_key("rate", order_id, tpl)

    got = claim("rate", order_id, "", variant=tpl)
    if not got.get("ok"):
        return {"order_id": order_id, "action": "rate", "status": "duplicate",
                "note": got.get("reason")}

    # 必须确认这一单在平台待评价列表里，且用列表里的 id 提交
    trade_id = extract_trade_id(row) if row else ""
    if not trade_id:
        found = find_in_pending(cookie_str, order_id, app_key=app_key,
                                cookie_id=cookie_id)
        if not found.get("ok"):
            # 读列表失败 ≠ 这单不能评。风控/鉴权/网络都意味着"什么都没写"，
            # 必须撤销占位让下次能重来；只有平台明确说"不在待评价列表"才落 waiting。
            kind = found.get("kind")
            release(key)
            if kind == "risk":
                return {"order_id": order_id, "action": "rate", "status": "risk_stop",
                        "note": str(found.get("error"))[:200]}
            if kind == "auth":
                return {"order_id": order_id, "action": "rate", "status": "auth",
                        "note": str(found.get("error"))[:200]}
            return {"order_id": order_id, "action": "rate", "status": "failed",
                    "note": str(found.get("error"))[:200], "retryable": True}
        if not found.get("found"):
            finish(key, "waiting", str(found.get("reason"))[:200])
            return {"order_id": order_id, "action": "rate", "status": "waiting",
                    "note": found.get("reason")}
        trade_id = found.get("trade_id") or ""
        row = found.get("row") or row

    if not trade_id:
        finish(key, "skipped", "拿不到平台认可的 tradeId，不提交（防评错单）")
        return {"order_id": order_id, "action": "rate", "status": "skipped",
                "note": "拿不到 tradeId"}

    verdict = buyer_rate_verdict(row)
    if verdict == "pending":
        finish(key, "skipped", "买家尚未评价，禁止先回评")
        return {"order_id": order_id, "action": "rate", "status": "skipped",
                "note": "买家尚未评价（buyerRateStatus=5）"}
    if verdict == "bad":
        finish(key, "skipped", "买家评价为中评/差评，禁止互评")
        logger.warning("订单 {} 买家评价不是好评（rateResult={}），已禁止回评",
                       order_id, (row or {}).get("rateResult"))
        return {"order_id": order_id, "action": "rate", "status": "skipped",
                "note": "买家评价为中评/差评，禁止互评"}
    if verdict != "good":
        finish(key, "skipped", "买家评价状态不完整，禁止回评")
        return {"order_id": order_id, "action": "rate", "status": "skipped",
                "note": "买家评价状态未知，禁止互评"}

    feedback = render_feedback(cfg)
    if dry:
        finish(key, "dry_run", f"{trade_id} | {feedback}")
        return {"order_id": order_id, "action": "rate", "status": "dry_run",
                "trade_id": trade_id, "text": feedback}

    # 写操作配额（1 次/60 秒兜底）
    try:
        import rate_limiter
        # 用账号做 key，不是固定的 "care"：熔断是按账号记的，固定 key 会让
        # 一个正在罚站的账号照样把评价/小红花写出去，也会把各账号的日上限串成一本账。
        allowed, retry = rate_limiter.acquire("write", cookie_id or "care")
        if not allowed:
            release(key)          # 没写出去，允许重来
            return {"order_id": order_id, "action": "rate", "status": "rate_limited",
                    "note": f"建议 {retry}s 后重试"}
    except Exception:
        pass

    res = submit_rate(cookie_str, trade_id, feedback, app_key=app_key,
                      cookie_id=cookie_id)
    kind = res.get("kind")
    if kind == "ok" or kind == "already":
        finish(key, "sent", res.get("ret", ""))
        return {"order_id": order_id, "action": "rate", "status": "sent",
                "trade_id": trade_id, "text": feedback}
    if kind == "risk":
        release(key)              # 风控没写成功，允许以后重来
        return {"order_id": order_id, "action": "rate", "status": "risk_stop",
                "note": res.get("ret", "")}
    if kind == "waiting":
        finish(key, "waiting", res.get("ret", ""))
        return {"order_id": order_id, "action": "rate", "status": "waiting",
                "note": res.get("ret", "")}
    if kind == "skipped":
        finish(key, "skipped", res.get("ret", ""))
        return {"order_id": order_id, "action": "rate", "status": "skipped",
                "note": res.get("ret", "")}
    if kind == "unknown":
        # 超时不等于失败，可能已受理 —— 不重试
        finish(key, "unknown", res.get("ret", ""))
        return {"order_id": order_id, "action": "rate", "status": "unknown",
                "note": "请求超时，平台可能已受理；不自动重试"}
    if kind == "auth":
        release(key)
        return {"order_id": order_id, "action": "rate", "status": "auth",
                "note": "Cookie/token 过期，刷新后重试"}
    finish(key, "failed", str(res.get("ret") or res.get("error"))[:200])
    return {"order_id": order_id, "action": "rate", "status": "failed",
            "note": res.get("ret") or res.get("error")}


def _one_flower(cfg: Dict[str, Any], cookie_str: str, order_id: str,
                app_key: str = "", dry: bool = False,
                cookie_id: str = "") -> Dict[str, Any]:
    key = make_key("flower", order_id)
    got = claim("flower", order_id, "")
    if not got.get("ok"):
        return {"order_id": order_id, "action": "flower", "status": "duplicate",
                "note": got.get("reason")}

    text = render_flower(cfg)
    if dry:
        finish(key, "dry_run", text)
        return {"order_id": order_id, "action": "flower", "status": "dry_run", "text": text}

    try:
        import rate_limiter
        allowed, retry = rate_limiter.acquire("write", cookie_id or "care")
        if not allowed:
            release(key)
            return {"order_id": order_id, "action": "flower", "status": "rate_limited",
                    "note": f"建议 {retry}s 后重试"}
    except Exception:
        pass

    res = ask_flower(cookie_str, order_id, text, app_key=app_key,
                     cookie_id=cookie_id)
    kind = res.get("kind")
    if kind in ("ok", "already"):
        finish(key, "sent", res.get("ret", ""))
        return {"order_id": order_id, "action": "flower", "status": "sent", "text": text}
    if kind == "risk":
        release(key)
        return {"order_id": order_id, "action": "flower", "status": "risk_stop",
                "note": res.get("ret", "")}
    if kind == "auth":
        release(key)
        return {"order_id": order_id, "action": "flower", "status": "auth",
                "note": "Cookie/token 过期"}
    if kind == "waiting":
        # 和评价一样：交易还没完成 → waiting（冷却后能再来），
        # 不能落 failed —— 那意味着这单的小红花永远不会再试。
        finish(key, "waiting", res.get("ret", ""))
        return {"order_id": order_id, "action": "flower", "status": "waiting",
                "note": res.get("ret", "")}
    finish(key, "failed", str(res.get("ret") or res.get("error"))[:200])
    return {"order_id": order_id, "action": "flower", "status": "failed",
            "note": res.get("ret") or res.get("error")}


def _one_message(cfg: Dict[str, Any], cookie_str: str, order_id: str,
                 sender=None, dry: bool = False) -> Dict[str, Any]:
    key = make_key("message", order_id)
    got = claim("message", order_id, "")
    if not got.get("ok"):
        return {"order_id": order_id, "action": "message", "status": "duplicate",
                "note": got.get("reason")}
    text = "东西收到就好，有任何问题随时找我，用得顺手的话给个评价就行～"
    if dry:
        finish(key, "dry_run", text)
        return {"order_id": order_id, "action": "message", "status": "dry_run", "text": text}
    try:
        ok = bool(sender(order_id, text))
    except Exception as e:
        finish(key, "failed", f"{type(e).__name__}: {e}"[:200])
        return {"order_id": order_id, "action": "message", "status": "failed", "note": str(e)[:160]}
    if ok:
        finish(key, "sent", text)
        return {"order_id": order_id, "action": "message", "status": "sent", "text": text}
    finish(key, "failed", "sender 返回 False")
    return {"order_id": order_id, "action": "message", "status": "failed", "note": "sender False"}


# ---------------------------------------------------------------- 状态

def stats(days: int = 30) -> Dict[str, Any]:
    since = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT action,status,COUNT(*) n FROM care_actions WHERE created_at >= ? "
            "GROUP BY action,status", (since,)).fetchall()
        grid: Dict[str, Dict[str, int]] = {}
        for r in rows:
            grid.setdefault(r["action"], {})[r["status"]] = r["n"]
    finally:
        conn.close()
    return {
        "config": get_config(),
        "today": {"rate": day_count("rate"), "flower": day_count("flower"),
                  "message": day_count("message")},
        "by_action": grid,
        "recent": recent(40),
        "templates": DEFAULT_TEMPLATES,
        "flower_default": DEFAULT_FLOWER_TEXT,
        "apis": {
            "rate_list": f"{RATE_LIST_API} v{RATE_LIST_VERSION}",
            "rate_create": f"{RATE_CREATE_API} v{RATE_CREATE_VERSION}",
            "red_flower": f"{RED_FLOWER_API} v{RED_FLOWER_VERSION}",
        },
        "notes": [
            "自动评价是真的：mtop.taobao.idle.rate.create v4.0（好评 rate=1）。",
            "求小红花是真的：mtop.taobao.idlemessage.red.flower v1.0。",
            "评价前必须先读到平台「待评价列表」，用列表里的 id 提交，不猜 tradeId。",
            "评价和小红花都是写操作，串行 + 随机间隔 40–120 秒；触发风控立即停本轮。",
            "默认关闭 + 默认演练；演练只读列表，不写任何东西。",
            "超时不判死：每轮开跑前先回读列表核验，最多 2 次，不重发。",
        ],
    }


__all__ = [
    "run_once", "fetch_pending", "find_in_pending", "extract_trade_id",
    "submit_rate", "ask_flower", "classify_ret", "render_feedback", "render_flower",
    "claim", "finish", "release", "make_key", "get_config", "save_config",
    "stats", "recent", "day_count", "recheck_unknown", "list_unknown",
    "DEFAULT_TEMPLATES", "DEFAULT_CONFIG",
    "RATE_LIST_API", "RATE_CREATE_API", "RED_FLOWER_API",
]
