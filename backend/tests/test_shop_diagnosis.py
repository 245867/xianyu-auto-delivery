"""
B2 店铺复盘 + 限流归因的用例。

守住的四件事：
[1] 分类边界 —— 尤其是杀品线必须"浏览低**且**想要低"，不能把高浏览低想要误杀
[2] 归因信号 —— 每条结论都能从我们自己读到的数里算出来；没有数据就不许编
[3] 快照幂等 —— 同一天跑多次只留一条，且没补到详情的字段用 COALESCE 保留旧值
[4] 只读性   —— 全程不调任何写接口（这条最关键，错了就是账号风险）

跑法：cd backend && SHOP_DIAG_DB=<tmp> python -m pytest tests/test_shop_diagnosis.py -q
或直接 python tests/test_shop_diagnosis.py（自带断言）
"""

import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="shopdiag_"))
os.environ["SHOP_DIAG_DB"] = str(_TMP / "shop.db")
os.environ["SHOP_DIAG_CONFIG"] = str(_TMP / "shop.json")
os.environ["RATE_LIMIT_STATE"] = str(_TMP / "rate.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shop_diagnosis as sd

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


# ---------------------------------------------------------------- [1] 分类
section("1 分类边界")

T = sd.THRESHOLDS

# 刚记录 <24h → 观察中，无论数字多难看
check(sd.classify({"browse": 0, "want": 0}, 0.3)[0] == "observe",
      "不足 24h 判观察中（不误杀新品）")

# 杀品线：2 天以上 + 浏览 <50 + 想要 <=2
check(sd.classify({"browse": 12, "want": 0}, 3.0)[0] == "cull",
      "3 天浏览 12 想要 0 → 该淘汰")
check(sd.classify({"browse": 49, "want": 1}, 2.0)[0] == "cull",
      "恰好 49/1 仍在杀品线内")
# 边界外：浏览 50 不该杀
check(sd.classify({"browse": 50, "want": 0}, 3.0)[0] != "cull",
      "浏览恰好 50 **不**判淘汰（阈值是 <50）")
# 高浏览低想要不该被当僵尸，而该走正常/其他分支
check(sd.classify({"browse": 900, "want": 0}, 5.0)[0] not in ("cull", "zombie"),
      "浏览 900 想要 0 不判淘汰也不判僵尸（是主图/价格问题，另有信号）")

# 爆款
check(sd.classify({"browse": 3000, "want": 260}, 10.0)[0] == "hot",
      "想要 260 → 爆款")
check(sd.classify({"browse": 3000, "want": 199}, 10.0)[0] != "hot",
      "想要 199 不判爆款（阈值 200）")

# 潜力：想要 5–10 或 转化率 >10%，且浏览 <500
check(sd.classify({"browse": 120, "want": 6}, 5.0)[0] == "potential",
      "浏览 120 想要 6 → 有潜力")
check(sd.classify({"browse": 60, "want": 8}, 5.0)[0] == "potential",
      "转化率 13% → 有潜力")
check(sd.classify({"browse": 800, "want": 9}, 5.0)[0] != "potential",
      "浏览 800 想要 9 → 不算潜力（浏览已过 500 门槛）")

# 僵尸
check(sd.classify({"browse": 150, "want": 1}, 4.0)[0] == "zombie",
      "浏览 150 想要 1 → 僵尸")

# 无数据
check(sd.classify({"browse": None, "want": None}, 3.0)[0] == "unknown",
      "browse 为 None → 无数据（不瞎猜）")

# 天数未知（历史库空）时不能判淘汰
check(sd.classify({"browse": 3, "want": 0}, None)[0] != "cull",
      "不知道多久了就不判淘汰（宁漏勿误）")


# ---------------------------------------------------------------- [2] 信号
section("2 归因信号")

CTX = {"median_price": 100.0}

# 不足 1 天只给 fresh 一条，不吓人
sigs = sd.item_signals({"browse": 5, "want": 0, "sold": 0, "images": 2,
                        "price": 100.0}, 0.5, CTX)
check(len(sigs) == 1 and sigs[0]["code"] == "fresh",
      "刚记录只给一条『先别动』，不叠加其他罪名")

# 浏览低 + 过观察期 → cull 信号，权重最高
sigs = sd.item_signals({"browse": 12, "want": 0, "sold": 0, "images": 6,
                        "first_image_ratio": "1:1", "price": 100.0}, 4.0, CTX)
codes = [s["code"] for s in sigs]
check("cull" in codes, "浏览 12 / 4 天 → 触发杀品信号")
check(sigs[0]["code"] == "cull", "杀品信号排第一（权重最高）")

# 有人看没人想要
sigs = sd.item_signals({"browse": 400, "want": 0, "sold": 0, "images": 6,
                        "first_image_ratio": "1:1", "price": 100.0}, 6.0, CTX)
codes = [s["code"] for s in sigs]
check("click_bad" in codes, "浏览 400 想要 0 → 判主图/标题/价格问题")
check("cull" not in codes, "高浏览不触发杀品线（两条互斥）")

# 有意向没成交
sigs = sd.item_signals({"browse": 600, "want": 12, "sold": 0, "images": 6,
                        "first_image_ratio": "1:1", "price": 100.0}, 8.0, CTX)
check("no_deal" in [s["code"] for s in sigs], "想要 12 成交 0 → 判详情/信任/价格")

# 图片少
sigs = sd.item_signals({"browse": 300, "want": 8, "sold": 1, "images": 2,
                        "first_image_ratio": "1:1", "price": 100.0}, 8.0, CTX)
check("few_images" in [s["code"] for s in sigs], "只有 2 张图 → 提示补图")

# 首图非 1:1
sigs = sd.item_signals({"browse": 300, "want": 8, "sold": 1, "images": 6,
                        "first_image_ratio": "4:3", "price": 100.0}, 8.0, CTX)
check("ratio" in [s["code"] for s in sigs], "首图 4:3 → 提示换 1:1")

# 价格离群（高）
sigs = sd.item_signals({"browse": 300, "want": 8, "sold": 1, "images": 6,
                        "first_image_ratio": "1:1", "price": 250.0}, 8.0, CTX)
check("price_high" in [s["code"] for s in sigs], "价格 250 / 中位 100 → 判偏高")

# 价格离群（低）
sigs = sd.item_signals({"browse": 300, "want": 8, "sold": 1, "images": 6,
                        "first_image_ratio": "1:1", "price": 30.0}, 8.0, CTX)
check("price_low" in [s["code"] for s in sigs], "价格 30 / 中位 100 → 判偏低（会被当钓鱼）")

# 健康商品：不该有任何 high 信号
sigs = sd.item_signals({"browse": 800, "want": 40, "sold": 3, "images": 8,
                        "first_image_ratio": "1:1", "price": 105.0}, 20.0, CTX)
check(all(s["level"] != "high" for s in sigs), "健康商品不产生 high 级信号")

# 无任何计数时不编结论
sigs = sd.item_signals({"browse": None, "want": None, "sold": None, "images": None,
                        "price": None}, 5.0, {"median_price": None})
check(len(sigs) == 0, "一个数都没有 → 一条信号都不编")


# ---------------------------------------------------------------- [2b] 店铺级
section("2b 店铺级归因")

rows_all_dead = [{"browse": 3, "want": 0, "classify": "cull"} for _ in range(5)]
sigs = sd.shop_signals(rows_all_dead, {"publish_today": 0, "polish_today": 0,
                                       "recent_unknown": 0})
check(any(s["code"] == "account_wide" for s in sigs),
      "5/5 件浏览都 <20 → 判账号级（别逐件改链接）")

rows_half = [{"browse": 3, "want": 0, "classify": "cull"},
             {"browse": 900, "want": 30, "classify": "hot"},
             {"browse": 800, "want": 20, "classify": "normal"}]
sigs = sd.shop_signals(rows_half, {"publish_today": 0, "polish_today": 0,
                                    "recent_unknown": 0})
check(not any(s["code"] == "account_wide" for s in sigs),
      "1/3 件烂不判账号级（样本不够/比例不到）")

sigs = sd.shop_signals(rows_all_dead, {"publish_today": 18, "polish_today": 0,
                                       "recent_unknown": 0})
check(any(s["code"] == "ops_dense_publish" for s in sigs), "今日发 18 条 → 提示发布量偏大")
sigs = sd.shop_signals(rows_all_dead, {"publish_today": 0, "polish_today": 0,
                                       "recent_unknown": 2})
check(any(s["code"] == "recent_unknown" for s in sigs),
      "有 submission_unknown → 最高优先级提示先回读")


# ---------------------------------------------------------------- [3] 快照
section("3 快照幂等与合并")

cid = "cookie_test"
items = [
    {"item_id": "1001", "title": "测试商品A", "price": 55.0, "status": "在售",
     "category": "1", "browse": 120, "want": 8, "collect": 3, "sold": 1,
     "stock": 2, "images": 6, "first_image_ratio": "1:1"},
    {"item_id": "1002", "title": "测试商品B", "price": 88.0, "status": "在售",
     "category": "1", "browse": 9, "want": 0, "collect": 0, "sold": 0,
     "stock": 1, "images": 3, "first_image_ratio": "1:1"},
]
n = sd.record_snapshot(cid, items)
check(n == 2, "首次写入 2 条")

h = sd.history(cid, "1001", days=7)
check(len(h) == 1 and h[0]["browse"] == 120, "history 能读回浏览 120")

# 同一天再写一次，数值更新但不新增行
sd.record_snapshot(cid, [{**items[0], "browse": 150, "want": 9}])
h = sd.history(cid, "1001", days=7)
check(len(h) == 1, "同一天不新增快照行（主键去重）")
check(h[0]["browse"] == 150, "同一天覆盖为最新值 150")

# 这次只拿到 browse（模拟详情失败），want 必须保留旧值 9 而不是被清空
sd.record_snapshot(cid, [{**items[0], "browse": 160, "want": None}])
h = sd.history(cid, "1001", days=7)
check(h[0]["browse"] == 160, "browse 更新为 160")
check(h[0]["want"] == 9, "want 为空时不覆盖旧值（COALESCE 保住 9）")

# prev_snapshot 取的是"今天之前"的，今天刚写的不能算上次
prev = sd.prev_snapshot(cid, "1001")
check(prev is None, "只有今天的快照 → 没有『上次』（不拿今天跟自己比）")

# 造一条昨天的
conn = sd._conn()
yday = (date.today() - timedelta(days=1)).isoformat()
conn.execute(
    "INSERT INTO shop_snapshots(cookie_id,item_id,day,browse,want,collect,sold,stock,"
    "images,first_image_ratio,price,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    (cid, "1001", yday, 80, 5, 2, 0, 2, 6, "1:1", 55.0, "在售"))
conn.commit()
conn.close()
prev = sd.prev_snapshot(cid, "1001")
check(prev is not None and prev["browse"] == 80, "造了昨天的 80 后能读到 prev")

# first_seen_days: 刚写入的应该接近 0
d = sd.first_seen_days(cid, "1001")
check(d is not None and d >= 0, "first_seen_days 能算出观测起点")

# 手工把 first_seen 改成 3 天前，验证按天判定
conn = sd._conn()
older = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
conn.execute("UPDATE shop_items SET first_seen=? WHERE cookie_id=? AND item_id=?",
             (older, cid, "1001"))
conn.commit()
conn.close()
d = sd.first_seen_days(cid, "1001")
check(d is not None and 2.9 < d < 3.1, f"改早 3 天后 first_seen_days≈3（实际 {d}）")


# ---------------------------------------------------------------- [3b] 报告
section("3b 报告结构")

rep = sd.diagnose(items, cid)
check(rep["ok"] is True, "diagnose 返回 ok")
check(len(rep["items"]) == 2, "报告含 2 件商品")
check("counts" in rep and isinstance(rep["counts"], dict), "含 counts 汇总")
check(rep["median_price"] == 71.5, f"中位价 (55+88)/2=71.5（实际 {rep['median_price']}）")
check(len(rep["data_notes"]) == 3, "三条数据边界说明必须在报告里（曝光/咨询/天数）")
check(any("曝光" in s for s in rep["data_notes"]), "第一条明确说曝光拿不到")
check(isinstance(rep["actions"], list), "含可执行动作列表")
check(all(set(a) >= {"scope", "title", "action", "level"} for a in rep["actions"]),
      "每条动作字段完整")

# 刚记录的商品必须被判"观察中"，不能被第一天的数字杀掉
row2 = [r for r in rep["items"] if r["item_id"] == "1002"][0]
check(row2["classify"] == "observe", "今天刚记录 → 观察中（第一天不该杀品）")

# 把 1002 的观测起点改到 3 天前，再判一次 → 现在才该是淘汰
conn = sd._conn()
older2 = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
conn.execute("UPDATE shop_items SET first_seen=? WHERE cookie_id=? AND item_id=?",
             (older2, cid, "1002"))
conn.commit()
conn.close()
rep2 = sd.diagnose(items, cid)
row2 = [r for r in rep2["items"] if r["item_id"] == "1002"][0]
check(row2["classify"] == "cull", "3 天后浏览仍是 9 → 判该淘汰")
check(any(a["item_id"] == "1002" for a in rep2["actions"]), "1002 出现在待办动作里")

# 上次报告
sd._save_run(cid, True, 2, rep)
last = sd.last_report(cid)
check(last is not None and last.get("_items") == 2, "last_report 能读回且带条数")



# ---------------------------------------------------------------- [3c] 真成交口径
section("3c 真成交口径（订单接口）")

# 拿不到：报告照出，但要明说"这期没有真实成交数据"，不能拿累计销量冒充
rep3 = sd.diagnose(items, cid)
sd._apply_sales(rep3, {"ok": False, "reason": "卖家端接口已关闭", "days": 0})
check(rep3["sales"]["ok"] is False, "sales 进报告（ok=False）")
check(not any(s["code"].startswith("sales_") for s in rep3["shop_signals"]),
      "拿不到就不出成交类信号（不编数）")
check(any("没取到真实成交" in n for n in rep3["data_notes"]),
      "数据边界里写明为什么没有")

# 拿到了：待发货 → 高优动作顶到最前
sales_ok = {"ok": True, "days": 7, "orders": 10, "amount": 1280.5,
            "refund_orders": 3, "pending_ship": 2, "pages": 2, "cancelled": 1,
            "reason": "ok"}
rep4 = sd.diagnose(items, cid)
sd._apply_sales(rep4, sales_ok)
check(rep4["sales"]["amount"] == 1280.5, "成交额原样带进报告")
codes = [s["code"] for s in rep4["shop_signals"]]
check("sales_pending_ship" in codes, "待发货出现在店铺信号里")
check(codes.index("sales_pending_ship") == 0 or rep4["shop_signals"][0]["weight"] >= 0.75,
      "待发货权重够高（能排到前面）")
check("sales_refund_high" in codes, "10 单退 3 单 → 退款率偏高")
check(rep4["actions"][0]["title"] == "有成交单还没发货", "动作列表第一条是去发货")
check(any("真实订单口径" in n for n in rep4["data_notes"]),
      "说明成交来自订单接口，不是累计销量")
check(len(rep4["actions"]) <= 14, "动作有上限，不无限膨胀")

# 一周零成交 → 低优提醒（不能和"待发货"抢位置）
rep5 = sd.diagnose(items, cid)
sd._apply_sales(rep5, {"ok": True, "days": 7, "orders": 0, "amount": 0,
                       "refund_orders": 0, "pending_ship": 0, "pages": 1})
codes5 = [s["code"] for s in rep5["shop_signals"]]
check("sales_no_order" in codes5, "零成交给一条低优信号")
check(rep5["shop_signals"][0]["code"] != "sales_no_order" or len(rep5["shop_signals"]) == 1,
      "零成交不会盖过更重的店铺问题")

# 接线：run() 里真的调了，且失败不影响报告
src = Path("shop_diagnosis.py").read_text(encoding="utf-8")
check("sales_summary(cookie_str, cookie_id" in src, "run() 接了卖家端成交口径")
check(src.find("_apply_sales(report, sales)") > src.find("report = diagnose(items, cookie_id)"),
      "在出报告之后合并")
check("except Exception as _e:\n        sales = {\"ok\": False" in src,
      "接口异常降级成 ok=False，不让复盘整体失败")
check('"sales_days"' in src, "窗口天数可配（默认 7）")


# ---------------------------------------------------------------- [4] 只读性
section("4 只读性（最关键）")

# 静态检查：本模块不应 import 任何写接口
src = Path(sd.__file__).read_text(encoding="utf-8")
banned = ["publish_item", "polish_item(", "sold_out_relist", "delete_item",
          "update_price", "downShelf", "status.update", "item.edit"]
hits = [b for b in banned if b in src]
check(not hits, f"模块源码不含写接口调用（命中：{hits}）")

# 运行时不调用：把 _mtop_request 换成记录器，跑一遍 fetch_my_items 的纯解析路径
called = []
orig = sd._mtop_request


def fake(cookie_str, api, data, version, spm_cnt, app_key, timeout=30):
    called.append(api)
    raise RuntimeError("stop after first call (test)")


sd._mtop_request = fake
try:
    sd.fetch_my_items("unb=1; cookie2=abc", "appkey", cookie_id="c")
except Exception:
    pass
sd._mtop_request = orig
check(all(a == sd.LIST_API for a in called), f"只访问了列表接口（实际 {called}）")


# ---------------------------------------------------------------- [5] 拒绝路径
section("5 明确的拒绝路径")

TOKEN = sd.TOKEN_COOKIE_NAME      # 从模块取，不写死（改名时测试不会假过）

r = sd.fetch_my_items("", "appkey")
check(r["ok"] is False and ("token" in (r.get("error") or "").lower()
                            or "登录" in (r.get("error") or "")),
      "空 Cookie → 明确报错不硬跑")

# 「能进到请求层」用桩验证：不真的发网络请求（否则测试依赖外网，不可复现）
_orig2 = sd._mtop_request
_reached = []


def _boom(cookie_str, api, data, version, spm_cnt, app_key, timeout=30):
    _reached.append((api, data))
    raise RuntimeError("blocked by test")


sd._mtop_request = _boom
try:
    sd.fetch_my_items(f"{TOKEN}=abc; unb=123", "appkey")
finally:
    sd._mtop_request = _orig2
check(bool(_reached), "有 token 有 unb → 放行到请求层（已用桩截住，未真联网）")

r = sd.fetch_my_items(f"{TOKEN}=abc", "appkey")
check(r["ok"] is False and "unb" in (r.get("error") or ""), "缺 unb → 报错说明原因")

r = sd.fetch_detail(f"{TOKEN}=abc", "1", "appkey")
check(r["ok"] is False, "详情失败返回 ok=False 不抛到外层")


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
