"""
B4 评分 v2 测试
===============
v1 的两个实测缺陷必须锁死，防止有人"简化"回去：

1. **冷门词上限**: want 0→8 时 v1 给 76→94，全部落在推荐区
2. **热门词无区分**: want 50→500 时 v1 全部 100.0 分，10 条一模一样

v2 的核心主张：用**本批分位数**而不是绝对值，所以同一份数据里"谁更好"永远能分开。
另外锁死：漏斗率、样本自适应、风险分档、排序带理由、v1 可回滚。

跑法（backend 目录）：python3 tests/test_opportunity_score_v2.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import opportunity_score_v2 as v2  # noqa: E402
from opportunity_engine import score_item  # noqa: E402

PASSES = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ✅ {name}")
    else:
        FAILS.append(name)
        print(f"  ❌ {name}  {detail}")


def mk(want, view, sold, price=39, title="测试商品", desc=""):
    return {"title": title, "description": desc, "price": price,
            "want_count": want, "view_count": view, "sold_count": sold}


# ---------------------------------------------------------------- 1. v1 的缺陷
print("\n【1】先证明 v1 确实坏了（这是 B4 存在的理由）")

cold = [mk(w, w * 6, 0) for w in range(0, 9)]
v1_cold = [score_item(i)["opportunity_score"] for i in cold]
check(all(s >= 60 for s in v1_cold),
      f"v1：冷门词 want 0~8 全部 ≥60（{v1_cold}）",
      "如果这里失败说明 v1 公式被改过，B4 的前提要重估")

hot = [mk(w, w * 5, w // 10) for w in (50, 80, 120, 180, 250, 320, 400, 500, 600, 800)]
v1_hot = [score_item(i)["opportunity_score"] for i in hot]
check(len(set(v1_hot)) == 1,
      f"v1：热门词 want 50~800 得分完全一致（{v1_hot[0]}）—— 零区分度",
      f"实际 {len(set(v1_hot))} 种不同分数")


# ---------------------------------------------------------------- 2. v2 修复
print("\n【2】v2 在同样数据上有区分度")

r_cold = v2.score_batch(cold, "冷门")
v2_cold = [x["opportunity_score"] for x in r_cold["items"]]
check(len(set(v2_cold)) >= 8, f"v2 冷门词至少 8 种不同分数（实际 {len(set(v2_cold))}）")
check(max(v2_cold) - min(v2_cold) > 8,
      f"v2 冷门词极差 > 8（实际 {max(v2_cold)-min(v2_cold):.1f}）")

r_hot = v2.score_batch(hot, "热门")
v2_hot = [x["opportunity_score"] for x in r_hot["items"]]
check(len(set(v2_hot)) == len(hot),
      f"v2 热门词每条分数都不同（{len(set(v2_hot))}/{len(hot)}）")
check(v2_hot[0] > v2_hot[-1], "v2 热门词有单调性：want 高的分高")

# 关键：want 更高的那条，分数不能更低
pairs = list(zip([50, 80, 120, 180, 250], v2_hot[:5]))
check(pairs[0][1] >= pairs[-1][1],
      f"want 50 的分数不低于 want 250（{pairs[0][1]} vs {pairs[-1][1]}）")


# ---------------------------------------------------------------- 3. 漏斗率
print("\n【3】漏斗率：分母为 0 是「没有数据」，不是「转化率 0」")

f = v2.funnel_rates(mk(10, 100, 3))
check(abs(f["want_rate"] - 0.1) < 1e-9, "想要率 = 10/100 = 0.1", f)
check(abs(f["deal_rate"] - 0.3) < 1e-9, "成交率 = 3/10 = 0.3", f)

f2 = v2.funnel_rates(mk(0, 0, 0))
check(f2["want_rate"] is None, "浏览 0 → 想要率 None（不是 0）")
check(f2["deal_rate"] is None, "想要 0 → 成交率 None（不是 0）")

f3 = v2.funnel_rates(mk(200, 100, 50))
check(f3["want_rate"] == v2.MAX_WANT_RATE, "想要 > 浏览（脏数据）→ 截断到 1.0", f3["want_rate"])

f4 = v2.funnel_rates(mk(10, 100, 999))
check(f4["deal_rate"] == v2.MAX_DEAL_RATE, "成交 > 想要（脏数据）→ 截断到 1.0")


# ---------------------------------------------------------------- 4. 高转化排前面
print("\n【4】真实场景：高转化的必须排在高浏览零转化的前面")

scene = [
    mk(120, 900, 45, 19, "真爆款"),
    mk(5, 400, 0, 9, "高浏览零转化"),
    mk(60, 150, 30, 25, "高转化"),
    mk(200, 2000, 3, 500, "高浏览低成交"),
]
res = v2.score_batch(scene, "手机配件")
titles = [x["title"] for x in res["items"]]
check(titles.index("高转化") < titles.index("高浏览零转化"),
      f"高转化排在高浏览零转化前面（{titles}）")
check(titles.index("真爆款") < titles.index("高浏览低成交"),
      f"真爆款排在高浏览低成交前面（{titles}）")


# ---------------------------------------------------------------- 4b. 字段别名
print("\n【4b】浏览数字段有三个名字，认不出就等于「想要率」这个维度不存在")

# 这条是实盘发现的真缺口：详情补数把浏览数写成 browse_count，
# 引擎归一化写成 view_count，上游叫 showCount。只认 view_count 时
# want_rate 权重 0.34 会一直按中性 0.5 处理，分数虚高且不可解释。
alias_sets = [
    {"title": "别名A", "price": 25, "want_count": 60, "view_count": 150, "sold_count": 30},
    {"title": "别名B", "price": 25, "want_count": 60, "browse_count": 150, "sold_count": 30},
    {"title": "别名C", "price": 25, "want_count": 60, "viewCount": 150, "soldCount": 30},
]
for a in alias_sets:
    fa = v2.funnel_rates(a)
    check(fa["want_rate"] is not None and abs(fa["want_rate"] - 0.4) < 1e-9,
          f"{a['title']} 想要率被算出来（0.4）", fa)

base = v2.score_batch([mk(60, 150, 30, 25, "标准名")], "x")["items"][0]
alt = v2.score_batch([{"title": "标准名", "price": 25, "want_count": 60,
                       "browse_count": 150, "sold_count": 30}], "x")["items"][0]
check(base["opportunity_score"] == alt["opportunity_score"],
      f"browse_count 与 view_count 得分一致（{base['opportunity_score']} vs {alt['opportunity_score']}）",
      "字段名不同不能让同一个商品换个分")

# normalize_item 必须把顶层的浏览/成交数带进结果（详情补数就写在顶层）
from opportunity_engine import normalize_item  # noqa: E402
norm = normalize_item({"item_id": "x1", "title": "顶层字段", "price": 25,
                       "want_count": 60, "view_count": 150, "sold_count": 30}, "kw")
check(norm["view_count"] == 150, "normalize_item 读顶层 view_count", norm["view_count"])
check(norm["sold_count"] == 30, "normalize_item 读顶层 sold_count", norm["sold_count"])
norm2 = normalize_item({"item_id": "x2", "title": "补数后", "price": 25,
                        "want_count": 60, "browse_count": 150}, "kw")
check(norm2["view_count"] == 150, "normalize_item 也认 browse_count", norm2["view_count"])
norm3 = normalize_item({
    "item_id": "x3",
    "title": "搜索卡片",
    "price": 25,
    "want_count": 1087,
    "want_source": "search",
    "want_stale": False,
    "detail_sold_count": 12,
    "detail_stock": 8,
    "sold_out": False,
}, "kw")
check(norm3["want_source"] == "search",
      "normalize_item 保留搜索想要数来源", norm3["want_source"])
check(norm3["want_stale"] is False,
      "normalize_item 保留搜索想要数新鲜状态", norm3["want_stale"])
check(norm3["detail_sold_count"] == 12,
      "normalize_item 保留详情成交数", norm3["detail_sold_count"])


# ---------------------------------------------------------------- 5. 风险分档
print("\n【5】风险分档：违禁直接出局，不是「扣点分继续排」")

bad = v2.scan_risk(mk(500, 1000, 100, 199, "高仿 LV 钱包 复刻原单"))
check(bad["blocked"] is True, "高仿+复刻 → blocked")
check(bad["penalty"] == 100.0, "违禁扣满 100")

res_bad = v2.score_batch(
    [mk(500, 1000, 100, 199, "高仿 LV 钱包 复刻原单"), mk(10, 100, 2, 39, "正常商品")],
    "钱包")
blocked_rows = [x for x in res_bad["items"] if x["blocked"]]
check(len(blocked_rows) == 1, "1 条被判 blocked")
check(blocked_rows[0]["opportunity_score"] == 0.0, "违禁条得 0 分")
check(blocked_rows[0]["opinion"] == "不推荐", "违禁条意见固定「不推荐」")
check(res_bad["items"][0]["title"] == "正常商品",
      "违禁条即使原数据更好也排最后", res_bad["items"][0]["title"])

cp = v2.scan_risk(mk(50, 500, 10, 89, "Apple 原装充电头"))
check(cp["level"] == "copyright", "品牌词 → copyright", cp["level"])
check(not cp["blocked"], "品牌词不直接出局（可能是正品）")
check(cp["penalty"] == v2.RISK_WEIGHTS["copyright"], "按版权档扣分")

gray = v2.scan_risk(mk(50, 500, 10, 89, "电子烟 一次性"))
check(gray["level"] == "gray", "电子烟 → gray", gray["level"])

hint = v2.scan_risk(mk(50, 500, 10, 89, "二手 拆封 耳机"))
check(hint["level"] == "hint" and hint["penalty"] == 0,
      "擦边词只提示不扣分", hint)

clean = v2.scan_risk(mk(50, 500, 10, 39, "数据线"))
check(clean["level"] == "none" and clean["penalty"] == 0, "干净商品不扣分")


# ---------------------------------------------------------------- 6. 样本自适应
print("\n【6】样本自适应：3 条以内不给「封神」，要往中性收缩")

one = v2.score_batch([mk(500, 600, 200, 39, "唯一一条")], "孤例")
check(one["meta"]["degraded"] is True, "1 条 → degraded")
check(one["meta"]["confidence"] == "低", "1 条 → 置信度低")
check(abs(one["items"][0]["opportunity_score"] - 50) < 25,
      f"单条分数被拉向 50（实际 {one['items'][0]['opportunity_score']}）",
      "不收缩会把「唯一一条」封成 100 分")

mid = v2.score_batch([mk(10 * i, 100 * i, i, 39, f"商品{i}") for i in range(1, 5)], "四条")
check(mid["meta"]["confidence"] == "中", "4 条 → 置信度中")

big = v2.score_batch([mk(10 * i, 100 * i, i, 39, f"商品{i}") for i in range(1, 11)], "十条")
check(big["meta"]["confidence"] == "高", "10 条 → 置信度高")
check(big["meta"]["degraded"] is False, "10 条 → 不降级")


# ---------------------------------------------------------------- 7. 排序带理由
print("\n【7】排序必须能解释（AutoJs6 Ranker 的教训）")

r = v2.score_batch([mk(60, 150, 30, 25, "好品"),
                    mk(5, 400, 0, 9, "差品")], "测试")
for x in r["items"]:
    check(len(x.get("reasons") or []) >= 2,
          f"{x['title']} 至少 2 条理由", x.get("reasons"))
    check(all(isinstance(s, str) and s for s in x["reasons"]),
          f"{x['title']} 理由都是非空字符串")

top_reasons = " ".join(r["items"][0]["reasons"])
check("想要率" in top_reasons or "成交率" in top_reasons,
      "最高分那条的理由里有转化率说明", top_reasons)


# ---------------------------------------------------------------- 8. 批内分档
print("\n【8】推荐意见是「批内相对位置」，不是固定分数线")

ten = [mk(10 * i, 100 * i, i, 39, f"商品{i}") for i in range(1, 11)]
r10 = v2.score_batch(ten, "十个")
ops = [x["opinion"] for x in r10["items"]]
check(ops[0] == "推荐关注", "第 1 名是推荐关注", ops[0])
check(ops[-1] == "暂不推荐", "最后一名是暂不推荐", ops[-1])
check(ops.count("推荐关注") <= 3, f"推荐关注不超过 20%（实际 {ops.count('推荐关注')}/10）", ops)
ranks = [x["rank_in_batch"] for x in r10["items"]]
check(ranks == list(range(1, 11)), "rank_in_batch 连续 1..10", ranks)


# ---------------------------------------------------------------- 9. 回滚
print("\n【9】v1 可回滚（上线第一周的保险）")

os.environ.pop("OPPORTUNITY_SCORE_V2", None)
check(v2.v2_enabled() is True, "默认开 v2")
os.environ["OPPORTUNITY_SCORE_V2"] = "0"
check(v2.v2_enabled() is False, "环境变量 0 → 关")
os.environ["OPPORTUNITY_SCORE_V2"] = "false"
check(v2.v2_enabled() is False, "环境变量 false → 关")
os.environ["OPPORTUNITY_SCORE_V2"] = "1"
check(v2.v2_enabled() is True, "环境变量 1 → 开")
os.environ.pop("OPPORTUNITY_SCORE_V2", None)


# ---------------------------------------------------------------- 10. 重算一致性
print("\n【10】补数后重算：不能出现「排名和分数对不上」")

rows = [mk(10 * i, 100 * i, i, 39, f"商品{i}") for i in range(1, 9)]
v2.score_batch(rows, "x")
# 模拟详情补数：前 3 条的想要数暴涨
for r_ in rows[:3]:
    r_["want_count"] = 999
v2.merge_inplace(rows, "x")
scores = [r_["opportunity_score"] for r_ in rows]
check(scores == sorted(scores, reverse=True),
      "merge_inplace 后按分数倒序", scores[:4])
check(all("opportunity_score" in r_ and "reasons" in r_ for r_ in rows),
      "所有行都被重算过（没有残留旧分数）")

# refresh_rank：改分之后排名要跟上
rows[0]["opportunity_score"] = 99.0
rows[-1]["opportunity_score"] = 1.0
v2.refresh_rank(rows)
check(rows[0]["rank_in_batch"] == 1, "改分后第 1 名确实排第一")
check(rows[0]["opportunity_score"] >= rows[1]["opportunity_score"], "排序按分数")


# ---------------------------------------------------------------- 11. 边界
print("\n【11】边界：空批、单条、全违禁、缺字段")

check(v2.score_batch([])["items"] == [], "空批 → 空列表")
check(v2.score_batch([])["meta"]["n"] == 0, "空批 meta.n=0")

all_bad = v2.score_batch([mk(100, 1000, 50, 199, "高仿 A货"),
                          mk(100, 1000, 50, 199, "复刻 精仿")], "全违禁")
check(all(x["blocked"] for x in all_bad["items"]), "全违禁批次全 blocked")
check(all(x["opportunity_score"] == 0 for x in all_bad["items"]), "全 0 分")
check(all(x["opinion"] == "不推荐" for x in all_bad["items"]), "全不推荐")
check(any("出局" in n for n in all_bad["meta"]["notes"]), "meta 说明有几条出局")

nofield = v2.score_batch([{"title": "没有数值"}, {"title": "第二条"}], "缺字段")
check(len(nofield["items"]) == 2, "缺字段不崩")
check(all("opportunity_score" in x for x in nofield["items"]), "都给出了分数")

p_neg, _ = v2.price_fit_score(-5)
check(p_neg == 0.5, "负价格按中性处理")
p0, _ = v2.price_fit_score(0)
check(p0 == 0.5, "0 价格按中性")
p, why = v2.price_fit_score(39)
check(p >= 0.9 and "薄利多销" in why, f"39 元落在最优区间（{p}）", why)
p_hi, _ = v2.price_fit_score(2000)
check(p_hi <= 0.25, f"2000 元低适配（{p_hi}）")


# ---------------------------------------------------------------- 12. 覆盖率（B4 剩余项）
print("\n【12】覆盖率：字段缺失 ≠ 数值为 0；中性 0.5 必须写理由")

full = v2.score_batch([mk(10, 100, 3), mk(20, 200, 4)], "全字段")
mf = full["meta"]
check(mf["view_coverage"] == 1.0, "全字段 → 浏览覆盖 100%", mf.get("view_coverage"))
check(mf["want_coverage"] == 1.0, "全字段 → 想要覆盖 100%", mf.get("want_coverage"))
check(mf["deal_coverage"] == 1.0, "全字段 → 成交覆盖 100%", mf.get("deal_coverage"))
check(mf["supply_sampled"] == 2, "meta.supply_sampled = 本批条数", mf.get("supply_sampled"))
check(mf["supply_truncated"] is False, "2 条未触顶")
check(not any("数据覆盖" in n for n in mf["notes"]), "全字段不报覆盖缺失", mf["notes"])

part = v2.score_batch([
    mk(10, 100, 3, 39, "全字段"),
    {"title": "缺成交", "price": 25, "want_count": 5, "view_count": 50},
], "缺成交")
mp = part["meta"]
check(mp["deal_coverage"] == 0.5, "2 条里 1 条缺成交 → 覆盖 50%", mp.get("deal_coverage"))
check(mp["want_coverage"] == 1.0 and mp["view_coverage"] == 1.0,
      "想要/浏览仍 100%", (mp["want_coverage"], mp["view_coverage"]))
miss = next(x for x in part["items"] if x["title"] == "缺成交")
check(any("缺成交数" in s and "中性" in s for s in miss["reasons"]),
      "缺成交的维度给了中性理由", miss["reasons"])
check(any("数据覆盖" in n and "中性 0.5" in n for n in mp["notes"]),
      "meta.notes 汇总覆盖率与中性口径", mp["notes"])

f_missing = v2.funnel_rates({"title": "x", "want_count": 5, "view_count": 50})
check(f_missing["deal_rate"] is None, "成交字段缺失 → deal_rate None（不是 0 成交）", f_missing)
f_zero = v2.funnel_rates({"title": "x", "want_count": 5,
                           "view_count": 50, "sold_count": 0})
check(f_zero["deal_rate"] == 0.0 and f_zero["sold_known"],
      "成交数 0 是有效数据（真的没卖出）", f_zero)

f_noview = v2.funnel_rates({"title": "x", "want_count": 5, "sold_count": 1})
check(f_noview["view_known"] is False and f_noview["want_rate"] is None,
      "浏览字段缺失 → 想要率 None", f_noview)
f_nowant = v2.funnel_rates({"title": "x", "view_count": 80, "sold_count": 2})
check(f_nowant["want_known"] is False and f_nowant["want_rate"] is None,
      "想要字段缺失 → 想要率 None（不是 0%）", f_nowant)

mix = v2.score_batch([
    mk(10, 100, 3, 39, "全字段"),
    {"title": "缺想要", "price": 20, "view_count": 80, "sold_count": 2},
], "缺想要")
miss_w = next(x for x in mix["items"] if x["title"] == "缺想要")
check(any("缺想要数" in s and "中性" in s for s in miss_w["reasons"]),
      "缺想要数时想要率与需求体量都写中性理由", miss_w["reasons"])

thirty = v2.score_batch([mk(i + 1, (i + 1) * 10, 0, 39, f"商品{i}")
                         for i in range(30)], "三十条")
check(thirty["meta"]["supply_sampled"] == 30, "30 条 → supply_sampled=30")
check(thirty["meta"]["supply_truncated"] is True, "30 条 → 样本触顶")
check(any("触顶" in n for n in thirty["meta"]["notes"]),
      "触顶时 meta 明确写真实供给 ≥30", thirty["meta"]["notes"])

empty_meta = v2.score_batch([])["meta"]
check(empty_meta["want_coverage"] == 0.0 and empty_meta["deal_coverage"] == 0.0
      and empty_meta["view_coverage"] == 0.0, "空批覆盖率全 0", empty_meta)
check(empty_meta["supply_sampled"] == 0 and empty_meta["supply_truncated"] is False,
      "空批 supply_sampled=0、未触顶")

cov_direct = v2.coverage_meta([{"title": "a", "price": 9},
                               {"title": "b", "price": 9, "want_count": 3}])
check(cov_direct["want_coverage"] == 0.5, "coverage_meta 可单独调用（补数后重算用）", cov_direct)


# ---------------------------------------------------------------- 口径
# 拆解 39 §八-5：平台展示值 / 本机计算 / 样本下限 必须分开标，
# 否则"成交率 80%"和"样本只有 30 条"会被读成同一件事。
prov = v2.metric_provenance()
pm = prov.get("metrics") or {}
check("口径含 8 个指标", len(pm) == 8, str(sorted(pm)))
check("平台展示值：想要/浏览/成交/价格",
      [k for k in ("want_count", "view_count", "sold_count", "price")
       if (pm.get(k) or {}).get("kind") == "displayed"] ==
      ["want_count", "view_count", "sold_count", "price"],
      str({k: (pm.get(k) or {}).get("kind") for k in pm}))
check("本机计算：想要率/成交率/机会分",
      [k for k in ("want_rate", "deal_rate", "score") if (pm.get(k) or {}).get("kind") == "computed"] ==
      ["want_rate", "deal_rate", "score"])
check("样本是下限（不是精确值）", (pm.get("sample") or {}).get("kind") == "lower_bound")
check("每个指标都写了来源", all((v or {}).get("source") for v in pm.values()))
check("每个指标都写了备注", all((v or {}).get("note") for v in pm.values()))
check("样本上限跟着常量走", prov.get("sample_cap") == v2.SUPPLY_SAMPLE_CAP, str(prov.get("sample_cap")))
check("总说明同时点出三种口径",
      all(x in (prov.get("disclaimer") or "") for x in ("平台展示值", "本机计算", "下限")),
      prov.get("disclaimer"))

check("coverage_meta 带上口径", "provenance" in cov_direct, str(list(cov_direct)))
check("空批也带口径（页面不能因为空就少了说明）",
      "provenance" in empty_meta and len(empty_meta["provenance"]["metrics"]) == 8)

_html = (BACKEND.parent / "static" / "opportunity.html").read_text(encoding="utf-8")
check("页面按三类分组展示",
      "平台展示值（快照）" in _html and "本机计算" in _html and "仅为下限" in _html)
check("页面的口径来自后端 provenance", "m.provenance" in _html)
check("页面调用受保护发布接口时带 Bearer Token",
      "localStorage.getItem('auth_token')" in _html
      and "headers.Authorization = `Bearer ${token}`" in _html)

print("\n" + "=" * 62)
if FAILS:
    print(f"通过 {PASSES} 项，失败 {len(FAILS)} 项")
    for f_ in FAILS:
        print(f"  - {f_}")
    sys.exit(1)
print(f"通过 {PASSES} 项，失败 0 项")
sys.exit(0)
