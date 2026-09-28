# -*- coding: utf-8 -*-
"""
生成 README 界面截图。

要点：
  1. 主题可控：--theme light（默认，与仓库中已提交的截图一致）/ --theme dark。
     两种主题都通过 localStorage.theme + <html class="dark"> 显式指定，
     不依赖浏览器 prefers-color-scheme，保证结果可复现。
  2. 用演示库的登录态直接注入 localStorage，跳过手工登录。
  3. 逐页点击左侧导航截图；关键词管理页还需切换三个 Tab，
     分别对应 README 引用的 keywords-reply / keywords-delivery / keywords-default。
  4. 截图后做主题校验（暗色要求平均亮度低于阈值），杜绝“假暗黑”。
  5. 视口 1680x1000（与历史截图尺寸一致）。

前置：
  1) python tools/seed_demo_data.py           # 生成 data/demo_shot.db
  2) python tools/demo_server.py --port 8099  # 必须用它，普通 uvicorn 会让
                                              # cookie_manager.manager 为 None，
                                              # 导致关键词/账号接口 500

用法：
    python tools/shoot_screenshots.py [--base http://127.0.0.1:8099]
                                      [--theme light|dark] [--out docs/images]
                                      [--suffix -dark]

说明：浅色截图不带后缀，暗色截图默认带 -dark 后缀，两套主题可共存。
    python tools/shoot_screenshots.py --theme light            # dashboard.png ...
    python tools/shoot_screenshots.py --theme dark             # dashboard-dark.png ...
"""
import argparse
import io
import json
import os
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 每个页面：左侧导航文字 -> [(Tab 文字 或 None, 输出文件名), ...]
PAGES = [
    ("仪表盘", [(None, "dashboard.png")]),
    ("账号管理", [(None, "accounts.png")]),
    ("订单管理", [(None, "orders.png")]),
    ("卡密库存", [(None, "cards.png")]),
    ("商品列表", [(None, "items.png")]),
    ("关键词管理", [
        ("关键词回复", "keywords-reply.png"),
        ("关键词发货", "keywords-delivery.png"),
        ("账号默认回复", "keywords-default.png"),
    ]),
    ("黑名单管理", [(None, "blacklist.png")]),
    ("系统与AI", [(None, "settings.png")]),
]

VIEWPORT = {"width": 1680, "height": 1000}

# 暗色截图要求平均亮度低于该值
DARK_LUMA_MAX = 90.0
# 亮色截图要求平均亮度高于该值
LIGHT_LUMA_MIN = 200.0


def api_login(base, username, password):
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        base + "/login", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    if not data.get("success") or not data.get("token"):
        raise RuntimeError(f"登录失败: {data}")
    return data["token"]


def avg_luma(path):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im = im.resize((160, 100))
    px = list(im.getdata())
    return sum(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in px) / len(px)


def click_by_text(page, text, timeout=15000):
    """按可见文字点击按钮；优先语义角色，回退到文本定位。"""
    try:
        loc = page.get_by_role("button", name=text, exact=False)
        if loc.count() > 0:
            loc.first.click(timeout=timeout)
            return True
    except Exception:
        pass
    try:
        loc = page.locator(f"button:has-text('{text}')")
        if loc.count() > 0:
            loc.first.click(timeout=timeout)
            return True
    except Exception:
        pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "images"))
    ap.add_argument("--config", default=os.path.join(ROOT, "global_config.yml"))
    ap.add_argument("--theme", choices=["light", "dark"], default="light",
                    help="截图主题，默认 light（与仓库已提交截图保持一致）")
    ap.add_argument("--suffix", default=None,
                    help="输出文件名后缀。默认：浅色不加后缀，暗色加 -dark，"
                         "以便两套主题共存于 docs/images/")
    ap.add_argument("--settle", type=float, default=2.2, help="每页渲染等待秒数")
    args = ap.parse_args()

    # 默认后缀：浅色不加，暗色加 -dark，保证两套主题互不覆盖
    if args.suffix is None:
        args.suffix = "-dark" if args.theme == "dark" else ""

    import yaml
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    auth = cfg["AUTH"]
    token = api_login(args.base, auth["admin_username"], auth["admin_password"])
    print(f"登录成功，token 长度 {len(token)}")

    os.makedirs(args.out, exist_ok=True)

    from playwright.sync_api import sync_playwright

    want_dark = args.theme == "dark"
    failures = []
    written = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            color_scheme=args.theme,
            locale="zh-CN",
        )
        # 在任何页面脚本执行前写入：主题 + 登录态
        init = "localStorage.setItem('auth_token', %s);" % json.dumps(token)
        if want_dark:
            init += ("localStorage.setItem('theme','dark');"
                     "document.documentElement.classList.add('dark');")
        else:
            init += ("localStorage.setItem('theme','light');"
                     "document.documentElement.classList.remove('dark');")
        ctx.add_init_script(init)

        page = ctx.new_page()
        page.goto(args.base + "/", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)

        is_dark = page.evaluate("document.documentElement.classList.contains('dark')")
        print(f"主题={args.theme}，html.dark={is_dark}")
        if is_dark != want_dark:
            failures.append(f"主题未生效：期望 dark={want_dark}，实际 {is_dark}")

        for nav, shots in PAGES:
            if not click_by_text(page, nav):
                print(f"  [{nav}] 导航点击失败")
                failures.append(f"{nav}: 导航失败")
                continue
            page.wait_for_timeout(int(args.settle * 1000))

            for tab, fname in shots:
                if tab:
                    if not click_by_text(page, tab):
                        print(f"  [{nav}/{tab}] Tab 点击失败")
                        failures.append(f"{nav}/{tab}: Tab 失败")
                        continue
                    page.wait_for_timeout(int(args.settle * 1000))

                # 等待加载动画消失
                try:
                    page.wait_for_selector(".animate-spin", state="detached", timeout=3000)
                except Exception:
                    pass
                page.wait_for_timeout(600)

                out_name = fname.replace(".png", f"{args.suffix}.png")
                out = os.path.join(args.out, out_name)
                page.screenshot(path=out, full_page=False)
                luma = avg_luma(out)
                if want_dark:
                    ok = luma < DARK_LUMA_MAX
                    why = "偏亮"
                else:
                    ok = luma > LIGHT_LUMA_MIN
                    why = "偏暗"
                label = f"{nav}/{tab}" if tab else nav
                print(f"  [{'OK ' if ok else why}] {label:14} -> {out_name:27} 亮度 {luma:6.2f}")
                written.append((out_name, luma, ok))
                if not ok:
                    failures.append(f"{label}: 亮度异常 {luma:.1f}")

        browser.close()

    print("\n=== 汇总 ===")
    for fname, luma, ok in written:
        print(f"  {'✓' if ok else '✗'} {fname:27} {luma:6.2f}")
    print(f"\n共生成 {len(written)} 张截图（主题 {args.theme}）")
    if failures:
        print("\n存在问题:")
        for f in failures:
            print("  -", f)
        return 1
    print("全部截图通过主题校验。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
