# -*- coding: utf-8 -*-
"""
核对「前端调用的接口路径」与「后端实际注册的路由」是否一致。

做法：
  1. 启动后端 app（仅导入，不监听端口），用 FastAPI 自身的路由表拿到真实路径；
  2. 从前端源码 frontend/services/api.ts 抽取所有字面量路径；
  3. 把前端路径里的模板变量（${...}）替换成通配，再与后端路由做匹配；
  4. 输出前端调用了但后端不存在的路径（潜在 404）。

用法：python tools/check_api_paths.py
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("DB_PATH", "data/demo_shot.db")


def backend_routes():
    import reply_server  # noqa: F401
    routes = set()
    for r in reply_server.app.routes:
        p = getattr(r, "path", None)
        if p:
            routes.add(p)
            # 去掉 FastAPI 路径参数名，转成通配形式便于比对
            routes.add(re.sub(r"\{[^}]+\}", "{}", p))
    return routes


def frontend_paths():
    src = open(os.path.join(ROOT, "frontend", "services", "api.ts"), encoding="utf-8").read()
    found = {}
    # get/post/put/del('/xxx' 或 `/xxx`
    for m in re.finditer(
            r"\b(?:get|post|put|del)(?:<[^>]*>)?\(\s*[`'\"]([^`'\"]+)[`'\"]",
            src):
        raw = m.group(1)
        ln = src[:m.start()].count("\n") + 1
        found.setdefault(raw, ln)
    return found


def normalize(p):
    """把 ${...} 与 {xxx} 统一成 {}，并去掉查询串。"""
    p = p.split("?")[0]
    p = re.sub(r"\$\{[^}]*\}", "{}", p)
    p = re.sub(r"\{[^}]*\}", "{}", p)
    return p


def main():
    routes = backend_routes()
    exact = {r for r in routes if "{" not in r}
    wild = {re.sub(r"\{[^}]+\}", "{}", r) for r in routes if "{" in r}
    print(f"后端注册路由 {len(exact)} 条（无参数）、{len(wild)} 条（带参数）\n")

    paths = frontend_paths()
    print(f"前端 api.ts 中抽取到 {len(paths)} 个路径字面量\n")

    missing, ok = [], []
    for raw, ln in sorted(paths.items(), key=lambda kv: kv[1]):
        n = normalize(raw)
        if n in exact or n in wild:
            ok.append((raw, ln, n))
        else:
            missing.append((raw, ln, n))

    print("=== 前端调用但后端【不存在】的路径（会 404） ===")
    if not missing:
        print("  （无）")
    for raw, ln, n in missing:
        print(f"  api.ts L{ln:5}  {raw:52} 归一化: {n}")

    print(f"\n=== 已匹配 {len(ok)} 条 ===")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
