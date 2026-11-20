import asyncio, os, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1600, "height": 1200}, device_scale_factor=1)
        console = []
        page.on("console", lambda msg: console.append(f"{msg.type}: {msg.text[:200]}"))
        failed = []
        page.on("requestfailed", lambda req: failed.append(f"{req.method} {req.url[:160]} -> {req.failure}"))
        await page.goto("http://127.0.0.1:8080/", wait_until="networkidle", timeout=60000)
        inputs = page.locator("input")
        await inputs.nth(0).fill("15869047589")
        await inputs.nth(1).fill("2572725313")
        await page.get_by_role("button", name="立即登录").click()
        await page.wait_for_timeout(5000)
        await page.get_by_text("货源比价", exact=True).first.click()
        await page.wait_for_timeout(75000)
        body = await page.locator("body").inner_text()
        print("sourcing_now:", body[:1800].replace("\n", " | "))
        await page.screenshot(path=r"G:\have time fish\01-源码\xianyu-auto-delivery\data\ui_sourcing_loaded.png", full_page=True)
        print("failed:", json.dumps(failed, ensure_ascii=False)[:1000])
        print("console:", json.dumps(console[-20:], ensure_ascii=False)[:1200])
        await browser.close()
asyncio.run(main())
