"""
热搜词 / 爆款词榜（B1）
========================

**先说清楚这是什么、不是什么。**

闲鱼没有"官方热搜词接口"，闲管家开放平台也明确不给（见八·十）。所以这里**不假装是平台官方热搜榜**。
做法是：拿几个种子词真搜一遍，把返回商品的**标题**当成语料做中文 n-gram 挖掘，
统计每个词出现在多少件商品里、这些商品的热度/价格/想要数是什么水平，
再用一个透明可解释的公式排序。词是从真实数据里"长"出来的，不是编的，但它是**推导榜**，不是官方榜。

三种用法：
1. **挖掘榜**（scan）：种子词 → 搜索 → 标题分词 → 出榜。默认 6 个种子、每个 1 页，约 2–4 分钟。
2. **验证**（verify）：对榜上某个词单独真搜一次，看供给量、价格带、真实想要数，
   判断这个词到底是"需求真旺"还是"只是大家都在挂"。
3. **定时**（默认关）：每天固定时间自动扫一轮，留历史快照，能看到词的热度在涨还是在跌。

安全口径（与 Gate 0 一致）：
- 干跑/默认关：`enabled=False`，只有手动点"开始扫描"才会跑；
- 搜索走统一限流 `rate_limiter`（1 次/10 秒）+ 风控守卫 `opportunity_guard`（连续失败/风控信号直接停）；
- 不写任何商品、不发消息、不改库存——这个模块**全程只读**，最坏结果是搜索被限流；
- 夜间不跑（23:00–08:00），避免和人抢浏览器。

评分公式（可解释，不玩黑箱）：
    supply   = 该词覆盖商品数（当前数据里可观测的在售供给代理）
    freq_r   = supply / 榜内最高 supply                    权重 20
    demand_r = min(1, log1p(平均想要数) / log1p(50))       权重 45
    opp_ratio = log1p(平均想要数) / log1p(supply)          权重 25
    opp_r    = opp_ratio / 榜内最高 opp_ratio
    cov_r    = 命中种子数 / 榜内最高                       权重 10
    score    = 20*freq_r + 45*demand_r + 25*opp_r + 10*cov_r

直白解释：**想要的人多、在售供给少、横跨更多品类** → 分更高。`freq_r` 仍保留少量权重，
用于识别真实成交语料里稳定出现的词；供需倍率按榜内最高值归一化，保证各项权重合计为 100。
供给为 0 时无法计算供需倍率，该项按 0 处理，不把未知供给伪造成无限机会。
命中高仿/违禁类词直接判 0 分：仍留在榜尾让你看见，但绝不推荐做。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# 测试可用环境变量指向临时库，绝不动真实 data/
CONFIG_FILE = Path(os.environ.get("HOT_KW_CONFIG") or (DATA_DIR / "hot_keyword_config.json"))
DB_FILE = Path(os.environ.get("HOT_KW_DB") or (DATA_DIR / "hot_keyword.db"))

VERIFY_TTL_HOURS = 12
# verify_word 只搜第一页。数满一页 ≠ 市场只有这么多，只说明真实供给 ≥ 这个数。
SUPPLY_PAGE_SIZE = 30
BLUE_OCEAN_WEIGHTS = {
    "freq_r": 20.0,
    "demand_r": 45.0,
    "opp_ratio": 25.0,
    "coverage_r": 10.0,
}
DEMAND_LOG_CAP = 50.0

DEFAULT_SEEDS = ["手机", "蓝牙耳机", "机械键盘", "球鞋", "手办", "咖啡机"]

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,          # 定时扫描，默认关
    "seeds": DEFAULT_SEEDS,
    "max_seeds": 10,
    "page_size": 30,
    "min_freq": 2,             # 至少出现在 2 件商品标题里才算候选词
    "top_n": 50,
    "detail_enrich": True,     # 是否给每个种子前几件补真实想要数
    "detail_limit": 3,         # 每个种子补几件（限流 12 次/分钟）
    "scan_hour": 9,
    "keep_days": 90,
    # 词池生命周期（拆解 8）。口径从 cumin777/xianyu-hotword-scout 借，
    # 但它的判据是"日均在售 / 供需比"，我们手上只有榜单分数——所以换成"上榜天数 + 均分"。
    "trial_days": 2,           # 连续上榜几天才算"验证过"
    "promote_score": 45.0,     # 转 active 的最低均分
    "retire_absent_days": 7,   # 连续几天没上榜 → 退休
    "retire_score": 20.0,      # 均分低于这条线……
    "retire_low_days": 5,      # ……且连续这么多天 → 退休
    "pool_cap": 200,           # active 池上限，满了末位淘汰
}

# --------------------------------------------------------------------------
# 分词：中文 n-gram + 英文 token
# --------------------------------------------------------------------------
_NOISE_URL = re.compile(r"https?://\S+|www\.\S+")
_NOISE_BRACKET = re.compile(r"[【\[（(][^】\]）)]{0,30}[】\]）)]")
_CJK_RUN = re.compile(r"[一-鿿]{2,}")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9+]{2,}")

# 单字停用（全部由它们组成时丢弃）
_STOP_CHARS = set("的了吗呢吧啊哦呀是和与及或在有无不也很太更最这那我你他她它们个件台只双套款张支把颗块元年月日号次点等上下就还都要会能可以给对从被让使些其该此某人东西")

# 出现在词首/词尾就基本不是词（"的""了""很"这类纯虚词）
_STOP_EDGE = set("的了吗呢吧啊哦呀是和与及或就很太更最这那我你他她它们")

# 整词停用：全是交易口水话，不是品类词
_STOP_WORDS = {
    "闲置", "全新", "包邮", "正品", "自用", "二手", "转卖", "低价", "秒发", "现货",
    "可小刀", "小刀", "议价", "支持", "顺丰", "当天", "发货", "如图", "九成新",
    "九五新", "成色", "功能", "正常", "无拆", "无修", "已拆封", "未拆封", "有货",
    "急出", "搬家", "换新", "升级", "个人", "出售", "代购", "专柜", "验货",
    "无理由", "退货", "到手", "合适", "需要", "联系", "私聊", "详情", "看货",
    "使用", "国行", "港版", "美版", "日版", "全网通", "配件", "赠品", "价格",
    "最低", "品牌", "型号", "颜色", "尺码", "款式", "小编", "图片", "东西",
    "以上", "左右", "一个", "两个", "三件", "便宜", "特价", "清仓", "女生",
    "男生", "学生", "礼物", "生日", "新款", "老款", "经典", "限量", "绝版",
    "喜欢", "看看", "有意", "速来", "先到先得", "手慢无", "懂的", "走心",
}

# 违禁/敏感：命中直接重罚（与选品引擎同一张表）
try:
    from opportunity_engine import SENSITIVE_WORDS
except Exception:  # pragma: no cover - 单测时可能没装依赖
    SENSITIVE_WORDS = ["高仿", "复刻", "A货", "精仿", "违禁", "烟", "电子烟",
                       "药", "处方", "枪", "刀"]


def clean_title(text: str) -> str:
    """去掉链接、成对的【】[]（）() 里的内容（多半是"包邮""急出"这类噪音）。"""
    s = str(text or "")
    s = _NOISE_URL.sub(" ", s)
    s = _NOISE_BRACKET.sub(" ", s)
    return s


def extract_phrases(text: str, min_len: int = 2, max_len: int = 4) -> set:
    """从一个标题里抽候选短语：中文 2–4 字 n-gram + 英文/型号 token。"""
    s = clean_title(text)
    out: set = set()
    for run in _CJK_RUN.findall(s):
        run = run[:40]  # 超长串不做，n-gram 会爆炸
        n_run = len(run)
        for n in range(min_len, max_len + 1):
            if n > n_run:
                break
            for i in range(n_run - n + 1):
                sub = run[i:i + n]
                if sub in _STOP_WORDS:
                    continue
                if all(ch in _STOP_CHARS for ch in sub):
                    continue
                # 首尾是纯虚词（"的""很""这"）基本不成词，直接丢
                if sub[0] in _STOP_EDGE or sub[-1] in _STOP_EDGE:
                    continue
                out.add(sub)
    for tok in _LATIN.findall(s):
        low = tok.lower()
        if len(low) >= 3 and low not in _STOP_WORDS:
            out.add(low)
    return out


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def _clamp(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def normalize_seeds(seeds: Any, max_seeds: int = 10) -> List[str]:
    """洗种子词：去空白、去重、限长、限量。空串直接丢。"""
    out: List[str] = []
    seen = set()
    for s in seeds or []:
        w = str(s or "").strip()
        if not w or len(w) > 20:
            continue
        key = w.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
        if len(out) >= max(0, max_seeds):
            break
    return out


def get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f) or {}
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    except Exception:
        pass
    cfg["max_seeds"] = _clamp(cfg.get("max_seeds"), 1, 20, 10)
    cfg["seeds"] = normalize_seeds(cfg.get("seeds") or DEFAULT_SEEDS, cfg["max_seeds"])
    cfg["page_size"] = _clamp(cfg.get("page_size"), 10, 30, 30)
    cfg["min_freq"] = _clamp(cfg.get("min_freq"), 2, 10, 2)
    cfg["top_n"] = _clamp(cfg.get("top_n"), 10, 100, 50)
    cfg["detail_limit"] = _clamp(cfg.get("detail_limit"), 0, 10, 3)
    cfg["scan_hour"] = _clamp(cfg.get("scan_hour"), 0, 23, 9)
    cfg["keep_days"] = _clamp(cfg.get("keep_days"), 7, 365, 90)
    cfg["detail_enrich"] = bool(cfg.get("detail_enrich"))
    cfg["enabled"] = bool(cfg.get("enabled"))
    # 生命周期那几项也收敛一次：手改配置文件写了 999 也不能把池子搞坏
    return _clean_numeric(cfg)


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """只接受白名单字段；非法值就地收敛，不抛异常（页面别因为一个输入框崩掉）。"""
    patch = patch or {}
    cfg = get_config()
    for key in DEFAULT_CONFIG:
        if key not in patch:
            continue
        val = patch[key]
        if key == "seeds":
            cfg["seeds"] = normalize_seeds(val, cfg["max_seeds"])
        elif key in ("enabled", "detail_enrich"):
            cfg[key] = bool(val)
        else:
            cfg[key] = val
    # 再走一遍收敛，把越界的数值拉回合法区间（页面填 999 也不会写进文件）
    cfg = _clean_numeric(cfg)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def _clean_numeric(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg)
    out["max_seeds"] = _clamp(out.get("max_seeds"), 1, 20, 10)
    out["page_size"] = _clamp(out.get("page_size"), 10, 30, 30)
    out["min_freq"] = _clamp(out.get("min_freq"), 2, 10, 2)
    out["top_n"] = _clamp(out.get("top_n"), 10, 100, 50)
    out["detail_limit"] = _clamp(out.get("detail_limit"), 0, 10, 3)
    out["scan_hour"] = _clamp(out.get("scan_hour"), 0, 23, 9)
    out["keep_days"] = _clamp(out.get("keep_days"), 7, 365, 90)
    out["trial_days"] = _clamp(out.get("trial_days"), 1, 14, 2)
    out["retire_absent_days"] = _clamp(out.get("retire_absent_days"), 2, 60, 7)
    out["retire_low_days"] = _clamp(out.get("retire_low_days"), 2, 30, 5)
    out["pool_cap"] = _clamp(out.get("pool_cap"), 20, 1000, 200)
    try:
        out["promote_score"] = max(0.0, min(100.0, float(out.get("promote_score"))))
    except (TypeError, ValueError):
        out["promote_score"] = 45.0
    try:
        out["retire_score"] = max(0.0, min(100.0, float(out.get("retire_score"))))
    except (TypeError, ValueError):
        out["retire_score"] = 20.0
    out["seeds"] = normalize_seeds(out.get("seeds"), out["max_seeds"])
    return out


# --------------------------------------------------------------------------
# 挖掘 + 评分（纯函数，便于单测）
# --------------------------------------------------------------------------
def mine_candidates(items: List[Dict[str, Any]], min_freq: int = 2) -> List[Dict[str, Any]]:
    """
    商品列表 → 候选词统计。

    每个商品只用一次自己的短语集合（freq = 覆盖商品数，不是出现次数），
    否则同一个标题里"耳机"出现两次会把频次灌水。
    """
    agg: Dict[str, Dict[str, Any]] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "")
        if not title:
            continue
        phrases = extract_phrases(title)
        if not phrases:
            continue
        heat = float(it.get("heat_score") or 0)
        price = it.get("price")
        # 0 是平台明确展示的“没人想要”，也是有效需求样本；只有缺失值才排除。
        raw_want = it.get("want_count")
        try:
            want = None if raw_want is None else int(raw_want)
        except (TypeError, ValueError):
            want = None
        if want is not None and want < 0:
            want = None
        sold = int(it.get("sold_count") or 0)
        seed = str(it.get("seed") or "")
        for p in phrases:
            row = agg.setdefault(p, {
                "word": p, "freq": 0, "seeds": set(),
                "heat_sum": 0.0, "prices": [], "wants": [], "solds": [],
                "titles": [],
            })
            row["freq"] += 1
            if seed:
                row["seeds"].add(seed)
            row["heat_sum"] += heat
            if isinstance(price, (int, float)) and price > 0:
                row["prices"].append(float(price))
            if want is not None:
                row["wants"].append(want)
            if sold > 0:
                row["solds"].append(sold)
            row["titles"].append((heat, title))

    out: List[Dict[str, Any]] = []
    for word, row in agg.items():
        if row["freq"] < max(2, int(min_freq)):
            continue
        prices = sorted(row["prices"])
        titles = sorted(row["titles"], key=lambda x: x[0], reverse=True)
        out.append({
            "word": word,
            "freq": row["freq"],
            "coverage": len(row["seeds"]),
            "seeds": sorted(row["seeds"]),
            "heat_avg": round(row["heat_sum"] / row["freq"], 1),
            "price_min": prices[0] if prices else None,
            "price_max": prices[-1] if prices else None,
            "price_avg": round(sum(prices) / len(prices), 1) if prices else None,
            "want_avg": round(sum(row["wants"]) / len(row["wants"]), 1) if row["wants"] else 0,
            "sold_avg": round(sum(row["solds"]) / len(row["solds"]), 1) if row["solds"] else 0,
            "examples": [t for _, t in titles[:2]],
        })
    return out


def _dedupe_substrings(cands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    "机械键盘"和"键盘"同时高频时只留长的那个。

    判据：短词被某个已保留的长词包含，且短词频次没有明显更高（≤1.2 倍）——
    说明它不是独立概念，只是长词的碎片。反过来，若短词频次远高（比如"键盘"覆盖 25 件、
    "机械键盘"只覆盖 3 件），那它是真上位词，两个都留。
    """
    kept: List[Dict[str, Any]] = []
    for c in sorted(cands, key=lambda x: (-len(x["word"]), -x["freq"])):
        dominated = False
        for k in kept:
            if c["word"] in k["word"] and c["freq"] <= k["freq"] * 1.2:
                dominated = True
                break
        if not dominated:
            kept.append(c)
    return kept


