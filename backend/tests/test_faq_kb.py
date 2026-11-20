"""
方向三：买家提问观测与 FAQ 建议
================================

要守住的东西：
[1] 归一化：全角/空格/标点/表情不该改变一句话的身份
[2] 落盘 + 回填：每条买家消息都有结果，覆盖率算得出来
[3] 聚类：三个买家问同一件事 → 冒出一条建议；只问一次的噪声不冒
[4] 剔除已覆盖：已经有词条的问题不该再被建议
[5] 模糊命中门槛：< 4 个字的词条绝不参与归一化兜底
[6] 接线：回复链路先落盘（关键词命中的消息原来根本不进任何记录）

跑法：cd backend && python3 tests/test_faq_kb.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="faq_"))
os.environ["FAQ_DB"] = str(_TMP / "faq.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import faq_kb as fk  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
FAILS = []


# 假的词条库：真的 db_manager 会把主库拉起来（沙箱里还会 disk I/O error），
# 而这里要验证的只是"已覆盖的短语要被剔除"。
KW = []


class _FakeDB:
    def get_keywords_with_type(self, cookie_id):
        return list(KW)


sys.modules["db_manager"] = type("M", (), {"db_manager": _FakeDB()})


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


# ---------------------------------------------------------------- [1]
section("1 归一化")
check(fk.norm("多久 发货？") == "多久发货", "空格+问号被吃掉")
check(fk.norm("多久发货？？？") == "多久发货", "重复标点被吃掉")
check(fk.norm("多久发货...") == "多久发货", "英文省略号被吃掉")
check(fk.norm("  多 久 发 货  ") == "多久发货", "全空白被吃掉")
check(fk.norm("ＤＵＯ　ＪＩＵ") == "duojiu", "全角字母转半角并小写")
check(fk.norm("多久发货😀") == "多久发货", "表情被吃掉")
check(fk.norm("") == "", "空串还是空串")

section("1b 模糊命中门槛")
check(fk.match_normalized("多久发货", "大概 多久 发货 呀") is True, "4 字词条可以兜底命中")
check(fk.match_normalized("包邮", "包邮吗") is False, "2 字词条不参与（太容易误命中）")
check(fk.match_normalized("多久发货", "在吗") is False, "完全不相关不命中")

# ---------------------------------------------------------------- [2]
section("2 落盘 + 回填 + 覆盖率")
q1 = fk.log("CK", "CHAT1", "U1", "IT1", "多久发货？")
q2 = fk.log("CK", "CHAT1", "U1", "IT1", "能便宜点吗")
q3 = fk.log("CK", "CHAT1", "U1", "IT1", "支持验货吗")
q4 = fk.log("CK", "CHAT1", "U1", "IT1", "这个怎么激活")
check(q1 and q2 and q3 and q4, "四条都拿到 id")
fk.note(q1, "keyword", "多久发货")
fk.note(q2, "ai")
fk.note(q3, "default")
# q4 故意不回填：模拟"没答上"
st = fk.stats(30, "CK")
check(st["total"] == 4, "总数 4")
check(st["answered"] == 3, "答上 3（keyword/ai/default 都算）")
check(abs(st["coverage"] - 0.75) < 1e-6, "覆盖率 0.75")
check(st["by_outcome"].get("none") == 1, "没回填的那条算 none")

check(fk.note(None, "ai") is False, "qid 为空时不炸（落盘失败也不影响回复）")
check(fk.log("CK", "CHAT1", "U1", "IT1", "") is None, "空消息不落盘")

# 时间窗
old = datetime.now() - timedelta(days=40)
q_old = fk.log("CK", "CHAT1", "U1", "IT1", "很久以前的问题", now=old)
fk.note(q_old, "none")
check(fk.stats(30, "CK")["total"] == 4, "40 天前的不进 30 天窗口")
check(fk.stats(60, "CK")["total"] == 5, "60 天窗口能看到它")

# ---------------------------------------------------------------- [3]
section("3 聚类：三条以上同类问题才成建议")
for raw in ["有货吗", "还有货吗", "请问有货吗", "有现货吗", "现货吗今天能发吗"]:
    fk.note(fk.log("CK", "CHAT2", "U2", "IT1", raw), "none")

sug = fk.suggestions(30, min_count=3, cookie_id="CK")
phrases = [s["phrase"] for s in sug]
print("   建议：", phrases)
check(any("有货" in p or "现货" in p for p in phrases), "冒出与「有货/现货」相关的建议")
check(all(s["count"] >= 3 for s in sug), "每条建议至少 3 个买家问过")
check(all(s["examples"] for s in sug), "每条建议带例句（人能判断该不该加）")

# 只问一次的噪声不该成建议
fk.log("CK", "CHAT3", "U3", "IT1", "你们家在北京吗")
sug2 = fk.suggestions(30, min_count=3, cookie_id="CK")
check(not any("北京" in s["phrase"] for s in sug2), "只问一次的不成建议")

# ---------------------------------------------------------------- [4]
section("4 已有词条覆盖的问题不再建议")
KW.append({"keyword": "有货吗", "reply": "有货", "item_id": "", "type": "text"})
sug3 = fk.suggestions(30, min_count=3, cookie_id="CK")
check(not any("有货" in s["phrase"] for s in sug3), "已覆盖的短语被剔除")

# ---------------------------------------------------------------- [5]
section("5 起手词条不含价格类")
starter_kws = [k for k, _ in fk.STARTER_PACK]
check(starter_kws, "起手词条非空")
check(not any(w in k for k in starter_kws for w in ("便宜", "最低", "多少钱", "优惠", "刀")),
      "价格类一条都不收（否则会把 B3 议价整条绕过）")
for k, r in fk.STARTER_PACK:
    check("微信" not in r and "加我" not in r, f"「{k}」的回复不含导流词")

# ---------------------------------------------------------------- [6]
section("6 接线")
src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
check("import faq_kb" in src, "回复主流程 import faq_kb")
check("_fk_q.safe_log(" in src, "每条买家消息先落盘")
check("safe_note(_qid" in src, "回复完回填结果")
check("_kw_match" in src, "关键词命中走统一判定（精确优先 + 归一化兜底）")
check("_fk.match_keyword(kw, message)" in src, "归一化兜底真的接到了")
check("def _kw_match(keyword: str, message: str)" in src, "_kw_match 是独立方法（两个入口共用）")

api = (BACKEND / "faq_api.py")
check(api.exists(), "有 faq_api.py")
srv = (BACKEND / "reply_server.py").read_text(encoding="utf-8")
check("faq_api" in srv, "路由已注册")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")