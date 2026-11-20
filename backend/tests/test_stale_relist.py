"""
C2 长期零出单重发的用例。

守住的六件事：
[1] 判据是"与"不是"或" —— 三个条件缺一个都不许翻新（宁可漏杀，不可误杀）
[2] 数据缺失不判死 —— 取不到浏览/成交/天数时不动，绝不当成 0 处理
[3] 冷却是真的拦 —— 翻新过的不许立刻再来一次
[4] 半自动边界 —— 旧链接只在重建**成功**后才进待下架清单，且我们从不调下架接口
[5] 默认关 + 演练 —— 出厂状态一定不写任何东西
[6] 标题变体 —— 换字符串但不换卖点、不超 30 字、过得了 publish_guard

跑法：cd backend && python tests/test_stale_relist.py
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="stalere_"))
os.environ["STALE_CONFIG"] = str(_TMP / "cfg.json")
os.environ["STALE_STATE"] = str(_TMP / "state.json")
os.environ["SHOP_DIAG_DB"] = str(_TMP / "shop.db")
os.environ["SHOP_DIAG_CONFIG"] = str(_TMP / "shop.json")
os.environ["RATE_LIMIT_STATE"] = str(_TMP / "rate.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stale_relist as sr

FAILS = []


def check(cond, msg, detail=""):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}" + (f"  ← {detail}" if detail else ""))


def section(t):
    print(f"\n[{t}]")


def reset():
    for p in (sr.CONFIG_FILE, sr.STATE_FILE):
        try:
            Path(p).unlink()
        except FileNotFoundError:
            pass


def item(iid="1001", title="测试商品", days=10, browse=10, want=0, sold=0, **kw):
    """造一条候选。days 用 first_seen 反推，走的是真实的时间计算路径。"""
    now = datetime.now()
    base = {
        "item_id": iid, "title": title,
        "first_seen": (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
        "browse": browse, "want": want, "sold": sold, "stock": 1,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- [1] 判据
section("1 三个条件的与关系")
reset()
cfg = sr.get_config()

check(sr.judge(item(days=10, browse=10, sold=0), cfg)["rework"] is True,
      "10 天 / 浏览 10 / 0 单 → 该翻新")

# 缺任一条都不行
r = sr.judge(item(days=3, browse=10, sold=0), cfg)
check(r["rework"] is False and "too_new" in r["blockers"],
      "只上架 3 天 → 不翻新（新品有初始流量池）", str(r["blockers"]))

r = sr.judge(item(days=10, browse=500, sold=0), cfg)
check(r["rework"] is False and "has_traffic" in r["blockers"],
      "浏览 500 → 不翻新（还有流量）", str(r["blockers"]))

r = sr.judge(item(days=10, browse=10, sold=3), cfg)
check(r["rework"] is False and "has_sales" in r["blockers"],
      "出过 3 单 → 不翻新（出过单的链接数据最好）", str(r["blockers"]))

# 边界：恰好达标
check(sr.judge(item(days=7, browse=49, sold=1), {
    **cfg, "require_zero_sold": False, "min_days": 7})["rework"] is True,
    "边界：7 天整、浏览 49、允许出单 → 翻新")
check(sr.judge(item(days=7, browse=50, sold=0), cfg)["rework"] is False,
    "边界：浏览恰好 50 → 不翻新（门槛是 <50）")
check(sr.judge(item(days=6.9, browse=10, sold=0), cfg)["rework"] is False,
    "边界：6.9 天 → 不翻新")

# ---------------------------------------------------------------- [2] 缺数据
section("2 数据缺失时不动（不拿 0 冒充）")

r = sr.judge(item(browse=None), cfg)
check(r["rework"] is False and "no_browse" in r["blockers"],
      "没有浏览数 → 不判（不当成 0）", str(r["blockers"]))
check("没有浏览数" in r["reason"], "理由是人话，能贴到页面上")

r = sr.judge({"item_id": "x", "title": "t", "browse": 0, "sold": 0}, cfg)
check(r["rework"] is False and "no_age" in r["blockers"],
      "不知道上架多久 → 不判")

r = sr.judge(item(sold=None), cfg)
check(r["rework"] is False and "no_sold" in r["blockers"],
      "拿不到成交数 + 要求从未出单 → 不判（不能猜它没出单）", str(r["blockers"]))

check(sr.judge(item(sold=None), {**cfg, "require_zero_sold": False})["rework"] is True,
      "关掉『必须从未出单』后，成交数缺失不再是阻塞项")

# ---------------------------------------------------------------- [3] 冷却
section("3 冷却与停用")

now = datetime.now()
just = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
r = sr.judge(item(last_reworked_at=just), cfg, now=now)
check(r["rework"] is False and "cooldown" in r["blockers"],
      "1 天前刚翻新过 → 冷却拦住（默认 7 天）", str(r["blockers"]))
check("还要等" in r["reason"], "冷却理由给出剩余天数")

old = (now - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
check(sr.judge(item(last_reworked_at=old), cfg, now=now)["rework"] is True,
      "8 天前翻新过 → 冷却已过，可以再来")

r = sr.judge(item(disabled=True), cfg)
check(r["rework"] is False and "disabled" in r["blockers"],
      "人工/自动停用的不再动")

# ---------------------------------------------------------------- [4] 上架时长
section("4 上架时长：平台时间优先，观测起点兜底，取更老的")

now = datetime.now()
# 平台给的是 30 天前，我们 2 天前才第一次看到 → 应该认 30 天
d = sr.age_days((now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
                int((now - timedelta(days=30)).timestamp() * 1000), now=now)
check(d is not None and 29.9 < d < 30.1,
      "毫秒时间戳 30 天前 + 观测 2 天 → 认 30 天（否则老店要空跑 7 天）", str(d))

d2 = sr.age_days((now - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S"), None, now=now)
check(d2 is not None and 19.9 < d2 < 20.1, "没有平台时间 → 用观测起点")

d3 = sr.age_days((now - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S"),
                 "垃圾数据", now=now)
check(d3 is not None and 19.9 < d3 < 20.1, "平台时间解析不了 → 不崩，退回观测起点")

# 未来时间（时区错/脏数据）不能变负数，否则冷却全线失效
d4 = sr.age_days(None, int((now + timedelta(days=5)).timestamp()), now=now)
check(d4 == 0.0, "未来时间 → 0 天（不是负数）", str(d4))

check(sr.age_days(None, None, now=now) is None, "什么都没有 → None（不猜）")

# ---------------------------------------------------------------- [5] 标题变体
section("5 标题变体：变字符串，不变卖点")

t = "全新未拆封 蓝牙耳机 长续航"
v0 = sr.title_variant(t, 0)
check(v0 != t, "第 0 个变体和原标题不同", repr(v0))
check(len(v0) <= 30, f"变体不超 30 字（{len(v0)}）", v0)
check(sr.title_variant(t, 0) == v0, "同一轮次幂等（重试对得上）")
check(sr.title_variant(t, 1) != v0, "不同轮次给不同变体")

check(sr.title_variant("", 0) == "", "空标题不炸")

# 变体必须过合规闸：它只是换分隔符/加时效前缀，不该引入违禁词
try:
    from publish_guard import check_publish
    for i in range(6):
        v = sr.title_variant(t, i)
        g = check_publish(v, "描述")
        check(g["ok"], f"变体 #{i} 过 publish_guard", f"{v} → {g.get('block')}")
except Exception as e:
    check(False, "publish_guard 可用", str(e))

# ---------------------------------------------------------------- [6] 默认状态
section("6 出厂默认：关 + 演练 + 绝不写盘")

reset()
c = sr.get_config()
check(c["enabled"] is False, "总开关默认关")
check(c["dry_run"] is True, "默认演练")
check(c["night_ban"] is True, "默认夜间禁令")
check(c["require_zero_sold"] is True, "默认要求从未出单")
check(c["max_per_round"] <= 10, f"单轮默认不超 10（{c['max_per_round']}）")
check(sr.HARD_MAX_PER_ROUND == 10, "硬上限 10（配置改成 999 也不许超）")

# 配置被写成 999 也要夹回 10
sr.save_config({"max_per_round": 999})
check(sr.get_config()["max_per_round"] == 10, "max_per_round=999 被夹到 10")

# 0 不能被 `or default` 吃掉（0 是合法输入 → 夹成下限）
sr.save_config({"max_browse": 0})
check(sr.get_config()["max_browse"] == 0, "max_browse=0 被保留（不是退回默认 50）")

reset()
r = sr.run_round(force=False)
check(r["ok"] is False and "未启用" in r["error"], "没开开关 → 直接退出")
check(r["reworked"] == 0, "没开开关 → 一条都没重建")

# 夜间禁令即使 force 也生效（用小时桩验证逻辑本身）
_orig_hour = sr.datetime


class _Night:
    @staticmethod
    def now():
        return _orig_hour.now().replace(hour=23, minute=30)

    def __getattr__(self, k):
        return getattr(_orig_hour, k)


sr.datetime = _Night
try:
    r = sr.run_round(force=True)
    check(r["ok"] is False and "夜间" in r["error"], "force 也守夜间禁令")
finally:
    sr.datetime = _orig_hour

# ---------------------------------------------------------------- [7] 半自动边界
section("7 半自动边界：重建成功才登记旧链接，且从不调下架接口")

reset()
st = sr.get_state()
check(st["pending_down"] == [], "初始待下架清单为空")

# mark_down 对不存在的条目要报错，不能静默成功
check(sr.mark_down("c1", "999")["ok"] is False, "标记不存在的条目 → 报错")

# 手动塞一条 pending，验证 list + mark
st["pending_down"].append({
    "cookie_id": "c1", "item_id": "1001", "title": "旧链接",
    "url": sr.item_url("1001"), "new_item_id": "2002",
    "new_url": sr.item_url("2002"),
    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "status": "pending", "reason": "测试",
})
sr._save_state(st)
got = sr.list_pending()
check(got["count"] == 1, "待下架清单能读出来")
check(got["items"][0]["url"].endswith("item?id=1001"),
      "每条带直达链接（人工点进去下架）", got["items"][0]["url"])

check(sr.mark_down("c1", "1001", True)["ok"] is True, "标记已下架")
check(sr.list_pending()["count"] == 0, "标记后不在待下架里了")
check(sr.list_pending(include_done=True)["count"] == 1,
      "标记后仍能在『含已完成』里查到（不删数据）")

# 结构测试：整个模块不许**调用**下架/改状态的 MTOP 接口。
# 不能用"源码文本里没有这个词"——文档字符串里必须写明"我们不碰 item.status.update"，
# 那句解释本身就会命中。所以用 AST 只看**字符串常量**：真要调接口，接口名一定是
# 一个字符串常量传给 _mtop/requests；只出现在注释或 docstring 里的不算。
import ast

_tree = ast.parse(Path(sr.__file__).read_text(encoding="utf-8"))
_docstrings = set()
for _node in ast.walk(_tree):
    if isinstance(_node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        _d = ast.get_docstring(_node, clean=False)
        if _d:
            _docstrings.add(_d)
_consts = {c.value for c in ast.walk(_tree)
           if isinstance(c, ast.Constant) and isinstance(c.value, str)}
_code_strs = _consts - _docstrings

for _bad in ("mtop.taobao.idle.item.edit", "mtop.taobao.idle.item.status.update",
             "product.downShelf", "idle.item.downShelf"):
    check(all(_bad not in s for s in _code_strs), f"没有把 {_bad} 传给任何接口")

_src = Path(sr.__file__).read_text(encoding="utf-8")
check("sold_out_relist" in _src and "relaunch" in _src,
      "重建复用 sold_out_relist.relaunch（已验证的发布链路）")
# 判"有没有自己另写一套发布"要看**有没有真的调它/import 它**，不是看词出现过没有
# （注释里写"publish_item 内部没有限流"是完全正常的说明）。
check("from opportunity_publisher import publish_item" not in _src
      and "opportunity_publisher.publish_item" not in _src,
      "没有自己另写一套发布（避免两份 MTOP 实现漂移）")

# 跨模块契约：C2 传 `_title_override` 给 relaunch，relaunch 必须真的读它。
# 这类"我传了但对面没接"的断裂不会报错，只会静默失效——标题变体永远不生效，
# 也就永远逃不掉重复铺货。只有读源码才测得到。
_so_src = Path(str(Path(sr.__file__).parent / "sold_out_relist.py")).read_text(encoding="utf-8")
check("_title_override" in _src, "C2 会传 _title_override")
check("_title_override" in _so_src,
      "relaunch 真的读了 _title_override（否则标题变体静默失效）")

# relaunch 的发布前检查顺序：变量残留 → 合规闸。两者都在 publish_item 之前。
# 注意定位"实际调用"而不是词：`publish_guard` 这个词在注释里先出现过一次
# （"下面统一过 publish_guard"），用 .find("publish_guard") 会取到注释的位置。
_pub_i = _so_src.find("res = publish_item(")
if _pub_i < 0:
    _pub_i = _so_src.find("publish_item(")
_guard_i = _so_src.find("from publish_guard import check_publish")
_brace_i = _so_src.find('if "{{" in title')
check(0 < _brace_i < _pub_i, "变量没填全的检查在发布之前",
      f"brace={_brace_i} pub={_pub_i}")
check(0 < _guard_i < _pub_i, "合规闸在发布之前", f"guard={_guard_i} pub={_pub_i}")
check(0 < _brace_i < _guard_i, "先查变量残留，再查违禁词（顺序：填错的先报）",
      f"brace={_brace_i} guard={_guard_i}")

# ---------------------------------------------------------------- [8] 无模板不重建
section("8 没绑模板 → 不重建，且说清楚原因")

reset()
res = sr.rework("c1", "fake_cookie", {"item_id": "1001", "title": "t"})
check(res["ok"] is False, "没绑定 → 拒绝重建")
check(res.get("kind") == "no_template", "错误类型是 no_template（页面能据此提示）", str(res.get("kind")))
check("绑定模板" in str(res.get("error")), "错误文案告诉用户去绑模板")

# bind 校验模板是否存在
check(sr.bind("c1", "1001", 99999)["ok"] is False, "绑定不存在的模板 → 报错")
check(sr.bind("", "1001", 1)["ok"] is False, "缺 cookie_id → 报错")

# ---------------------------------------------------------------- [8b] 扫描链路
section("8b 扫描：没有本地历史也能判（靠详情里的上架时间）")

import shop_diagnosis as sd

_orig_list, _orig_detail, _orig_first = sd.fetch_my_items, sd.fetch_detail, sd.first_seen_days
_scan_calls = []


def _fake_list(cookie_str, app_key, cookie_id="", max_pages=20):
    _scan_calls.append(("list", cookie_id))
    return {"ok": True, "pages": 1, "items": [
        {"item_id": "111", "title": "老链接A", "price": 9.9, "status": "在售"},
        {"item_id": "222", "title": "有流量B", "price": 9.9, "status": "在售"},
    ]}


def _fake_detail(cookie_str, item_id, app_key, cookie_id=""):
    _scan_calls.append(("detail", item_id))
    if item_id == "111":
        # 关键：gmtCreate 有值 → 完全不依赖本地观测历史
        return {"ok": True, "browse": 8, "want": 0, "sold": 0, "stock": 1,
                "gmt_create": (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S"),
                "cookie_str": cookie_str}
    # 也给上架时间，这样"不动"的原因只可能是"有流量"，而不是"不知道多老"——
    # 否则这条断言测的是 no_age 而不是 has_traffic，两条原因混在一起就分不清。
    return {"ok": True, "browse": 300, "want": 12, "sold": 4, "stock": 1,
            "gmt_create": (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S"),
            "cookie_str": cookie_str}


sd.fetch_my_items = _fake_list
sd.fetch_detail = _fake_detail
sd.first_seen_days = lambda c, i: None      # 模拟"从没跑过 B2"

reset()
try:
    r = sr.scan("c1", "cookie=1", "appkey")
    check(r["ok"] is True, "扫描成功")
    check(r["total"] == 2, "两条在售都读到了")
    # 关键回归：days=None 的商品也必须补详情。只按 days 过滤会让本模块在全店
    # 没有观测历史时一条都不查，静默空跑。
    check(("detail", "111") in _scan_calls and ("detail", "222") in _scan_calls,
          "days 未知的商品也补了详情（不会静默空跑）", str(_scan_calls))

    by_id = {c["item_id"]: c for c in r["candidates"]}
    check(by_id["111"]["rework"] is True,
          "40 天 / 浏览 8 / 0 单 → 该翻新（用详情给的真实上架时间）",
          str(by_id["111"]))
    check(by_id["111"]["days"] and 39.5 < by_id["111"]["days"] < 40.5,
          f"天数来自 gmtCreate（{by_id['111']['days']}）")
    check(by_id["222"]["rework"] is False and "has_traffic" in by_id["222"]["blockers"],
          "浏览 300 那条不动")
    check(by_id["111"]["has_template"] is False,
          "没绑模板的候选会被标出来（页面据此提示）")
finally:
    sd.fetch_my_items, sd.fetch_detail, sd.first_seen_days = _orig_list, _orig_detail, _orig_first


# 第三种情况：既没有本地历史、详情也没给上架时间 → 不许静默不动，要给出 warning
def _fake_detail_no_time(cookie_str, item_id, app_key, cookie_id=""):
    return {"ok": True, "browse": 8, "want": 0, "sold": 0, "stock": 1,
            "gmt_create": None, "cookie_str": cookie_str}


sd.fetch_my_items = _fake_list
sd.fetch_detail = _fake_detail_no_time
sd.first_seen_days = lambda c, i: None
reset()
try:
    r = sr.scan("c1", "cookie=1", "appkey")
    check(bool(r.get("warning")), "算不出天数时给出 warning（不让用户以为程序坏了）",
          str(r.get("warning")))
    check(all(c["rework"] is False for c in r["candidates"]),
          "算不出天数时一条都不翻新")
    check("店铺复盘" in (r.get("warning") or ""), "warning 里指明了怎么办")
finally:
    sd.fetch_my_items, sd.fetch_detail, sd.first_seen_days = _orig_list, _orig_detail, _orig_first


# ---------------------------------------------------------------- [8c] 日发布配额
section("8c 日发布配额的缺口两边都补上了")

_so_path = Path(sr.__file__).parent / "sold_out_relist.py"
_so_src2 = _so_path.read_text(encoding="utf-8")

# `publish_item` 内部没有任何限流；`create_job` 的 20 条/天只覆盖批量发布路径。
# 所以"售罄重发"和"僵尸翻新"这两条直接调 relaunch 的路径都必须自己查日配额，
# 否则反复手动触发就能一天发出三四十条（平台降权的量级）。
check("DAILY_PUBLISH_CAP" in _src, "C2 自己检查日发布上限")
check("DAILY_PUBLISH_CAP" in _so_src2, "A6 也补上了日发布上限（同一个缺口）")

# 两边都必须在 acquire("publish") **之前**查 —— 先查再抢配额，语义才一致
for _name, _s in (("C2", _src), ("A6", _so_src2)):
    _cap_i = _s.find("DAILY_PUBLISH_CAP")
    _acq_i = _s.find('acquire("publish"')
    check(0 < _cap_i < _acq_i, f"{_name}：日上限检查在发布限流之前",
          f"cap={_cap_i} acq={_acq_i}")

# 缺口本身也要锁住：publish_item 已接入账号级熔断（circuit_blocked/trip_circuit），
# 这与调用方的日配额+请求限流是两层不同的事，不算重复。真正要防的是把
# acquire()/daily_count() 这类请求级限流塞进 publish_item——那会让下面两处
# 检查变成重复，届时应合并。
_pub_src = (Path(sr.__file__).parent / "opportunity_publisher.py").read_text(encoding="utf-8")
check("acquire(" not in _pub_src and "daily_count" not in _pub_src,
      "前提仍成立：publish_item 内部无请求级限流（circuit_blocked 熔断不算；若已加，两处检查可合并）")
check("circuit_blocked" in _pub_src,
      "发布入口仍接账号级熔断（第二批改动，防回退）")


# ---------------------------------------------------------------- [9] 调度器
section("9 调度器幂等")

reset()
check(sr.start_scheduler() is True, "首次启动返回 True")
check(sr.start_scheduler() is False, "重复启动返回 False（不会起两个线程）")
stt = sr.get_status()
check("config" in stt and "pending_count" in stt, "状态结构完整")
check(stt["config"]["enabled"] is False, "状态里读到的仍是默认关")

# ---------------------------------------------------------------- [10] 状态一致性
section("10 状态文件坏了不能崩")

reset()
Path(sr.STATE_FILE).write_text("{ 这不是 json", encoding="utf-8")
try:
    st = sr.get_state()
    check(isinstance(st, dict) and "pending_down" in st, "坏状态文件 → 退回空结构")
except Exception as e:
    check(False, "坏状态文件不抛异常", str(e))

Path(sr.CONFIG_FILE).write_text("[]", encoding="utf-8")
try:
    c = sr.get_config()
    check(c["enabled"] is False, "配置是数组（类型错）→ 退回默认（关）")
except Exception as e:
    check(False, "坏配置不抛异常", str(e))

# 原子写：不能留下 .tmp
reset()
sr.save_config({"min_days": 9})
check(not Path(str(sr.CONFIG_FILE) + ".tmp").exists(), "原子写不留 .tmp 残骸")
check(sr.get_config()["min_days"] == 9, "配置真的写进去了")

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 条失败：")
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print("✅ 全部通过")
