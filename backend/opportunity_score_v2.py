"""
选品评分 v2（B4）
=================
替换 v1 的绝对分公式：`heat = min(100, 20 + want*2 + view//20 + sold*6)`。

**v1 为什么必须换掉（实测，不是猜）**：
- 冷门词（最大 want=8）：want 从 0 加到 8，分数从 86 涨到 100 —— 全在推荐区
- 热门词（want 50~500）：**全部 100 分**，完全无法区分

原因：v1 是**绝对公式**，没有"这个词下什么叫高"的参照系，也没有归一化。
同一个 want=50，在冷门词里是天花板，在热门词里是垫底，但 v1 给同一个分。

v2 的四件事：

1. **漏斗率**（最有信息量的指标）
   `想要/浏览` 反映"看到的人里有多少动了心"，`成交/想要` 反映"动心的人里有多少掏了钱"。
   绝对值受品类体量影响，比率跨品类可比。这是 B2 四维漏斗的直接延伸。

2. **分层加权 + 归一化**
   每个维度在当前结果集内做**分位数归一化**，再按权重加权。
   这样"这个词里的相对位置"才决定分数，而不是绝对值。
   权重分三层：转化层（想要率/成交率）> 需求层（想要/成交绝对量）> 曝光层（浏览）。

3. **样本自适应**
   n 小时（结果条数少）时分位数不稳，自动退化为绝对分 + 降低置信度；
   n 大时才相信相对排名。并且**样本不足的直接不参与排名**，而不是给个假分。

4. **风险扣分（分档，不是一次性）**
   v1 是 `风险词数 × 18` 一刀切。v2 分为：违禁（直接出局）> 侵权 > 灰色 > 擦边。
   违禁词命中直接 0 分并标记 `blocked`，不再让它"高分但被扣一点"。

**排序必须带理由**（来自 AutoJs6 的 Ranker/Explanation 与 `codegen` 的
"ranking must carry reasons"）：
每个结果都返回 `reasons` 数组，说清"为什么排在这"。没有理由的分数不可调试。

回滚：环境变量 `OPPORTUNITY_SCORE_V2=0` 或 `get_config()["score_version"]="v1"`
即回到 v1 公式。
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---- 风险分档（比 v1 的一刀切更细，且严重档直接出局）----
# 违禁：命中即 0 分 + blocked，不进推荐榜（宁可漏，不要上架被封）
RISK_BLOCK = (
    "高仿", "复刻", "A货", "精仿", "莆田", "原单", "尾单", "外贸原单",
    "违禁", "假货", "山寨", "仿品", "防品",
)
# 侵权：知名品牌词（卖正品也可能被判定为侵权，需要人工判断）
RISK_COPYRIGHT = (
    "耐克", "Nike", "阿迪", "Adidas", "苹果", "Apple", "华为", "小米",
    "路易威登", "LV", "古驰", "Gucci", "香奈儿", "Chanel", "爱马仕", "Hermes",
)
# 灰色：平台限制类（不一定违规，但容易被下架/限流）
RISK_GRAY = (
    "烟", "电子烟", "药", "处方", "枪", "刀", "弩", "管制",
    "破解", "激活码", "账号", "代练", "点卡", "充值",
    "减肥", "丰胸", "壮阳", "保健",
)
# 擦边：只是需要留意（不扣分，只在 reasons 里提示）
RISK_HINT = ("二手", "拆封", "无盒", "散装", "渠道货", "水货")

RISK_WEIGHTS = {"block": 100.0, "copyright": 25.0, "gray": 12.0, "hint": 0.0}

# ---- 维度权重（转化层 > 需求层 > 曝光层）----
WEIGHTS = {
    "want_rate": 0.34,      # 想要/浏览
    "deal_rate": 0.26,      # 成交/想要
    "want_abs": 0.18,       # 想要数（绝对需求体量）
    "sold_abs": 0.12,       # 成交数（已验证的变现能力）
    "price_fit": 0.10,      # 价格是否落在"薄利多销"区间
}

# ---- 样本自适应阈值 ----
SAMPLE_MIN = 3          # 少于 3 条：不给相对排名，只给绝对分 + 低置信
SAMPLE_OK = 8           # 8 条以上：完全相信相对排名

# ---- 漏斗率的合理上限（超过基本是脏数据或刷量）----
MAX_WANT_RATE = 1.0     # 想要不可能多于浏览
MAX_DEAL_RATE = 1.0

# ---- 字段别名（浏览数在链路上有三个名字，见 _first_num）----
WANT_KEYS = ("want_count", "wantCnt", "wantNum")
VIEW_KEYS = ("view_count", "browse_count", "viewCount", "browseCnt")
SOLD_KEYS = ("sold_count", "soldCnt", "soldCount")

# 一页搜索结果最多 30 条：样本数触顶时，真实供给只会更多（与 B1 同口径）
SUPPLY_SAMPLE_CAP = 30

# ---- 指标来源与统计口径（拆解 39 §八-5）----
# 咸鱼站把"转化率只能给 L/M 区间"写在产品说明里，等于承认：**一个数字不给口径，
# 读的人会把它当成精确值**。我们页面上的分、率、样本数来自三种完全不同的东西，
# 必须逐项标出来，否则"87 分"和"想要率 62%"看起来一样可信。
#
# kind 的三种取值：
#   displayed  平台页面直接展示的字段（快照，会变，且可能有展示口径）
#   computed   我们由展示字段算出来的比值（不是平台口径）
#   lower_bound 只能保证"至少这么多"（分页/条数上限导致）
METRIC_PROVENANCE: Dict[str, Dict[str, str]] = {
    "want_count": {"label": "想要数", "kind": "displayed",
                   "source": "平台搜索结果卡片展示值",
                   "note": "快照，非实时；不同入口展示可能不一致"},
    "view_count": {"label": "浏览数", "kind": "displayed",
                   "source": "平台搜索结果卡片展示值",
                   "note": "部分卡片不展示，缺了就按未覆盖计"},
    "sold_count": {"label": "成交数", "kind": "displayed",
                   "source": "平台搜索结果卡片展示值",
                   "note": "是卖家累计展示值，不是本店销量，也不含退款"},
    "price": {"label": "价格", "kind": "displayed",
              "source": "平台搜索结果展示价", "note": "可能是区间价或起售价"},
    "want_rate": {"label": "想要率", "kind": "computed",
                  "source": "想要数 ÷ 浏览数", "note": "本机计算，非平台口径"},
    "deal_rate": {"label": "成交率", "kind": "computed",
                  "source": "成交数 ÷ 想要数", "note": "本机计算；分母为 0 时按未知处理"},
    "score": {"label": "机会分", "kind": "computed",
              "source": "批内分位数加权", "note": "只是同批结果的相对位置，不能跨批比较"},
    "sample": {"label": "有效样本", "kind": "lower_bound",
               "source": "本页搜索返回条数", "note": "触顶 30 条时真实供给只会更多，故为下限"},
}


def metric_provenance() -> Dict[str, Any]:
    """给前端的一份口径说明（纯常量，可直接缓存）。"""
    return {
        "metrics": {k: dict(v) for k, v in METRIC_PROVENANCE.items()},
        "sample_cap": SUPPLY_SAMPLE_CAP,
        "disclaimer": ("平台展示值会变化；比值由本机计算，分母取平台展示值、不是本机"
                       "观测样本，所以只给点估计、不冒充区间；样本触顶时只代表下限。"),
    }



def _identity(item: Dict[str, Any]) -> Tuple[Any, ...]:
    """用于在复制后配对回原对象（itemId 优先，缺失时用内容指纹）。"""
    iid = item.get("item_id") or item.get("itemId")
    if iid:
        return ("id", str(iid))
    return ("fp", str(item.get("title", ""))[:60], _num(item.get("price"), -1),
            _num(item.get("want_count")), _num(item.get("view_count")),
            _num(item.get("sold_count")))


def v2_enabled() -> bool:
    """开关：环境变量优先，默认开。"""
    env = str(os.environ.get("OPPORTUNITY_SCORE_V2", "")).strip().lower()
    if env in ("0", "false", "off", "no"):
        return False
    if env in ("1", "true", "on", "yes"):
        return True
    return True


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or isinstance(v, bool):
            return default
        return float(v)
    except Exception:
        return default


def _pct_rank(values: Sequence[float], x: float) -> float:
    """
    分位数排名 [0,1]：x 在这一组里超过多少比例的值。

    用排名而不是 min-max，是因为 min-max 被极端值主导
    （一个 want=5000 的爆款会把其他全部压到 0.0x）。
    并列值取平均排名，避免顺序依赖。
    """
    n = len(values)
    if n <= 1:
        return 0.5
    less = sum(1 for v in values if v < x)
    equal = sum(1 for v in values if v == x)
    return (less + 0.5 * equal) / n


def _first_num(item: Dict[str, Any], keys: Sequence[str]) -> float:
    """按顺序取第一个存在的数值字段。

    存在的必要：浏览数在链路上有三个名字 ——
    搜索层叫 `browse_count`（详情接口 browseCnt），引擎归一化时叫 `view_count`，
    上游原仓库又叫 `showCount`。只认一个名字的后果是**权重最高的维度（想要率 0.34）
    永远拿不到分母**，全程按中性处理，等于这个维度不存在。
    """
    for k in keys:
        v = item.get(k)
        if v is None or v == "":
            continue
        return _num(v)
    return 0.0


def _has_num(item: Dict[str, Any], keys: Sequence[str]) -> bool:
    """这个字段到底有没有——存在且能解析成数字，`0` 也算有。

    `sold_count = 0` 是"成交 0"，字段缺失是"不知道成交多少"。
    两者都读成 0 的话，覆盖率永远是 100%，中性 0.5 的法律也就无从谈起。
    """
    for k in keys:
        v = item.get(k)
        if v is None or v == "":
            continue
        try:
            float(v)
        except (TypeError, ValueError):
            continue
        return True
    return False


STALE_WANT_SOURCES = frozenset({"list", "unknown", "none"})


def want_metric_is_usable(item: Dict[str, Any]) -> bool:
    """想要数是否来自当前可用的搜索卡片或详情接口。

    历史缓存里的 `want_stale=True` 和旧来源 `list` 都不能继续参与分位数或
    漏斗率。它们即使还带着一个数字，也只代表过去某个不可验证的快照。
    """
    if item.get("want_stale") is True:
        return False
    source = str(item.get("want_source") or "").strip().lower()
    if source in STALE_WANT_SOURCES:
        return False
    return _has_num(item, WANT_KEYS)


def _want_unknown_reason(item: Dict[str, Any]) -> str:
    if item.get("want_stale") is True:
        return "想要数已标记过期（不当作 0），相关维度按中性处理"
    source = str(item.get("want_source") or "").strip().lower()
    if source in STALE_WANT_SOURCES:
        return "想要数来源不可用（不当作 0），相关维度按中性处理"
    return "缺想要数字段（≠ 想要为 0），相关维度按中性处理"


def funnel_rates(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    算漏斗率。分母为 0 时返回 None 而不是 0 —— "没有数据"和"转化率 0"是两件事。

    `want_known / view_known / sold_known` 用于覆盖率：只有字段真实存在，
    这个维度才算"有数"；字段缺失时该维度按中性 0.5 处理，并写进理由。
    """
    want = _first_num(item, WANT_KEYS)
    view = _first_num(item, VIEW_KEYS)
    sold = _first_num(item, SOLD_KEYS)
    want_known = want_metric_is_usable(item)
    view_known = _has_num(item, VIEW_KEYS)
    sold_known = _has_num(item, SOLD_KEYS)

    want_rate: Optional[float] = None
    # 想要数缺失时不能算成 0/浏览 = 0%——那是"不知道"，不是"没人想要"。
    if want_known and view_known and view > 0:
        want_rate = min(MAX_WANT_RATE, want / view)

    deal_rate: Optional[float] = None
    # 成交数缺失时不能算成 0/想要 = 0 成交——那是"不知道"，不是"没卖出"。
    if want_known and sold_known and want > 0:
        deal_rate = min(MAX_DEAL_RATE, sold / want)

    return {
        "want": want, "view": view, "sold": sold,
        "want_known": want_known, "view_known": view_known, "sold_known": sold_known,
        "want_rate": want_rate,
        "deal_rate": deal_rate,
    }