def score_words(cands: List[Dict[str, Any]], top_n: int = 50) -> List[Dict[str, Any]]:
    """按文档里的公式打分排序。空输入返回空列表，不做除零。"""
    cands = _dedupe_substrings(list(cands or []))
    if not cands:
        return []

    def non_negative(value: Any) -> float:
        try:
            number = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(number):
            return 0.0
        return max(0.0, number)

    prepared = []
    for c in cands:
        supply = non_negative(c.get("freq"))
        want_avg = non_negative(c.get("want_avg"))
        coverage = non_negative(c.get("coverage"))
        demand_log = math.log1p(want_avg)
        opp_ratio = demand_log / math.log1p(supply) if supply > 0 else None
        prepared.append({
            "candidate": c,
            "supply": supply,
            "coverage": coverage,
            "demand_log": demand_log,
            "opp_ratio": opp_ratio,
        })

    max_supply = max(row["supply"] for row in prepared) or 1.0
    max_coverage = max(row["coverage"] for row in prepared) or 1.0
    max_opp_ratio = max(
        (row["opp_ratio"] for row in prepared if row["opp_ratio"] is not None),
        default=0.0,
    ) or 1.0

    for row in prepared:
        c = row["candidate"]
        supply = row["supply"]
        coverage = row["coverage"]
        demand_log = row["demand_log"]
        opp_ratio = row["opp_ratio"]
        freq_r = min(1.0, supply / max_supply)
        coverage_r = min(1.0, coverage / max_coverage)
        demand_r = min(1.0, demand_log / math.log1p(DEMAND_LOG_CAP))
        opp_r = opp_ratio / max_opp_ratio if opp_ratio is not None else 0.0

        base = (
            BLUE_OCEAN_WEIGHTS["freq_r"] * freq_r
            + BLUE_OCEAN_WEIGHTS["demand_r"] * demand_r
            + BLUE_OCEAN_WEIGHTS["opp_ratio"] * opp_r
            + BLUE_OCEAN_WEIGHTS["coverage_r"] * coverage_r
        )
        risk = [w for w in SENSITIVE_WORDS if w and w in c["word"]]
        # 违禁词直接判 0 分而不是"扣一点"：高仿/烟/药这类词热度再高也不能拿来选品。
        # 仍然留在榜里（排最后），是为了让你看见"这些词被挖到了但被拒了"，而不是悄悄消失。
        score = 0.0 if risk else base
        c["risk_hits"] = risk
        c["opp_ratio"] = round(opp_ratio, 4) if opp_ratio is not None else None
        c["opp_ratio_r"] = round(opp_r, 4)
        c["score"] = round(score, 1)

    cands.sort(key=lambda x: (x["score"], x["freq"]), reverse=True)
    for i, c in enumerate(cands, 1):
        c["rank"] = i
    return cands[: max(1, min(int(top_n), 100))]


