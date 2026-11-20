"""
安全解析回归测试（APK 逆向对照产物）
=====================================
对照开源 App（AutoJs6 v6.8.0，MPL-2.0）的解析/重试/验证工程做法后，
在我方代码里找到并修掉的问题，这里逐条锁死，防回退。

三个断言组：
1. `_deep_find` 不再递归爆栈（原实现深 5000 层即 RecursionError）；
2. 环形引用不再炸，且**顺序语义不变**（仍然取"第一个命中"，不是最后一个）；
3. 节点超限时返回 None，不抛异常——畸形返回体不能把整次搜索带崩。

跑法（backend 目录）：python3 tests/test_safe_parse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from opportunity_engine import _deep_find  # noqa: E402

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


def test_no_recursion_error():
    print("\n[1] 深层嵌套不再爆栈")
    deep = {}
    cur = deep
    for _ in range(5000):
        cur["n"] = {}
        cur = cur["n"]
    cur["wantCount"] = 42
    try:
        r = _deep_find(deep, ("wantCount",))
        check("深 5000 层不抛异常", True)
        # 超过 max_depth(40) 就放弃，这是设计选择：真实接口不会有这么深
        check("超过深度上限时返回 None（设计如此）", r is None, str(r))
    except RecursionError:
        check("深 5000 层不抛异常", False, "RecursionError")

    shallow = {"a": {"b": {"c": {"wantCount": 9}}}}
    check("正常深度仍能取到", _deep_find(shallow, ("wantCount",)) == 9)


def test_cycle():
    print("\n[2] 环形引用")
    cyc = {}
    cyc["self"] = cyc
    cyc["wantCount"] = 7
    try:
        check("环不抛异常", _deep_find(cyc, ("wantCount",)) == 7)
    except RecursionError:
        check("环不抛异常", False, "RecursionError")

    # 环且目标值藏在环里
    inner = {"v": 3}
    inner["loop"] = inner
    check("环内目标可取", _deep_find({"x": inner}, ("v",)) == 3)


def test_order_semantics():
    print("\n[3] 顺序语义（必须仍是「取第一个命中」）")
    d = {"a": {"wantCount": 1}, "b": {"wantCount": 2}}
    check("同级取第一个（不是最后一个）", _deep_find(d, ("wantCount",)) == 1)

    d2 = {"list": [{"wantCount": 10}, {"wantCount": 20}]}
    check("列表取第一个", _deep_find(d2, ("wantCount",)) == 10)

    d3 = {"a": {"wantCount": ""}, "b": {"wantCount": 5}}
    check("空字符串被跳过", _deep_find(d3, ("wantCount",)) == 5)

    d4 = {"a": {"wantCount": None}, "b": {"wantCount": 6}}
    check("None 被跳过", _deep_find(d4, ("wantCount",)) == 6)

    # 键优先级：同级里先按 keys 顺序找
    d5 = {"x": {"wantCount": 1, "wantCnt": 2}}
    check("多候选键按声明顺序", _deep_find(d5, ("wantCnt", "wantCount")) == 2)


def test_size_limit():
    print("\n[4] 超大结构不拖死进程")
    big = {"x": [{"i": i} for i in range(20000)]}
    try:
        r = _deep_find(big, ("wantCount",))
        check("2 万节点返回 None 而不是崩", r is None, str(r))
    except RecursionError:
        check("2 万节点返回 None 而不是崩", False, "RecursionError")

    exact = {"x": [{"wantCount": 5}]}
    check("小结构正常取到", _deep_find(exact, ("wantCount",)) == 5)

    check("None 输入返回 None", _deep_find(None, ("k",)) is None)
    check("字符串输入返回 None", _deep_find("str", ("k",)) is None)


def main():
    test_no_recursion_error()
    test_cycle()
    test_order_semantics()
    test_size_limit()

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
