# -*- coding: utf-8 -*-
"""
仅供截图使用的演示服务器。

为什么不直接用 Start.py：
  Start.py 会把数据库里的 Cookie 全部拉起 XianyuLive 任务，真实连上闲鱼。
  本脚本只做「Web 层 + CookieManager 数据加载」，绝不创建任何 XianyuLive 任务，
  因此不会产生任何对外的闲鱼连接。

用法：
    python tools/demo_server.py [--db data/demo_shot.db] [--port 8099]
"""
import argparse
import asyncio
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, ROOT)

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=os.path.join(ROOT, "data", "demo_shot.db"))
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8099)
args = ap.parse_args()

os.environ["DB_PATH"] = args.db
os.chdir(ROOT)

import cookie_manager  # noqa: E402

# 关键：只实例化 CookieManager（仅从数据库读取），不调用 _run_xianyu / main()
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
cookie_manager.manager = cookie_manager.CookieManager(loop)
print(f"[demo_server] CookieManager 已就绪，加载账号 {len(cookie_manager.manager.cookies)} 个"
      f"（未启动任何闲鱼连接）", flush=True)

import uvicorn  # noqa: E402

uvicorn.run("reply_server:app", host=args.host, port=args.port, log_level="warning")