def build_board(items: List[Dict[str, Any]], min_freq: int = 2,
                top_n: int = 50) -> List[Dict[str, Any]]:
    """入口：商品列表 → 排序好的词榜。"""
    return score_words(mine_candidates(items, min_freq=min_freq), top_n=top_n)


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS word_snapshots (
            day        TEXT NOT NULL,
            word       TEXT NOT NULL,
            rank       INTEGER DEFAULT 0,
            freq       INTEGER DEFAULT 0,
            coverage   INTEGER DEFAULT 0,
            heat_avg   REAL DEFAULT 0,
            price_min  REAL,
            price_max  REAL,
            price_avg  REAL,
            want_avg   REAL DEFAULT 0,
            sold_avg   REAL DEFAULT 0,
            score      REAL DEFAULT 0,
            risk       TEXT DEFAULT '[]',
            seeds      TEXT DEFAULT '[]',
            examples   TEXT DEFAULT '[]',
            ts         TEXT NOT NULL,
            PRIMARY KEY (day, word)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kw_day_score "
                 "ON word_snapshots(day, score)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS verify_cache (
            word    TEXT PRIMARY KEY,
            ts      TEXT NOT NULL,
            payload TEXT NOT NULL
        )
    """)
    # 词池（拆解 8）：给"推导榜"加上记忆。原来每天重算一遍、算完就扔，
    # 一个连涨三天的稳定词和一个偶然蹦出来一次的词长得一模一样。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS word_pool (
            word        TEXT PRIMARY KEY,
            state       TEXT NOT NULL DEFAULT 'trial',   -- trial|active|retired
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            days_seen   INTEGER DEFAULT 0,
            score_avg   REAL DEFAULT 0,
            score_last  REAL DEFAULT 0,
            low_streak  INTEGER DEFAULT 0,
            state_since TEXT DEFAULT '',
            retire_reason TEXT DEFAULT '',
            updated_at  TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pool_state ON word_pool(state, score_avg)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pool_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            day     TEXT NOT NULL,
            word    TEXT NOT NULL,
            from_s  TEXT DEFAULT '',
            to_s    TEXT DEFAULT '',
            why     TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            ok      INTEGER DEFAULT 0,
            message TEXT DEFAULT '',
            seeds   TEXT DEFAULT '[]',
            items   INTEGER DEFAULT 0,
            words   INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def save_board(words: List[Dict[str, Any]], day: Optional[str] = None,
               keep_days: int = 90) -> int:
    """写当天词榜（同日重扫覆盖，不累积）。顺手清理过期快照。"""
    day = day or date.today().isoformat()
    ts = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        conn.execute("DELETE FROM word_snapshots WHERE day = ?", (day,))
        rows = [(
            day, w["word"], int(w.get("rank") or 0), int(w.get("freq") or 0),
            int(w.get("coverage") or 0), float(w.get("heat_avg") or 0),
            w.get("price_min"), w.get("price_max"), w.get("price_avg"),
            float(w.get("want_avg") or 0), float(w.get("sold_avg") or 0),
            float(w.get("score") or 0),
            json.dumps(w.get("risk_hits") or [], ensure_ascii=False),
            json.dumps(w.get("seeds") or [], ensure_ascii=False),
            json.dumps(w.get("examples") or [], ensure_ascii=False),
            ts,
        ) for w in words]
        conn.executemany(
            "INSERT OR REPLACE INTO word_snapshots (day, word, rank, freq, coverage, "
            "heat_avg, price_min, price_max, price_avg, want_avg, sold_avg, score, "
            "risk, seeds, examples, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("DELETE FROM word_snapshots WHERE day < date(?, ?)",
                     (day, f"-{max(7, int(keep_days))} days"))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 词池生命周期（拆解 8：cumin777/xianyu-hotword-scout 的 trial → active → retired）
# --------------------------------------------------------------------------
# 它解决的问题：榜单每天重算一遍、算完就扔，于是"连续 7 天前 10 名的稳定词"和
# "今天偶然蹦上来一次、明天就掉 40 名的噪声词"在数据里长得一模一样。选品要靠前者，
# 而后者会把预算骗走。它的做法是给词一个状态机 + 容量上限。
#
# 三处**故意不同**：
# 1. 它的判据是"日均在售 / 供需比"，那需要每天对每个词真搜一次（词多就是几千次请求）。
#    我们只有榜单分数，所以判据换成"上榜天数 + 均分"——少一个维度的信息，
#    但零额外请求，且完全可解释。
# 2. 它的 retired 永不回炉（同市场去重时含 retired）。我们照做，但留一个
#    `revive_word()` 人工出口：算法判错时人得能把它拉回来，否则只能删库。
# 3. 它按天全量重算；我们同一天重复扫描只更新分数和 last_seen，
#    不重复累加 days_seen（否则下午再扫一次就白送一天）。
#
# 状态语义：
#   trial    新词，先看几天；攒够 trial_days 天后按均分决定转正还是退休
#   active   已验证，正式参与选品；连续低分或连续缺席会被退休
#   retired  已验证不行的词，不再自动回到池里（人可 revive）

POOL_STATES = ("trial", "active", "retired")


def _conn_pool_upsert(conn, word: str, day: str, score: float, ts: str) -> None:
    row = conn.execute("SELECT * FROM word_pool WHERE word = ?", (word,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO word_pool (word, state, first_seen, last_seen, days_seen, "
            "score_avg, score_last, low_streak, state_since, updated_at) "
            "VALUES (?, 'trial', ?, ?, 1, ?, ?, 0, ?, ?)",
            (word, day, day, score, score, day, ts))
        return
    d = dict(row)
    if d["last_seen"] == day:
        # 同日重扫：只更新分数，不重复计天（否则一天扫三次 = 三天）。
        # 均分要**先把今天早上记的旧分减掉**再加新分：直接累加会让均分越扫越高，
        # 一个词上午 50 分、下午重扫 60 分，均分就变成 55 —— 它其实还是同一个 60 分的词。
        days = max(1, int(d["days_seen"] or 0))
        base = float(d["score_avg"] or 0) * days - float(d["score_last"] or 0)
        avg = (base + score) / days
        conn.execute(
            "UPDATE word_pool SET score_last=?, score_avg=?, updated_at=? WHERE word=?",
            (score, round(avg, 2), ts, word))
        return
    days = int(d["days_seen"] or 0) + 1
    avg = (float(d["score_avg"] or 0) * int(d["days_seen"] or 0) + score) / max(1, days)
    conn.execute(
        "UPDATE word_pool SET last_seen=?, days_seen=?, score_avg=?, score_last=?, "
        "updated_at=? WHERE word=?",
        (day, days, round(avg, 2), score, ts, word))


def _pool_set_state(conn, word: str, state: str, day: str, why: str, ts: str) -> None:
    conn.execute(
        "UPDATE word_pool SET state=?, state_since=?, retire_reason=?, updated_at=? "
        "WHERE word=?", (state, day, why if state == "retired" else "", ts, word))


def _evict_weakest(conn, day: str, ts: str, cap: int) -> str:
    """active 池满 → 淘汰最弱者（均分最低；没分的排最前）。种子词不占池。"""
    rows = conn.execute(
        "SELECT word, score_avg FROM word_pool WHERE state='active' "
        "ORDER BY score_avg ASC, days_seen ASC").fetchall()
    if len(rows) < cap:
        return ""
    weak = rows[0]
    _pool_set_state(conn, weak["word"], "retired", day,
                    f"池满({cap})末位让位，均分 {float(weak['score_avg'] or 0):.1f}", ts)
    conn.execute("INSERT INTO pool_log (day, word, from_s, to_s, why) VALUES (?,?,?,?,?)",
                 (day, weak["word"], "active", "retired",
                  f"池满末位让位（均分 {float(weak['score_avg'] or 0):.1f}）"))
    return weak["word"]


def update_pool(words: List[Dict[str, Any]], day: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    用今天的榜单更新词池，返回状态变化（给日报/页面看）。

    顺序很重要：**先记今天的数，再判迁移，最后判缺席**。反过来会把
    "今天刚上来但分数还不够"的词误判成"连续缺席"。
    """
    cfg = get_config()
    day = day or date.today().isoformat()
    ts = datetime.now().isoformat(timespec="seconds")
    actions: List[Dict[str, Any]] = []
    conn = _conn()
    try:
        seen_today = set()
        for w in words or []:
            word = str(w.get("word") or "").strip()
            if not word:
                continue
            score = float(w.get("score") or 0)
            # 违禁词不进池：它们分数被强制为 0，进了池只会占容量、还会误导"这个词验证过"。
            # 已经在池里的，今天一旦命中就当场退役——不等缺席判定，这种词一天都不该留在"在用"里。
            if score <= 0 or w.get("risk_hits"):
                row = conn.execute("SELECT state FROM word_pool WHERE word=?", (word,)).fetchone()
                if row and row["state"] != "retired":
                    why = "命中违禁词：" + "、".join(str(x) for x in (w.get("risk_hits") or []))[:60]
                    conn.execute("INSERT INTO pool_log (day, word, from_s, to_s, why) "
                                 "VALUES (?,?,?,?,?)", (day, word, str(row["state"]), "retired", why))
                    _pool_set_state(conn, word, "retired", day, why, ts)
                    actions.append({"word": word, "from": str(row["state"]),
                                    "to": "retired", "why": why})
                continue
            seen_today.add(word)
            _conn_pool_upsert(conn, word, day, score, ts)

        # 1) trial：攒够天数就评一次（转正 or 退休）
        for r in conn.execute("SELECT * FROM word_pool WHERE state='trial'").fetchall():
            d = dict(r)
            if int(d["days_seen"] or 0) < int(cfg["trial_days"]):
                continue
            avg = float(d["score_avg"] or 0)
            if avg >= float(cfg["promote_score"]):
                evicted = _evict_weakest(conn, day, ts, int(cfg["pool_cap"]))
                _pool_set_state(conn, d["word"], "active", day, "", ts)
                why = f"trial 达标（{d['days_seen']} 天均分 {avg:.1f}）"
                if evicted:
                    why += f"，挤出 {evicted}"
                conn.execute("INSERT INTO pool_log (day, word, from_s, to_s, why) "
                             "VALUES (?,?,?,?,?)", (day, d["word"], "trial", "active", why))
                actions.append({"word": d["word"], "from": "trial", "to": "active", "why": why})
            else:
                why = f"trial 未达标（{d['days_seen']} 天均分 {avg:.1f} < {cfg['promote_score']:g}）"
                _pool_set_state(conn, d["word"], "retired", day, why, ts)
                conn.execute("INSERT INTO pool_log (day, word, from_s, to_s, why) "
                             "VALUES (?,?,?,?,?)", (day, d["word"], "trial", "retired", why))
                actions.append({"word": d["word"], "from": "trial", "to": "retired", "why": why})

        # 2) active：连续低分 → 退休。今天没上榜的不在这里判（交给第 3 步的缺席判定），
        #    否则一次扫描失败就会被当成"词不行了"。
        for r in conn.execute("SELECT * FROM word_pool WHERE state='active'").fetchall():
            d = dict(r)
            if d["word"] not in seen_today:
                continue
            low = float(d["score_last"] or 0) < float(cfg["retire_score"])
            streak = int(d["low_streak"] or 0) + (1 if low else 0)
            conn.execute("UPDATE word_pool SET low_streak=?, updated_at=? WHERE word=?",
                         (streak, ts, d["word"]))
            if streak >= int(cfg["retire_low_days"]):
                why = f"连续 {streak} 天低于 {cfg['retire_score']:g} 分"
                _pool_set_state(conn, d["word"], "retired", day, why, ts)
                conn.execute("INSERT INTO pool_log (day, word, from_s, to_s, why) "
                             "VALUES (?,?,?,?,?)", (day, d["word"], "active", "retired", why))
                actions.append({"word": d["word"], "from": "active", "to": "retired", "why": why})

        # 3) 缺席：连续 N 天没上榜的词自然消退（trial/active 都算）。
        #    "没上榜"比"分数低"更硬：连榜都进不去，说明市场已经没它了。
        cutoff_days = int(cfg["retire_absent_days"])
        for r in conn.execute("SELECT * FROM word_pool WHERE state IN ('trial','active')").fetchall():
            d = dict(r)
            if d["word"] in seen_today:
                continue
            try:
                gap = (date.fromisoformat(day) - date.fromisoformat(d["last_seen"])).days
            except Exception:
                continue
            if gap >= cutoff_days:
                why = f"连续 {gap} 天未上榜"
                conn.execute("INSERT INTO pool_log (day, word, from_s, to_s, why) "
                             "VALUES (?,?,?,?,?)", (day, d["word"], d["state"], "retired", why))
                _pool_set_state(conn, d["word"], "retired", day, why, ts)
                actions.append({"word": d["word"], "from": d["state"], "to": "retired", "why": why})

        conn.commit()
        return actions
    finally:
        conn.close()