def coverage_meta(rows: Sequence[Dict[str, Any]],
                  sample_cap: int = SUPPLY_SAMPLE_CAP) -> Dict[str, Any]:
    """
    覆盖率：这批结果里**有多少条真的带了这个字段**。

    为什么必须单独返回：B4 的漏斗率维度遇到缺失会按中性 0.5 处理，
    否则整批分数会系统性偏移；但中性 0.5 一多，排名就变成"谁缺得少谁靠前"。
    所以覆盖率必须跟着分数一起给，让"这批评分有多少是猜的"可见。

    口径是**字段存在**，不是数值 > 0：`sold_count=0` 是有效数据（真的没卖出），
    只有字段完全缺失才算没覆盖。
    """
    valid = [r for r in rows if isinstance(r, dict)]
    n = len(valid)
    cap = max(1, int(sample_cap or SUPPLY_SAMPLE_CAP))
    if n == 0:
        return {"n": 0, "want_coverage": 0.0, "deal_coverage": 0.0,
                "view_coverage": 0.0, "coverage_counts": {"want": 0, "deal": 0, "view": 0},
                "supply_sampled": 0, "supply_cap": cap, "supply_truncated": False,
                "provenance": metric_provenance()}

    counts = {"want": 0, "deal": 0, "view": 0}
    for r in valid:
        f = r.get("_funnel") or funnel_rates(r)
        counts["want"] += 1 if f.get("want_known") else 0
        counts["deal"] += 1 if f.get("sold_known") else 0
        counts["view"] += 1 if f.get("view_known") else 0

    return {
        "n": n,
        "want_coverage": round(counts["want"] / n, 3),
        "deal_coverage": round(counts["deal"] / n, 3),
        "view_coverage": round(counts["view"] / n, 3),
        "coverage_counts": counts,
        "supply_sampled": n,
        "supply_cap": cap,
        "supply_truncated": n >= cap,
        "provenance": metric_provenance(),
    }


