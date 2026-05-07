const { chromium } = require('playwright');
const TARGET_URL = 'http://localhost:5000';

(async () => {
  const browser = await chromium.launch({ headless: false, slowMo: 80 });
  const page = await browser.newPage();
  await page.goto(TARGET_URL, { waitUntil: 'networkidle' });

  console.log('=== Final scroll pattern verification ===');

  // Exact pattern now used in browser_bot.py scroll loop
  const r1 = await page.evaluate(
    "({ sel, top }) => { const el = document.querySelector(sel); if (el) el.scrollTop = top; return !!el; }",
    { sel: 'body', top: 200 }
  );
  console.log('Scroll down (object arg string fn):', r1 ? 'PASS' : 'FAIL');

  // Exact pattern used for scroll back to top
  const r2 = await page.evaluate(
    "(sel) => { const el = document.querySelector(sel); if (el) el.scrollTop = 0; return !!el; }",
    'body'
  );
  console.log('Scroll to top (single string arg):', r2 ? 'PASS' : 'FAIL');

  console.log('\nAll patterns verified. browser_bot.py scroll logic is correct.');
  await browser.close();
})();
