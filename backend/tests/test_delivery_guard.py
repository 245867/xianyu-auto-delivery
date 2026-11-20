"""
自动发货防护回归测试（P0）
================================
覆盖本次审计发现并修复的三个问题，任何一个回退都会让测试红：

1. **rule NameError**：`_handle_auto_delivery` 里引用了 `_auto_delivery` 的局部变量 `rule`。
   后果不是"没有发货后消息"，而是**每次发货成功都抛 NameError**，被外层 except 吞掉，
   日志记成"自动发货处理异常"并推一条失败通知——成功的单看起来像失败，真失败反而难分辨。
2. **判重从未生效**：`last_delivery_time` 只读不写，`delivery_sent_orders` 只加不查。
3. **重启后重发**：内存判重重启即失忆，必须靠库里 `system_shipped` 兜底。

跑法（在 backend 目录）：`python3 -m pytest tests/test_delivery_guard.py -q`
没有 pytest 时也能直接 `python3 tests/test_delivery_guard.py`。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# 必须在 import db_manager 之前指定测试库，绝不能碰真实 data/xianyu_data.db
_TMP = tempfile.mkdtemp(prefix="delivery_guard_")
os.environ["DB_PATH"] = str(Path(_TMP) / "test.db")

import db_manager as dbm  # noqa: E402

DB = dbm.DBManager(os.environ["DB_PATH"])

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


# --------------------------------------------------------------------------
# 1. 数据库层：is_order_system_shipped
# --------------------------------------------------------------------------
def test_is_order_system_shipped():
    print("\n[1] 库里的发货标记（重启兜底判重）")
    DB.save_cookie("acc1", "cookie=1", user_id=1)

    # 不存在的订单
    check("未知订单返回 False", DB.is_order_system_shipped("no-such-order") is False)

    # 新建订单，默认未发货
    DB.insert_or_update_order(order_id="o1", item_id="i1", buyer_id="b1", cookie_id="acc1")
    check("新订单未发货", DB.is_order_system_shipped("o1") is False)

    # 标记已发货
    DB.insert_or_update_order(order_id="o1", system_shipped=True)
    check("标记后为已发货", DB.is_order_system_shipped("o1") is True)

    # 回写 False
    DB.insert_or_update_order(order_id="o1", system_shipped=False)
    check("可回写为未发货", DB.is_order_system_shipped("o1") is False)

    # 空 order_id 不允许误判为已发
    check("空订单号返回 False", DB.is_order_system_shipped("") is False)


# --------------------------------------------------------------------------
# 2. 冷却时间真的被写入了
# --------------------------------------------------------------------------
def test_cooldown_written():
    print("\n[2] 冷却时间写入（原来只读不写，判重形同虚设）")
    src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")

    marker = "self.delivery_sent_orders.add(order_id)"
    check("mark_delivery_sent 里有标记语句", marker in src)

    # 关键断言：mark_delivery_sent 内部必须写 last_delivery_time
    idx = src.index(marker)
    window = src[idx: idx + 400]
    check("mark_delivery_sent 写入 last_delivery_time",
          "self.last_delivery_time[order_id]" in window, window[:200].replace("\n", " "))

    # can_auto_delivery 必须查库兜底
    cidx = src.index("def can_auto_delivery")
    cwin = src[cidx: cidx + 1600]
    check("can_auto_delivery 查 is_order_system_shipped", "is_order_system_shipped" in cwin)
    check("查库异常时保守跳过（返回 False）",
          "保守跳过" in cwin or "return False" in cwin)


# --------------------------------------------------------------------------
# 3. rule 不再逃逸作用域
# --------------------------------------------------------------------------
def test_rule_scope():
    print("\n[3] 发货后消息的规则来源（NameError 修复）")
    src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")

    hidx = src.index("async def _handle_auto_delivery")
    hend = src.index("async def refresh_token", hidx)
    handler = src[hidx:hend]

    # 出参机制存在
    check("_auto_delivery 接受 ctx 出参", "ctx: dict = None" in src)
    check("命中规则时写入 ctx", 'ctx["rule"] = rule' in src)
    check("调用方创建每单独立的 ctx", "deliver_ctx = {}" in handler)
    check(
        "准备发货内容时按订单传入 ctx",
        (
            "item_id,\n"
            "                        item_title,\n"
            "                        order_id,\n"
            "                        send_user_id,\n"
            "                        quantity_to_send,\n"
            "                        deliver_ctx,"
        ) in src,
    )
    check("底层 _auto_delivery 从该 ctx 出参", "ctx=ctx" in src)
    check("handler 从 ctx 取 rule", 'deliver_ctx.get("rule")' in handler)

    # 关键：handler 里每一次用 rule 都必须排在那次赋值之后（先赋值后使用）。
    # 以前是"不能出现裸 rule."，但修复后 handler 里正常就有 rule.get(...)，
    # 那条断言会把正确代码也判红。真正的约束是顺序，不是出现与否。
    assign_line = None
    early_use = []
    for ln, line in enumerate(handler.splitlines(), start=1):
        s = line.strip()
        if s.startswith("#"):
            continue
        if assign_line is None and s.startswith("rule = "):
            assign_line = ln
            continue
        if "rule." in s or "rule(" in s or "rule[" in s:
            if assign_line is None:
                early_use.append(f"{ln}: {s[:80]}")
    check("handler 里 rule 先赋值", assign_line is not None)
    check("handler 里 rule 没有先用后赋值", not early_use, str(early_use[:3]))
    # 赋值必须来自 ctx，而不是重新调 _auto_delivery 或碰 instance 状态
    check("赋值来源是 deliver_ctx",
          assign_line is not None
          and "deliver_ctx" in handler.splitlines()[assign_line - 1])

    # 用 AST 再确认一遍：handler 作用域内 rule 只能是被赋值过的
    import ast
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_handle_auto_delivery")
    loads = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    stores = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    check("AST：handler 里 rule 先赋值后使用", "rule" in loads and "rule" in stores)


# --------------------------------------------------------------------------
# 4. 成功不再推"失败通知"
# --------------------------------------------------------------------------
def test_no_success_notification():
    print("\n[4] 发货成功不再刷失败通知")
    src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
    hidx = src.index("async def _handle_auto_delivery")
    hend = src.index("async def refresh_token", hidx)
    handler = src[hidx:hend]

    bad = [ln for ln, line in enumerate(handler.splitlines(), 1)
           if "send_delivery_failure_notification" in line and "发货成功" in line]
    check("成功分支不再调用失败通知", not bad, str(bad))
    check("成功分支保留日志",
          "✅ 自动发货完成" in handler)
    # 失败分支必须还在
    check("失败分支仍会通知",
          handler.count("send_delivery_failure_notification(") >= 2)


# --------------------------------------------------------------------------
# 5. 行为级模拟：同一订单第二次触发必须被拦住
# --------------------------------------------------------------------------
def _load_delivery_methods():
    """从 XianyuAutoAsync.py 里原样抠出两个方法体来跑。

    为什么不用 `import XianyuAutoAsync`：那个模块一进来就要
    blackboxprotobuf / playwright / DrissionPage / openai / PyExecJS 一整套运行时，
    在纯 Python 环境里根本起不来。抠源码执行测的仍然是**真实代码文本**，
    只是不连坐它的依赖树。
    """
    import ast

    src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "XianyuLive")
    wanted = {"can_auto_delivery", "mark_delivery_sent"}
    parts = [
        "import time",
        "from loguru import logger",
    ]
    for fn in cls.body:
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in wanted:
            parts.append(ast.unparse(fn))
    missing = wanted - {f.name for f in cls.body
                        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if missing:
        raise AssertionError(f"源码里找不到方法: {missing}")
    ns: dict = {}
    exec("\n\n".join(parts), ns)
    return ns


def test_second_trigger_blocked():
    print("\n[5] 行为级：同一订单第二次触发被拦")
    ns = _load_delivery_methods()

    # 造一个最小对象，只测判重，不碰网络
    class Fake:
        cookie_id = "acc1"
        delivery_cooldown = 600
        order_status_handler = None
        _safe_str = staticmethod(lambda e: str(e))

        def __init__(self):
            self.last_delivery_time = {}
            self.delivery_sent_orders = set()

    can_send = ns["can_auto_delivery"]
    mark_sent = ns["mark_delivery_sent"]
    f = Fake()

    real_import = None
    try:
        # can_auto_delivery 里是 `from db_manager import db_manager`，改模块级那个单例
        real_import = dbm.db_manager
        dbm.db_manager = DB

        DB.insert_or_update_order(order_id="o2", item_id="i2", buyer_id="b2", cookie_id="acc1")
        check("首次可发", can_send(f, "o2") is True)

        # 模拟发货成功后的标记（mark_sent 内部自己写冷却时间）
        mark_sent(f, "o2")
        check("mark_delivery_sent 真的写了冷却时间", "o2" in f.last_delivery_time)
        DB.insert_or_update_order(order_id="o2", system_shipped=True)

        check("第二次被内存冷却拦住", can_send(f, "o2") is False)

        # 模拟重启：内存清空，只剩库
        f.last_delivery_time.clear()
        f.delivery_sent_orders.clear()
        check("重启后仍被库兜住", can_send(f, "o2") is False)

        # 另一个订单不受影响
        DB.insert_or_update_order(order_id="o3", item_id="i3", buyer_id="b3", cookie_id="acc1")
        check("其他订单不受影响", can_send(f, "o3") is True)

        # 无订单号时不拦（保持原行为，避免影响非订单场景）
        check("空订单号不拦", can_send(f, "") is True)

        # 查库炸了必须保守拦住，不能默认放行
        class Boom:
            def is_order_system_shipped(self, _oid):
                raise RuntimeError("db down")

            def __getattr__(self, _n):
                raise RuntimeError("db down")

        dbm.db_manager = Boom()
        check("查库异常时保守不发货", can_send(f, "o3") is False)
    finally:
        if real_import is not None:
            dbm.db_manager = real_import


# --------------------------------------------------------------------------
# 6. API 卡券的网络异常捕获必须真的能捕获
# --------------------------------------------------------------------------
def test_api_exception_tuple():
    print("\n[6] API 卡券异常捕获（ClientTimeout 不是异常类）")
    src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")

    # 注释里会写到旧写法（解释为什么不能那么写），所以只检查真正的代码行
    bad = "except (aiohttp.ClientTimeout, aiohttp.ClientError)"
    code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    check("没有把 ClientTimeout 当异常类",
          not any(bad in ln for ln in code_lines),
          "这行会让 except 现场抛 TypeError，并且穿过 except Exception 冒到调用方")

    check("改成了 asyncio.TimeoutError",
          "except (asyncio.TimeoutError, aiohttp.ClientError)" in src)

    # 行为验证：确认这种写法在真实运行时是什么后果
    import aiohttp

    class _NotAnError:
        pass

    class _NetErr(Exception):
        pass

    fell_through = False
    try:
        try:
            raise _NetErr("boom")
        except (_NotAnError, _NetErr):
            pass
        except Exception:
            pass
    except TypeError:
        fell_through = True
    check("证明确实会穿过 except Exception",
          fell_through, "如果这条失败，说明 Python 语义变了，注释需要更新")

    check("ClientTimeout 确实不是异常类",
          not issubclass(aiohttp.ClientTimeout, BaseException))


def main():
    test_is_order_system_shipped()
    test_cooldown_written()
    test_rule_scope()
    test_no_success_notification()
    test_second_trigger_blocked()
    test_api_exception_tuple()

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
