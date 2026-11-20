"""
C2 长期零出单的定时重新发布（默认关）
=====================================

**先说这个模块为什么不叫"定时下架重上"。**

148 份实操文档里对「下架再上架」的结论是明确否定的（`030` 误区篇原文：下架再上架
并不能得到更多流量，能得到的只有"卖出后立即重发"）。平台侧的另一个事实是：
同一件商品存在两条相似链接 = 重复铺货 = 降权。所以"早中晚各下一次"那种操作，
既没有流量收益，又踩重复铺货，是纯负收益。

这个模块做的是**兜底淘汰**，不是刷流量：
只处理「上架 ≥7 天 且 浏览 <50 且 从未出单」的僵化链接——B2 的杀品线判的就是
这类商品（`THRESHOLDS["cull_browse"]`）。它已经七天没带来任何成交，占着位置
不如换一条新的去试试新的首图和标题。

**旧链接怎么处理：自动重建 + 人工点下架（半自动）。**

这里绕不开一个死结：重新发布必须"先下旧链接再新建"，否则两条相似链接算重复铺货；
但我们**没有实证过的下架接口**（`mtop.taobao.idle.item.edit` /
`item.status.update` 的参数名平台从未公开，猜错的后果可能是静默改坏商品）。
A6 售罄重发之所以敢全自动，是因为商品已经售罄、平台已自动下架，不需要我们动手。

所以这里的边界是：
- **重建**（不碰任何未实证接口）：走 A6 已验证的链路 —— 模板 + 素材 →
  传图 CDN → 类目推荐 → 默认地址 → `mtop.idle.pc.idleitem.publish`；
- **下架**（不自动化）：输出「待下架」清单，每条给一个直达商品页的链接，
  人工点进去下架（一条 5 秒）。零风险，且用户始终知道自己在删什么。
- 若将来开放平台开通（铂金版约 198/年），`product.downShelf` 可以补上最后一步。

**为什么必须"先重建、后下架"而不是反过来。**
反过来的话，一旦重建失败（模板变量缺、图片没了、触达日配额），用户手里就
既没有老链接也没有新链接——商品直接消失。现在的顺序最坏情况是"两条链接都在"，
那是可恢复的（把新的删掉即可）。

三层保护（与 A6 同构，单独一份配置）：
1. 默认关闭 + 默认演练；演练会把"本轮会重建哪些、为什么"逐条列出来。
2. 冷却：同一件商品 reworked 后至少 `cooldown_days` 天不再动。
3. 连续失败 `max_attempts` 次自动停用该条并推通知，防越修越坏。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# 测试用环境变量覆盖，绝不动真实 data/
CONFIG_FILE = Path(os.environ.get("STALE_CONFIG") or (DATA_DIR / "stale_relist_config.json"))
STATE_FILE = Path(os.environ.get("STALE_STATE") or (DATA_DIR / "stale_relist_state.json"))

DEFAULTS: Dict[str, Any] = {
    "enabled": False,            # 默认关：先跑 dry-run 看清单
    "dry_run": True,             # 默认演练
    "interval_hours": 12,        # 巡检间隔（这个动作天生慢，没必要勤扫）
    "night_ban": True,           # 23:00-08:00 不动
    "min_days": 7,               # 上架至少几天才判（对齐 C2 口径）
    "max_browse": 50,            # 浏览低于多少算没流量（B2 杀品线同一个数）
    "require_zero_sold": True,   # 必须"从未出单"
    "max_per_round": 3,          # 单轮最多重建几条（≤10 是平台级上限，这里更保守）
    "cooldown_days": 7,          # 同一件商品翻新后的冷却
    "max_attempts": 3,           # 连续失败几次自动停用该条
    "keep_old_link_days": 3,     # 旧链接清单保留几天（提醒用户去下架）
    "rotate_title": True,        # 重建时换一个标题变体，避免同质化
}

# 单轮硬上限。即便配置被改成 999，也不许一轮发超过这个数——发布是账号级风控的
# 最敏感动作，配置写错不该变成"一口气发 50 条"。
HARD_MAX_PER_ROUND = 10

_lock = threading.Lock()
_tick_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_last_round: Dict[str, Any] = {}


# ---------------------------------------------------------------- 存取

def _load(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path: Path, data) -> None:
    """原子写：`.tmp` + replace。轮询线程可能正好读到半个文件，
    半截 JSON 会被 `_load` 当成"没配置"→ 静默退回默认值（默认关），
    这会表现成"定时器莫名其妙不跑了"。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception:
        pass


def _clamp(v: Any, lo: int, hi: int, default: int) -> int:
    """夹到 [lo, hi]。0 是合法输入（夹成 lo），不能用 `v or default`。"""
    try:
        n = int(v)
    except Exception:
        return default
    return max(lo, min(hi, n))


