#browser_scraper.py
"""
Browser-based Search Scraper using Playwright.
Uses Bing (primary) then DuckDuckGo (fallback) to bypass bot detection.
Both engines index LinkedIn profiles from Google's boolean search syntax.
"""

import asyncio
import random
import re
import urllib.parse

from playwright.async_api import async_playwright


def normalize_linkedin_url(url: str) -> str:
    match = re.search(r'linkedin\.com/in/([^\/\?#]+)', str(url), re.IGNORECASE)
    if not match:
        return ""
    slug = match.group(1).strip().strip('/')
    if slug.lower() in {"404", "pub", "jobs", "job", "pulse", "feed", "posts", "company"}:
        return ""
    return f"https://www.linkedin.com/in/{slug}/"


async def _scrape_bing_playwright_async(query: str, target_count: int, existing_urls: set) -> list:
    """Uses Playwright + real Chromium to search Bing for LinkedIn profiles."""
    leads = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="en-US"
        )
        page = await context.new_page()

        # Mask automation
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        pages_scraped = 0
        while len(leads) < target_count and pages_scraped < 5:
            first_param = pages_scraped * 10
            encoded = urllib.parse.quote_plus(query)
            url = f"https://www.bing.com/search?q={encoded}&first={first_param}"

            try:
                await page.goto(url, wait_until="commit", timeout=60000)
            except Exception:
                break
            await asyncio.sleep(random.uniform(2, 3))

            page_content = await page.content()
            if "captcha" in page_content.lower() or "blocked" in page_content.lower():
                break

            # Extract all hrefs from the Bing results
            raw_links = await page.evaluate("""
                () => {
                    const results = [];
                    document.querySelectorAll('li.b_algo a[href]').forEach(a => {
                        const href = a.href || '';
                        const title = a.innerText || '';
                        results.push({href, title});
                    });
                    // Also check all anchors as fallback
                    document.querySelectorAll('a[href]').forEach(a => {
                        const href = a.href || '';
                        if (href.includes('linkedin.com/in/')) {
                            const title = a.innerText || '';
                            results.push({href, title});
                        }
                    });
                    return results;
                }
            """)

            for item in raw_links:
                if len(leads) >= target_count:
                    break

                href = item.get('href', '')
                title = item.get('title', '').strip()

                if 'linkedin.com/in/' not in href:
                    continue

                # Extract clean LinkedIn profile URL
                clean_url = normalize_linkedin_url(href)
                if not clean_url:
                    continue

                if clean_url in existing_urls:
                    continue

                # Parse name, designation, company from Bing result title
                # Bing format: "John Doe - Chief Marketing Officer - Microsoft | LinkedIn"
                clean_title = re.sub(r'\|\s*LinkedIn.*', '', title).strip()
                parts = [p.strip() for p in clean_title.split('-')]
                name = parts[0] if len(parts) > 0 else "Unknown"
                designation = parts[1] if len(parts) > 1 else ""
                company = parts[2] if len(parts) > 2 else ""

                existing_urls.add(clean_url)
                leads.append({
                    "Rank": 0,
                    "Full_Name": name,
                    "Designation": designation,
                    "Company": company,
                    "LinkedIn_URL": clean_url,
                    "Lead_Score": 85 + (3 if designation else 0) + (3 if company and company != "Unknown" else 0)
                })

            pages_scraped += 1

        await browser.close()

    return leads


async def _scrape_ddg_playwright_async(query: str, target_count: int, existing_urls: set) -> list:
    """Uses Playwright + real Chromium to search DuckDuckGo for LinkedIn profiles."""
    leads = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="en-US"
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        encoded = urllib.parse.quote_plus(query)
        try:
            await page.goto(f"https://duckduckgo.com/?q={encoded}&kl=in-en", wait_until="commit", timeout=60000)
        except Exception:
            return leads
        await asyncio.sleep(random.uniform(3, 4))

        # Scroll to load more results
        for _ in range(3):
            await page.keyboard.press("End")
            await asyncio.sleep(1.5)

        raw_links = await page.evaluate("""
            () => {
                const results = [];
                document.querySelectorAll('a[data-testid="result-title-a"], article a[href]').forEach(a => {
                    const href = a.href || '';
                    const title = a.innerText || '';
                    if (href.includes('linkedin.com/in/')) {
                        results.push({href, title});
                    }
                });
                // Broader fallback
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.href || '';
                    if (href.includes('linkedin.com/in/')) {
                        results.push({href, title: a.innerText || ''});
                    }
                });
                return results;
            }
        """)

        for item in raw_links:
            if len(leads) >= target_count:
                break

            href = item.get('href', '')
            title = item.get('title', '').strip()

            clean_url = normalize_linkedin_url(href)
            if not clean_url:
                continue

            if clean_url in existing_urls:
                continue

            clean_title = re.sub(r'\|\s*LinkedIn.*', '', title).strip()
            parts = [p.strip() for p in clean_title.split('-')]
            name = parts[0] if len(parts) > 0 else "Unknown"
            designation = parts[1] if len(parts) > 1 else ""
            company = parts[2] if len(parts) > 2 else ""

            existing_urls.add(clean_url)
            leads.append({
                "Rank": 0,
                "Full_Name": name,
                "Designation": designation,
                "Company": company,
                "LinkedIn_URL": clean_url,
                "Lead_Score": 85 + (3 if designation else 0) + (3 if company and company != "Unknown" else 0)
            })

        await browser.close()

    return leads


def scrape_bing(query: str, target_count: int, existing_urls: set) -> list:
    """Synchronous wrapper — Bing browser scraper."""
    return asyncio.run(_scrape_bing_playwright_async(query, target_count, existing_urls))


def scrape_ddg_browser(query: str, target_count: int, existing_urls: set) -> list:
    """Synchronous wrapper — DuckDuckGo browser scraper."""
    return asyncio.run(_scrape_ddg_playwright_async(query, target_count, existing_urls))


if __name__ == "__main__":
    test_query = 'site:linkedin.com/in ("Chief Marketing Officer" OR CMO) ("Delhi NCR" OR Gurgaon OR Noida)'
    print("Testing Bing scraper...")
    results = scrape_bing(test_query, 15, set())
    print(f"Bing → Found {len(results)} leads:")
    for r in results:
        print(f"  [{r['Lead_Score']}] {r['Full_Name']} | {r['Designation']} | {r['Company']} | {r['LinkedIn_URL']}")

    if not results:
        print("\nBing returned 0. Testing DuckDuckGo browser scraper...")
        results = scrape_ddg_browser(test_query, 15, set())
        print(f"DDG → Found {len(results)} leads:")
        for r in results:
            print(f"  [{r['Lead_Score']}] {r['Full_Name']} | {r['Designation']} | {r['Company']} | {r['LinkedIn_URL']}")
