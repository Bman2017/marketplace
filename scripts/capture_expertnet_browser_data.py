#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path("florida-expertnet-browser-capture")
OUT.mkdir(exist_ok=True)
PAGE_URL = "https://expertnet.org/index.cfm?prefilter=false&fuseaction=search.multiSearch&view=technologies"
TARGET = "ajaxSearchData.cfc?method=loadTabData"


async def main() -> None:
    captured: dict[str, object] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
            locale="en-US",
        )
        page = await context.new_page()

        async def handle_response(response):
            if TARGET not in response.url:
                return
            text = await response.text()
            captured["url"] = response.url
            captured["status"] = response.status
            captured["headers"] = await response.all_headers()
            captured["text"] = text
            (OUT / "response.txt").write_text(text, encoding="utf-8")

        page.on("response", handle_response)
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=120_000)
        # The page fetches all rows before initializing DataTables. Wait for either
        # the raw response capture or the visible results table to become populated.
        for _ in range(180):
            if "text" in captured:
                break
            await page.wait_for_timeout(1000)

        (OUT / "page.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT / "page.png"), full_page=True)
        cookies = await context.cookies()
        await browser.close()

    if "text" not in captured:
        raise SystemExit("Browser did not observe ExpertNet loadTabData response")

    text = str(captured["text"])
    data = json.loads(text)
    query = data.get("queryData", [])
    summary = {
        "page_url": PAGE_URL,
        "response_url": captured.get("url"),
        "response_status": captured.get("status"),
        "response_bytes": len(text.encode("utf-8")),
        "top_level_keys": sorted(data.keys()),
        "record_count_value": data.get("recordCount"),
        "query_count": len(query),
        "record_keys": sorted(query[0].keys()) if query else [],
        "sample_records": query[:3],
        "cookie_names": sorted({cookie["name"] for cookie in cookies}),
    }
    (OUT / "decoded.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False)[:100_000])


if __name__ == "__main__":
    asyncio.run(main())