def price_fit_score(price: Any) -> Tuple[float, str]:
    """
    价格适配：我们的定位是"薄利多销"，不是高客单。

    与 v1 的 `60 - log10(p)*8` 不同，这里做成**钟形**：
    太低（<5 元）大概率是配件/引流品，利润不够；
    太高（>300 元）周转慢、售后重，与"少量成本+可不断换品"冲突；
    20~80 元是最舒服的区间。
    """
    p = _num(price, -1)
    if p <= 0:
        return 0.5, "无价格，按中性处理"
    if p < 5:
        return 0.15, f"价格 {p:g} 元偏低，多为配件/引流品"
    if p > 300:
        return 0.2, f"价格 {p:g} 元偏高，周转与售后成本重"
    if p > 150:
        return 0.45, f"价格 {p:g} 元偏中高"
    # 20~80 元：用一条平顶曲线，中间略高
    if 20 <= p <= 80:
        return 0.95, f"价格 {p:g} 元落在薄利多销区间"
    if p < 20:
        return 0.7, f"价格 {p:g} 元偏低但可做"
    return 0.7, f"价格 {p:g} 元偏高但可做"


def scan_risk(item: Dict[str, Any]) -> Dict[str, Any]:
    """风险分档扫描。返回 {level, penalty, hits, blocked}。"""
    text = f"{item.get('title', '')} {item.get('description', '')}"
    hits: Dict[str, List[str]] = {}
    for level, words in (("block", RISK_BLOCK), ("copyright", RISK_COPYRIGHT),
                         ("gray", RISK_GRAY), ("hint", RISK_HINT)):
        got = [w for w in words if w in text]
        if got:
            hits[level] = got

    if "block" in hits:
        return {"level": "block", "penalty": RISK_WEIGHTS["block"],
                "hits": hits, "blocked": True}
    penalty = 0.0
    level = "none"
    if "copyright" in hits:
        penalty += RISK_WEIGHTS["copyright"]
        level = "copyright"
    if "gray" in hits:
        penalty += RISK_WEIGHTS["gray"]
        level = "gray" if level == "none" else level
    if "hint" in hits and level == "none":
        level = "hint"
    return {"level": level, "penalty": penalty, "hits": hits, "blocked": False}


