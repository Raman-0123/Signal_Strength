#debug_playwright.py
"""Debug script to see what Google returns when we hit it with Playwright"""
import asyncio

from playwright.async_api import async_playwright


async def debug():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = await context.new_page()
        await page.goto("https://www.google.com/search?q=site:linkedin.com/in+CMO+Delhi+NCR", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        
        # Save full HTML to file
        content = await page.content()
        with open("debug_google.html", "w") as f:
            f.write(content)
        
        # Print all anchor hrefs containing linkedin
        links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a')).map(a => ({href: a.href, text: a.innerText.slice(0,80)}))
        """)
        
        linkedin_links = [l for l in links if 'linkedin' in l.get('href', '')]
        print(f"Total links: {len(links)}")
        print(f"LinkedIn links: {len(linkedin_links)}")
        for l in linkedin_links[:10]:
            print(f"  {l['href']}")
        
        title = await page.title()
        print(f"Page title: {title}")
        
        await browser.close()

asyncio.run(debug())
