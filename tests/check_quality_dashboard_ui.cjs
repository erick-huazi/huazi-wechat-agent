const path = require("node:path");
const fs = require("node:fs");
const assert = require("node:assert/strict");
const [modules, base, output] = process.argv.slice(2);
const {chromium} = require(path.join(modules, "playwright"));

(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || "chrome"});
  const results = [];
  try {
    for (const [width, height] of [[1440, 900], [768, 1024], [390, 844]]) {
      for (const state of ["verified", "stale", "unverified"]) {
        const page = await browser.newPage({viewport: {width, height}});
        const errors = [];
        page.on("pageerror", error => errors.push(error.message));
        await page.route("**/api/dashboard*", route => route.continue({url: `${base}/api/dashboard?case=${state}`}));
        await page.goto(base, {waitUntil: "networkidle"});
        await page.locator("#tab-assets").click();
        const tiles = page.locator(".image-tile");
        assert.equal(await tiles.count(), 5);
        await tiles.last().scrollIntoViewIfNeeded();
        await page.waitForFunction(() => [...document.querySelectorAll("#image-grid img")].every(i => i.complete && i.naturalWidth > 0));
        const labels = await page.locator(".image-review-status").allTextContents();
        if (state === "verified") assert(labels.every(s => s.includes("100")));
        else {
          assert(labels.every(s => !s.includes("100")));
          assert(labels[0].includes(state === "stale" ? "待重验" : "待验收"));
        }
        const overflow = await page.evaluate(() => ({
          document: document.documentElement.scrollWidth > innerWidth + 1,
          labels: [...document.querySelectorAll(".image-review-status")].filter(e => e.scrollWidth > e.clientWidth + 1).length,
          overlap: [...document.querySelectorAll(".image-review-status")].filter(e => e.getBoundingClientRect().bottom > e.nextElementSibling.getBoundingClientRect().top + 1).length,
        }));
        assert.equal(overflow.document, false);
        assert.equal(overflow.labels, 0);
        assert.equal(overflow.overlap, 0);
        await page.locator("#data-health-banner summary").click();
        assert(await page.locator("#data-health-banner li code").count() > 0);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
        await page.locator("#data-health-banner summary").click();
        await tiles.first().click();
        assert(await page.locator("#image-dialog").isVisible());
        assert((await page.locator("#dialog-caption").innerText()).includes(labels[0]));
        await page.keyboard.press("Escape");
        await page.evaluate(() => scrollTo(0, 0));
        await page.screenshot({path: path.join(output, `${width}-${state}.png`), fullPage: true});
        assert.deepEqual(errors, []);
        results.push({width, height, state, image_count: 5, labels, overflow, errors});
        await page.close();
      }
    }
    fs.writeFileSync(path.join(output, "receipt.json"), JSON.stringify({fixture_only: true, passed: true, results}, null, 2));
    process.stdout.write(JSON.stringify({passed: true, scenarios: results.length, output}) + "\n");
  } finally {
    await browser.close();
  }
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