def _confidence(n: int) -> Tuple[str, float]:
    """样本量决定置信度。返回 (档位文字, 0~1 系数)。"""
    if n < SAMPLE_MIN:
        return "低", 0.55
    if n < SAMPLE_OK:
        return "中", 0.8
    return "高", 1.0


def score_batch(items: List[Dict[str, Any]],
                keyword: str = "") -> Dict[str, Any]:
    """
    对一批搜索结果打分并排序。**这是 v2 的唯一入口**——
    单条打分没有意义，因为分位数归一化必须有参照集。

    返回：
        {
          "items": [...按分数倒序，每条带 reasons...],
          "meta": {"n": 条数, "confidence": 高/中/低, "degraded": bool,
                   "keyword": 词, "notes": [...]}
        }
    """
    rows = [dict(it) for it in (items or []) if isinstance(it, dict)]
    n = len(rows)
    if not rows:
        return {"items": [], "meta": {"n": 0, "confidence": "低", "degraded": True,
                                      "keyword": keyword, "notes": ["没有结果"],
                                      **coverage_meta([])}}

    conf_text, conf_k = _confidence(n)
    degraded = n < SAMPLE_MIN

    # 先算漏斗与风险（这两步与批次无关）
    for r in rows:
        f = funnel_rates(r)
        r["_funnel"] = f
        r["_risk"] = scan_risk(r)

    # 参照集：只用**未被封禁**的条目做分位数，否则违禁词会污染分布
    pool = [r for r in rows if not r["_risk"]["blocked"]] or rows

    want_rates = [r["_funnel"]["want_rate"] for r in pool
                  if r["_funnel"]["want_rate"] is not None]
    deal_rates = [r["_funnel"]["deal_rate"] for r in pool
                  if r["_funnel"]["deal_rate"] is not None]
    # 参照集只收**字段真实存在**的条目。把缺失当 0 放进来，
    # 缺数据的条目会稳定排在需求层末尾，等于把"不知道"当成"最差"。
    wants = [r["_funnel"]["want"] for r in pool if r["_funnel"]["want_known"]]
    solds = [r["_funnel"]["sold"] for r in pool if r["_funnel"]["sold_known"]]

    for r in rows:
        f, risk = r["_funnel"], r["_risk"]
        reasons: List[str] = []

        if risk["blocked"]:
            r.update({
                "opportunity_score": 0.0, "opinion": "不推荐",
                "risk_level": "block", "risk_penalty": 100.0,
                "risk_hits": {k: v for k, v in risk["hits"].items()},
                "confidence": conf_text, "blocked": True,
                "dimensional": {},
                "reasons": [f"命中违禁/侵权词 {risk['hits'].get('block')}，直接出局"],
            })
            continue

        dim: Dict[str, float] = {}
        # 转化层
        if f["want_rate"] is not None and want_rates:
            dim["want_rate"] = _pct_rank(want_rates, f["want_rate"])
            reasons.append(
                f"想要率 {f['want_rate']*100:.1f}%（{int(f['want'])}/{int(f['view'])}）"
                f"，本批第 {dim['want_rate']*100:.0f} 百分位")
        else:
            dim["want_rate"] = 0.5
            if not f["want_known"]:
                reasons.append(_want_unknown_reason(r))
            elif not f["view_known"]:
                reasons.append("缺浏览数字段（≠ 浏览为 0），想要率按中性处理")
            else:
                reasons.append("浏览数为 0，想要率无法计算，按中性处理")

        if f["deal_rate"] is not None and deal_rates:
            dim["deal_rate"] = _pct_rank(deal_rates, f["deal_rate"])
            reasons.append(
                f"成交率 {f['deal_rate']*100:.1f}%（{int(f['sold'])}/{int(f['want'])}），"
                f"第 {dim['deal_rate']*100:.0f} 百分位")
        else:
            dim["deal_rate"] = 0.5
            if not f["sold_known"]:
                reasons.append("缺成交数字段（≠ 成交为 0），成交率按中性处理")
            elif not f["want_known"]:
                reasons.append(_want_unknown_reason(r).replace(
                    "相关维度按中性处理", "成交率按中性处理"
                ))
            elif f["want"] <= 0:
                reasons.append("想要数为 0，成交率无法计算，按中性处理")
            else:
                reasons.append("无成交数据，成交率按中性处理")

        # 需求层：字段缺失时给中性 0.5，而不是拿 0 去参与分位数（见 wants/solds）
        if f["want_known"] and wants:
            dim["want_abs"] = _pct_rank(wants, f["want"])
        else:
            dim["want_abs"] = 0.5
            reasons.append(_want_unknown_reason(r).replace(
                "相关维度按中性处理", "需求体量按中性处理"
            ))

        if f["sold_known"] and solds:
            dim["sold_abs"] = _pct_rank(solds, f["sold"])
        else:
            dim["sold_abs"] = 0.5
            reasons.append("缺成交数字段，成交体量按中性处理")

        # 价格
        pf, pf_reason = price_fit_score(r.get("price"))
        dim["price_fit"] = pf
        reasons.append(pf_reason)

        base = sum(dim[k] * w for k, w in WEIGHTS.items()) * 100.0

        # 样本不足：往中性收缩（避免"3 条里第 1 名"被封神）
        if degraded:
            base = base * conf_k + 50.0 * (1 - conf_k)

        score = max(0.0, min(100.0, base - risk["penalty"]))

        if risk["level"] == "copyright":
            reasons.append(f"含品牌词 {risk['hits'].get('copyright')}，需人工确认是否侵权")
        elif risk["level"] == "gray":
            reasons.append(f"含限制类词 {risk['hits'].get('gray')}，有下架风险")
        elif risk["level"] == "hint":
            reasons.append(f"注意：{risk['hits'].get('hint')}")

        r.update({
            "opportunity_score": round(score, 1),
            "risk_level": risk["level"],
            "risk_penalty": risk["penalty"],
            "risk_hits": risk["hits"],
            "confidence": conf_text,
            "blocked": False,
            "dimensional": {k: round(v, 3) for k, v in dim.items()},
            "reasons": reasons,
        })

    # 排序：分数优先；同分时置信/成交率更高的在前（同分不能靠字典序）
    rows.sort(key=lambda x: (
        -x["opportunity_score"],
        -(x["dimensional"].get("deal_rate", 0) if not x["blocked"] else -1),
        -_num(x.get("want_count")),
    ))

    # 推荐意见用**分档**而不是固定阈值：v1 的 60 分在热门词里等于全部推荐
    _assign_opinion(rows)

    cov = coverage_meta(rows)
    notes: List[str] = []
    if degraded:
        notes.append(f"仅 {n} 条结果，样本不足，分数已向中性收缩（置信度：{conf_text}）")
    if any(r["blocked"] for r in rows):
        notes.append(f"{sum(1 for r in rows if r['blocked'])} 条因违禁/高仿词出局")
    if min(cov["want_coverage"], cov["deal_coverage"], cov["view_coverage"]) < 1:
        c = cov["coverage_counts"]
        notes.append(
            f"数据覆盖：浏览 {c['view']}/{n}、想要 {c['want']}/{n}、成交 {c['deal']}/{n}；"
            f"缺失维度按中性 0.5 计，已写进对应条目的理由")
    if cov["supply_truncated"]:
        notes.append(f"本批 {n} 条已触顶（上限 {cov['supply_cap']}），真实供给 ≥{n}")

    return {"items": rows,
            "meta": {"n": n, "confidence": conf_text, "degraded": degraded,
                     "keyword": keyword, "notes": notes,
                     "weights": dict(WEIGHTS), "version": "v2",
                     **cov}}