def pool_overview(state: str = "", limit: int = 200) -> Dict[str, Any]:
    """词池概览：三个状态各多少、各自最强的几个。"""
    conn = _conn()
    try:
        counts = {s: 0 for s in POOL_STATES}
        for r in conn.execute("SELECT state, COUNT(*) AS n FROM word_pool GROUP BY state"):
            counts[str(r["state"])] = int(r["n"])
        sql = ("SELECT * FROM word_pool" + (" WHERE state=?" if state else "")
               + " ORDER BY score_avg DESC, days_seen DESC LIMIT ?")
        args = ([state] if state else []) + [max(1, min(int(limit), 500))]
        rows = [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]
        return {"counts": counts, "words": rows}
    finally:
        conn.close()


def pool_log(limit: int = 50) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM pool_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()]
    finally:
        conn.close()


def seed_from_pool() -> Dict[str, Any]:
    """
    把 active 词按均分顺序补进种子词（不覆盖，只填空位）。

    为什么是"补"不是"替换"：用户手写的种子词往往是他真正在做的品类，
    算法不该把它顶掉。空位用完就停，返回加了哪几个，页面上看得见。
    这也是词池唯一的出口 —— 没有这一步，词池就只是个好看的表。
    """
    cfg = get_config()
    seeds = list(cfg.get("seeds") or [])
    have = {str(x).strip().lower() for x in seeds}
    room = max(0, int(cfg["max_seeds"]) - len(seeds))
    if room <= 0:
        return {"ok": True, "added": [], "seeds": seeds,
                "message": f"种子词已满（{cfg['max_seeds']} 个），先删掉几个再来"}
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT word FROM word_pool WHERE state='active' "
            "ORDER BY score_avg DESC, days_seen DESC LIMIT ?", (int(cfg["pool_cap"]),)).fetchall()
    finally:
        conn.close()
    added: List[str] = []
    for r in rows:
        w = str(r["word"] or "").strip()
        if not w or w.lower() in have:
            continue
        # 同市场去重：已经有个种子词包含它（或它包含种子词）就跳过，
        # 否则"键盘"和"机械键盘"会同时占两个种子位、搜出同一批商品。
        if any(w in s0 or s0 in w for s0 in have if s0):
            continue
        seeds.append(w)
        have.add(w.lower())
        added.append(w)
        if len(added) >= room:
            break
    if added:
        save_config({"seeds": seeds})
    return {"ok": True, "added": added, "seeds": seeds,
            "message": (f"补了 {len(added)} 个在用词" if added else "没有可补的词（都已在种子里）")}


