#!/usr/bin/env python3
"""通过 CDP Page.navigate 在 iPad Safari 页面中导航到指定 URL（支持 URL Scheme）。

用法：
    python cdp_navigate.py <webSocketDebuggerUrl> <url>
    python cdp_navigate.py ws://127.0.0.1:9222/devtools/page/PID:4996:1 "apple-magnifier://"

说明：
    - 需要先启动 CDP 服务器：pymobiledevice3 webinspector cdp
    - 从 http://127.0.0.1:9222/json 获取 webSocketDebuggerUrl
"""
import asyncio
import json
import sys

import websockets


async def cdp_navigate(ws_url: str, url: str) -> None:
    print(f"连接: {ws_url}")
    print(f"导航到: {url}")
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}))
        try:
            resp = await asyncio.wait_for(ws.recv(), timeout=5)
            print(f"RESPONSE: {resp}")
        except asyncio.TimeoutError:
            print("NO RESPONSE within 5s (可能已触发系统级 scheme 处理)")


if __name__ == "__main__":
    ws_url, url = sys.argv[1], sys.argv[2]
    asyncio.run(cdp_navigate(ws_url, url))
