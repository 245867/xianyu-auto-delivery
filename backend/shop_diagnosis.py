"""
店铺数据复盘 + 限流归因（B2）
==============================

**先说清楚数据边界，别自欺欺人。**

平台没有任何接口给「曝光数」。闲管家开放平台官方 FAQ 明确否认（见 `goofish_open.py`
顶部：曝光/浏览/想要/收藏/咨询/搜索排名 → 没有），MTOP 的列表接口与详情接口只回：

    mtop.idle.web.xyh.item.list   → 标题/价格/状态/类目/图片/标签（无任何计数）
    mtop.taobao.idle.pc.detail    → wantCnt / browseCnt / collectCnt / soldCnt / itemStatus / skuList

所以本模块能算的是 **浏览 → 想要/收藏 → 成交** 这条三段漏斗，**不是** 文档里那条
「曝光 → 浏览 → 咨询 → 成交」四段漏斗。两者差在两头：
- 曝光拿不到 → 用「浏览绝对值」代替「曝光/浏览比」，判据随之放宽（见 THRESHOLDS 注释）；
- 咨询拿不到 → 用「想要数」当意向代理（点了想要 ≈ 有明确意向，比纯浏览强）。
这两处替代**必须写在报告里**，否则用户会拿它当平台数据用，判断就会偏。

**这个模块和别人的差别在哪。**

`goofish-cli` 的 `shop-diagnosis` skill 自己也承认："v0.2.4 是无状态的——它不记你昨天的
曝光是多少，所以『和昨天相比』的对比类诊断做不到"。我们不缺落库能力，所以这里每跑一次
就把当天的计数存进 SQLite，**第二天起就能给趋势**（涨/跌/持平 + 七日曲线）。这是本模块
相对那个 skill 唯一的结构性优势，也是 B2 存在的理由。

**为什么按"信号"打分而不是给一个总分。**

限流是**多因一果**：可能是账号级、可能是单品级、可能是操作太密、也可能这个类目本身就冷。
给一个 78 分毫无意义——用户不知道该改什么。所以这里输出的是**带证据的候选归因**
（最多 3 条，按可能性排序）+ 每条对应的**具体动作**，学 goofish-cli 的归因清单结构，
但每一条都必须是能从我们手上数据算出来的，不编。

**安全姿态**：全程只读。不发布、不改价、不下架、不删链接。最坏结果是多搜了两次被限流。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from opportunity_publisher import (
    TOKEN_COOKIE_NAME, extract_token_from_cookie, _classify_ret, _mtop_request,
)
import polish_service
import rate_limiter
from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# 测试用环境变量覆盖，绝不动真实 data/
DB_FILE = Path(os.environ.get("SHOP_DIAG_DB") or (DATA_DIR / "shop_diagnosis.db"))
CONFIG_FILE = Path(os.environ.get("SHOP_DIAG_CONFIG") or (DATA_DIR / "shop_diagnosis_config.json"))

LIST_API = "mtop.idle.web.xyh.item.list"
LIST_VERSION = "1.0"
LIST_SPM = "a21ybx.item.0.0"      # 与 goofish-cli item/list.py、底座 get_item_list_info 一致

DETAIL_API = "mtop.taobao.idle.pc.detail"   # 与 A3 opportunity_detail 同一个接口
DETAIL_VERSION = "1.0"
DETAIL_SPM = "a21ybx.im.0.0"

MAX_PAGES = 20              # 翻页硬上限，防 nextPage 恒真死循环
PAGE_SIZE = 20
DETAIL_BUDGET = 30          # 单次复盘最多补多少条详情（详情 12 次/分钟，30 条约 3 分钟）
DETAIL_MIN_DELAY = 1.0
DETAIL_MAX_DELAY = 2.0

# ---------------------------------------------------------------- 判据
# 全部来自 148 份实操文档 + goofish-cli 归因清单，注释里写清出处与改动理由。
THRESHOLDS: Dict[str, float] = {
    # 「链接上了 2–3 天，曝光和浏览都在 50 以下 → 直接淘汰」（133 号文档，唯一被反复确认的杀品线）
    # 我们拿不到曝光，只能看浏览，所以门槛**只严不松**：浏览 < 50 才判，等于把"两低"里
    # 更低的那一头当唯一依据，宁可漏杀也不误杀。
    "cull_browse": 50,
    "cull_min_days": 2,
    # 僵尸：想要 ≤2 且浏览 <200（沿用原 B2 排期里的口径）
    "zombie_want": 2,
    "zombie_browse": 200,
    # 潜力：想要 5–10，或想要/浏览 >10%，且浏览 <500
    "potential_want": 5,
    "potential_rate": 0.10,
    "potential_browse": 500,
    # 爆款：想要 ≥200
    "hot_want": 200,
    # 窗口：上架不足 24h 不判（初始流量池还没走完，见 goofish-cli 归因 1）
    "observe_hours": 24,
    # 账号级：≥60% 商品浏览 <20 → 不是单品问题
    "account_ratio": 0.6,
    "account_browse": 20,
    # 价格离群：偏离店内同类中位数 2 倍以上 / 低于 40%
    "price_high": 2.0,
    "price_low": 0.4,
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "detail_budget": DETAIL_BUDGET,
    "auto_scan": False,          # 定时复盘默认关
    "hour": 10,
    "minute": 0,
    "keep_days": 180,            # 快照保留天数
    # 真成交口径的统计窗口（天）。只在 `SELLER_API_ENABLED=1` 时生效
    "sales_days": 7,
}


# ---------------------------------------------------------------- 存储

def _conn() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shop_items (
            cookie_id TEXT NOT NULL,
            item_id   TEXT NOT NULL,
            title     TEXT,
            price     REAL,
            status    TEXT,
            category  TEXT,
            first_seen TEXT NOT NULL,
            last_seen  TEXT NOT NULL,
            PRIMARY KEY (cookie_id, item_id)
        )""")
    # 每天一条，主键去重 → 同一天跑多次只留最后一条，趋势按天算不会被刷成锯齿
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shop_snapshots (
            cookie_id TEXT NOT NULL,
            item_id   TEXT NOT NULL,
            day       TEXT NOT NULL,
            browse    INTEGER,
            want      INTEGER,
            collect   INTEGER,
            sold      INTEGER,
            stock     INTEGER,
            images    INTEGER,
            first_image_ratio TEXT,
            price     REAL,
            status    TEXT,
            PRIMARY KEY (cookie_id, item_id, day)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shop_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cookie_id TEXT NOT NULL,
            at TEXT NOT NULL,
            ok INTEGER NOT NULL,
            items INTEGER NOT NULL,
            payload TEXT NOT NULL
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


# ---------------------------------------------------------------- 读数（只读）

def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_card(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 cardList 的一张卡里取商品关键字段（结构对齐 goofish-cli item/list.py）。"""
    data = (card or {}).get("cardData") or card or {}
    if not isinstance(data, dict):
        return None
    item_id = str((data.get("detailParams") or {}).get("itemId")
                  or data.get("id") or "").strip()
    if not item_id.isdigit():
        return None
    price_info = data.get("priceInfo") or {}
    price = _to_float(str(price_info.get("price") or "").replace("¥", "").replace(",", ""))
    pic = data.get("picInfo") or {}
    labels: List[str] = []
    vo = data.get("itemLabelDataVO") or {}
    if isinstance(vo, dict):
        for _region, region in (vo.get("labelData") or {}).items():
            for tag in (region.get("tagList") if isinstance(region, dict) else []) or []:
                td = tag.get("data") if isinstance(tag, dict) else None
                if isinstance(td, dict) and td.get("type") != "img" and td.get("content"):
                    labels.append(str(td["content"]))
    return {
        "item_id": item_id,
        "title": str(data.get("title") or ""),
        "price": price,
        "status": str(data.get("itemStatus", "")),
        "category": str(data.get("categoryId") or ""),
        "image_url": str(pic.get("picUrl") or ""),
        "labels": labels,
    }


def fetch_my_items(cookie_str: str, app_key: str, cookie_id: str = "",
                   max_pages: int = MAX_PAGES) -> Dict[str, Any]:
    """拉当前账号在售商品（分页，本页无新增即退出）。只读。"""
    if not cookie_str or not extract_token_from_cookie(cookie_str):
        return {"ok": False, "error": f"Cookie 里没有 {TOKEN_COOKIE_NAME}，请重新登录闲鱼", "items": []}
    unb = ""
    for part in cookie_str.split(";"):
        name, _, value = part.strip().partition("=")
        if name.strip() == "unb":
            unb = value.strip()
            break
    if not unb:
        return {"ok": False, "error": "Cookie 里没有 unb（用户 ID），无法查询自己的商品", "items": []}

    items: List[Dict[str, Any]] = []
    seen = set()
    page = 1
    while page <= max(1, int(max_pages)):
        try:
            raw, resp = _mtop_request(
                cookie_str, LIST_API,
                {"needGroupInfo": False, "pageNumber": page, "pageSize": PAGE_SIZE,
                 "groupName": "在售", "defaultGroup": True, "userId": unb},
                LIST_VERSION, LIST_SPM, app_key, timeout=20)
        except requests.RequestException as exc:
            return {"ok": False, "items": items,
                    "error": f"第 {page} 页请求失败：{type(exc).__name__}"}
        except Exception as exc:
            return {"ok": False, "items": items, "error": str(exc)[:200]}

        if resp.headers.get("set-cookie") and cookie_id:
            merged = polish_service._merge_response_cookies(resp, cookie_str)
            if merged != cookie_str:
                cookie_str = merged
                polish_service._persist_cookie(cookie_id, merged)

        ret = raw.get("ret") or []
        ret_str = " | ".join(str(r) for r in ret) if isinstance(ret, list) else str(ret)
        try:
            _classify_ret(LIST_API, ret_str)
        except Exception as exc:
            return {"ok": False, "items": items, "error": str(exc)[:240]}

        data = raw.get("data") or {}
        before = len(items)
        if page == 1:
            top = data.get("topItem")
            if isinstance(top, dict) and top:
                got = _extract_card(top)
                if got and got["item_id"] not in seen:
                    seen.add(got["item_id"]); items.append(got)
        for card in data.get("cardList") or []:
            got = _extract_card(card)
            if got and got["item_id"] not in seen:
                seen.add(got["item_id"]); items.append(got)

        # 本页没有新增 → 退出（nextPage 可能恒真，绝不能只信它）
        if len(items) == before or not data.get("nextPage"):
            break
        page += 1
        time.sleep(0.6)

    return {"ok": True, "items": items, "pages": page, "error": ""}


def fetch_detail(cookie_str: str, item_id: str, app_key: str,
                 cookie_id: str = "") -> Dict[str, Any]:
    """读单个商品详情里的四个计数 + 图片数/首图比例。失败不改写任何东西。"""
    try:
        raw, resp = _mtop_request(cookie_str, DETAIL_API, {"itemId": str(item_id)},
                                  DETAIL_VERSION, DETAIL_SPM, app_key, timeout=15)
    except requests.RequestException as exc:
        return {"ok": False, "error": f"详情请求失败：{type(exc).__name__}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}

    cookie_str2 = cookie_str
    if resp.headers.get("set-cookie") and cookie_id:
        merged = polish_service._merge_response_cookies(resp, cookie_str)
        if merged != cookie_str:
            cookie_str2 = merged
            polish_service._persist_cookie(cookie_id, merged)

    ret = raw.get("ret") or []
    ret_str = " | ".join(str(r) for r in ret) if isinstance(ret, list) else str(ret)
    try:
        _classify_ret(DETAIL_API, ret_str)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    data = raw.get("data") or {}
    item = data.get("itemDO") or {}
    if not isinstance(item, dict):
        return {"ok": False, "error": "详情里没有 itemDO"}

    infos = item.get("imageInfos") or []
    images = [i for i in infos if isinstance(i, dict) and i.get("url")]
    ratio = ""
    if images:
        w = _to_int(images[0].get("width"))
        h = _to_int(images[0].get("height"))
        if w and h:
            ratio = "1:1" if abs(w - h) <= max(2, w * 0.02) else f"{w}:{h}"

    skus = item.get("skuList") or []
    stock: Optional[int] = None
    if isinstance(skus, list) and skus:
        qty = [_to_int(s.get("quantity")) for s in skus if isinstance(s, dict)]
        qty = [q for q in qty if q is not None]
        if qty:
            stock = sum(qty)

    # 上架时间：接口是否返回过这个字段**没有实证**，所以这里只做"有就用、没有就算"。
    # C2 的"上架 ≥7 天"判据主要靠我们自己 first_seen 的观测起点；能读到真实创建
    # 时间就用真实的，读不到不会因此误判（见 stale_relist._age_days）。
    gmt_create = None
    for _k in ("gmtCreate", "createTime", "itemCreateTime", "publishTime", "gmtCreateTime"):
        if item.get(_k):
            gmt_create = item.get(_k)
            break

    return {
        "ok": True,
        "browse": _to_int(item.get("browseCnt")),
        "want": _to_int(item.get("wantCnt")),
        "collect": _to_int(item.get("collectCnt")),
        "sold": _to_int(item.get("soldCnt")),
        "gmt_create": gmt_create,
        "stock": stock,
        "images": len(images),
        "first_image_ratio": ratio,
        "status": str(item.get("itemStatusStr") or item.get("itemStatus") or ""),
        "cookie_str": cookie_str2,
    }


# ---------------------------------------------------------------- 快照

def record_snapshot(cookie_id: str, items: List[Dict[str, Any]]) -> int:
    """把这一轮读到的数落库。同一天重复跑只留最后一条。返回写入条数。"""
    today = date.today().isoformat()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    conn = _conn()
    try:
        for it in items:
            iid = str(it.get("item_id") or "")
            if not iid:
                continue
            conn.execute(
                "INSERT INTO shop_items(cookie_id,item_id,title,price,status,category,"
                "first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(cookie_id,item_id) DO UPDATE SET "
                "title=excluded.title,price=excluded.price,status=excluded.status,"
                "category=excluded.category,last_seen=excluded.last_seen",
                (cookie_id, iid, it.get("title") or "", it.get("price"),
                 it.get("status") or "", it.get("category") or "", now, now))
            conn.execute(
                "INSERT INTO shop_snapshots(cookie_id,item_id,day,browse,want,collect,"
                "sold,stock,images,first_image_ratio,price,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(cookie_id,item_id,day) DO UPDATE SET "
                "browse=COALESCE(excluded.browse,shop_snapshots.browse),"
                "want=COALESCE(excluded.want,shop_snapshots.want),"
                "collect=COALESCE(excluded.collect,shop_snapshots.collect),"
                "sold=COALESCE(excluded.sold,shop_snapshots.sold),"
                "stock=COALESCE(excluded.stock,shop_snapshots.stock),"
                "images=COALESCE(excluded.images,shop_snapshots.images),"
                "first_image_ratio=COALESCE(excluded.first_image_ratio,shop_snapshots.first_image_ratio),"
                "price=excluded.price,status=excluded.status",
                (cookie_id, iid, today, it.get("browse"), it.get("want"),
                 it.get("collect"), it.get("sold"), it.get("stock"),
                 it.get("images"), it.get("first_image_ratio") or "",
                 it.get("price"), it.get("status") or ""))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def first_seen_days(cookie_id: str, item_id: str) -> Optional[float]:
    """这件商品我们第一次看到它是多久以前（天）。**这是我们自己的观测起点，不是平台发布时间。**"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT first_seen FROM shop_items WHERE cookie_id=? AND item_id=?",
            (cookie_id, str(item_id))).fetchone()
    finally:
        conn.close()
    if not row or not row["first_seen"]:
        return None
    try:
        t0 = datetime.strptime(row["first_seen"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return round((datetime.now() - t0).total_seconds() / 86400, 2)


def history(cookie_id: str, item_id: str, days: int = 14) -> List[Dict[str, Any]]:
    """单件商品最近 N 天的计数曲线（按天，缺天不补 0 —— 没观测到就是没有）。"""
    since = (date.today() - timedelta(days=max(1, int(days)))).isoformat()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT day,browse,want,collect,sold,price FROM shop_snapshots "
            "WHERE cookie_id=? AND item_id=? AND day>=? ORDER BY day",
            (cookie_id, str(item_id), since)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def prev_snapshot(cookie_id: str, item_id: str) -> Optional[Dict[str, Any]]:
    """上一次（不含今天）的快照，用来算增量。"""
    today = date.today().isoformat()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM shop_snapshots WHERE cookie_id=? AND item_id=? AND day<? "
            "ORDER BY day DESC LIMIT 1", (cookie_id, str(item_id), today)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------- 判定

def classify(it: Dict[str, Any], days: Optional[float]) -> Tuple[str, str]:
    """给单件商品分类。返回 (code, 人话)。顺序有意义：先排除"还没到判断时候"。"""
    browse = it.get("browse")
    want = it.get("want")
    if days is not None and days < THRESHOLDS["observe_hours"] / 24.0:
        return "observe", "观察中"
    if browse is None:
        return "unknown", "无数据"
    if days is not None and days >= THRESHOLDS["cull_min_days"] and browse < THRESHOLDS["cull_browse"] \
            and (want is None or want <= THRESHOLDS["zombie_want"]):
        return "cull", "该淘汰"
    if want is None:
        return "unknown", "无想要数"
    if want >= THRESHOLDS["hot_want"]:
        return "hot", "爆款"
    rate = (want / browse) if browse else 0.0
    if (THRESHOLDS["potential_want"] <= want <= THRESHOLDS["hot_want"]
            or rate >= THRESHOLDS["potential_rate"]) and browse < THRESHOLDS["potential_browse"]:
        return "potential", "有潜力"
    if want <= THRESHOLDS["zombie_want"] and browse < THRESHOLDS["zombie_browse"]:
        return "zombie", "僵尸"
    return "normal", "正常"


def _sig(code: str, level: str, title: str, evidence: str, action: str,
         weight: float) -> Dict[str, Any]:
    return {"code": code, "level": level, "title": title, "evidence": evidence,
            "action": action, "weight": round(weight, 2)}


def item_signals(it: Dict[str, Any], days: Optional[float],
                 shop_ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """单品级归因信号。每一条都必须能从我们自己读到的数里算出来。"""
    out: List[Dict[str, Any]] = []
    browse = it.get("browse")
    want = it.get("want")
    sold = it.get("sold") or 0
    imgs = it.get("images")
    ratio = it.get("first_image_ratio") or ""
    price = it.get("price")

    # 1. 发布不足 24h：不判，先把话说清楚（避免用户误以为坏了）
    if days is not None and days < 1:
        out.append(_sig("fresh", "info", "刚记录不久，先别动",
                        f"我们第一次记录它到现在 {days:.1f} 天，初始流量池还没走完",
                        "24 小时内不要改标题/价格（改动会触发复审，审核期别人看不到）", 0.1))
        return out

    # 2. 浏览极低且已过观察期 → 杀品线（133 号文档）
    if browse is not None and days is not None and days >= THRESHOLDS["cull_min_days"] \
            and browse < THRESHOLDS["cull_browse"]:
        out.append(_sig("cull", "high", "浏览长期低于杀品线",
                        f"{days:.1f} 天累计浏览 {browse}（<{THRESHOLDS['cull_browse']:.0f}）",
                        "换首图+重写标题后再观察 2 天；仍不动就下架，别在这条链接上继续耗", 0.55))

    # 3. 浏览有、想要几乎为 0 → 主图/标题/价格不吸引人（文档：曝光高浏览低）
    if browse is not None and browse >= 100 and (want or 0) <= 1:
        out.append(_sig("click_bad", "high", "有人看没人想要",
                        f"浏览 {browse}，想要 {want or 0}",
                        "先换第 1 张主图（1:1 实拍、背景干净），再把价格压到同类 80% 分位", 0.45))

    # 4. 想要有、成交 0 → 详情/信任/价格（文档：浏览高咨询低 + 咨询高成交低）
    if (want or 0) >= 5 and sold == 0:
        out.append(_sig("no_deal", "mid", "有意向没成交",
                        f"想要 {want}，成交 {sold}",
                        "详情页补成色实拍、购买凭证、发货时效；首次成交可让 5–15%", 0.35))

    # 5. 图片少 / 首图不是 1:1（goofish-cli 归因 5；文档：六张图以上，主图最重要）
    if imgs is not None and imgs < 3:
        out.append(_sig("few_images", "mid", "图片太少",
                        f"只有 {imgs} 张图",
                        "补到 6 张以上：正面/背面/细节/瑕疵/配件/包装", 0.25))
    elif ratio and ratio != "1:1":
        out.append(_sig("ratio", "low", "首图不是 1:1",
                        f"首图比例 {ratio}",
                        "换成 1:1 方图，推荐流里非方图会被裁切，点击率直接掉", 0.2))

    # 6. 价格离店內同类太远（不是"不合市场"，是我们能证的最小版本）
    med = shop_ctx.get("median_price")
    if price and med and med > 0:
        if price >= med * THRESHOLDS["price_high"]:
            out.append(_sig("price_high", "low", "价格明显高于店内同类",
                            f"本件 ¥{price:g}，店内同类中位 ¥{med:g}",
                            "要么降价靠近中位，要么在标题/详情里把贵的理由写清楚", 0.15))
        elif price <= med * THRESHOLDS["price_low"]:
            out.append(_sig("price_low", "low", "价格明显低于店内同类",
                            f"本件 ¥{price:g}，店内同类中位 ¥{med:g}",
                            "过低会被当成钓鱼/有坑，反而没人问；抬回合理区间", 0.15))
    return out


def shop_signals(rows: List[Dict[str, Any]], ops: Dict[str, Any]) -> List[Dict[str, Any]]:
    """店铺级归因：区分"账号有事"和"单品有事"。"""
    out: List[Dict[str, Any]] = []
    judged = [r for r in rows if r.get("browse") is not None and r.get("classify") != "observe"]
    if len(judged) >= 3:
        dead = [r for r in judged if (r.get("browse") or 0) < THRESHOLDS["account_browse"]]
        ratio = len(dead) / len(judged)
        if ratio >= THRESHOLDS["account_ratio"]:
            out.append(_sig("account_wide", "high", "全店普遍没浏览（账号级，不是单品问题）",
                            f"{len(dead)}/{len(judged)} 件商品浏览 <{THRESHOLDS['account_browse']:.0f}"
                            f"（{ratio:.0%}）",
                            "别逐件改链接了，先看账号：App 里『我的-违规记录/闲气值』；"
                            "近期只发新链接、不删不发、不群改标题，等 7–30 天", 0.6))

    # 操作密度：今天已经写了多少次（发布/擦亮/发货），接近限额说明"操作太密"
    publish_n = int(ops.get("publish_today") or 0)
    polish_n = int(ops.get("polish_today") or 0)
    if publish_n >= 15:
        out.append(_sig("ops_dense_publish", "mid", "今天发布量偏大",
                        f"今日已发布 {publish_n} 条（自设上限 20）",
                        "剩下的明天再发；平台对『短时间大批量上架』有降权记录", 0.3))
    if polish_n >= 100:
        out.append(_sig("ops_dense_polish", "low", "今天擦亮次数偏多",
                        f"今日已擦亮 {polish_n} 次",
                        "擦亮是瞬时推送不是倍率，一天一次够用，多擦无益", 0.15))

    # 最近有 submission_unknown / 风控失败 → 账号可能已被盯上
    if int(ops.get("recent_unknown") or 0) > 0:
        out.append(_sig("recent_unknown", "high", "近期有『发布结果未知』的操作",
                        f"最近记录里 {ops['recent_unknown']} 条 submission_unknown",
                        "先去 App『我发布的』核对是否已上架；确认没有再用发布页重发", 0.5))
    return out


def diagnose(rows: List[Dict[str, Any]], cookie_id: str) -> Dict[str, Any]:
    """
    把已经带好计数的 rows 变成一份诊断报告。

    rows 每项需含：item_id/title/price/browse/want/collect/sold/images/
                   first_image_ratio/status/days
    """
    prices = [r["price"] for r in rows if r.get("price")]
    prices.sort()
    if not prices:
        median_price = None
    elif len(prices) % 2:
        median_price = prices[len(prices) // 2]
    else:   # 偶数取中间两个的平均，别让中位价偏向贵的那件
        mid = len(prices) // 2
        median_price = round((prices[mid - 1] + prices[mid]) / 2, 2)
    ctx = {"median_price": median_price}

    ops = _ops_snapshot(cookie_id)

    out_rows: List[Dict[str, Any]] = []
    for r in rows:
        # 调用方（run）已经填过 days；直接调 diagnose 时兜底查一次观测起点，
        # 否则所有商品都会因为"天数未知"而逃过杀品线判定。
        days = r.get("days")
        if days is None:
            days = first_seen_days(cookie_id, str(r.get("item_id") or ""))
        code, label = classify(r, days)
        sigs = item_signals(r, days, ctx)
        sigs.sort(key=lambda s: -s["weight"])
        browse = r.get("browse")
        want = r.get("want")
        out_rows.append({
            **r,
            "days": days,
            "classify": code,
            "classify_label": label,
            "want_rate": round((want / browse), 4) if browse and want is not None else None,
            "signals": sigs,
            "top_signal": sigs[0]["code"] if sigs else "",
            "prev": prev_snapshot(cookie_id, str(r.get("item_id"))),
        })

    overall = shop_signals(out_rows, ops)
    overall.sort(key=lambda s: -s["weight"])

    counts: Dict[str, int] = {}
    for r in out_rows:
        counts[r["classify"]] = counts.get(r["classify"], 0) + 1

    # 该做的事：店铺级优先（改账号比改单品重要），然后按单品信号权重取前 8 条
    actions: List[Dict[str, Any]] = []
    for s in overall[:3]:
        actions.append({"scope": "账号", "item_id": "", "title": s["title"],
                        "action": s["action"], "level": s["level"]})
    per_item = sorted(
        [(r, s) for r in out_rows for s in r["signals"] if s["code"] not in ("fresh",)],
        key=lambda x: -x[1]["weight"])
    for r, s in per_item[:8]:
        actions.append({"scope": "单品", "item_id": r["item_id"],
                        "title": f"{(r.get('title') or r['item_id'])[:24]} · {s['title']}",
                        "action": s["action"], "level": s["level"]})

    return {
        "ok": True,
        "at": datetime.now().isoformat(timespec="seconds"),
        "cookie_id": cookie_id,
        "counts": counts,
        "median_price": median_price,
        "items": out_rows,
        "shop_signals": overall,
        "actions": actions,
        "ops": ops,
        # 这两条必须一直在报告里，防止用户把替代指标当平台数据用
        "data_notes": [
            "曝光数拿不到（官方与 MTOP 都不给），本报告用「浏览绝对值」代替「曝光/浏览比」，"
            "判据因此只严不松：只有浏览 <50 且已过观察期才判淘汰。",
            "咨询数拿不到，用「想要数」当意向代理；想要数来自详情接口 wantCnt，"
            "列表接口的 wantNum 自 2026 起恒为 0，不可用。",
            "上架天数是「我们第一次记录到它的时间」，不是平台发布时间；第一次跑没有历史，"
            "所有商品都会显示观察中。",
        ],
    }


def sales_signals(sales: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    真成交口径（`seller_api.sales_summary`）单独出的信号。

    和 `item_signals` 分开的理由：那一条链的每个数都来自**商品**
    （浏览/想要/收藏/累计销量），这一条来自**订单**（真实成交额/退款/待发货）。
    混在一起会让"这条证据是平台给的还是我们算的"变得不清楚。

    拿不到（接口关着、熔断了、没 Cookie）就一条都不出——不编数。
    """
    out: List[Dict[str, Any]] = []
    if not sales or not sales.get("ok"):
        return out
    orders = int(sales.get("orders") or 0)
    pending = int(sales.get("pending_ship") or 0)
    refund = int(sales.get("refund_orders") or 0)
    days = int(sales.get("days") or 7)

    if pending > 0:
        out.append(_sig("sales_pending_ship", "high", "有成交单还没发货",
                        f"近 {days} 天 {pending} 单待发货（同期共 {orders} 单成交）",
                        "去发货页处理。虚拟商品拖过 24h 会被催，也直接影响账号权重", 0.75))
    if orders >= 5 and refund / max(orders, 1) >= 0.2:
        out.append(_sig("sales_refund_high", "mid", "退款率偏高",
                        f"近 {days} 天 {refund}/{orders} 单退款（{refund / orders:.0%}）",
                        "看退款原因：描述不符→改文案、补实拍；发货慢→模板里写清时效；"
                        "价格被比下去→换同价位段商品", 0.45))
    if orders == 0 and days >= 7:
        out.append(_sig("sales_no_order", "low", "近一周没有成交",
                        f"近 {days} 天卖出 0 单（不含已取消）",
                        "先看三段漏斗断在哪一段（浏览/想要/成交），别只会降价", 0.2))
    return out


def _apply_sales(report: Dict[str, Any], sales: Dict[str, Any]) -> Dict[str, Any]:
    """把成交口径并进报告：数放 `sales`，动作顶到 `actions` 最前。"""
    report["sales"] = sales
    sigs = sales_signals(sales)
    if not sigs:
        if not sales.get("ok"):
            notes = list(report.get("data_notes") or [])
            notes.append("本次没取到真实成交数据：" + str(sales.get("reason") or "未知原因")
                         + "（商品卡上的「累计销量」是历史累计，不能当本期成交）")
            report["data_notes"] = notes
        return report

    shop = list(report.get("shop_signals") or [])
    shop.extend(sigs)
    shop.sort(key=lambda s: -s["weight"])
    report["shop_signals"] = shop

    actions = list(report.get("actions") or [])
    for s in reversed(sigs):   # 保持权重从高到低插在最前
        actions.insert(0, {"scope": "账号", "item_id": "", "title": s["title"],
                           "action": s["action"], "level": s["level"]})
    report["actions"] = actions[:14]

    notes = list(report.get("data_notes") or [])
    notes.append(f"成交/金额/退款/待发货来自卖家端订单接口（近 {int(sales.get('days') or 7)} 天，"
                 f"读 {int(sales.get('pages') or 0)} 页），是真实订单口径，"
                 "不再是商品卡上的累计销量。")
    report["data_notes"] = notes
    return report


def _ops_snapshot(cookie_id: str) -> Dict[str, Any]:
    """今天本账号写操作的实际占用（来自统一限流器，重启不清零）。"""
    out = {"publish_today": 0, "polish_today": 0, "recent_unknown": 0}
    try:
        out["publish_today"] = int(rate_limiter.daily_count("publish", cookie_id) or 0)
    except Exception:
        pass
    try:
        out["polish_today"] = int(rate_limiter.daily_count("polish", cookie_id) or 0)
    except Exception:
        pass
    try:
        import idempotency
        recent = idempotency.recent(limit=60, action="publish")
        out["recent_unknown"] = len([r for r in recent
                                     if r.get("status") == "submission_unknown"])
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 一轮复盘

def run(cookie_id: str, cookie_str: str, app_key: str,
        with_detail: bool = True, detail_budget: Optional[int] = None) -> Dict[str, Any]:
    """拉列表 → 补详情 → 落快照 → 出报告。全程只读。"""
    cfg = get_config()
    budget = int(detail_budget if detail_budget is not None else cfg["detail_budget"])
    budget = max(0, min(DETAIL_BUDGET * 2, budget))

    listed = fetch_my_items(cookie_str, app_key, cookie_id=cookie_id)
    if not listed.get("ok"):
        return {"ok": False, "error": listed.get("error") or "拉取商品列表失败",
                "items": listed.get("items") or []}

    items = listed["items"]
    for it in items:
        it["days"] = first_seen_days(cookie_id, it["item_id"])
        it.update({"browse": None, "want": None, "collect": None, "sold": None,
                   "stock": None, "images": None, "first_image_ratio": ""})

    detail_stats = {"requested": 0, "success": 0, "failed": 0, "skipped": 0}
    if with_detail:
        # 先补"最久没更新 / 缺数据"的，避免每次都只补前 N 个导致后面的永远没数
        targets = items[:budget]
        for idx, it in enumerate(targets):
            ok, retry = rate_limiter.acquire("detail", "global")
            if not ok:
                detail_stats["skipped"] += 1
                if retry > 20:
                    break
                time.sleep(min(int(retry), 5))
                ok, retry = rate_limiter.acquire("detail", "global")
                if not ok:
                    detail_stats["skipped"] += 1
                    break
            if idx:
                rate_limiter.jitter(DETAIL_MIN_DELAY, DETAIL_MAX_DELAY)
            detail_stats["requested"] += 1
            d = fetch_detail(cookie_str, it["item_id"], app_key, cookie_id=cookie_id)
            if not d.get("ok"):
                detail_stats["failed"] += 1
                continue
            detail_stats["success"] += 1
            cookie_str = d.get("cookie_str") or cookie_str
            for k in ("browse", "want", "collect", "sold", "stock", "images",
                      "first_image_ratio"):
                if d.get(k) is not None:
                    it[k] = d[k]
            if d.get("status"):
                it["status"] = d["status"]

    # 没补到详情的，用上一次快照兜底（并标 stale，不冒充今天的数据）
    for it in items:
        if it.get("browse") is None:
            p = prev_snapshot(cookie_id, it["item_id"])
            if p:
                for k in ("browse", "want", "collect", "sold", "stock", "images"):
                    it[k] = p.get(k)
                it["first_image_ratio"] = p.get("first_image_ratio") or ""
                it["stale"] = True

    record_snapshot(cookie_id, items)
    report = diagnose(items, cookie_id)
    report["detail_enrich"] = detail_stats
    report["list_pages"] = listed.get("pages")

    # 真成交口径（B2 补）：商品卡只有「累计销量」，拿不到"这周卖了几单、退了没有"。
    # 卖家端订单接口直接给原值，代价是几次 HTTP，不启浏览器。拿不到就明说，不编。
    try:
        import seller_api
        sales = seller_api.sales_summary(cookie_str, cookie_id,
                                         days=int(cfg.get("sales_days") or 7))
    except Exception as _e:
        sales = {"ok": False, "reason": f"卖家端订单接口异常：{_e}", "days": 0}
    _apply_sales(report, sales)

    _save_run(cookie_id, True, len(items), report)

    # 诊断通知（覆盖）：定时复盘没人盯着，高危信号必须能推出去。
    # 只推 high 级，中低信号进页面即可（每天推会让人静音通知）。
    try:
        from notify_service import notify_diagnosis_summary
        notify_diagnosis_summary(report, cookie_id)
    except Exception:
        pass
    return report


def _save_run(cookie_id: str, ok: bool, n: int, payload: Dict[str, Any]) -> None:
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO shop_runs(cookie_id,at,ok,items,payload) VALUES (?,?,?,?,?)",
                (cookie_id, datetime.now().isoformat(timespec="seconds"),
                 1 if ok else 0, int(n), json.dumps(payload, ensure_ascii=False)))
            keep = int(get_config().get("keep_days") or 180)
            cutoff = (datetime.now() - timedelta(days=keep)).isoformat(timespec="seconds")
            conn.execute("DELETE FROM shop_runs WHERE at < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def last_report(cookie_id: str = "") -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        if cookie_id:
            row = conn.execute("SELECT * FROM shop_runs WHERE cookie_id=? ORDER BY id DESC LIMIT 1",
                               (cookie_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM shop_runs ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except Exception:
            payload = {}
        payload["_at"] = row["at"]
        payload["_items"] = row["items"]
        return payload
    finally:
        conn.close()


def status() -> Dict[str, Any]:
    from rate_limiter import circuit_status

    conn = _conn()
    try:
        n_items = conn.execute("SELECT COUNT(DISTINCT cookie_id||item_id) c FROM shop_items").fetchone()["c"]
        n_snap = conn.execute("SELECT COUNT(*) c FROM shop_snapshots").fetchone()["c"]
        days = [r["day"] for r in conn.execute(
            "SELECT DISTINCT day FROM shop_snapshots ORDER BY day DESC LIMIT 30").fetchall()]
        accounts = [r["cookie_id"] for r in conn.execute(
            "SELECT DISTINCT cookie_id FROM shop_items").fetchall()]
    finally:
        conn.close()
    return {"config": get_config(), "items_tracked": int(n_items),
            "snapshots": int(n_snap), "days": days, "accounts": accounts,
            "thresholds": THRESHOLDS, "circuit": circuit_status()}


# ---------------------------------------------------------------- 定时复盘

_tick_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_last_auto_day = ""


def _auto_target_time(cfg: Dict[str, Any]) -> str:
    h = max(0, min(23, int(cfg.get("hour") or 0)))
    m = max(0, min(59, int(cfg.get("minute") or 0)))
    return f"{h:02d}:{m:02d}"


def _loop() -> None:
    global _last_auto_day
    # 启动后等 90 秒，别和别的启动任务抢带宽
    if _tick_stop.wait(90):
        return
    import task_watchdog as _wd
    _wd.register("shop-diagnosis", 60, "店铺诊断定时扫描（每 60 秒看一次是否到点）")
    while not _tick_stop.is_set():
        cfg = get_config()
        try:
            if cfg.get("auto_scan"):
                now = datetime.now()
                target = _auto_target_time(cfg)
                today = now.date().isoformat()
                # 到点后 15 分钟内只跑一次；过了这段时间当天的就不补跑了，
                # 免得下午打开应用时突然发现自己一直在读店铺。
                if _last_auto_day != today and now.strftime("%H:%M") >= target \
                        and now.hour * 60 + now.minute - (int(cfg["hour"]) * 60 + int(cfg["minute"])) <= 15:
                    try:
                        cid, cstr = _resolve_cookie("")
                        key = _app_key_internal()
                        if cstr and key:
                            run(cid, cstr, key, with_detail=True)
                            _last_auto_day = today
                    except Exception:
                        pass
        except Exception:
            pass
        _wd.beat("shop-diagnosis")
        if _tick_stop.wait(60):
            return


def _resolve_cookie(cookie_id: str) -> Tuple[str, str]:
    try:
        from opportunity_detail import resolve_cookie
        return resolve_cookie(cookie_id, fallback_first=not cookie_id)
    except Exception:
        return "", ""


def _app_key_internal() -> str:
    try:
        from config import config
        return str(config.get("APP_CONFIG.app_key", ""))
    except Exception:
        return ""


def start_scheduler() -> bool:
    """启动定时复盘线程（幂等）。auto_scan 关着时线程空转，不产生任何请求。"""
    global _thread
    if _thread and _thread.is_alive():
        return False
    _tick_stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="shop-diagnosis")
    _thread.start()
    return True


__all__ = ["run", "diagnose", "classify", "item_signals", "shop_signals",
           "sales_signals", "fetch_my_items", "fetch_detail", "record_snapshot", "history",
           "first_seen_days", "prev_snapshot", "last_report", "status",
           "get_config", "save_config", "start_scheduler",
           "THRESHOLDS", "DEFAULT_CONFIG"]
