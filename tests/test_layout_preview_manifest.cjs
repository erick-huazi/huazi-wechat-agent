const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {spawnSync} = require('node:child_process');
const {verifyManifest} = require('../scripts/check_layout_preview.cjs');

const hash = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'huazi-layout-test-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const pack = path.join(root, 'pack');
  fs.mkdirSync(path.join(pack, 'images'), {recursive: true});
  const names = ['wechat-article.md', 'toutiao-article.md', 'zhihu-article.md', 'xiaohongshu.md', 'weibo-article.md'];
  for (let i = 1; i <= 5; i++) names.push(`images/2026-10-08-0${i}.png`);
  const inputs = {};
  for (const name of names) {
    const file = path.join(pack, name);
    fs.writeFileSync(file, name);
    inputs[name] = hash(file);
  }
  const previews = {};
  for (const platform of ['wechat', 'toutiao', 'zhihu', 'xiaohongshu', 'weibo']) {
    const file = `${platform}.html`;
    fs.writeFileSync(path.join(root, file), `<p>${platform}</p>`);
    previews[platform] = {file, sha256: hash(path.join(root, file)), image_count: 5};
  }
  const manifest = {
    schema_version: 1, status: 'local_preview_only', platform_drafts_modified: false,
    platform_native_styling_verified: false, pack, inputs_sha256: inputs, previews,
    renderer_sha256: hash(path.join(__dirname, '../scripts/article_layout.py')),
    preview_builder_sha256: hash(path.join(__dirname, '../scripts/preview_article_layout.py')),
  };
  const save = () => fs.writeFileSync(path.join(root, 'layout-preview-manifest.json'), JSON.stringify(manifest));
  save();
  return {root, pack, manifest, save};
}

test('binds the full preview manifest to its current files', t => {
  const {root} = fixture(t);
  const binding = verifyManifest(root);
  assert.equal(binding.manifest_sha256, hash(path.join(root, 'layout-preview-manifest.json')));
});

for (const [kind, relative] of [['page', 'wechat.html'], ['copy', 'pack/wechat-article.md'], ['image', 'pack/images/2026-10-08-01.png']]) {
  test(`rejects changed ${kind} bytes regardless of file timestamps`, t => {
    const {root} = fixture(t);
    const file = path.join(root, relative);
    const stat = fs.statSync(file);
    fs.appendFileSync(file, 'changed');
    fs.utimesSync(file, stat.atime, stat.mtime);
    assert.throws(() => verifyManifest(root), /LAYOUT_PREVIEW_STALE/);
  });
}

for (const field of ['renderer_sha256', 'preview_builder_sha256']) {
  test(`rejects stale ${field}`, t => {
    const {root, manifest, save} = fixture(t);
    manifest[field] = '0'.repeat(64);
    save();
    assert.throws(() => verifyManifest(root), /LAYOUT_PREVIEW_STALE/);
  });
}

test('rejects missing files', t => {
  const {root} = fixture(t);
  fs.unlinkSync(path.join(root, 'weibo.html'));
  assert.throws(() => verifyManifest(root), /LAYOUT_PREVIEW_INPUT_MISSING/);
});

test('rejects a partial platform set', t => {
  const {root, manifest, save} = fixture(t);
  delete manifest.previews.weibo;
  save();
  assert.throws(() => verifyManifest(root), /LAYOUT_PREVIEW_MANIFEST_INVALID/);
});

test('rejects a missing manuscript hash', t => {
  const {root, manifest, save} = fixture(t);
  delete manifest.inputs_sha256['weibo-article.md'];
  save();
  assert.throws(() => verifyManifest(root), /LAYOUT_PREVIEW_MANIFEST_INVALID/);
});

test('rejects renamed preview pages', t => {
  const {root, manifest, save} = fixture(t);
  manifest.previews.wechat.file = '../wechat.html';
  save();
  assert.throws(() => verifyManifest(root), /LAYOUT_PREVIEW_MANIFEST_INVALID/);
});

