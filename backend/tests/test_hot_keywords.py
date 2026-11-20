"""
热搜词 / 爆款词榜回归测试（B1）

不碰网络：只测分词、统计、评分、配置收敛、以及 SQLite 读写。
真搜索那一段（start_scan / verify_word）靠页面手点验证，单测里只测它的边界条件。

跑法（在 backend 目录）：python3 tests/test_hot_keywords.py
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# 必须在 import 之前改路径，绝不能碰真实 data/hot_keyword.db
_TMP = tempfile.mkdtemp(prefix="hot_kw_")
os.environ["HOT_KW_DB"] = str(Path(_TMP) / "hot_kw.db")
os.environ["HOT_KW_CONFIG"] = str(Path(_TMP) / "hot_kw_config.json")

import hot_keywords as hk  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {detail}")


def _item(title, price=None, want=0, sold=0, heat=60.0, seed="种子"):
    return {"title": title, "price": price, "want_count": want, "sold_count": sold,
            "heat_score": heat, "seed": seed}


# --------------------------------------------------------------------------
# 1. 分词
# --------------------------------------------------------------------------
def test_extract_phrases():
    print("\n[1] 标题分词")
    p = hk.extract_phrases("全新 iPhone 15 Pro 手机壳 包邮")
    check("抽出品类词『手机』", "手机" in p, str(sorted(p)[:8]))
    check("抽出『手机壳』", "手机壳" in p)
    check("英文型号归一为小写", "iphone" in p)
    check("交易口水话『包邮』不进候选", "包邮" not in p)
    check("口水话『全新』不进候选", "全新" not in p)

    p2 = hk.extract_phrases("【包邮】闲置 机械键盘 95新 急出")
    check("方括号里的内容被洗掉", "包邮" not in p2 and "急出" not in p2, str(sorted(p2)))
    check("关键词仍在", "机械键盘" in p2)

    p3 = hk.extract_phrases("好了 的呢")
    check("纯虚词不出词", not (p3 & {"好了", "的呢"}), str(sorted(p3)))

    check("空标题不炸", hk.extract_phrases("") == set())
    check("链接被去掉", "https" not in " ".join(hk.extract_phrases("看图 https://x.cn/a?b=1 键盘")))


# --------------------------------------------------------------------------
# 2. 候选词统计
# --------------------------------------------------------------------------
def test_mine_candidates():
    print("\n[2] 候选词统计")
    items = [
        _item("机械键盘 87键 青轴", 199, want=12, sold=2, heat=80, seed="键盘"),
        _item("机械键盘 104键 红轴", 249, want=8, sold=1, heat=70, seed="键盘"),
        _item("蓝牙耳机 降噪", 299, want=30, sold=5, heat=90, seed="耳机"),
        _item("键盘 便宜出", 50, want=0, sold=0, heat=40, seed="键盘"),
    ]
    cands = hk.mine_candidates(items, min_freq=2)
    by = {c["word"]: c for c in cands}
    check("机械键盘出现 2 次", by.get("机械键盘", {}).get("freq") == 2)
    check("键盘出现 3 次", by.get("键盘", {}).get("freq") == 3)
    check("只出现一次的词被滤掉", "蓝牙耳机" not in by or by["蓝牙耳机"]["freq"] >= 2)
    check("种子覆盖数正确", by["键盘"]["coverage"] == 1)
    check("价格区间取到", by["键盘"]["price_min"] == 50 and by["键盘"]["price_max"] == 249)
    check("价格均值正确", by["键盘"]["price_avg"] == round((199 + 249 + 50) / 3, 1))
    check("想要数为 0 计入真实需求均值", by["键盘"]["want_avg"] == 6.7,
          str(by["键盘"]["want_avg"]))
    check("示例标题取热度最高的", by["机械键盘"]["examples"][0].startswith("机械键盘 87"))
    check("min_freq 提高后 2 次词被滤",
          all(c["freq"] >= 3 for c in hk.mine_candidates(items, min_freq=3)))


def test_zero_want_count_is_a_real_sample():
    print("\n[2b] 零想要与缺失值的均值口径")
    items = [
        _item("机械键盘 热门款", 199, want=50, seed="键盘"),
        *[
            _item(f"机械键盘 普通款 {index}", 199, want=0, seed="键盘")
            for index in range(1, 30)
        ],
    ]
    by = {
        c["word"]: c
        for c in hk.mine_candidates(items, min_freq=2)
    }
    check("50 想要加 29 个零想要，真实均值为 1.7",
          by["机械键盘"]["want_avg"] == 1.7,
          str(by["机械键盘"]["want_avg"]))

    missing_items = [
        _item("平板电脑 高需求", 999, want=50, seed="平板"),
        _item("平板电脑 想要未知", 999, want=None, seed="平板"),
    ]
    missing_by = {
        c["word"]: c
        for c in hk.mine_candidates(missing_items, min_freq=2)
    }
    check("缺失想要数不冒充零需求样本",
          missing_by["平板电脑"]["want_avg"] == 50.0,
          str(missing_by["平板电脑"]["want_avg"]))


# --------------------------------------------------------------------------
# 3. 子串去重
# --------------------------------------------------------------------------
def test_dedupe():
    print("\n[3] 子串去重")
    cands = [
        {"word": "机械键盘", "freq": 5, "coverage": 1, "heat_avg": 70, "want_avg": 5},
        {"word": "键盘", "freq": 5, "coverage": 1, "heat_avg": 70, "want_avg": 5},
    ]
    kept = {c["word"] for c in hk.score_words(cands, top_n=10)}
    check("同频次时长词保留、碎片丢弃", kept == {"机械键盘"}, str(kept))

    cands2 = [
        {"word": "机械键盘", "freq": 3, "coverage": 1, "heat_avg": 70, "want_avg": 5},
        {"word": "键盘", "freq": 20, "coverage": 3, "heat_avg": 70, "want_avg": 5},
    ]
    kept2 = {c["word"] for c in hk.score_words(cands2, top_n=10)}
    check("短词频次远高时两个都留（上位词）", kept2 == {"机械键盘", "键盘"}, str(kept2))


# --------------------------------------------------------------------------
# 4. 评分
# --------------------------------------------------------------------------
def test_score():
    print("\n[4] 评分与排序")
    cands = [
        {"word": "机械键盘", "freq": 10, "coverage": 2, "heat_avg": 80,
         "want_avg": 20, "price_min": 100, "price_max": 300, "price_avg": 200,
         "examples": [], "seeds": ["a", "b"]},
        {"word": "冷门配件", "freq": 2, "coverage": 1, "heat_avg": 20,
         "want_avg": 0, "price_min": 10, "price_max": 20, "price_avg": 15,
         "examples": [], "seeds": ["a"]},
        {"word": "高仿手表", "freq": 10, "coverage": 2, "heat_avg": 90,
         "want_avg": 30, "price_min": 1, "price_max": 2, "price_avg": 1,
         "examples": [], "seeds": ["a", "b"]},
    ]
    out = hk.score_words(cands, top_n=10)
    check("按分数降序", [c["score"] for c in out] ==
          sorted([c["score"] for c in out], reverse=True))
    check("有需求且供需更优的词排第一", out[0]["word"] == "机械键盘",
          str([x["word"] for x in out]))
    check("违禁词被重罚到 0 分", next(c for c in out if c["word"] == "高仿手表")["score"] == 0)
    check("违禁词命中被记录",
          next(c for c in out if c["word"] == "高仿手表")["risk_hits"] == ["高仿"])
    check("名次从 1 开始连续", [c["rank"] for c in out] == list(range(1, len(out) + 1)))
    check("top_n 生效", len(hk.score_words(cands, top_n=2)) == 2)
    check("空输入返回空列表", hk.score_words([]) == [])
    check("蓝海公式四项权重合计 100%",
          sum(hk.BLUE_OCEAN_WEIGHTS.values()) == 100.0,
          str(hk.BLUE_OCEAN_WEIGHTS))
    check("蓝海公式权重严格为 20/45/25/10",
          hk.BLUE_OCEAN_WEIGHTS == {
              "freq_r": 20.0,
              "demand_r": 45.0,
              "opp_ratio": 25.0,
              "coverage_r": 10.0,
          },
          str(hk.BLUE_OCEAN_WEIGHTS))

    mechanical = next(c for c in out if c["word"] == "机械键盘")
    expected_opp = round(math.log1p(20) / math.log1p(10), 4)
    check("opp_ratio 使用想要数/供给代理精确计算",
          mechanical["opp_ratio"] == expected_opp,
          f"{mechanical['opp_ratio']} != {expected_opp}")


def test_blue_ocean_ranking_and_edges():
    print("\n[4b] 蓝海供给排序与零值边界")
    cands = [
        {"word": "手机壳", "freq": 30, "coverage": 2, "heat_avg": 100,
         "want_avg": 2},
        {"word": "露营灯", "freq": 3, "coverage": 1, "heat_avg": 10,
         "want_avg": 40},
    ]
    out = hk.score_words(cands, top_n=10)
    by = {c["word"]: c for c in out}
    check("高想要低供给词胜过低需求高供给词", out[0]["word"] == "露营灯",
          str([(c["word"], c["score"]) for c in out]))
    check("高供给词的供需倍率低于低供给词",
          by["手机壳"]["opp_ratio"] < by["露营灯"]["opp_ratio"],
          str({k: v["opp_ratio"] for k, v in by.items()}))
    check("榜内最高供需倍率归一化为 1",
          by["露营灯"]["opp_ratio_r"] == 1.0,
          str({k: v["opp_ratio_r"] for k, v in by.items()}))
    expected_lamp_score = round(
        20 * (3 / 30)
        + 45 * min(1.0, math.log1p(40) / math.log1p(50))
        + 25 * 1.0
        + 10 * 0.5,
        1,
    )
    check("蓝海评分严格按 20/45/25/10 合成",
          by["露营灯"]["score"] == expected_lamp_score,
          f"{by['露营灯']['score']} != {expected_lamp_score}")

    edge_cands = [
        {"word": "零供给高需求", "freq": 0, "coverage": 0, "want_avg": 40},
        {"word": "正供给零需求", "freq": 5, "coverage": 1, "want_avg": 0},
        {"word": "缺想要字段", "freq": 2, "coverage": 1},
    ]
    edge = {c["word"]: c for c in hk.score_words(edge_cands, top_n=10)}
    check("供给为零时 opp_ratio 明确为 None",
          edge["零供给高需求"]["opp_ratio"] is None,
          str(edge["零供给高需求"]))
    check("正供给零需求时 opp_ratio 为 0",
          edge["正供给零需求"]["opp_ratio"] == 0.0,
          str(edge["正供给零需求"]))
    check("缺失想要字段按 0 计算且不炸",
          edge["缺想要字段"]["opp_ratio"] == 0.0,
          str(edge["缺想要字段"]))
    check("零值样本分数均为有限值",
          all(math.isfinite(c["score"]) for c in edge.values()),
          str({k: v["score"] for k, v in edge.items()}))


def test_build_board():
    print("\n[5] 端到端：商品列表 → 词榜")
    items = [
        _item("机械键盘 青轴 出", 199, heat=80, seed="键盘"),
        _item("机械键盘 红轴 全新", 229, heat=75, seed="键盘"),
        _item("机械键盘 静音 客制化", 399, heat=85, seed="键盘"),
        _item("降噪耳机 出", 199, heat=60, seed="耳机"),
        _item("降噪耳机 全新未拆", 259, heat=65, seed="耳机"),
    ]
    board = hk.build_board(items, min_freq=2, top_n=10)
    check("出榜", len(board) >= 2, str([b["word"] for b in board]))
    check("『机械键盘』在榜", "机械键盘" in [b["word"] for b in board])
    check("『降噪耳机』在榜", "降噪耳机" in [b["word"] for b in board])
    check("榜首是覆盖更多的那个", board[0]["word"] == "机械键盘", board[0]["word"])


# --------------------------------------------------------------------------
# 6. 配置收敛
# --------------------------------------------------------------------------
def test_config():
    print("\n[6] 配置读写的边界")
    cfg = hk.save_config({
        "seeds": [" 手机 ", "手机", "", "x" * 30, "耳机"],
        "min_freq": 99, "top_n": 999, "detail_limit": -3,
        "scan_hour": 30, "keep_days": 1, "enabled": True, "detail_enrich": 1,
    })
    check("种子去重去空", cfg["seeds"] == ["手机", "耳机"], str(cfg["seeds"]))
    check("超长种子被丢", all(len(s) <= 20 for s in cfg["seeds"]))
    check("min_freq 收敛到 10", cfg["min_freq"] == 10)
    check("top_n 收敛到 100", cfg["top_n"] == 100)
    check("detail_limit 收敛到 0", cfg["detail_limit"] == 0)
    check("scan_hour 收敛到 23", cfg["scan_hour"] == 23)
    check("keep_days 收敛到 7", cfg["keep_days"] == 7)
    check("enabled 转布尔", cfg["enabled"] is True)
    check("detail_enrich 转布尔", cfg["detail_enrich"] is True)

    again = hk.get_config()
    check("写盘后能读回", again["min_freq"] == 10 and again["seeds"] == ["手机", "耳机"])

    many = hk.save_config({"seeds": [f"词{i}" for i in range(20)]})
    check("种子数受 max_seeds 限制", len(many["seeds"]) <= many["max_seeds"])

    check("normalize_seeds 洗空白", hk.normalize_seeds(["  a  ", "a", "b"]) == ["a", "b"])
    check("normalize_seeds 空输入", hk.normalize_seeds(None) == [])


# --------------------------------------------------------------------------
# 7. 词榜落库
# --------------------------------------------------------------------------
def test_storage():
    print("\n[7] 词榜落库与取回")
    day = date.today().isoformat()
    words = [{"rank": 1, "word": "机械键盘", "freq": 3, "coverage": 1, "heat_avg": 80,
              "price_min": 100, "price_max": 300, "price_avg": 200, "want_avg": 10,
              "sold_avg": 1, "score": 88.5, "risk_hits": [], "seeds": ["键盘"],
              "examples": ["机械键盘 青轴"]},
             {"rank": 2, "word": "降噪耳机", "freq": 2, "coverage": 1, "heat_avg": 60,
              "price_min": 150, "price_max": 260, "price_avg": 200, "want_avg": 5,
              "sold_avg": 0, "score": 55.0, "risk_hits": [], "seeds": ["耳机"],
              "examples": []}]
    n = hk.save_board(words, day=day)
    check("写入 2 行", n == 2)

    board = hk.get_board(limit=10)
    check("取回当天", board["day"] == day)
    check("按分数降序", [w["word"] for w in board["words"]] == ["机械键盘", "降噪耳机"])
    check("JSON 字段还原", board["words"][0]["seeds"] == ["键盘"])
    check("risk 字段改名", "risk_hits" in board["words"][0])

    hk.save_board(words[:1], day=day)
    check("同日重扫覆盖而不是累加", hk.get_board(limit=10)["count"] == 1)

    check("不存在的日期返回空", hk.get_board(day="1999-01-01")["words"] == [])

    trend = hk.word_trend("机械键盘")
    check("单词走势可查", len(trend) >= 1 and trend[0]["day"] == day, str(trend))

    logs = hk.scan_logs(limit=5)
    check("空日志不炸", isinstance(logs, list))


# --------------------------------------------------------------------------
# 8. 扫描入口的拒绝条件（不触网）
# --------------------------------------------------------------------------
def test_scan_guards():
    print("\n[8] 扫描入口的保护（不真启动扫描）")
    orig_night = hk._night_ban
    orig_running = hk._state.get("running")
    try:
        # 已有任务在跑：必须拒绝，不排队（排队会让用户以为点了两次就是两轮）
        hk._night_ban = lambda: False
        hk._state["running"] = True
        r = hk.start_scan(["手机"])
        check("已有扫描时拒绝", r.get("ok") is False and "已有" in r.get("error", ""), str(r))

        # 夜间：即使手动点也不跑
        hk._state["running"] = False
        hk._night_ban = lambda: True
        r2 = hk.start_scan(["手机"])
        check("夜间拒绝扫描", r2.get("ok") is False and "夜间" in r2.get("error", ""),
              str(r2))
    finally:
        hk._night_ban = orig_night
        hk._state["running"] = bool(orig_running)


def test_verify_cache():
    print("\n[9] 验证缓存")
    payload = {"word": "机械键盘", "supply": 30, "want_median": 12, "gap": 0.4,
               "judgement": "供需还行", "ts": hk.datetime.now().isoformat(timespec="seconds")}
    hk._save_verify("机械键盘", payload)
    got = hk._cached_verify("机械键盘")
    check("缓存能读回", got and got["supply"] == 30, str(got))
    check("读回带 cached 标记", got.get("cached") is True)
    check("没缓存的词返回 None", hk._cached_verify("从没搜过的词") is None)


def test_verify_basis():
    print("\n[10] 供给样本口径（B1 诚实化）")
    b30 = hk._supply_basis(30)
    check("数满一页判为饱和", b30["supply_saturated"] is True, str(b30))
    check("饱和保留旧字段别名 supply_truncated", b30["supply_truncated"] is True)
    check("样本数不被改写成市场总量",
          b30["supply_sampled"] == 30 and b30["supply_cap"] == 30, str(b30))

    b12 = hk._supply_basis(12)
    check("不足一页不算饱和", b12["supply_saturated"] is False, str(b12))
    b0 = hk._supply_basis(0)
    check("0 件不算饱和", b0["supply_saturated"] is False, str(b0))

    # 旧版缓存只有 supply，没有新字段：读出来必须补齐，页面才不会显示 undefined
    old = hk._backfill_basis({"supply": 30, "gap": 0.4, "want_median": 12})
    check("旧缓存补出 supply_sampled", old.get("supply_sampled") == 30, str(old))
    check("旧缓存补出 supply_saturated", old.get("supply_saturated") is True)
    check("旧缓存补出 want_sampled", old.get("want_sampled") == 0)
    check("旧缓存已有字段不被覆盖", old.get("gap") == 0.4)

    check("饱和时结论写明真实下限",
          "≥30" in hk._verify_judgement(3.2, "detail", 30, True),
          hk._verify_judgement(3.2, "detail", 30, True))
    check("未饱和时不写 >=",
          "≥" not in hk._verify_judgement(0.4, "detail", 12, False),
          hk._verify_judgement(0.4, "detail", 12, False))
    check("0 件有单独文案",
          "0 件" in hk._verify_judgement(0, "none", 0, False))
    check("没有可信想要数时仍带样本口径",
          "样本口径" in hk._verify_judgement(0, "none", 30, True))


def main():
    test_extract_phrases()
    test_mine_candidates()
    test_zero_want_count_is_a_real_sample()
    test_dedupe()
    test_score()
    test_blue_ocean_ranking_and_edges()
    test_build_board()
    test_config()
    test_storage()
    test_scan_guards()
    test_verify_cache()
    test_verify_basis()

    print(f"\n{'=' * 56}")
    if FAILS:
        print(f"失败 {len(FAILS)} 项 / 通过 {PASSES} 项")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"全部通过（{PASSES} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