def _assign_opinion(rows: List[Dict[str, Any]]) -> None:
    """
    推荐意见按**本批内的相对位置**分档（前 20% / 中 40% / 后 40%），
    而不是固定分数线。固定线在 500 条的热门词里意味着"全部推荐"。

    违禁条目固定"不推荐"。
    """
    valid = [r for r in rows if not r["blocked"]]
    m = len(valid)
    if m == 0:
        for r in rows:
            r["opinion"] = "不推荐"
        return
    top = max(1, int(math.ceil(m * 0.2)))
    mid = max(top + 1, int(math.ceil(m * 0.6)))

    for idx, r in enumerate(valid):
        if idx < top:
            r["opinion"] = "推荐关注"
        elif idx < mid:
            r["opinion"] = "可观察"
        else:
            r["opinion"] = "暂不推荐"
        r["rank_in_batch"] = idx + 1
    for r in rows:
        if r["blocked"]:
            r["opinion"] = "不推荐"
            r["rank_in_batch"] = m + 1


def rescore_in_place(rows: List[Dict[str, Any]], keyword: str = "") -> List[Dict[str, Any]]:
    """
    对**已经打过分**的一批重新算一次（详情补数后、加成长值后、AI 看图后）。
    复用同一套归一化参照集，保证"补数后的重排"和"初次打分"口径一致。

    直接改 rows 里的 dict，并返回排序后的同一列表（调用方常持有引用）。
    """
    scored = score_batch(rows, keyword)
    order = {id(x): i for i, x in enumerate(scored["items"])}
    rows.sort(key=lambda x: order.get(id(x), 10 ** 6))
    return rows


