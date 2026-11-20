"""
回复三档（auto / draft / block）回归测试（拆解 39 §八-3）
=========================================================
锁死验收口径：**draft 和 block 都不会自动发送。**

1. 判定：block（导流/号码）> draft（售后/退款/承诺）> auto；
2. 默认行为：draft 扣下不发（`REPLY_TIER_ENFORCE=0` 才退回"照发、只标注"）；
3. 发件箱：draft 落 `held`；**held 不进补发队列**，只能人放行；
   `begin()` 撞上 held 必须拒绝（不能顺着 pending/failed 的分支把状态改成 sending）；
4. 接线：判定必须在唯一的发送口（`XianyuAutoAsync._outbox_begin`）执行，
   而不是只放在 AI 引擎里 —— 关键词回复根本不走 AI 引擎；
5. AI 引擎不再自己吞掉 draft（否则发件箱里查不到"有句话被拦了"）。

跑法（在 backend 目录）：python3 tests/test_reply_tier.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="reply_tier_")
os.environ["REPLY_OUTBOX_DB"] = str(Path(_TMP) / "outbox.db")
for _k in ("REPLY_GUARD_ENABLED", "REPLY_TIER_ENFORCE"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reply_guard as rg   # noqa: E402
import reply_outbox as ro  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
FAILS: list[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {name}" + (f"（{detail}）" if detail else ""))
    else:
        FAILS.append(name + (f"（{detail}）" if detail else ""))
        print(f"  FAIL {name}" + (f"（{detail}）" if detail else ""))


def section(title: str) -> None:
    print(f"\n[{title}]")


# ---------------------------------------------------------------- 1 判定
section("1 三档判定")

c = rg.classify("可以呀，加我微信聊")
check("导流 → block", c["tier"] == "block", c["tier"])

c = rg.classify("我电话 13812345678，你打给我")
check("号码形态 → block", c["tier"] == "block", c["tier"])

c = rg.classify("这个支持七天无理由退款，收到不满意可以退")
check("售后/退款 → draft", c["tier"] == "draft", c["tier"])
check("命中词记下来了", "退款" in c["draft_hits"], str(c["draft_hits"]))

c = rg.classify("少发了您别急，我核对后补发")
check("补发 → draft", c["tier"] == "draft", c["tier"])

c = rg.classify("我保证是正品")
check("承诺 + 极限词 → draft", c["tier"] == "draft", c["tier"])

c = rg.classify("加微信退款给你")
check("block 优先于 draft", c["tier"] == "block", c["tier"])

c = rg.classify("已经发货了，预计后天到")
check("普通回复 → auto", c["tier"] == "auto", c["tier"])

c = rg.classify("")
check("空文本 → auto（交给上游判空）", c["tier"] == "auto", c["tier"])

# ---------------------------------------------------------------- 2 默认扣下
section("2 draft 默认扣下")

os.environ.pop("REPLY_TIER_ENFORCE", None)
check("默认 enforce_draft() 为真（draft 不发）", rg.enforce_draft() is True)
c = rg.classify("可以退款")
check("classify 也标了 enforced", c["enforced"] is True)

os.environ["REPLY_TIER_ENFORCE"] = "0"
check("REPLY_TIER_ENFORCE=0 → 退回照发", rg.enforce_draft() is False)
check("classify 相应标 enforced=False", rg.classify("可以退款")["enforced"] is False)
os.environ.pop("REPLY_TIER_ENFORCE", None)

# ---------------------------------------------------------------- 3 发件箱
section("3 发件箱 held：扣下、不补、只能人放行")

r = ro.hold("acc1", "MSG1", chat_id="c1", item_id="i1", user_id="u1",
            reply_text="这个支持七天退款", tier="draft", reason="需人工收尾：退款")
rec = r.get("record") or {}
check("登记为 held", rec.get("status") == "held", str(rec.get("status")))
check("tier 落库为 draft", rec.get("tier") == "draft", str(rec.get("tier")))
check("内容留在行里（人要看得到说了什么）", "退款" in (rec.get("reply_text") or ""))

check("held 不进补发队列", not any(x["source_msg_id"] == "MSG1" for x in ro.pending_rows("acc1")))
check("status_of 看到 held", ro.status_of("acc1", "MSG1") == "held")

b = ro.begin("acc1", "MSG1", reply_text="这个支持七天退款")
check("begin 撞上 held 被拒绝（绝不改成 sending）", b.get("ok") is False, str(b))
check("拒绝原因是 held", b.get("reason") == "held", str(b.get("reason")))
check("状态仍是 held", ro.status_of("acc1", "MSG1") == "held")

st = ro.stats()
check("统计里有 held 计数", st.get("held") == 1, str(st))
check("统计里有 tier_draft 计数", st.get("tier_draft", 0) >= 1, str(st.get("tier_draft")))

check("人放行成功", ro.release("acc1", "MSG1") is True)
check("放行后变 pending（下一轮心跳才发）", ro.status_of("acc1", "MSG1") == "pending")
check("放行后才进补发队列", any(x["source_msg_id"] == "MSG1" for x in ro.pending_rows("acc1")))

# held 也能被人工弃用
ro.hold("acc1", "MSG2", reply_text="我保证正品", tier="draft", reason="需人工收尾：保证")
check("held → skipped 允许（人确认不发）",
      (ro.finish("acc1", "MSG2", "skipped", "人工确认：不再发送") or {}).get("status") == "skipped")

# held 不能被迟到的 failed/sent 覆盖（除非人判 skipped）
ro.hold("acc1", "MSG3", reply_text="一定可以退", tier="draft", reason="需人工收尾")
ro.finish("acc1", "MSG3", "sent")
check("held 不被迟到的 sent 覆盖", ro.status_of("acc1", "MSG3") == "held")

# purge 不动 held
import sqlite3  # noqa: E402
conn = sqlite3.connect(str(ro.DB_PATH))
conn.execute("UPDATE reply_outbox SET updated_at='2020-01-01 00:00:00' WHERE source_msg_id='MSG3'")
conn.commit()
conn.close()
ro.purge(keep_days=1)
check("purge 不删 held（还有账要看）", ro.status_of("acc1", "MSG3") == "held")

# ---------------------------------------------------------------- 4 接线
section("4 接线：发送口执行三档")

src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
i_begin = src.find("def _outbox_begin")
i_gate = src.find('if _tier == "block"', i_begin)
i_draft = src.find('if _tier == "draft" and _tier_enforce', i_begin)
i_claim = src.find("res = _ro.begin(", i_begin)
check("判定在 _outbox_begin 里", i_begin > 0 and i_gate > i_begin)
check("block 先判、draft 后判", 0 < i_gate < i_draft)
check("两档都在抢发送权之前", 0 < i_gate < i_claim and 0 < i_draft < i_claim)
check("block 记录成 skipped（终态，重推不复活）",
      re.search(r'if _tier == "block":.*?_ro\.finish\([^)]*"skipped"', src, re.S) is not None)
check("draft 记录成 held", re.search(r'if _tier == "draft".*?_ro\.hold\(', src, re.S) is not None)
check("draft 登记失败时不照发", re.search(
    r"draft 扣下登记失败，本条不发.*?return False", src, re.S) is not None)
check("用了 enforce_draft() 而不是写死", "_rg_tier.enforce_draft()" in src)
check("held 在重推跳过名单里", '"unknown", "held"' in src or '"held", "unknown"' in src)

eng = (BACKEND / "ai_reply_engine.py").read_text(encoding="utf-8")
i_d = eng.find('if rg.get("tier") == "draft":')
check("AI 引擎还认得 draft 档", i_d > 0)
i_d_end = eng.find("except Exception as _e:", i_d)
check("AI 引擎不再自己吞掉 draft（draft 分支里没有 return None）",
      0 < i_d < i_d_end and "return None" not in eng[i_d:i_d_end],
      eng[i_d:i_d_end].replace("\n", " ")[:80])

html = (BACKEND.parent / "static" / "reply_outbox.html").read_text(encoding="utf-8")
check("发件箱页面有 held 的说明", "扣下待确认" in html)
check("发件箱页面能放行 draft", 'data-t="draft"' in html and "放行发送" in html)

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")