def get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    raw = _load(CONFIG_FILE, {})
    if isinstance(raw, dict):
        for k in DEFAULTS:
            if k in raw:
                cfg[k] = raw[k]
    for k in ("enabled", "dry_run", "night_ban", "require_zero_sold", "rotate_title"):
        cfg[k] = bool(cfg[k])
    cfg["interval_hours"] = _clamp(cfg["interval_hours"], 1, 168, 12)
    cfg["min_days"] = _clamp(cfg["min_days"], 1, 90, 7)
    cfg["max_browse"] = _clamp(cfg["max_browse"], 0, 100000, 50)
    cfg["max_per_round"] = _clamp(cfg["max_per_round"], 1, HARD_MAX_PER_ROUND, 3)
    cfg["cooldown_days"] = _clamp(cfg["cooldown_days"], 1, 90, 7)
    cfg["max_attempts"] = _clamp(cfg["max_attempts"], 1, 10, 3)
    cfg["keep_old_link_days"] = _clamp(cfg["keep_old_link_days"], 1, 30, 3)
    return cfg


def save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_config()
    for k in DEFAULTS:
        if k in (data or {}) and data[k] is not None:
            cfg[k] = data[k]
    _save(CONFIG_FILE, cfg)
    return get_config()


def get_state() -> Dict[str, Any]:
    st = _load(STATE_FILE, {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("reworked", {})    # cookie_id::item_id -> {...}
    st.setdefault("pending_down", [])  # 待下架的旧链接
    st.setdefault("bindings", {})    # cookie_id::item_id -> 模板/素材绑定
    st.setdefault("logs", [])
    return st


def bind(cookie_id: str, item_id: str, template_id: int, material_id: int = 0,
         price: Any = None, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    给某件商品绑定"重建用的模板 + 素材"。

    为什么不强制复用 A6 的白名单：A6 是"售罄后循环上架"，用户可能只想让它管少数
    几个爆款；C2 是"僵尸链接翻新"，覆盖面完全不同。两件事共用一个白名单，会让
    用户为了用 C2 而不得不把商品加进 A6 的自动上架——那是两个不同的风险敞口。
    所以这里有自己的绑定，A6 的绑定只作为**兜底复用**（见 `_find_watch_for`）。
    """
    cid, iid = str(cookie_id or "").strip(), str(item_id or "").strip()
    if not cid or not iid:
        return {"ok": False, "error": "需要 cookie_id 与 item_id"}
    tpl_id = int(template_id or 0)
    if tpl_id <= 0:
        return {"ok": False, "error": "必须绑定一个商品模板（自动重建靠模板）"}
    try:
        import product_template as pt
        if not pt.get_template(tpl_id).get("ok"):
            return {"ok": False, "error": f"模板 #{tpl_id} 不存在"}
    except Exception as e:
        return {"ok": False, "error": f"模板模块不可用：{str(e)[:100]}"}
    st = get_state()
    st["bindings"][f"{cid}::{iid}"] = {
        "cookie_id": cid, "item_id": iid, "template_id": tpl_id,
        "material_id": int(material_id or 0),
        "price": price, "variables": variables or {},
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _log(st, f"绑定模板 #{tpl_id}：{iid}")
    _save_state(st)
    return {"ok": True}


def _save_state(st: Dict[str, Any]) -> None:
    st["logs"] = (st.get("logs") or [])[-200:]
    _save(STATE_FILE, st)


def _log(st: Dict[str, Any], text: str, level: str = "info") -> None:
    st.setdefault("logs", []).append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": level, "text": text,
    })


def item_url(item_id: str) -> str:
    """闲鱼商品页直达链接。用户点进去就能自己下架。"""
    return f"https://www.goofish.com/item?id={str(item_id or '').strip()}"


# ---------------------------------------------------------------- 判据

def age_days(first_seen: str, gmt_create: Any = None,
             now: Optional[datetime] = None) -> Optional[float]:
    """
    这条链接有多老（天）。

    **优先用平台给的上架时间，拿不到才用我们自己的观测起点。**
    这个区别很重要：`first_seen` 是我们第一次扫到它的时间，不是平台发布时间。
    对一个昨天才开始用本工具的老店，所有商品的 first_seen 都是昨天 —— 若只用
    first_seen，`min_days=7` 会把全店判成"太新"而空跑七天；反过来，如果用户
    中途清了库，也可能把老链接判成新链接（这次是偏保守的方向，可以接受）。
    所以两个值都算，**取更老的**：宁可承认它老（从而可能触发翻新），
    也不要因为我们的观测起点不够长而永远判不出来。

    gmt_create 的格式没有实证（可能是毫秒时间戳、秒时间戳或日期串），
    所以这里全部做"能解析就用、解析不了就跳过"，绝不抛异常。
    """
    now = now or datetime.now()
    candidates: List[datetime] = []

    if gmt_create not in (None, "", 0, "0"):
        dt = _parse_time(gmt_create)
        if dt:
            candidates.append(dt)

    if first_seen:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                candidates.append(datetime.strptime(str(first_seen)[:19], fmt))
                break
            except ValueError:
                continue

    if not candidates:
        return None
    oldest = min(candidates)
    # 未来时间（时区/脏数据）算 0 天，不算负数——负数会让冷却判断全线失效
    return max(0.0, round((now - oldest).total_seconds() / 86400, 2))


def _parse_time(v: Any) -> Optional[datetime]:
    """容忍毫秒/秒时间戳与常见日期串。解析不出来返回 None。"""
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            n = float(v)
            if n > 1e11:      # 毫秒
                n /= 1000.0
            if n > 1e9:       # 秒
                return datetime.fromtimestamp(n)
            return None
    except Exception:
        return None
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    return None


def judge(item: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None,
          now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    判断一条商品该不该翻新。返回 {"rework": bool, "reason": str, ...明细}。

    判据严格按 C2 定稿：上架 ≥min_days 天 **且** 浏览 <max_browse **且** 从未出单。
    三个条件是**与**关系，缺一不可。宁可漏杀，不可误杀——翻新会把这条链接的
    想要数/浏览清零，误杀一条正在起量的链接比漏掉十条僵尸链接更亏。

    顺序也有讲究：先判"数据够不够判"，再判"该不该淘汰"。否则没拿到浏览数的
    商品（detail 预算用完/接口失败）会被当成 browse=0 直接判死。
    """
    cfg = cfg or get_config()
    it = item or {}
    days = age_days(it.get("first_seen") or "", it.get("gmt_create"), now=now)
    browse = it.get("browse")
    sold = it.get("sold")

    out: Dict[str, Any] = {
        "item_id": str(it.get("item_id") or ""),
        "title": str(it.get("title") or ""),
        "days": days, "browse": browse, "want": it.get("want"), "sold": sold,
        "rework": False, "reason": "", "blockers": [],
    }

    # ---- 1. 数据是否够判
    if browse is None:
        out["reason"] = "没有浏览数（详情没取到），本轮不判"
        out["blockers"].append("no_browse")
        return out
    if days is None:
        out["reason"] = "不知道上架多久了，本轮不判"
        out["blockers"].append("no_age")
        return out
    if it.get("stale"):
        # B2 在详情失败时用上一轮快照兜底并标 stale。拿旧数据判"该淘汰"是可以的
        # （浏览只会涨不会跌），但要记下来，免得用户以为这是今天的数。
        out["stale"] = True

    # ---- 2. 三个必要条件
    if days < cfg["min_days"]:
        out["reason"] = f"上架 {days} 天，不足 {cfg['min_days']} 天（新品有初始流量池）"
        out["blockers"].append("too_new")
        return out
    if browse >= cfg["max_browse"]:
        out["reason"] = f"浏览 {browse}，达到 {cfg['max_browse']}，还有流量，不动"
        out["blockers"].append("has_traffic")
        return out
    if cfg["require_zero_sold"]:
        # sold 拿不到时按"没出过单"处理？不 —— 那是猜测。这里保守：不判。
        if sold is None:
            out["reason"] = "拿不到成交数，无法确认『从未出单』，本轮不判"
            out["blockers"].append("no_sold")
            return out
        if int(sold) > 0:
            out["reason"] = f"已出单 {sold} 件，不是僵尸链接（出过单的链接数据最好）"
            out["blockers"].append("has_sales")
            return out

    # ---- 3. 人工停用（连续失败到上限后置位）
    if it.get("disabled"):
        out["reason"] = "这条之前连续重建失败，已自动停用（请人工检查后重新启用）"
        out["blockers"].append("disabled")
        return out

    # ---- 4. 冷却
    last = it.get("last_reworked_at") or ""
    if last:
        try:
            gap = (now or datetime.now()) - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
            if gap < timedelta(days=cfg["cooldown_days"]):
                left = cfg["cooldown_days"] - gap.total_seconds() / 86400
                out["reason"] = f"上次翻新才 {gap.total_seconds()/86400:.1f} 天，还要等 {left:.1f} 天"
                out["blockers"].append("cooldown")
                return out
        except Exception:
            pass

    out["rework"] = True
    out["reason"] = (f"上架 {days} 天 · 浏览 {browse} · 成交 {sold or 0}："
                     f"已过杀品线，建议重建一条")
    return out


# ---------------------------------------------------------------- 标题变体

# 只做"顺序/前后缀"级别的变体，不改卖点、不加违禁词。
# 为什么不用 AI 改写：重建的目的是"换一条新链接试试新主图"，标题若被 AI 改写
# 出了夸大词，风险直接落在账号上。这一步保持确定性，宁可变化小。
_TITLE_TWEAKS: Tuple[Tuple[str, str], ...] = (
    ("，", "｜"),
    ("｜", "，"),
    (" - ", "｜"),
    ("【", ""),
)

_PREFIXES = ("现货 · ", "当天下单 · ", "秒发 · ")


def title_variant(title: str, round_index: int = 0) -> str:
    """
    给同一件商品造一个"看得出是同类、但字符串不同"的标题。

    为什么要换：同一标题 + 同一主图重发，平台侧更容易被判成重复铺货；
    换标题是成本最低的差异化。但**不换卖点、不加新词**——只调分隔符和加一个
    发货时效前缀，且必须重新过 publish_guard（调用方负责）。

    保持幂等：同一个 round_index 永远得到同一个结果（便于重试时对得上）。
    """
    t = str(title or "").strip()
    if not t:
        return t
    idx = abs(int(round_index or 0))
    # 先试分隔符替换（最不显眼）
    a, b = _TITLE_TWEAKS[idx % len(_TITLE_TWEAKS)]
    if a in t:
        return t.replace(a, b, 1)
    # 再试前缀
    p = _PREFIXES[idx % len(_PREFIXES)]
    if not t.startswith(p):
        return (p + t)[:30]
    return t


# ---------------------------------------------------------------- 核心动作

def scan(cookie_id: str, cookie_str: str, app_key: str,
         cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    拉全店在售 → 补详情 → 逐条判。全程只读（列表 + 详情两个读接口）。

    复用 B2 的 `fetch_my_items` / `fetch_detail` 而不是重写一套：
    同一个 MTOP 接口在仓库里有两个实现是 bug 的温床（字段名、错误分类、
    cookie 回写各写一遍，迟早不一致）。
    """
    cfg = cfg or get_config()
    import shop_diagnosis as sd

    listed = sd.fetch_my_items(cookie_str, app_key, cookie_id=cookie_id)
    if not listed.get("ok"):
        return {"ok": False, "error": listed.get("error") or "拉取商品列表失败",
                "items": [], "candidates": []}

    st = get_state()
    reworked = st.get("reworked") or {}
    items: List[Dict[str, Any]] = []
    for it in listed.get("items") or []:
        iid = str(it.get("item_id") or "")
        if not iid:
            continue
        row = dict(it)
        row["first_seen"] = None
        try:
            row["days"] = sd.first_seen_days(cookie_id, iid)
            row["first_seen"] = _first_seen_raw(cookie_id, iid)
        except Exception:
            row["days"] = None
        rec = reworked.get(f"{cookie_id}::{iid}") or {}
        row["last_reworked_at"] = rec.get("last_reworked_at") or ""
        row["disabled"] = bool(rec.get("disabled"))
        items.append(row)

    # 补详情：只补"像候选"的，省详情配额（12 次/分钟是硬约束）。
    # 三个来源都要进候选池：
    #   - days 已知且够老（正常情况）；
    #   - days 未知（**没有观测历史**）。这一条不能漏：`first_seen` 是 B2 落库时才写的，
    #     用户若从没跑过店铺复盘，全店 days 都是 None，只按 days 过滤会让本模块
    #     一条都不补详情、静默空跑。而详情里可能带 gmtCreate，那才是真实上架时间。
    known_old = [x for x in items if (x.get("days") or 0) >= cfg["min_days"]]
    unknown = [x for x in items if x.get("days") is None]
    known_old.sort(key=lambda x: -(x.get("days") or 0))   # 最老的先补
    need = known_old + unknown
    budget = max(1, min(len(need), cfg["max_per_round"] * 4))
    for x in need[:budget]:
        try:
            d = sd.fetch_detail(cookie_str, x["item_id"], app_key, cookie_id=cookie_id)
        except Exception:
            d = {"ok": False}
        if not d.get("ok"):
            continue
        for k in ("browse", "want", "sold", "stock", "gmt_create"):
            if d.get(k) is not None:
                x[k] = d[k]
        if d.get("cookie_str"):
            cookie_str = d["cookie_str"]
        # 详情里带了真实上架时间 → 用真实的重算天数（优先于观测起点）。
        # 这是"没跑过 B2 也能判"的第二道保险：gmtCreate 有值就完全不依赖本地历史。
        if d.get("gmt_create") is not None:
            x["first_seen_real"] = d["gmt_create"]
        time.sleep(0.4)

    candidates = [judge(x, cfg) for x in items]
    # judge 用的是 first_seen；如果详情给了真实的 gmt_create，用真实值再判一次。
    for c, x in zip(candidates, items):
        if x.get("first_seen_real") is not None:
            c2 = judge({**x, "gmt_create": x["first_seen_real"]}, cfg)
            c.update(c2)
    # 给每条候选标上"绑定状态"，页面才能一眼看出"该翻新但没模板"的那些——
    # 否则用户开了开关，看到一堆候选却一条都没动，还会以为是程序坏了。
    bindings = st.get("bindings") or {}
    for c in candidates:
        key = f"{cookie_id}::{c['item_id']}"
        own = bindings.get(key) or {}
        w = own or _find_watch_for(cookie_id, c["item_id"])
        c["has_template"] = bool(w.get("template_id"))
        c["template_id"] = int(w.get("template_id") or 0)
    # 没有任何一处能算出天数时，要说出来——否则用户看到"全店不动"会以为程序坏了，
    # 实际是"没有观测历史、详情也没给上架时间"，得先跑一次 B2 店铺复盘打底。
    no_age = [c for c in candidates if c.get("days") is None]
    out: Dict[str, Any] = {"ok": True, "items": items, "candidates": candidates,
                          "total": len(items), "checked": min(len(need), budget)}
    if no_age:
        out["warning"] = (f"{len(no_age)} 条商品算不出上架天数（没有观测历史，详情也没给上架时间）。"
                          "先去跑一次『店铺复盘』把观测起点落库，之后就能判了。")
    return out


def _first_seen_raw(cookie_id: str, item_id: str) -> Optional[str]:
    """读 shop_items.first_seen 原始值（age_days 要自己算，所以不走 first_seen_days）。"""
    try:
        import shop_diagnosis as sd
        conn = sd._conn()
        try:
            row = conn.execute(
                "SELECT first_seen FROM shop_items WHERE cookie_id=? AND item_id=?",
                (cookie_id, str(item_id))).fetchone()
            return row["first_seen"] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _night_ban(cfg: Dict[str, Any]) -> bool:
    if not cfg.get("night_ban", True):
        return False
    h = datetime.now().hour
    return h >= 23 or h < 8


def _find_watch_for(cookie_id: str, item_id: str) -> Dict[str, Any]:
    """
    找这件商品重建用的"模板 + 素材"绑定。

    两级来源，**自己的绑定优先**：
    1. C2 自己的绑定（`bind()`）：这个页面就是为"僵尸翻新"设计的，覆盖面与 A6 不同；
    2. A6 白名单兜底：用户可能已经在那边为同一件商品配过一次，没必要让他再配一遍。

    找不到就**不重建**，只在候选里标"缺模板"——这是最容易踩的坑：
    用户以为开了开关就会自动跑，实际一条都没绑模板，静默空转。
    """
    key = f"{cookie_id}::{item_id}"
    try:
        st = get_state()
        own = (st.get("bindings") or {}).get(key)
        if own:
            return dict(own)
    except Exception:
        pass
    try:
        import sold_out_relist as sor
        st = sor.get_state()
        return st.get("watchlist", {}).get(key) or {}
    except Exception:
        return {}


def rework(cookie_id: str, cookie_str: str, item: Dict[str, Any],
           cfg: Optional[Dict[str, Any]] = None,
           round_index: int = 0) -> Dict[str, Any]:
    """
    重建一条新链接。**不碰旧链接**（下架由人工做）。

    为什么复用 `sold_out_relist.relaunch` 而不是自己写发布：
    那条链路已经跑通并带上了幂等/风控分类/看图回执，重写一份等于把已知的坑
    再踩一遍。代价是它要求"模板 + 素材"，所以这里必须先拿到绑定关系。
    """
    cfg = cfg or get_config()
    import sold_out_relist as sor

    iid = str(item.get("item_id") or "")
    w = _find_watch_for(cookie_id, iid)
    if not w:
        return {"ok": False, "kind": "no_template",
                "error": "这件商品没绑定模板，无法自动重建。"
                         "请在本页给它绑定模板与素材（或手动发一条新的）。"}

    watch = dict(w)
    # 标题变体：只改分隔符/前缀，卖点不动
    if cfg.get("rotate_title") and item.get("title"):
        try:
            import product_template as pt
            tpl = (pt.get_template(int(watch.get("template_id") or 0)) or {}).get("template") or {}
            # 模板是"{{品牌}} 型号"这种带变量的，直接改渲染后的标题没意义
            # （relaunch 会用模板重新渲染）。所以只在模板是**纯静态标题**时才换。
            if "{{" not in str(tpl.get("title_tpl") or ""):
                base = pt.render(tpl.get("title_tpl") or "", watch.get("variables") or {})
                watch["_title_override"] = title_variant(base, round_index)
        except Exception:
            pass

    watch.setdefault("cookie_id", cookie_id)
    watch.setdefault("item_id", iid)

    try:
        res = sor.relaunch(cookie_id, cookie_str, watch)
    except Exception as e:
        return {"ok": False, "kind": "exception", "error": str(e)[:200]}
    return res


def run_round(force: bool = False) -> Dict[str, Any]:
    """
    跑一轮。force=True 忽略 enabled（手动触发），但仍守夜间禁令与演练开关。

    **重建成功后不会自动下架旧链接**——只把它推进 `pending_down`，
    页面上列成清单给人工点。这是本模块唯一的"半自动"之处，也是刻意保留的。
    """
    global _last_round
    import rate_limiter
    from opportunity_detail import resolve_cookie

    cfg = get_config()
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _exit(msg: str, **extra) -> Dict[str, Any]:
        global _last_round
        out = {"ok": False, "scanned": 0, "candidates": 0, "reworked": 0,
               "skipped": 0, "failed": 0, "dry_run": cfg["dry_run"],
               "error": msg, "details": [], "pending_down": [], "finished_at": now_s}
        out.update(extra)
        _last_round = out
        return out

    if not force and not cfg["enabled"]:
        return _exit("长期零出单重发未启用（默认关闭）")
    if _night_ban(cfg):
        return _exit("夜间时段（23:00–08:00）不动作，已跳过本轮")
    if not _lock.acquire(blocking=False):
        return _exit("上一轮还在跑，本次跳过")
    try:
        return _run_locked(cfg, rate_limiter, resolve_cookie)
    finally:
        _lock.release()


def _run_locked(cfg: Dict[str, Any], rate_limiter, resolve_cookie) -> Dict[str, Any]:
    global _last_round
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st = get_state()

    # 用哪个账号：优先配置里指定的，否则取第一个有 Cookie 的。
    # 这个动作是"店铺级"的，不做多账号轮询——每轮只处理一个账号，
    # 避免一轮里把几个账号的发布配额同时打出去。
    accounts = _active_accounts()
    if not accounts:
        out = {"ok": False, "scanned": 0, "candidates": 0, "reworked": 0, "skipped": 0,
               "failed": 0, "dry_run": cfg["dry_run"], "details": [],
               "error": "没有任何账号有可用 Cookie", "pending_down": [],
               "finished_at": now_s}
        _last_round = out
        return out

    summary: Dict[str, Any] = {"ok": True, "scanned": 0, "candidates": 0, "reworked": 0,
                               "skipped": 0, "failed": 0, "dry_run": cfg["dry_run"],
                               "details": [], "accounts": []}
    budget = cfg["max_per_round"]
    for cid in accounts:
        if budget <= 0:
            break
        _, cstr = resolve_cookie(cid, fallback_first=False)
        if not cstr:
            continue
        summary["accounts"].append(cid)
        ok_q, retry = rate_limiter.acquire("detail", "global")
        if not ok_q:
            summary["skipped"] += 1
            continue
        try:
            got = scan(cid, cstr, _app_key(), cfg)
        except Exception as e:
            summary["failed"] += 1
            summary["details"].append({"cookie_id": cid, "action": f"扫描异常：{str(e)[:120]}"})
            continue
        if not got.get("ok"):
            summary["failed"] += 1
            summary["details"].append({"cookie_id": cid, "action": got.get("error", "扫描失败")[:120]})
            if got.get("kind") == "risk":
                summary["error"] = "扫描触发风控，本轮中止"
                break
            continue

        summary["scanned"] += got.get("total") or 0
        cands = got.get("candidates") or []
        hit = [c for c in cands if c.get("rework")]
        summary["candidates"] += len(hit)
        missing_tpl = [c for c in hit if not c.get("has_template")]
        if missing_tpl:
            summary["no_template"] = summary.get("no_template", 0) + len(missing_tpl)
            summary["details"].append({
                "action": f"⚠️ {len(missing_tpl)} 条该翻新但没绑模板，已跳过",
                "items": [c["item_id"] for c in missing_tpl][:10],
            })
        for c in hit:
            if budget <= 0:
                break
            iid = c["item_id"]
            title = c.get("title") or iid
            if not c.get("has_template"):
                summary["skipped"] += 1
                continue
            if cfg["dry_run"]:
                summary["details"].append({
                    "item_id": iid, "title": title, "action": "演练：该重建",
                    "reason": c.get("reason", "")})
                summary["skipped"] += 1
                continue

            # 日发布配额：`publish_item` 内部**没有任何限流**，`create_job` 的 20 条/天
            # 只作用在批量发布那条路上。这里自己挡一道——否则用户反复点"立即跑一轮"，
            # 每轮 3 条，一天能发出三四十条，正是平台明确降权的量级。
            try:
                import product_template as pt
                today = rate_limiter.daily_count("publish", cid)
                if today >= pt.DAILY_PUBLISH_CAP:
                    summary["skipped"] += 1
                    summary["details"].append({
                        "item_id": iid, "title": title,
                        "action": f"该账号今天已发 {today} 条，达到单日上限 "
                                  f"{pt.DAILY_PUBLISH_CAP} 条，明天再继续"})
                    continue
            except Exception:
                pass
            # 幂等键：同一件商品 + 同一轮次只能发一次。轮次用"上次翻新时间"参与，
            # 这样冷却期过后重试是允许的，但同一轮重复触发会被挡住。
            key = _claim_key(cid, iid)

            # B5 顺序：**只读预检 → 取配额 → 原子 claim**。同一轮重复触发不该先扣配额。
            pre = {"exists": False, "blocked": False}
            try:
                import idempotency
                pre = idempotency.precheck(key)
            except Exception:
                pass
            if pre.get("exists") or pre.get("blocked"):
                summary["skipped"] += 1
                summary["details"].append({"item_id": iid, "title": title,
                                           "action": "已有未完成的发布记录，跳过（防重复发布）"})
                continue

            ok_w, retry = rate_limiter.acquire("publish", cid, limit=1, window=60)
            if not ok_w:
                summary["skipped"] += 1
                summary["details"].append({"item_id": iid, "title": title,
                                           "action": f"发布限流，{retry}s 后再来"})
                break

            claim = {"claimed": True, "blocked": False}
            try:
                import idempotency
                claim = idempotency.claim(key, action="stale_rework", note=f"{title[:60]}")
            except Exception:
                pass
            if not claim.get("claimed"):
                summary["skipped"] += 1
                summary["details"].append({"item_id": iid, "title": title,
                                           "action": "已有未完成的发布记录，跳过（防重复发布）"})
                continue

            res = rework(cid, cstr, c, cfg, round_index=len(st.get("reworked") or {}))
            if res.get("ok"):
                budget -= 1
                summary["reworked"] += 1
                new_id = str(res.get("item_id") or "")
                rec = (st.get("reworked") or {}).get(f"{cid}::{iid}") or {}
                rec.update({
                    "cookie_id": cid, "item_id": iid, "title": title,
                    "last_reworked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "new_item_id": new_id, "attempts": 0,
                    "browse_at_rework": c.get("browse"), "days_at_rework": c.get("days"),
                })
                st.setdefault("reworked", {})[f"{cid}::{iid}"] = rec
                # 旧链接进"待下架"清单。**先重建成功才登记**——重建失败还让用户去删旧链接，
                # 等于把商品删没了。
                st.setdefault("pending_down", []).append({
                    "cookie_id": cid, "item_id": iid, "title": title,
                    "url": item_url(iid), "new_item_id": new_id,
                    "new_url": item_url(new_id) if new_id else "",
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "pending",
                    "reason": c.get("reason", ""),
                })
                summary["details"].append({
                    "item_id": iid, "title": title, "action": "已重建",
                    "new_item_id": new_id, "reason": c.get("reason", "")})
                try:
                    import idempotency
                    idempotency.finish(key, "done", f"new={new_id}")
                except Exception:
                    pass
                _notify("僵尸链接已重建",
                        f"{title}\n旧：{item_url(iid)}\n新：{item_url(new_id)}\n"
                        f"账号：{cid}\n\n去页面点『已下架』把旧链接删掉。")
            else:
                summary["failed"] += 1
                kind = str(res.get("kind") or "")
                err = str(res.get("error") or "")[:140]
                summary["details"].append({"item_id": iid, "title": title,
                                           "action": f"重建失败：{err}"})
                rec = (st.get("reworked") or {}).get(f"{cid}::{iid}") or {
                    "cookie_id": cid, "item_id": iid, "title": title}
                rec["attempts"] = int(rec.get("attempts") or 0) + 1
                rec["last_error"] = err
                rec["last_attempt_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if rec["attempts"] >= cfg["max_attempts"]:
                    rec["disabled"] = True
                    _notify("僵尸链接重建连续失败",
                            f"{title}\n错误：{err}\n已自动停用，请人工检查", level="error")
                st.setdefault("reworked", {})[f"{cid}::{iid}"] = rec
                # 失败要放掉幂等键，允许下一轮重试；但若结果未知（submission_unknown）
                # 绝不能放——那会因为"以为没发出去"而重复发布。
                try:
                    import idempotency
                    if kind != "unknown":
                        idempotency.finish(key, "failed", err)
                except Exception:
                    pass
                if kind == "risk":
                    summary["error"] = "重建触发风控，本轮中止"
                    break

            rate_limiter.jitter(10, 20)

    # 清理过期的待下架提醒（超过 keep_old_link_days 的降级成 archived，
    # 但仍留在清单里可查，绝不自动删——用户可能就是想留着）
    keep = cfg["keep_old_link_days"]
    cutoff = datetime.now() - timedelta(days=keep)
    changed = False
    for p in st.get("pending_down") or []:
        if p.get("status") != "pending":
            continue
        try:
            if datetime.strptime(p.get("created_at", ""), "%Y-%m-%d %H:%M:%S") < cutoff:
                p["status"] = "expired"
                changed = True
        except Exception:
            continue
    if changed:
        _log(st, "部分待下架提醒已超过保留期，标记为 expired（不会自动删除）", "warn")

    st["pending_down"] = (st.get("pending_down") or [])[-500:]
    summary["pending_down"] = [p for p in (st.get("pending_down") or [])
                              if p.get("status") == "pending"]
    summary["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _log(st, f"翻新巡检完成：扫 {summary['scanned']}，候选 {summary['candidates']}，"
             f"重建 {summary['reworked']}，失败 {summary['failed']}"
             + ("（演练）" if cfg["dry_run"] else ""))
    _save_state(st)
    _last_round = summary
    return summary


def _claim_key(cookie_id: str, item_id: str) -> str:
    try:
        import idempotency
        return idempotency.make_key("stale_rework", cookie_id, item_id)
    except Exception:
        return f"stale_rework:{cookie_id}:{item_id}"


def _active_accounts() -> List[str]:
    """有 Cookie 的账号列表。这个动作是店铺级的，一轮只处理一个账号最省配额，
    但多账号场景下用户会希望一轮把每个账号都扫到（否则第二个账号永远轮不上），
    所以按顺序全扫，`max_per_round` 是**全局**预算。"""
    out: List[str] = []
    try:
        import cookie_manager as cm
        cookies = getattr(getattr(cm, "manager", None), "cookies", None) or {}
        out = [str(k) for k, v in cookies.items() if v]
    except Exception:
        out = []
    if not out:
        try:
            from db_manager import db_manager
            cookies = db_manager.get_all_cookies() or {}
            out = [str(k) for k, v in cookies.items() if v]
        except Exception:
            out = []
    return out


def _app_key() -> str:
    try:
        from config import config
        return str(config.get("APP_CONFIG.app_key", ""))
    except Exception:
        return ""


def _notify(title: str, text: str, level: str = "info") -> None:
    try:
        from notify_service import send_event
        send_event("publish", title, text, level=level)
    except Exception:
        pass


# ---------------------------------------------------------------- 待下架清单

def list_pending(cookie_id: str = "", include_done: bool = False) -> Dict[str, Any]:
    """待下架清单。每条都带 url —— 用户点进去自己下架，这是半自动的核心。"""
    st = get_state()
    rows = st.get("pending_down") or []
    out = []
    for p in rows:
        if cookie_id and p.get("cookie_id") != cookie_id:
            continue
        if not include_done and p.get("status") != "pending":
            continue
        out.append({**p, "url": p.get("url") or item_url(p.get("item_id", ""))})
    out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"ok": True, "items": out, "count": len(out)}


def mark_down(cookie_id: str, item_id: str, done: bool = True) -> Dict[str, Any]:
    """
    用户点完"我已下架旧链接"后标记。

    **我们无法验证他是否真的下架了**（没有可用的下架状态查询接口），所以这个动作
    只是记一笔，不代表任何平台事实。页面上要说清楚这一点，不能让它看起来像
    "系统确认已下架"。
    """
    st = get_state()
    found = False
    for p in st.get("pending_down") or []:
        if p.get("cookie_id") == cookie_id and p.get("item_id") == item_id:
            p["status"] = "done" if done else "pending"
            p["marked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            found = True
    if not found:
        return {"ok": False, "error": "清单里没有这条"}
    _log(st, f"人工标记{'已下架' if done else '未下架'}：{item_id}")
    _save_state(st)
    return {"ok": True}


def get_status() -> Dict[str, Any]:
    st = get_state()
    pend = [p for p in (st.get("pending_down") or []) if p.get("status") == "pending"]
    reworked = st.get("reworked") or {}
    return {
        "config": get_config(),
        "pending_count": len(pend),
        "reworked_count": len(reworked),
        "disabled_count": len([1 for v in reworked.values() if v.get("disabled")]),
        "last_round": _last_round,
        "recent_logs": (st.get("logs") or [])[-20:],
        "running": bool(_thread and _thread.is_alive()),
    }


# ---------------------------------------------------------------- 调度

def _loop() -> None:
    if _tick_stop.wait(90):     # 错开 A6 的 60 秒启动偏移
        return
    import task_watchdog as _wd
    while not _tick_stop.is_set():
        cfg = get_config()
        # 期望节奏跟着配置走：改完间隔不用重启，检测阈值也一起变
        _wd.register("stale-relist", max(60, int(cfg["interval_hours"]) * 3600),
                     "僵尸链接重发扫描")
        try:
            if cfg["enabled"]:
                run_round(force=False)
        except Exception:
            pass
        _wd.beat("stale-relist")
        if _tick_stop.wait(max(60, int(cfg["interval_hours"]) * 3600)):
            return


def start_scheduler() -> bool:
    global _thread
    if _thread and _thread.is_alive():
        return False
    _tick_stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="stale-relist")
    _thread.start()
    return True


__all__ = [
    "DEFAULTS", "HARD_MAX_PER_ROUND", "get_config", "save_config", "get_state",
    "age_days", "judge", "title_variant", "scan", "rework", "run_round",
    "list_pending", "mark_down", "get_status", "start_scheduler", "item_url",
]
