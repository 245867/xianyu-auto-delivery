"""
拆解 8：词池生命周期（cumin777/xianyu-hotword-scout 的 trial → active → retired）。

守住七件事：
[1] 新词一律先进 trial，不会直接 active
[2] 攒够天数才评一次；不够天数一直等着（不提前判死刑）
[3] 均分够 → active，不够 → retired；退休带原因
[4] 同一天重扫不重复计天（否则一天扫三次等于三天）
[5] active 连续低分 / 连续缺席 → retired；缺席比低分更硬
[6] 池满末位淘汰；退役词不会自己回池；人工可 revive
[7] 违禁词不进池，已在池里的当场面退

跑法：python tests/test_pool_lifecycle.py
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="hkpool_"))
os.environ["HOT_KW_DB"] = str(_TMP / "hk.db")
os.environ["HOT_KW_CONFIG"] = str(_TMP / "hk.json")

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import hot_keywords as hk

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


def board(*pairs):
    return [{"word": w, "score": s, "risk_hits": []} for w, s in pairs]


def reset(**patch):
    if hk.DB_FILE.exists():
        hk.DB_FILE.unlink()
    if hk.CONFIG_FILE.exists():
        hk.CONFIG_FILE.unlink()
    hk.save_config(dict(hk.DEFAULT_CONFIG, **patch))


def state(word):
    for w in hk.pool_overview(limit=500)["words"]:
        if w["word"] == word:
            return w
    return None


# ---------------------------------------------------------------- [1] 先进 trial
section("1 新词先进 trial")
reset()
hk.update_pool(board(("机械键盘", 70.0)), day="2026-11-01")
w = state("机械键盘")
check(w and w["state"] == "trial", "第一天是 trial（不直接 active）")
check(w["days_seen"] == 1, "记了 1 天")
check(hk.pool_overview()["counts"] == {"trial": 1, "active": 0, "retired": 0},
      "计数对得上")


# ---------------------------------------------------------------- [2] 不提前判
section("2 天数不够不评估")
reset(trial_days=3)
hk.update_pool(board(("好词", 90.0)), day="2026-11-01")
hk.update_pool(board(("好词", 90.0)), day="2026-11-02")
check(state("好词")["state"] == "trial", "2 天 < trial_days=3 → 还在 trial")
acts = hk.update_pool(board(("好词", 90.0)), day="2026-11-03")
check(state("好词")["state"] == "active", "第 3 天才转正")
check(any(a["word"] == "好词" and a["to"] == "active" for a in acts), "迁移被记录下来")


# ---------------------------------------------------------------- [3] 转正/退休
section("3 均分决定转正还是退休")
reset(trial_days=2, promote_score=45)
hk.update_pool(board(("强词", 80.0), ("弱词", 5.0)), day="2026-11-01")
acts = hk.update_pool(board(("强词", 80.0), ("弱词", 5.0)), day="2026-11-02")
check(state("强词")["state"] == "active", "均分 80 ≥ 45 → active")
check(state("弱词")["state"] == "retired", "均分 5 < 45 → retired")
weak = state("弱词")
check("未达标" in (weak["retire_reason"] or ""), f"退休原因写得清（{weak['retire_reason']}）")
check(any("5 天" in x or "2 天均分" in x["why"] for x in acts if x["word"] == "弱词"),
      "动作里有原因")

# 边界：正好等于阈值算过（>= 而不是 >）
reset(trial_days=1, promote_score=45)
hk.update_pool(board(("临界词", 45.0)), day="2026-11-01")
check(state("临界词")["state"] == "active", "正好 45 分 → 转正（阈值含等号）")


# ---------------------------------------------------------------- [4] 同日不重复计天
section("4 同一天重扫不重复计天")
reset(trial_days=2)
hk.update_pool(board(("键盘", 50.0)), day="2026-11-01")
hk.update_pool(board(("键盘", 55.0)), day="2026-11-01")
hk.update_pool(board(("键盘", 60.0)), day="2026-11-01")
w = state("键盘")
check(w["days_seen"] == 1, f"扫三次仍算 1 天（实际 {w['days_seen']}）")
check(abs(float(w["score_last"]) - 60.0) < 0.01, "分数取最后一次")
check(abs(float(w["score_avg"]) - 60.0) < 0.01, "均分不因为多次扫描被稀释")


# ---------------------------------------------------------------- [5] active 退休
section("5 active 的两种退休")
reset(trial_days=1, promote_score=10, retire_score=20, retire_low_days=3,
      retire_absent_days=7)
hk.update_pool(board(("活跃词", 50.0)), day="2026-11-01")
check(state("活跃词")["state"] == "active", "先转正")

# 连续低分 3 天 → 退休
for i, d in enumerate(("2026-11-02", "2026-11-03", "2026-11-04"), 1):
    hk.update_pool(board(("活跃词", 5.0)), day=d)
    st = state("活跃词")["state"]
    if i < 3:
        check(st == "active", f"第 {i} 天低分还在 active（不能一天就杀）")
    else:
        check(st == "retired", "连续 3 天低分 → retired")

# 缺席：连着几天没上榜
reset(trial_days=1, promote_score=10, retire_score=0, retire_low_days=99,
      retire_absent_days=3)
hk.update_pool(board(("会消失的词", 50.0)), day="2026-11-01")
check(state("会消失的词")["state"] == "active", "先转正")
hk.update_pool(board(("别的词", 50.0)), day="2026-11-02")
check(state("会消失的词")["state"] == "active", "缺席 1 天不动")
hk.update_pool(board(("别的词", 50.0)), day="2026-11-04")
check(state("会消失的词")["state"] == "retired", "缺席满 3 天 → retired")
check("未上榜" in (state("会消失的词")["retire_reason"] or ""), "原因写明缺席")

# trial 也会因缺席被清掉（不然池子里全是"露一次脸就消失"的噪声）
reset(trial_days=5, retire_absent_days=3)
hk.update_pool(board(("一闪而过", 90.0)), day="2026-11-01")
hk.update_pool(board(("别的", 10.0)), day="2026-11-05")
check(state("一闪而过")["state"] == "retired", "trial 里的噪声词也会被清掉")


# ---------------------------------------------------------------- [6] 容量 + 复活
section("6 池满淘汰、退役不回池、人工可复活")
reset(trial_days=1, promote_score=1, pool_cap=20)
# pool_cap 最小 20，所以造 20 个占满
for i in range(20):
    hk.update_pool(board((f"词{i}", 30.0 + i)), day="2026-11-01")
check(hk.pool_overview()["counts"]["active"] == 20, "池子满了（20）")
acts = hk.update_pool(board(*[((f"词{i}", 30.0 + i)) for i in range(20)],
                            ("新来的", 99.0)), day="2026-11-02")
check(any(("池满" in a["why"]) or ("挤出" in a["why"]) for a in acts),
      "有末位淘汰动作（转正那条记录里写着挤出了谁）")
check(any(x["from_s"] == "active" and x["to_s"] == "retired"
          for x in hk.pool_log(50)), "被挤出的词有独立的迁移记录")
check(state("新来的")["state"] in ("trial", "active"), "新词自己进池")
check(hk.pool_overview()["counts"]["active"] <= 20, "active 不超过上限")

# 退役词明天又上榜了，也不能自己回到 active
reset(trial_days=1, promote_score=45)
hk.update_pool(board(("差词", 10.0)), day="2026-11-01")
check(state("差词")["state"] == "retired", "先被退休")
hk.update_pool(board(("差词", 99.0)), day="2026-11-02")
check(state("差词")["state"] == "retired", "高分回来也不会自己复活")
check(hk.revive_word("差词", "人工复核通过"), "人工可以复活")
check(state("差词")["state"] == "trial", "复活后回 trial 重新考察")
check(hk.revive_word("不存在") is False, "不存在的词复活返回 False")
hk.update_pool(board(("差词", 99.0)), day="2026-11-03")
check(state("差词")["state"] == "active", "复活后确实能再转正")


# ---------------------------------------------------------------- [7] 违禁词
section("7 违禁词不进池")
reset(trial_days=1, promote_score=1)
hk.update_pool([{"word": "高仿手表", "score": 0.0, "risk_hits": ["高仿"]}],
               day="2026-11-01")
check(state("高仿手表") is None, "违禁词压根不进池")

# 已经在池里的词，今天命中违禁 → 当场面退，不等缺席
reset(trial_days=1, promote_score=1)
hk.update_pool(board(("某词", 90.0)), day="2026-11-01")
check(state("某词")["state"] == "active", "先进 active")
hk.update_pool([{"word": "某词", "score": 0.0, "risk_hits": ["违禁"]}], day="2026-11-02")
check(state("某词")["state"] == "retired", "命中违禁当场面退（不等 7 天）")
check("违禁词" in (state("某词")["retire_reason"] or ""), "原因写明")


# ---------------------------------------------------------------- [8] 配置收敛
section("8 配置收敛")
reset()
c = hk.save_config({"trial_days": 999, "retire_absent_days": -5,
                    "pool_cap": 99999, "promote_score": "abc",
                    "retire_low_days": 0})
check(c["trial_days"] == 14, "trial_days 收到上限 14")
check(c["retire_absent_days"] == 2, "retire_absent_days 收到下限 2")
check(c["pool_cap"] == 1000, "pool_cap 收到上限 1000")
check(c["promote_score"] == 45.0, "非法分数回默认值")
check(c["retire_low_days"] == 2, "retire_low_days 不低于 2")


# ---------------------------------------------------------------- [8b] 词池出口
section("8b 用词池补种子词（唯一的出口）")
reset(trial_days=1, promote_score=10)
hk.save_config({"seeds": ["手机"], "max_seeds": 4})
hk.update_pool(board(("机械键盘", 80.0), ("键盘", 90.0), ("手办", 60.0)), day="2026-11-01")
hk.update_pool(board(("机械键盘", 80.0), ("键盘", 90.0), ("手办", 60.0)), day="2026-11-02")
r = hk.seed_from_pool()
check(r["ok"] and "键盘" in r["seeds"], "在高分的在用词被补进种子")
check("机械键盘" not in r["seeds"], "同市场词不重复占位（键盘已进就不再加机械键盘）")
check(r["seeds"][0] == "手机", "手写的种子词排在前、不会被顶掉")
check(hk.get_config()["seeds"] == r["seeds"], "确实落盘了（不是只返回）")

r2 = hk.seed_from_pool()
check(r2["added"] == [], "再点一次不重复加")

# 空位不够就只补到上限
reset(trial_days=1, promote_score=1)
hk.save_config({"seeds": ["手机", "球鞋"], "max_seeds": 3})
hk.update_pool(board(("手办", 50.0), ("咖啡机", 40.0)), day="2026-11-01")
r3 = hk.seed_from_pool()
check(len(r3["seeds"]) <= 3, f"不超过 max_seeds（实际 {len(r3['seeds'])}）")
check(len(r3["added"]) == 1, "只补一个空位")

# 退役词不会被补进来
reset(trial_days=1, promote_score=45)
hk.save_config({"seeds": [], "max_seeds": 5})
hk.update_pool(board(("差词", 5.0)), day="2026-11-01")
check(state("差词")["state"] == "retired", "（先让它退役）")
check(hk.seed_from_pool()["added"] == [], "退役词不进种子")


# ---------------------------------------------------------------- [9] 接线
section("9 接线")
src = (BACKEND / "hot_keywords.py").read_text(encoding="utf-8")
i_save = src.find("save_board(words, keep_days=cfg[\"keep_days\"])")
i_pool = src.find("update_pool(words)")
check(i_pool > i_save > 0, "先存榜单、再更新词池")
check("except Exception as e:" in src[i_pool - 200:i_pool + 300],
      "词池失败不影响榜单（各自 try）")

api = (BACKEND / "hot_keyword_api.py").read_text(encoding="utf-8")
check("/api/hot-keywords/pool" in api, "有词池接口")
check("/api/hot-keywords/revive" in api, "有复活接口")
check("/api/hot-keywords/seed-from-pool" in api, "有补种子接口")

html = (BACKEND.parent / "static" / "hot_keywords.html").read_text(encoding="utf-8")
check("loadPool()" in html, "页面会拉词池")
check("reviveWord(" in html, "页面能点复活")
check("seedFromPool(" in html and "btnSeedPool" in html, "页面能一键补种子")


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
