#!/usr/bin/env python3
"""通过 Web Inspector (InspectorSession) 在 iPad Safari 中打开 URL Scheme。

背景：`pymobiledevice3 webinspector launch <url>` 走 AutomationSession + WebDriver.get，
     会等待页面加载事件，对 `apple-magnifier://` 这类 URL Scheme 导航会卡死。
     本脚本改用 InspectorSession.runtime_evaluate 执行 `window.location = <url>`，
     不等待页面加载，因此不会卡死（参见 Kline-Update-Plans.md 的验证记录）。

用法：
    python webinspector_open_url.py "apple-magnifier://"
    python webinspector_open_url.py "apple-magnifier://install?url=https://gitee.com/.../Kline.ipa"

前置条件：
    - iPad 通过 USB 连接，且已开启 Safari 的 Web Inspector（设置 → Safari → 高级）
    - iPad 上 Safari 已打开至少一个标签页（本脚本选择第一个 Safari 页面）
"""
import asyncio
import sys

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.webinspector import WebinspectorService


async def open_url(url: str) -> None:
    lockdown = await create_using_usbmux()
    inspector = WebinspectorService(lockdown=lockdown)
    await inspector.connect()
    async with inspector:
        app_pages = await inspector.get_open_application_pages(timeout=5.0)
        if not app_pages:
            print("未找到可用的 Safari 页面。请先在 iPad 上打开 Safari 并停留在一个标签页。")
            return

        # 优先选 Safari 的页面
        app_page = next(
            (ap for ap in app_pages if "Safari" in ap.application.name),
            app_pages[0],
        )
        print(f"目标页面: {app_page}")
        session = await inspector.inspector_session(app_page.application, app_page.page)
        await session.navigate_to_url(url)
        print(f"已发送导航: {url}")
        await asyncio.sleep(1.0)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "apple-magnifier://"
    asyncio.run(open_url(url))
