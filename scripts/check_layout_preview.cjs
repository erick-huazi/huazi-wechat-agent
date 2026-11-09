// Offline QA; never attaches to a signed-in platform browser.
const fs = require('node:fs');
const path = require('node:path');
const {createHash, randomUUID} = require('node:crypto');
const {pathToFileURL} = require('node:url');

const PLATFORMS = ['wechat', 'toutiao', 'zhihu', 'xiaohongshu', 'weibo'];
const MANUSCRIPTS = ['wechat-article.md', 'toutiao-article.md', 'zhihu-article.md', 'xiaohongshu.md', 'weibo-article.md'];
const sha256 = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');

function verifyManifest(root) {
  const manifestPath = path.join(root, 'layout-preview-manifest.json');
  const raw = fs.readFileSync(manifestPath);
  const manifest = JSON.parse(raw.toString('utf8'));
  const platforms = Object.keys(manifest.previews || {}).sort();
  const inputs = Object.keys(manifest.inputs_sha256 || {});
  const declarationPath = path.join(manifest.pack, 'image-count.json');
  let imageCount = 5;
  if (fs.existsSync(declarationPath)) {
    const date = path.basename(manifest.pack).match(/^\d{4}-\d{2}-\d{2}/)?.[0];
    const declaration = JSON.parse(fs.readFileSync(declarationPath, 'utf8'));
    if (!date || date < '2026-11-09' || Object.keys(declaration).sort().join(',') !== 'count,schema_version' ||
        declaration.schema_version !== 1 || !Number.isInteger(declaration.count) || declaration.count < 5 || declaration.count > 20 ||
        manifest.image_count_sha256 !== sha256(declarationPath)) throw new Error('LAYOUT_PREVIEW_MANIFEST_INVALID');
    imageCount = declaration.count;
  } else if (manifest.image_count_sha256 != null) throw new Error('LAYOUT_PREVIEW_STALE: image count');
  let expected = PLATFORMS;
  if (Object.hasOwn(manifest, 'inspection_platforms')) {
    const policyPath = path.join(__dirname, '../config/draft-workflow.json');
    const policy = JSON.parse(fs.readFileSync(policyPath, 'utf8'));
    const planPath = path.join(manifest.pack, 'distribution-plan.json');
    const declared = manifest.inspection_platforms;
    if (!Array.isArray(declared) || declared.length === 0 ||
        declared.some(p => !PLATFORMS.includes(p)) || new Set(declared).size !== declared.length ||
        manifest.workflow_policy_sha256 !== sha256(policyPath)) {
      throw new Error('LAYOUT_PREVIEW_MANIFEST_INVALID');
    }
    if (manifest.plan_sha256 !== null) {
      if (!fs.existsSync(planPath) || manifest.plan_sha256 !== sha256(planPath)) throw new Error('LAYOUT_PREVIEW_STALE: plan');
      const plan = JSON.parse(fs.readFileSync(planPath, 'utf8'));
      const ids = (plan.items || []).map(item => item.platform_id).filter(id => PLATFORMS.includes(id)).sort();
      if (JSON.stringify(ids) !== JSON.stringify([...declared].sort())) throw new Error('LAYOUT_PREVIEW_MANIFEST_INVALID');
    }
    const date = path.basename(manifest.pack).match(/^\d{4}-\d{2}-\d{2}/)?.[0];
    const changed = policy.scope_changed_on || policy.effective_from;
    const previous = policy.previous_active_platforms || policy.active_platforms;
    const scopes = !date || date < policy.effective_from ? [PLATFORMS] :
      date < changed ? [previous] : date === changed ? [previous, policy.active_platforms] : [policy.active_platforms];
    if (!scopes.some(scope => JSON.stringify([...scope].sort()) === JSON.stringify([...declared].sort())) ||
        (declared.length !== PLATFORMS.length && manifest.plan_sha256 === null)) {
      throw new Error('LAYOUT_PREVIEW_MANIFEST_INVALID');
    }
    expected = declared;
  }
  const manuscripts = expected.map(p => MANUSCRIPTS[PLATFORMS.indexOf(p)]);
  if (manifest.schema_version !== 1 || manifest.status !== 'local_preview_only' ||
      manifest.platform_drafts_modified !== false || manifest.platform_native_styling_verified !== false ||
      JSON.stringify(platforms) !== JSON.stringify([...expected].sort()) ||
      inputs.length !== manuscripts.length + imageCount || manuscripts.some(name => !inputs.includes(name)) ||
      inputs.filter(name => /^images[\\/]\d{4}-\d{2}-\d{2}-\d{2}\.png$/.test(name)).length !== imageCount) {
    throw new Error('LAYOUT_PREVIEW_MANIFEST_INVALID');
  }
  const check = (file, expected) => {
    if (!fs.existsSync(file)) throw new Error(`LAYOUT_PREVIEW_INPUT_MISSING: ${path.basename(file)}`);
    if (!/^[a-f0-9]{64}$/.test(expected || '') || sha256(file) !== expected) {
      throw new Error(`LAYOUT_PREVIEW_STALE: ${path.basename(file)}`);
    }
  };
  for (const platform of expected) {
    const preview = manifest.previews[platform];
    if (preview.file !== `${platform}.html` || preview.image_count !== imageCount) {
      throw new Error('LAYOUT_PREVIEW_MANIFEST_INVALID');
    }
    check(path.join(root, preview.file), preview.sha256);
  }
  for (const name of inputs) check(path.join(manifest.pack, name), manifest.inputs_sha256[name]);
  check(path.join(__dirname, 'article_layout.py'), manifest.renderer_sha256);
  check(path.join(__dirname, 'preview_article_layout.py'), manifest.preview_builder_sha256);
  return {manifest, manifest_sha256: createHash('sha256').update(raw).digest('hex')};
}