test('a stale preview overwrites an older success report with incomplete', t => {
  const {root} = fixture(t);
  const output = path.join(root, 'layout-browser-check.json');
  fs.writeFileSync(output, JSON.stringify({status: 'local_measurements_complete'}));
  fs.appendFileSync(path.join(root, 'wechat.html'), 'changed');
  const run = spawnSync(process.execPath, [path.join(__dirname, '../scripts/check_layout_preview.cjs'), root], {encoding: 'utf8'});
  assert.equal(run.status, 1);
  const report = JSON.parse(fs.readFileSync(output, 'utf8'));
  assert.equal(report.status, 'incomplete');
  assert.equal(report.platform_draft_verification, false);
  assert.match(report.issue, /LAYOUT_PREVIEW_STALE/);
  assert.deepEqual(report.results, []);
});

function singleChannel(t, date = '2026-10-20') {
  const f = fixture(t);
  const next = path.join(f.root, `${date}-wechat-huazi`);
  fs.renameSync(f.pack, next);
  f.pack = next;
  f.manifest.pack = next;
  f.manifest.previews = {wechat: f.manifest.previews.wechat};
  for (const name of ['toutiao-article.md', 'zhihu-article.md', 'xiaohongshu.md', 'weibo-article.md']) delete f.manifest.inputs_sha256[name];
  const plan = path.join(next, 'distribution-plan.json');
  fs.writeFileSync(plan, JSON.stringify({items: [{platform_id: 'wechat'}]}));
  Object.assign(f.manifest, {inspection_platforms: ['wechat'], plan_sha256: hash(plan),
    workflow_policy_sha256: hash(path.join(__dirname, '../config/draft-workflow.json'))});
  f.save();
  return f;
}

test('accepts the current WeChat-only full plan and five images', t => {
  const f = singleChannel(t);
  assert.deepEqual(Object.keys(verifyManifest(f.root).manifest.previews), ['wechat']);
});

test('does not shrink a historical five-channel inspection to WeChat', t => {
  const f = singleChannel(t, '2026-10-18');
  assert.throws(() => verifyManifest(f.root), /LAYOUT_PREVIEW_MANIFEST_INVALID/);
});

test('requires an unchanged full plan for single-channel previews', t => {
  const f = singleChannel(t);
  fs.appendFileSync(path.join(f.pack, 'distribution-plan.json'), ' ');
  assert.throws(() => verifyManifest(f.root), /LAYOUT_PREVIEW_STALE/);
});

test('rejects an unbound single-channel plan', t => {
  const f = singleChannel(t);
  f.manifest.plan_sha256 = null;
  f.save();
  assert.throws(() => verifyManifest(f.root), /LAYOUT_PREVIEW_MANIFEST_INVALID/);
});

test('still requires five image hashes for WeChat-only', t => {
  const f = singleChannel(t);
  delete f.manifest.inputs_sha256['images/2026-10-08-01.png'];
  f.save();
  assert.throws(() => verifyManifest(f.root), /LAYOUT_PREVIEW_MANIFEST_INVALID/);
});

test('accepts six declared images and rejects a changed declaration', t => {
  const f = singleChannel(t, '2026-11-09');
  for (let i = 1; i <= 5; i++) {
    const old = `images/2026-10-08-0${i}.png`;
    const next = `images/2026-11-09-0${i}.png`;
    fs.renameSync(path.join(f.pack, old), path.join(f.pack, next));
    f.manifest.inputs_sha256[next] = f.manifest.inputs_sha256[old];
    delete f.manifest.inputs_sha256[old];
  }
  const image = 'images/2026-11-09-06.png';
  fs.writeFileSync(path.join(f.pack, image), 'sixth');
  f.manifest.inputs_sha256[image] = hash(path.join(f.pack, image));
  const declaration = path.join(f.pack, 'image-count.json');
  fs.writeFileSync(declaration, JSON.stringify({schema_version: 1, count: 6}));
  f.manifest.image_count_sha256 = hash(declaration);
  f.manifest.previews.wechat.image_count = 6;
  f.save();
  assert.equal(verifyManifest(f.root).manifest.previews.wechat.image_count, 6);
  fs.writeFileSync(declaration, JSON.stringify({schema_version: 1, count: 7}));
  assert.throws(() => verifyManifest(f.root), /LAYOUT_PREVIEW_MANIFEST_INVALID/);
});
