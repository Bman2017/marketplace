#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

BASE = "https://available-inventions.umich.edu/"
PRODUCTS = urljoin(BASE, "products")
OUT = Path("umich-discovery")


def safe_name(value: str, limit: int = 180) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return value[:limit] or "response"


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    json_dir = OUT / "json_responses"
    body_dir = OUT / "response_bodies"
    json_dir.mkdir(exist_ok=True)
    body_dir.mkdir(exist_ok=True)

    network: list[dict] = []
    json_index = 0
    response_index = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1100},
            locale="en-US",
        )
        page = await context.new_page()

        async def capture(response):
            nonlocal json_index, response_index
            request = response.request
            headers = await response.all_headers()
            content_type = headers.get("content-type", "")
            entry = {
                "url": response.url,
                "status": response.status,
                "method": request.method,
                "resource_type": request.resource_type,
                "content_type": content_type,
                "request_post_data": request.post_data,
            }
            network.append(entry)
            try:
                if "json" in content_type.lower():
                    body = await response.body()
                    name = f"{json_index:04d}_{safe_name(urlparse(response.url).path)}.json"
                    (json_dir / name).write_bytes(body)
                    entry["saved_body"] = str(json_dir / name)
                    entry["sha256"] = hashlib.sha256(body).hexdigest()
                    json_index += 1
                elif any(token in response.url.lower() for token in ["product", "search", "catalog", "api", "graphql"]):
                    body = await response.body()
                    if len(body) <= 5_000_000:
                        suffix = ".html" if "html" in content_type.lower() else ".txt"
                        name = f"{response_index:04d}_{safe_name(urlparse(response.url).path)}{suffix}"
                        (body_dir / name).write_bytes(body)
                        entry["saved_body"] = str(body_dir / name)
                        entry["sha256"] = hashlib.sha256(body).hexdigest()
                        response_index += 1
            except Exception as exc:
                entry["capture_error"] = repr(exc)

        page.on("response", capture)
        await page.goto(PRODUCTS, wait_until="networkidle", timeout=120_000)
        await page.screenshot(path=str(OUT / "products_initial.png"), full_page=True)
        (OUT / "products_initial.html").write_text(await page.content(), encoding="utf-8")

        # Attempt to expose all cards through repeated scrolling and common controls.
        stable_rounds = 0
        prior_links = -1
        for _ in range(120):
            await page.mouse.wheel(0, 5000)
            await page.wait_for_timeout(750)
            for label in ["Load more", "Show more", "View more", "Next"]:
                locator = page.get_by_text(label, exact=False)
                try:
                    if await locator.count() and await locator.first.is_visible():
                        await locator.first.click(timeout=1500)
                        await page.wait_for_timeout(1200)
                except Exception:
                    pass
            links = await page.locator('a[href*="/product/"]').count()
            if links == prior_links:
                stable_rounds += 1
            else:
                stable_rounds = 0
                prior_links = links
            if stable_rounds >= 8:
                break

        product_links = await page.locator('a[href*="/product/"]').evaluate_all(
            "els => Array.from(new Set(els.map(e => e.href)))"
        )
        text = await page.locator("body").inner_text()
        (OUT / "products_final.html").write_text(await page.content(), encoding="utf-8")
        (OUT / "products_final_text.txt").write_text(text, encoding="utf-8")
        await page.screenshot(path=str(OUT / "products_final.png"), full_page=True)

        storage = await page.evaluate(
            """() => ({
                localStorage: Object.fromEntries(Object.entries(localStorage)),
                sessionStorage: Object.fromEntries(Object.entries(sessionStorage))
            })"""
        )
        cookies = await context.cookies()

        # Directly inspect common public metadata endpoints in the same browser context.
        endpoint_results = []
        endpoints = [
            "robots.txt", "sitemap.xml", "sitemap_index.xml", "product-sitemap.xml",
            "products.json", "api/products", "api/v1/products", "api/catalog/products",
            "graphql", "api/graphql", "manifest.json"
        ]
        for endpoint in endpoints:
            url = urljoin(BASE, endpoint)
            try:
                response = await context.request.get(url, timeout=60_000)
                body = await response.body()
                file_name = f"endpoint_{safe_name(endpoint)}.bin"
                (OUT / file_name).write_bytes(body)
                endpoint_results.append({
                    "url": url,
                    "status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "saved_body": file_name,
                })
            except Exception as exc:
                endpoint_results.append({"url": url, "error": repr(exc)})

        summary = {
            "base": BASE,
            "products_url": PRODUCTS,
            "final_page_url": page.url,
            "product_link_count": len(product_links),
            "product_links": sorted(product_links),
            "network_request_count": len(network),
            "json_response_count": json_index,
            "captured_response_count": response_index,
            "body_text_head": text[:5000],
            "storage": storage,
            "cookies": cookies,
            "endpoint_results": endpoint_results,
        }
        (OUT / "discovery_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (OUT / "network_log.json").write_text(
            json.dumps(network, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        await browser.close()

    print(json.dumps({
        "product_link_count": len(product_links),
        "network_request_count": len(network),
        "json_response_count": json_index,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