def revive_word(word: str, why: str = "人工恢复") -> bool:
    """人工把 retired 的词拉回 trial。算法判错时唯一的出口，否则只能删库。"""
    word = str(word or "").strip()
    if not word:
        return False
    ts = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        row = conn.execute("SELECT state FROM word_pool WHERE word=?", (word,)).fetchone()
        if not row or row["state"] == "active":
            return False
        conn.execute("UPDATE word_pool SET state='trial', state_since=?, retire_reason='', "
                     "days_seen=1, score_avg=COALESCE(score_last,0), low_streak=0, "
                     "updated_at=? WHERE word=?",
                     (date.today().isoformat(), ts, word))
        conn.execute("INSERT INTO pool_log (day, word, from_s, to_s, why) VALUES (?,?,?,?,?)",
                     (date.today().isoformat(), word, str(row["state"]), "trial",
                      str(why or "")[:120]))
        conn.commit()
        return True
    finally:
        conn.close()


def get_board(limit: int = 50, day: Optional[str] = None) -> Dict[str, Any]:
    """取词榜：不传 day 就取最近一次有数据的那天。"""
    conn = _conn()
    try:
        if not day:
            row = conn.execute("SELECT MAX(day) AS d FROM word_snapshots").fetchone()
            day = row["d"] if row and row["d"] else None
        if not day:
            return {"day": None, "words": [], "count": 0}
        rows = conn.execute(
            "SELECT * FROM word_snapshots WHERE day = ? ORDER BY score DESC LIMIT ?",
            (day, max(1, min(int(limit), 200)))).fetchall()
        words = []
        for r in rows:
            d = dict(r)
            for k in ("risk", "seeds", "examples"):
                try:
                    d[k if k != "risk" else "risk_hits"] = json.loads(d.pop(k) or "[]")
                except Exception:
                    d[k if k != "risk" else "risk_hits"] = []
            words.append(d)
        return {"day": day, "words": words, "count": len(words)}
    finally:
        conn.close()


