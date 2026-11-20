#!/usr/bin/env python3
"""
启动前自检（只读）
==================
在本机第一次启动、或每次改完配置准备重启前跑一遍。它**不启动服务、不连闲鱼、
不写业务数据**，只回答一个问题：现在按启动键，会不会立刻踩坑。

检查项：
  1. 项目结构（入口、静态页、内置运行时）
  2. 配置文件能否解析、关键项是否还停在示例值
  3. Python 依赖是否齐全（缺一个就起不来，且报错很难懂）
  4. data/ 是否可写、SQLite 是否完好（只读打开，不修复）
  5. 端口是否被占用（默认 8080，读配置）
  6. 限流/熔断/通知状态文件是否可解析
  7. 后台任务与通知的接线是否在位（静态检查，确认版本没被旧包覆盖）

输出约定：`ok` / `warn`（能起但要注意）/ `fail`（起不来或会做错事）。
有 fail 时退出码 1；warn 不影响退出码。

**绝不打印任何配置值**（密钥、Cookie、webhook 地址都在里面），只报"已填 / 空 / 长度像示例"。

跑法（项目根目录）：
    python tools/preflight.py
    python tools/preflight.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
DATA = ROOT / "data"

OK = WARN = FAIL = 0
ROWS: list[tuple[str, str, str]] = []


def say(level: str, name: str, detail: str = "") -> None:
    global OK, WARN, FAIL
    if level == "ok":
        OK += 1
    elif level == "warn":
        WARN += 1
    else:
        FAIL += 1
    ROWS.append((level, name, detail))
    mark = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[fail]"}[level]
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ---------------------------------------------------------------- 1 结构
def check_structure() -> None:
    section("项目结构")
    must = [
        ("入口 Start.py", ROOT / "Start.py"),
        ("后端 reply_server.py", BACKEND / "reply_server.py"),
        ("首页 static/index.html", ROOT / "static" / "index.html"),
        ("通知页 static/notify.html", ROOT / "static" / "notify.html"),
    ]
    for name, p in must:
        say("ok" if p.exists() else "fail", name, "" if p.exists() else f"缺少 {p.name}")

    exe = "python.exe" if os.name == "nt" else str(Path("bin") / "python3")
    runtime = ROOT / "runtime" / "python" / exe
    if sys.platform == "win32":
        say("ok" if runtime.exists() else "warn", "内置 Python 运行时",
            "已就绪" if runtime.exists() else "没找到 runtime/python/python.exe，将用系统 Python")
    else:
        say("warn", "操作系统", "当前不是 Windows：本程序按 Windows 目标打包，这里只能做离线检查")


# ---------------------------------------------------------------- 2 配置
def _load_yaml():
    try:
        import yaml
    except Exception:
        say("fail", "PyYAML", "未安装，无法读取配置")
        return None
    path = ROOT / "global_config.yml"
    if not path.exists():
        say("fail", "global_config.yml", "不存在：从 global_config.example.yml 复制一份再改")
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        say("fail", "global_config.yml", f"解析失败：{type(e).__name__}（不要带 BOM、注意缩进）")
        return None
    say("ok", "global_config.yml", "可解析")
    return data


def _is_placeholder(v: str) -> bool:
    low = str(v or "").lower()
    return any(k in low for k in ("replace-with", "your-", "example.com", "changeme", "todo"))


def check_config(cfg) -> None:
    section("配置项")
    if not isinstance(cfg, dict) or not cfg:
        say("fail", "配置内容", "为空或不是键值结构")
        return

    def filled(path: str) -> bool:
        cur = cfg
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur[part]
        return bool(str(cur or "").strip())

    for label, path in [
        ("管理员账号", "AUTH.admin_username"),
        ("管理员密码", "AUTH.admin_password"),
        ("闲鱼 app_key", "APP_CONFIG.app_key"),
        ("AI 服务地址", "AI_PROVIDER.api_url"),
    ]:
        if not filled(path):
            say("warn", label, "未填写（相关功能不可用）")
            continue
        cur = cfg
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else ""
        if _is_placeholder(str(cur)):
            say("warn", label, "还是示例值，启动前请改掉")
        else:
            say("ok", label, "已填写")

    # 只报数量，不报内容：Cookie 列表里有账号 ID 与票据
    cookies = cfg.get("COOKIES")
    n = len(cookies) if isinstance(cookies, list) else 0
    say("ok" if n else "warn", "COOKIES", f"{n} 个账号" if n else "没有账号，启动后需要登录一次")


# ---------------------------------------------------------------- 3 依赖
def check_deps() -> None:
    section("Python 依赖")
    groups = {
        "核心 Web": ["fastapi", "uvicorn", "pydantic"],
        "网络": ["requests", "httpx", "aiohttp", "websockets"],
        "基础": ["yaml", "loguru", "psutil"],
        "图片/二维码": ["PIL", "qrcode"],
        "安全": ["jwt", "passlib", "cryptography"],
        "数据": ["pandas", "openpyxl", "dateutil"],
        "浏览器": ["playwright", "DrissionPage"],
        "其它": ["openai", "multipart", "execjs", "blackboxprotobuf"],
    }
    missing: list[str] = []
    for group, mods in groups.items():
        bad = []
        for m in mods:
            try:
                __import__(m)
            except Exception:
                bad.append(m)
        if bad:
            missing.extend(bad)
            say("warn", group, "缺：" + "、".join(bad))
        else:
            say("ok", group, "齐全")
    if missing:
        say("warn", "依赖缺口汇总",
            "缺依赖不影响离线自检，但对应功能会不可用；按 requirements.txt 安装")


# ---------------------------------------------------------------- 4 数据
def check_data() -> None:
    section("数据目录与数据库")
    if not DATA.exists():
        say("warn", "data/", "不存在，首次启动会自动创建")
        return
    probe = DATA / ".preflight_write_test"
    try:
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        say("ok", "data/ 可写", "")
    except Exception as e:
        say("fail", "data/ 不可写", type(e).__name__)

    dbs = sorted(DATA.glob("*.db"))
    if not dbs:
        say("warn", "SQLite", "还没有数据库文件（首次启动会建）")
        return
    bad = []
    for db in dbs:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                row = con.execute("PRAGMA quick_check").fetchone()
            finally:
                con.close()
            if not row or str(row[0]).lower() != "ok":
                bad.append(f"{db.name}:{row[0] if row else '空结果'}")
        except Exception as e:
            bad.append(f"{db.name}:{type(e).__name__} {str(e)[:60]}")
    if bad:
        say("fail", "SQLite 检查", "；".join(bad[:5]) + ("…" if len(bad) > 5 else ""))
    else:
        say("ok", "SQLite 检查", f"{len(dbs)} 个库 quick_check 通过")


def check_state_files() -> None:
    section("状态文件")
    for name in ("notify_config.json", "rate_limit.json", "circuit.json", "notify_failures.json"):
        p = DATA / name
        if not p.exists():
            say("ok", name, "还没生成（正常）")
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            kind = "数组" if isinstance(raw, list) else "对象"
            say("ok", name, f"可解析（{kind}）")
        except Exception as e:
            say("warn", name, f"解析失败：{type(e).__name__}（程序会按“放行/空”处理，不阻塞启动）")

    half = {}
    try:
        half = (json.loads((DATA / "circuit.json").read_text(encoding="utf-8")) or {}).get("half") or {}
    except Exception:
        pass
    if half:
        names = "、".join(list(half)[:5])
        say("warn", "半开中的账号", f"{len(half)} 个（{names}）；启动后会先试探再放量")


# ---------------------------------------------------------------- 5 端口
def check_port(cfg, override: int | None) -> None:
    section("端口")
    port = override
    if port is None:
        try:
            port = int((cfg or {}).get("WEB_SECURITY", {}).get("port") or 8080)
        except Exception:
            port = 8080
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", int(port)))
        say("ok", f"127.0.0.1:{port}", "可绑定")
    except OSError as e:
        say("fail", f"127.0.0.1:{port}", f"被占用（{e.errno}）：先关掉旧进程再启动")
    finally:
        s.close()


# ---------------------------------------------------------------- 6 接线
def check_wiring() -> None:
    section("接线（防旧包覆盖）")
    checks = [
        ("通知重试与留痕", BACKEND / "notify_service.py", ["RETRY_ATTEMPTS", "_record_failure"]),
        ("停滞检测", BACKEND / "task_watchdog.py", ["def scan", "check_and_notify"]),
        ("半开探针", BACKEND / "rate_limiter.py", ["def gate_write", "PROBE_SPACING"]),
        ("停滞路由", BACKEND / "watchdog_api.py", ["/api/watchdog/status"]),
        ("页面卡片", ROOT / "static" / "notify.html", ["failureBox", "watchBox"]),
    ]
    for label, path, tokens in checks:
        if not path.exists():
            say("fail", label, f"缺少 {path.name}")
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        miss = [t for t in tokens if t not in text]
        if miss:
            say("fail", label, "缺少：" + "、".join(miss))
        else:
            say("ok", label, "在位")


# ---------------------------------------------------------------- 汇总
def main() -> int:
    ap = argparse.ArgumentParser(description="启动前只读自检")
    ap.add_argument("--port", type=int, default=None, help="覆盖配置里的端口")
    args = ap.parse_args()

    print("启动前自检（只读，不会启动服务、不会连闲鱼）")
    print(f"项目目录：{ROOT}")

    check_structure()
    cfg = _load_yaml()
    check_config(cfg)
    check_deps()
    check_data()
    check_state_files()
    check_port(cfg, args.port)
    check_wiring()

    print("\n" + "=" * 56)
    print(f"ok {OK} 项 / warn {WARN} 项 / fail {FAIL} 项")
    if FAIL:
        print("结论：先处理 fail 项再启动。")
        return 1
    if WARN:
        print("结论：可以启动，但先看一眼 warn 项。")
        return 0
    print("结论：可以启动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
