"""
Generate labels-guide.pdf from labels-guide.html using Playwright.
Run: python export_labels_pdf.py
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

HTML_FILE = Path(__file__).parent / "dashboard" / "static" / "labels-guide.html"
PDF_FILE  = Path(__file__).parent / "dashboard" / "static" / "labels-guide.pdf"


async def export():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page    = await browser.new_page()

        await page.goto(HTML_FILE.as_uri())
        await page.wait_for_timeout(500)   # let fonts/styles settle

        await page.pdf(
            path=str(PDF_FILE),
            format="A4",
            print_background=True,          # keeps dark background + colours
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )

        await browser.close()
        print(f"PDF saved: {PDF_FILE}")


asyncio.run(export())