def refresh_rank(rows: List[Dict[str, Any]]) -> None:
    """
    改过分数之后重新分配 opinion / rank_in_batch。

    为什么必须单独一步：`rank_in_batch` 在 `score_batch` 里定，任何后续加分
    （成长值、AI 看图）都会让排名和分数对不上，前端显示"第 5 名"却排在第一位。
    """
    rows.sort(key=lambda x: -float(x.get("opportunity_score") or 0))
    _assign_opinion(rows)


def merge_inplace(rows: List[Dict[str, Any]], keyword: str = "") -> List[Dict[str, Any]]:
    """
    详情补数之后整批重算。

    要点：`score_batch` 内部会 `dict(it)` 复制，所以**不能用 id() 匹配**回原对象。
    这里改用「输入下标」配对：`score_batch` 保持输入顺序打分，打完才排序，
    所以只要在调用前记下每条的身份键，就能把结果按内容配对回去。

    补数会改 `want_count`，而 v2 的分位数必须基于**补数后**的新值重算，
    所以是整批重算，不是逐条 rescore。
    """
    if not rows:
        return rows

    keys = [_identity(r) for r in rows]
    scored = score_batch(rows, keyword)

    # 结果按内容配对回原对象；同一内容出现多次时按出现顺序依次配对
    buckets: Dict[Any, List[Dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(_identity(r), []).append(r)

    for src in scored["items"]:
        bucket = buckets.get(_identity(src)) or []
        if not bucket:
            continue
        dst = bucket.pop(0)
        for k in ("opportunity_score", "opinion", "risk_level", "risk_penalty",
                  "risk_hits", "confidence", "blocked", "dimensional", "reasons",
                  "rank_in_batch"):
            if k in src:
                dst[k] = src[k]

    rows.sort(key=lambda x: -float(x.get("opportunity_score") or 0))
    return rows


def opinion_for_score(score: float) -> str:
    """
    只有分数、没有批次上下文时的意见（用在成长值/AI 加分之后）。

    v2 的主意见是**批内分档**（前 20% / 中 40% / 后 40%），但那需要整批。
    这里的兜底映射与分档的典型结果对齐：v2 的分数已经是分位数加权的结果，
    所以 65 以上基本落在前 20%，40–65 落在中间。
    """
    s = float(score or 0)
    if s >= 65:
        return "推荐关注"
    if s >= 40:
        return "可观察"
    return "暂不推荐"