async function main(root) {
  const results = [];
  let passed = false;
  let browser;
  let binding;
  let issue;
  try {
    binding = verifyManifest(root);
    const {manifest} = binding;
    const {chromium} = require('playwright');
    browser = await chromium.launch({headless: true, channel: 'msedge'});
    for (const width of [320, 375, 1280]) {
      const page = await browser.newPage({viewport: {width, height: 900}, deviceScaleFactor: 1});
      for (const platform of Object.keys(manifest.previews)) {
        await page.goto(pathToFileURL(path.join(root, `${platform}.html`)).href);
        await page.locator('img').evaluateAll(async images => {
          await Promise.all(images.map(image => image.decode()));
        });
        const checks = await page.evaluate(() => {
          const source = document.body.cloneNode(true);
          source.querySelectorAll('[data-huazi-decoration]').forEach(node => node.remove());
          return {
          images: [...document.images].map(image => ({
            loaded: image.complete && image.naturalWidth > 0,
            width: image.getBoundingClientRect().width,
          })),
          overflow: document.documentElement.scrollWidth > innerWidth,
          h1Count: document.querySelectorAll('h1').length,
          sourceLinks: document.querySelectorAll('section a[href^="https://"]').length,
          aiDisclosure: /AI\s*(辅助|协助)|人工智能辅助/.test(source.textContent),
          brandMarks: document.querySelectorAll('[data-huazi-decoration]').length,
        }; });
        const prefix = `${platform}-${width}`;
        await page.screenshot({path: path.join(root, `${prefix}-top.png`)});
        await page.evaluate(() => scrollTo(0, document.body.scrollHeight));
        await page.screenshot({path: path.join(root, `${prefix}-bottom.png`)});
        results.push({platform, width, ...checks});
        if (checks.overflow || checks.h1Count !== 1 || checks.images.length !== manifest.previews[platform].image_count ||
            checks.images.some(image => !image.loaded || image.width <= 0 || image.width > width) ||
            !checks.aiDisclosure) throw new Error(`LAYOUT_PREVIEW_CHECK_FAILED: ${prefix}`);
      }
      await page.close();
    }
    if (verifyManifest(root).manifest_sha256 !== binding.manifest_sha256) {
      throw new Error('LAYOUT_PREVIEW_CHANGED_DURING_CHECK');
    }
    passed = true;
  } catch (error) {
    issue = error.message;
    throw error;
  } finally {
    if (browser) await browser.close();
    const output = path.join(root, 'layout-browser-check.json');
    const temporary = `${output}.${randomUUID()}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify({
      status: passed ? 'local_measurements_complete' : 'incomplete',
      platform_draft_verification: false,
      verified_at: new Date().toISOString(),
      manifest_sha256: binding?.manifest_sha256 || null,
      checker_sha256: sha256(__filename),
      issue: issue || null,
      results,
    }, null, 2), {flag: 'wx'});
    try { fs.renameSync(temporary, output); }
    finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
  }
  console.log(`${results.length} viewport checks passed: declared images loaded, no horizontal overflow`);
}

module.exports = {verifyManifest};
if (require.main === module) {
  if (!process.argv[2]) {
    console.error('Usage: node scripts/check_layout_preview.cjs <preview-directory>');
    process.exitCode = 1;
  } else {
    main(path.resolve(process.argv[2])).catch(error => {console.error(error.message); process.exitCode = 1;});
  }
}