def board_history(limit_days: int = 30) -> List[Dict[str, Any]]:
    """按天返回榜单条数最高分，用于页面画"词榜生命力"。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT day, COUNT(*) AS n, MAX(score) AS top FROM word_snapshots "
            "GROUP BY day ORDER BY day DESC LIMIT ?", (max(1, int(limit_days)),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def word_trend(word: str, limit_days: int = 30) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT day, score, freq, heat_avg FROM word_snapshots WHERE word = ? "
            "ORDER BY day DESC LIMIT ?", (word, max(1, int(limit_days)))).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def _log_scan(ok: bool, message: str, seeds: List[str], items: int, words: int) -> None:
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO scan_log (ts, ok, message, seeds, items, words) "
                "VALUES (?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), 1 if ok else 0,
                 message[:300], json.dumps(seeds, ensure_ascii=False), items, words))
            conn.execute("DELETE FROM scan_log WHERE id NOT IN "
                         "(SELECT id FROM scan_log ORDER BY id DESC LIMIT 100)")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def scan_logs(limit: int = 20) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM scan_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["seeds"] = json.loads(d.get("seeds") or "[]")
            except Exception:
                d["seeds"] = []
            out.append(d)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 扫描（唯一会碰网络的地方）
# --------------------------------------------------------------------------
_state: Dict[str, Any] = {
    "running": False, "progress": "", "done": 0, "total": 0,
    "started": "", "last": None,
}
_thread: Optional[threading.Thread] = None
_tick_stop = threading.Event()


def _night_ban() -> bool:
    now = datetime.now()
    return now.hour >= 23 or now.hour < 8


def _app_key() -> str:
    try:
        from config import config
        return str(config.get("APP_CONFIG.app_key", "") or "")
    except Exception:
        return ""


async def _scan_async(seeds: List[str]) -> Dict[str, Any]:
    from utils.item_search import search_xianyu_items
    from opportunity_engine import normalize_item, score_item
    import rate_limiter
    from opportunity_guard import check_allowed, record_success, record_failure

    cfg = get_config()
    items: List[Dict[str, Any]] = []
    failures = 0
    message = ""
    cookie_id, cookie_str = "", ""
    if cfg.get("detail_enrich"):
        try:
            from opportunity_detail import resolve_cookie
            cookie_id, cookie_str = resolve_cookie("", fallback_first=True)
        except Exception:
            pass

    for i, seed in enumerate(seeds, 1):
        _state.update(progress=f"搜索「{seed}」（{i}/{len(seeds)}）", done=i - 1,
                      total=len(seeds))
        allowed, reason = check_allowed()
        if not allowed:
            message = f"风控守卫拦下：{reason}"
            break
        ok, _retry = rate_limiter.acquire("search", "global")
        if not ok:
            rate_limiter.wait("search", "global", max_wait=30)
        try:
            result = await search_xianyu_items(keyword=seed, page=1,
                                               page_size=cfg["page_size"])
        except Exception as e:
            failures += 1
            record_failure(str(e))
            message = f"搜索「{seed}」异常：{str(e)[:100]}"
            if failures >= 2:
                break
            continue
        if result.get("error") and not result.get("items"):
            failures += 1
            record_failure(str(result.get("error")))
            message = f"搜索「{seed}」失败：{str(result.get('error'))[:100]}"
            if failures >= 2:
                break
            continue
        record_success()
        failures = 0
        batch = [normalize_item(r, keyword=seed) for r in (result.get("items") or [])
                 if isinstance(r, dict)]
        for it in batch:
            it["seed"] = seed
            it["heat_score"] = score_item(it)["heat_score"]
        if cfg.get("detail_enrich") and cookie_str and cfg.get("detail_limit"):
            try:
                from opportunity_detail import enrich_items
                enrich_items(batch[:cfg["detail_limit"]], cookie_str, _app_key(),
                             cookie_id=cookie_id, limit=cfg["detail_limit"])
                for it in batch[:cfg["detail_limit"]]:
                    it["heat_score"] = score_item(it)["heat_score"]
            except Exception:
                pass
        items.extend(batch)
        await asyncio.sleep(0)  # 让出事件循环

    words = build_board(items, min_freq=cfg["min_freq"], top_n=cfg["top_n"])
    if words:
        save_board(words, keep_days=cfg["keep_days"])
        # 词池跟着更新（拆解 8）。放在保存榜单之后：榜单是当天的原始快照，
        # 词池是跨天的判断。两者写失败互不影响——生命周期坏了不该让榜单丢。
        try:
            _state["transitions"] = update_pool(words)
        except Exception as e:
            _state["transitions"] = []
            _state["pool_error"] = str(e)[:150]
    _state.update(done=len(seeds), progress="")
    ok = bool(words)
    msg = message or (f"扫描完成：{len(items)} 件商品，出榜 {len(words)} 个词" if ok
                      else "没有搜到可用商品，词榜为空")
    _log_scan(ok, msg, seeds, len(items), len(words))
    _state["last"] = {"ts": datetime.now().isoformat(timespec="seconds"),
                      "ok": ok, "message": msg, "items": len(items),
                      "words": len(words), "seeds": seeds}
    return _state["last"]


def _run_scan_thread(seeds: List[str]) -> None:
    try:
        asyncio.run(_scan_async(seeds))
    except Exception as e:
        _log_scan(False, f"扫描线程异常：{str(e)[:150]}", seeds, 0, 0)
        _state["last"] = {"ts": datetime.now().isoformat(timespec="seconds"),
                          "ok": False, "message": f"扫描线程异常：{str(e)[:150]}"}
    finally:
        _state.update(running=False, progress="", done=0, total=0)


def start_scan(seeds: Optional[List[str]] = None) -> Dict[str, Any]:
    """手动触发一轮扫描（后台线程）。已经有任务在跑就拒绝，不排队。"""
    global _thread
    if _state.get("running"):
        return {"ok": False, "error": "已有扫描在进行，请等它跑完"}
    if _night_ban():
        return {"ok": False, "error": "当前处于夜间时段（23:00–08:00），已禁止扫描"}
    cfg = get_config()
    seed_list = normalize_seeds(seeds or cfg["seeds"], cfg["max_seeds"])
    if not seed_list:
        return {"ok": False, "error": "种子词为空，请先配置至少一个"}
    _state.update(running=True, progress="准备中", done=0, total=len(seed_list),
                  started=datetime.now().isoformat(timespec="seconds"))
    _thread = threading.Thread(target=_run_scan_thread, args=(seed_list,),
                               daemon=True, name="hot-keyword-scan")
    _thread.start()
    return {"ok": True, "seeds": seed_list, "total": len(seed_list)}


# --------------------------------------------------------------------------
# 单词语验证（真实搜索）
# --------------------------------------------------------------------------
def _cached_verify(word: str) -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute("SELECT ts, payload FROM verify_cache WHERE word = ?",
                           (word,)).fetchone()
        if not row:
            return None
        ts = datetime.fromisoformat(row["ts"])
        if (datetime.now() - ts).total_seconds() > VERIFY_TTL_HOURS * 3600:
            return None
        payload = json.loads(row["payload"])
        payload["cached"] = True
        return payload
    except Exception:
        return None
    finally:
        conn.close()


def _save_verify(word: str, payload: Dict[str, Any]) -> None:
    try:
        conn = _conn()
        try:
            conn.execute("INSERT OR REPLACE INTO verify_cache (word, ts, payload) "
                         "VALUES (?,?,?)",
                         (word, datetime.now().isoformat(timespec="seconds"),
                          json.dumps(payload, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _supply_basis(supply: int, cap: int = SUPPLY_PAGE_SIZE) -> Dict[str, Any]:
    """
    给"供给数"标口径。

    只搜第一页：`supply_sampled` 是真实数到的件数，`cap` 是这一页的上限。
    数满一页说明市场供给 **≥ cap**（饱和），绝不把 30 当成市场总量。
    `supply_truncated` 是旧字段名，留给已落盘的缓存和下游兼容。
    """
    n = max(0, int(supply or 0))
    cap = max(1, int(cap or SUPPLY_PAGE_SIZE))
    saturated = n >= cap
    return {
        "supply_sampled": n,
        "supply_cap": cap,
        "supply_saturated": saturated,
        "supply_truncated": saturated,
    }


def _backfill_basis(payload: Dict[str, Any]) -> Dict[str, Any]:
    """12 小时缓存可能是旧版写的，读出来补齐口径字段，不让页面显示 undefined。"""
    out = dict(payload)
    cap = int(out.get("supply_cap") or SUPPLY_PAGE_SIZE)
    basis = _supply_basis(out.get("supply_sampled", out.get("supply", 0)), cap)
    for k, v in basis.items():
        out.setdefault(k, v)
    out.setdefault("want_sampled", 0)
    return out


def _verify_judgement(gap: Optional[float], want_source: str, supply: int,
                      saturated: bool) -> str:
    """判断句 + 样本口径。口径必须跟着结论走，不能只在页面上补一行小字。"""
    if supply <= 0:
        return "第一页 0 件（可能限流或词太偏），无法判断（样本口径：第一页 0 件）"
    if want_source == "none" or gap is None:
        core = "没有可信想要数（缺 Cookie 或详情失败），建议先补数再判断"
    elif gap >= 3:
        core = "需求明显大于供给，值得做"
    elif gap >= 1:
        core = "供需还行，看价格带决定"
    else:
        core = "供给充足、需求一般，谨慎"
    if saturated:
        return core + f"（样本口径：第一页 {supply} 件已满，真实在售 ≥{supply}）"
    return core + f"（样本口径：第一页 {supply} 件已见底）"


async def verify_word(word: str, cookie_id: str = "") -> Dict[str, Any]:
    """
    对单个词真搜一次，给出供给量/价格带/真实想要数。

    搜索卡片上的「N人想要」标签与详情接口的 wantCnt 都可作为需求信号。
    详情接口遇风控时保留搜索卡片值；两边都没有才明确说"没有可信想要数"，
    绝不拿字段里的假 0 冒充。

    供给也只数第一页：返回里 `supply_sampled` 是数到的件数，`supply_saturated`
    表示数满一页（真实供给 ≥ 这个数），`gap` 只是"第一页样本想要/在售比"，
    不是全市场供需缺口。
    """
    word = str(word or "").strip()
    if not word:
        return {"ok": False, "error": "词语为空"}
    cached = _cached_verify(word)
    if cached:
        return {"ok": True, **_backfill_basis(cached)}

    from utils.item_search import search_xianyu_items
    from opportunity_engine import normalize_item, score_item
    from opportunity_guard import check_allowed, record_success, record_failure
    import rate_limiter

    allowed, reason = check_allowed()
    if not allowed:
        return {"ok": False, "error": reason}
    ok, _retry = rate_limiter.acquire("search", "global")
    if not ok:
        rate_limiter.wait("search", "global", max_wait=30)

    result = await search_xianyu_items(keyword=word, page=1,
                                      page_size=SUPPLY_PAGE_SIZE)
    if result.get("error") and not result.get("items"):
        record_failure(str(result.get("error")))
        return {"ok": False, "error": str(result.get("error"))[:200]}
    record_success()

    items = [normalize_item(r, keyword=word) for r in (result.get("items") or [])
             if isinstance(r, dict)]
    for it in items:
        it["heat_score"] = score_item(it)["heat_score"]

    cookie_str = ""
    try:
        from opportunity_detail import resolve_cookie
        cookie_id, cookie_str = resolve_cookie(cookie_id, fallback_first=True)
    except Exception:
        pass
    want_source = "none"
    if cookie_str and items:
        try:
            from opportunity_detail import enrich_items
            stats = enrich_items(items[:5], cookie_str, _app_key(),
                                 cookie_id=cookie_id, limit=5)
            if stats.get("success"):
                want_source = "detail"
        except Exception:
            pass

    prices = sorted([float(i["price"]) for i in items
                     if isinstance(i.get("price"), (int, float)) and i["price"] > 0])
    want_values = [
        int(i["want_count"])
        for i in items
        if i.get("want_source") in ("search", "detail")
        and i.get("want_stale") is not True
        and i.get("want_count") is not None
    ]
    solds = [int(i.get("sold_count") or 0) for i in items if int(i.get("sold_count") or 0) > 0]

    supply = len(items)
    basis = _supply_basis(supply)
    search_samples = sum(
        1 for i in items
        if i.get("want_source") == "search" and i.get("want_stale") is not True
    )
    detail_samples = sum(
        1 for i in items
        if i.get("want_source") == "detail" and i.get("want_stale") is not True
    )
    want_sampled = len(want_values)
    if search_samples and detail_samples:
        want_source = "mixed"
    elif search_samples:
        want_source = "search"
    elif detail_samples:
        want_source = "detail"
    else:
        want_source = "none"
    want_med = sorted(want_values)[len(want_values) // 2] if want_values else None
    sold_avg = round(sum(solds) / len(solds), 1) if solds else 0
    price_med = prices[len(prices) // 2] if prices else None
    # 只是第一页样本的想要/在售比：分子只有前 5 件补到的想要数，
    # 分母是第一页件数。能看方向，不能当全市场供需缺口。
    gap = round(want_med / supply, 2) if supply and want_med is not None else None
    judgement = _verify_judgement(gap, want_source, supply,
                                  basis["supply_saturated"])

    payload = {
        "word": word,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "supply": supply,
        **basis,
        "want_sampled": want_sampled,
        "want_search_samples": search_samples,
        "want_detail_samples": detail_samples,
        "price_min": prices[0] if prices else None,
        "price_max": prices[-1] if prices else None,
        "price_median": price_med,
        "want_median": want_med,
        "want_max": max(want_values) if want_values else None,
        "want_source": want_source,
        "sold_avg": sold_avg,
        "gap": gap,
        "gap_label": "第一页样本想要/在售比",
        "judgement": judgement,
        "cached": False,
    }
    _save_verify(word, payload)
    return {"ok": True, **payload}


# --------------------------------------------------------------------------
# 状态 / 调度
# --------------------------------------------------------------------------
def _pool_counts() -> Dict[str, int]:
    """词池三个状态各多少。查不到就返回全 0，页面不该因此空白。"""
    out = {s: 0 for s in POOL_STATES}
    try:
        for r in _conn().execute("SELECT state, COUNT(*) AS n FROM word_pool GROUP BY state"):
            out[str(r["state"])] = int(r["n"])
    except Exception:
        pass
    return out


def status() -> Dict[str, Any]:
    board = get_board(limit=1)
    today = date.today().isoformat()
    return {
        "config": get_config(),
        "running": bool(_state.get("running")),
        "progress": _state.get("progress", ""),
        "done": _state.get("done", 0),
        "total": _state.get("total", 0),
        "last_scan": _state.get("last"),
        "board_day": board.get("day"),
        "board_count": board.get("count", 0),
        "board_today": board.get("day") == today,
        "logs": scan_logs(limit=10),
        "history": board_history(limit_days=14),
        "night_ban": _night_ban(),
        "pool": _pool_counts(),
        "transitions": _state.get("transitions") or [],
    }


def _loop() -> None:
    if _tick_stop.wait(90):
        return
    import task_watchdog as _wd
    _wd.register("hot-keywords", 600, "热门词扫描调度（每 10 分钟看一次是否到点）")
    last_run_day = ""
    while not _tick_stop.is_set():
        try:
            cfg = get_config()
            now = datetime.now()
            today = now.date().isoformat()
            if (cfg["enabled"] and now.hour == cfg["scan_hour"]
                    and last_run_day != today and not _state.get("running")):
                last_run_day = today
                start_scan(cfg["seeds"])
        except Exception:
            pass
        _wd.beat("hot-keywords")
        if _tick_stop.wait(600):
            return


def start_scheduler() -> bool:
    if _thread and _thread.is_alive():
        return False
    _tick_stop.clear()
    t = threading.Thread(target=_loop, daemon=True, name="hot-keyword-scheduler")
    t.start()
    return True


__all__ = [
    "DEFAULT_SEEDS", "DEFAULT_CONFIG", "clean_title", "extract_phrases",
    "mine_candidates", "score_words", "build_board", "normalize_seeds",
    "get_config", "save_config", "save_board", "get_board", "board_history",
    "word_trend", "scan_logs", "start_scan", "verify_word", "status",
    "start_scheduler", "update_pool", "pool_overview", "pool_log", "revive_word",
    "seed_from_pool", "POOL_STATES",
]
