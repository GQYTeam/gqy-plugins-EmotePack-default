#!/usr/bin/env node
// GQY 表情包插件静态自检（零依赖）：校验 manifest.json 字段与 data/assets.json 资产清单，
// 并核对清单与磁盘文件（存在、非空、PNG/GIF 魔数与尺寸、目录内无未登记文件；系统元数据忽略）。
// 另用仓库的 /tmp 副本验证：emotes/ 里出现 .DS_Store 时 list 仍成功，出现 foo.txt 时仍报错。
// 失败打印原因并以 1 退出；成功打印一行 OK。
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const EXPECTED_ACTIONS = ['list', 'show', 'match', 'suggest'];
// 与 index.js 保持一致：macOS/Windows 系统元数据不算资产，也不算「未登记文件」。
const IGNORED_METADATA_NAMES = new Set(['.DS_Store', '.localized', 'Thumbs.db', 'thumbs.db']);

function isIgnoredMetadata(name) {
  return IGNORED_METADATA_NAMES.has(name) || name.startsWith('._');
}

const problems = [];
let checks = 0;

function check(cond, why) {
  checks += 1;
  if (!cond) problems.push(why);
}

function readJson(file, label) {
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (err) {
    problems.push(label + ' 无法读取：' + err.message);
    return null;
  }
  try {
    return JSON.parse(text);
  } catch (err) {
    problems.push(label + ' 不是合法 JSON：' + err.message);
    return null;
  }
}

function readHead(abs) {
  const fd = fs.openSync(abs, 'r');
  try {
    const buf = Buffer.alloc(33);
    const read = fs.readSync(fd, buf, 0, buf.length, 0);
    return buf.subarray(0, read);
  } finally {
    fs.closeSync(fd);
  }
}

// PNG：8 字节魔术 + 必须首个 chunk 是 IHDR，宽高为大端 32 位。
function pngSize(head) {
  if (head.length < 33 || !head.subarray(0, 8).equals(PNG_MAGIC)) return null;
  if (head.subarray(12, 16).toString('latin1') !== 'IHDR') return null;
  return { width: head.readUInt32BE(16), height: head.readUInt32BE(20) };
}

// GIF：GIF87a/GIF89a 魔术 + 逻辑屏宽高为小端 16 位。
function gifSize(head) {
  if (head.length < 10) return null;
  const sig = head.subarray(0, 6).toString('latin1');
  if (sig !== 'GIF87a' && sig !== 'GIF89a') return null;
  return { width: head.readUInt16LE(6), height: head.readUInt16LE(8) };
}

function isSafeRelative(file) {
  if (typeof file !== 'string' || !file.trim() || file.includes('\0')) return false;
  if (path.isAbsolute(file)) return false;
  const normalized = path.normalize(file);
  const resolved = path.resolve(ROOT, normalized);
  return (
    normalized !== '..' &&
    !normalized.startsWith('..' + path.sep) &&
    (resolved === ROOT || resolved.startsWith(ROOT + path.sep))
  );
}

// ---------- manifest.json ----------

const manifest = readJson(path.join(ROOT, 'manifest.json'), 'manifest.json');

if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
  console.error('FAIL: manifest.json 根节点必须是 JSON 对象');
  process.exit(1);
}

check(typeof manifest.id === 'string' && /^[a-z0-9][a-z0-9_-]{0,31}$/.test(manifest.id), 'manifest.id 不符合 ^[a-z0-9][a-z0-9_-]{0,31}$');
check(manifest.id === 'emote-pack-default', 'manifest.id 应为 emote-pack-default');
check(
  typeof manifest.name === 'string' && manifest.name.trim().length > 0 && [...manifest.name.trim()].length <= 64,
  'manifest.name 应为 1–64 字符'
);
check(manifest.name === '表情包插件', 'manifest.name 应为 表情包插件');
check(typeof manifest.version === 'string' && /^\d{1,4}\.\d{1,4}\.\d{1,4}$/.test(manifest.version), 'manifest.version 应形如 1.1.0');
check(manifest.version === '1.2.0', 'manifest.version 应为 1.2.0');
check(
  typeof manifest.description === 'string' && manifest.description.trim().length > 0 && [...manifest.description].length <= 200,
  'manifest.description 应为 1–200 字符'
);
check(manifest.protocol === 'gqy-jsonrpc-v1', 'manifest.protocol 应为 gqy-jsonrpc-v1');
check(
  Array.isArray(manifest.command) && manifest.command.length > 0 && manifest.command.every((c) => typeof c === 'string' && c.length > 0),
  'manifest.command 应为非空字符串数组'
);
check(JSON.stringify(manifest.command) === JSON.stringify(['node', 'index.js']), 'manifest.command 应为 ["node","index.js"]');
check(manifest.cwd === '.', 'manifest.cwd 应为 "."');
check(Number.isInteger(manifest.timeout_ms) && manifest.timeout_ms >= 1 && manifest.timeout_ms <= 120000, 'manifest.timeout_ms 应为 1–120000 的整数');
check(manifest.timeout_ms === 10000, 'manifest.timeout_ms 应为 10000');
check(['concurrent', 'single-channel', 'exclusive'].includes(manifest.concurrency), 'manifest.concurrency 取值非法');
check(manifest.concurrency === 'concurrent', 'manifest.concurrency 应为 concurrent');
check(Array.isArray(manifest.provides) && manifest.provides.length === 0, 'manifest.provides 应为空数组');
check(Array.isArray(manifest.requires) && manifest.requires.length === 0, 'manifest.requires 应为空数组');

// settings schema（PL3）：auto_emote / min_score 控制「回复后自动发表情」
const settings = Array.isArray(manifest.settings) ? manifest.settings : [];
check(settings.length > 0, 'manifest.settings 应声明设置项');
const settingKeys = new Set();
for (const s of settings) {
  check(s && typeof s === 'object' && !Array.isArray(s), 'settings 每项必须是对象');
  check(typeof s.key === 'string' && s.key.length > 0, 'settings.key 必须非空');
  check(!settingKeys.has(s.key), 'settings.key 重复：' + String(s.key));
  settingKeys.add(s.key);
  check(['string', 'number', 'boolean', 'select'].includes(s.type), 'settings.type 非法：' + String(s.type));
  check(typeof s.title === 'string' && s.title.length > 0, 'settings.title 必填');
  check(s.default !== undefined, 'settings.default 必填：' + String(s.key));
  check(s.type !== 'select' || (Array.isArray(s.options) && s.options.length > 0), 'select 必须给 options');
}
const auto = settings.find((s) => s.key === 'auto_emote');
const minScore = settings.find((s) => s.key === 'min_score');
check(Boolean(auto) && auto.type === 'boolean' && auto.default === true, 'auto_emote 应为 boolean 且默认 true');
check(Boolean(minScore) && minScore.type === 'number' && minScore.default === 0, 'min_score 应为 number 且默认 0');


const actions = Array.isArray(manifest.actions) ? manifest.actions : [];
check(Array.isArray(manifest.actions) && manifest.actions.length === 4, 'manifest.actions 应恰好 4 个动作');
check(
  actions.map((a) => (a && typeof a === 'object' ? a.name : undefined)).join(',') === EXPECTED_ACTIONS.join(','),
  'manifest.actions 必须依次为 list/show/match/suggest'
);
const actionMap = new Map();
for (const action of actions) {
  if (!action || typeof action !== 'object' || Array.isArray(action)) {
    check(false, 'manifest.actions 每项都必须是对象');
    continue;
  }
  check(/^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(String(action.name)), '动作名不合法：' + String(action.name));
  check(!actionMap.has(action.name), '动作名重复：' + String(action.name));
  actionMap.set(action.name, action);
}
for (const name of EXPECTED_ACTIONS) {
  const action = actionMap.get(name);
  check(Boolean(action), 'manifest.actions 缺少动作 ' + name);
  if (!action) continue;
  check(
    typeof action.description === 'string' && [...action.description].length >= 1 && [...action.description].length <= 200,
    '动作 ' + name + ' 的 description 应为 1–200 字符'
  );
  check(action.schema && typeof action.schema === 'object' && !Array.isArray(action.schema), '动作 ' + name + ' 的 schema 应为对象');
  check(action.schema && action.schema.type === 'object', '动作 ' + name + ' 的 schema 根 type 必须为 object');
}

const listSchema = (actionMap.get('list') || {}).schema || {};
check(JSON.stringify(listSchema.properties || {}) === '{}', 'list 的 schema.properties 应为空对象');

const showSchema = (actionMap.get('show') || {}).schema || {};
check(Array.isArray(showSchema.required) && showSchema.required.includes('name'), 'show 的 schema.required 应包含 name');
check(showSchema.properties && showSchema.properties.name && showSchema.properties.name.type === 'string', 'show 的 name 应为 string');
check(showSchema.properties && showSchema.properties.inline && showSchema.properties.inline.type === 'boolean', 'show 的 inline 应为 boolean');

const matchSchema = (actionMap.get('match') || {}).schema || {};
check(Array.isArray(matchSchema.required) && matchSchema.required.includes('query'), 'match 的 schema.required 应包含 query');
check(matchSchema.properties && matchSchema.properties.query && matchSchema.properties.query.type === 'string', 'match 的 query 应为 string');
const limitSchema = (matchSchema.properties || {}).limit || {};
check(
  limitSchema.type === 'integer' && limitSchema.minimum === 1 && limitSchema.maximum === 24,
  'match 的 limit 应为 1–24 的 integer'
);

const suggestSchema = (actionMap.get('suggest') || {}).schema || {};
check(Array.isArray(suggestSchema.required) && suggestSchema.required.includes('context'), 'suggest 的 schema.required 应包含 context');
const contextSchema = (suggestSchema.properties || {}).context || {};
check(
  contextSchema.type === 'string' && contextSchema.minLength === 1 && contextSchema.maxLength === 2000,
  'suggest 的 context 应为 1–2000 字符的 string'
);
const suggestLimitSchema = (suggestSchema.properties || {}).limit || {};
check(
  suggestLimitSchema.type === 'integer' && suggestLimitSchema.minimum === 1 && suggestLimitSchema.maximum === 5 && suggestLimitSchema.default === 3,
  'suggest 的 limit 应为 1–5、默认 3 的 integer'
);


// ---------- data/assets.json ----------

const assets = readJson(path.join(ROOT, 'data', 'assets.json'), 'data/assets.json');

if (!assets || typeof assets !== 'object' || Array.isArray(assets)) {
  console.error('FAIL: data/assets.json 根节点必须是 JSON 对象');
  process.exit(1);
}

const emotes = Array.isArray(assets.emotes) ? assets.emotes : [];
const motions = Array.isArray(assets.motions) ? assets.motions : [];
check(Array.isArray(assets.emotes) && assets.emotes.length === 24, 'assets.emotes 应恰好 24 条');
check(Array.isArray(assets.motions) && assets.motions.length === 4, 'assets.motions 应恰好 4 条');

const registered = new Map([
  ['emotes', new Set()],
  ['motions', new Set()],
]);
const seenNames = new Set();

function checkItem(item, section, index) {
  const at = section + '[' + index + ']';
  check(Boolean(item) && typeof item === 'object' && !Array.isArray(item), at + ' 应为对象');
  if (!item || typeof item !== 'object' || Array.isArray(item)) return;

  check(typeof item.name === 'string' && item.name.trim().length > 0, at + '.name 应为非空字符串');
  check(typeof item.label === 'string' && item.label.trim().length > 0, at + '.label 应为非空字符串');
  check(
    Array.isArray(item.tags) && item.tags.length > 0 && item.tags.every((t) => typeof t === 'string' && t.trim().length > 0),
    at + '.tags 应为非空字符串数组'
  );
  check(isSafeRelative(item.file), at + '.file 必须是仓库内相对路径：' + String(item.file));
  check(Number.isInteger(item.width) && item.width > 0, at + '.width 应为正整数');
  check(Number.isInteger(item.height) && item.height > 0, at + '.height 应为正整数');

  if (typeof item.name === 'string' && item.name.trim()) {
    check(!seenNames.has(item.name), '清单内 name 重复：' + item.name);
    seenNames.add(item.name);
  }

  if (typeof item.file !== 'string' || !isSafeRelative(item.file)) return;
  registered.get(section).add(item.file);

  const abs = path.resolve(ROOT, item.file);
  let stat;
  try {
    stat = fs.statSync(abs);
  } catch {
    check(false, at + ' 登记的文件不存在：' + item.file);
    return;
  }
  check(stat.isFile(), at + ' 登记的不是普通文件：' + item.file);
  check(stat.size > 0, at + ' 登记的文件为空：' + item.file);
  if (!stat.isFile() || stat.size <= 0) return;

  const head = readHead(abs);
  if (section === 'emotes') {
    check(item.width === 512 && item.height === 512, at + ' 表情尺寸必须是 512×512');
    const size = pngSize(head);
    check(size !== null, at + ' 不是合法 PNG（魔术/IHDR 校验失败）：' + item.file);
    if (size) {
      check(size.width === item.width && size.height === item.height, at + ' PNG IHDR 尺寸与清单不符：' + item.file);
    }
  } else {
    const size = gifSize(head);
    check(size !== null, at + ' 不是合法 GIF（GIF87a/89a 魔术校验失败）：' + item.file);
    if (size) {
      check(size.width === item.width && size.height === item.height, at + ' GIF 逻辑屏尺寸与清单不符：' + item.file);
    }
    check(Number.isInteger(item.frames) && item.frames > 0, at + '.frames 应为正整数');
    check(Number.isInteger(item.frame_ms) && item.frame_ms > 0, at + '.frame_ms 应为正整数');
    check(Number.isInteger(item.duration_ms) && item.duration_ms > 0, at + '.duration_ms 应为正整数');
    check(
      Number.isInteger(item.frames) && Number.isInteger(item.frame_ms) && item.duration_ms === item.frames * item.frame_ms,
      at + '.duration_ms 应等于 frames × frame_ms'
    );
    check(typeof item.note === 'string' && item.note.trim().length > 0, at + '.note 应为非空字符串');
  }
}

emotes.forEach((item, index) => checkItem(item, 'emotes', index));
motions.forEach((item, index) => checkItem(item, 'motions', index));

// 资产目录内必须恰好是清单登记的文件：不允许多余文件或子目录；系统元数据（.DS_Store 等）忽略、也不计入「已登记」。
for (const [section, dir] of [
  ['emotes', 'emotes'],
  ['motions', 'motions'],
]) {
  const absDir = path.join(ROOT, dir);
  let entries;
  try {
    entries = fs.readdirSync(absDir, { withFileTypes: true });
  } catch (err) {
    check(false, dir + '/ 无法读取：' + err.message);
    continue;
  }
  const diskNames = new Set();
  for (const entry of entries) {
    if (isIgnoredMetadata(entry.name)) continue;
    diskNames.add(entry.name);
    check(!entry.isDirectory(), dir + '/ 内不允许出现子目录：' + entry.name);
    check(registered.get(section).has(dir + '/' + entry.name), dir + '/' + entry.name + ' 未在 data/assets.json 登记');
  }
  check(
    diskNames.size === registered.get(section).size,
    dir + '/ 文件数应与清单一致（磁盘 ' + diskNames.size + '，清单 ' + registered.get(section).size + '）'
  );
}

// ---------- 语境模型 data/emote-model.json（训练产物，基本形状；深度校验见 test/model.mjs） ----------

const modelFile = path.join(ROOT, 'data', 'emote-model.json');
const emoteNames = emotes.map((item) => (item && typeof item === 'object' ? item.name : undefined));

{
  let stat = null;
  try {
    stat = fs.statSync(modelFile);
  } catch {
    check(false, 'data/emote-model.json 不存在（语境模型训练产物未就绪）');
  }
  if (stat) {
    check(stat.isFile(), 'data/emote-model.json 应为普通文件');
    check(
      stat.size > 0 && stat.size <= 600 * 1024,
      'data/emote-model.json 体积应在 (0, 600KB] 内（实际 ' + stat.size + ' 字节）'
    );
    const model = readJson(modelFile, 'data/emote-model.json');
    if (model && typeof model === 'object' && !Array.isArray(model)) {
      check(model.schema === 1, '语境模型 schema 应为 1');
      check(model.task === 'context-emote-clf', '语境模型 task 应为 context-emote-clf');
      check(Array.isArray(model.classes) && model.classes.length === 25, '语境模型 classes 应为 25 项');
      if (Array.isArray(model.classes)) {
        check(model.classes[24] === 'none', '语境模型 classes 末位应为 none');
        check(
          JSON.stringify(model.classes.slice(0, 24)) === JSON.stringify(emoteNames),
          '语境模型 classes 前 24 项顺序应与 data/assets.json 表情清单一致'
        );
      }
      const input = model.input && typeof model.input === 'object' && !Array.isArray(model.input) ? model.input : {};
      check(
        input.normalize === 'nfkc-lower-strip-all-whitespace' &&
          input.max_chars === 120 &&
          input.feature_value === 'binary' &&
          JSON.stringify(input.ngram_order) === JSON.stringify([1, 2, 3]),
        '语境模型 input 规格应匹配 model-format 第 4 节'
      );
      check(Array.isArray(input.vocab) && input.vocab.length > 0 && input.vocab.length <= 1200, '语境模型 vocab 应为 1–1200 项');
      const net = model.model && typeof model.model === 'object' && !Array.isArray(model.model) ? model.model : {};
      check(net.type === 'mlp-tanh-softmax', '语境模型 model.type 应为 mlp-tanh-softmax');
      check(net.hidden === 32, '语境模型 model.hidden 应为 32（预算硬上限）');
      check(
        Array.isArray(net.w1) && Array.isArray(input.vocab) && net.w1.length === input.vocab.length,
        '语境模型 w1 行数应等于 vocab 长度'
      );
      check(Array.isArray(net.w2) && Number.isInteger(net.hidden) && net.w2.length === net.hidden, '语境模型 w2 行数应等于 hidden');
      check(Array.isArray(net.b2) && net.b2.length === 25, '语境模型 b2 长度应为 25');
      check(Array.isArray(model.parity) && model.parity.length >= 8, '语境模型 parity 应 ≥ 8 条');
      check(
        model.metrics &&
          typeof model.metrics === 'object' &&
          Number.isFinite(model.metrics.val_acc1) &&
          Number.isFinite(model.metrics.holdout_acc1) &&
          Number.isFinite(model.metrics.none_recall),
        '语境模型 metrics 应含 val_acc1 / holdout_acc1 / none_recall'
      );
    }
  }
}

// ---------- 临时副本：系统元数据不应让运行时整体报错 ----------

function copyPluginForTempTest(destDir) {
  fs.mkdirSync(destDir, { recursive: true });
  for (const file of ['index.js', 'manifest.json', 'package.json']) {
    fs.copyFileSync(path.join(ROOT, file), path.join(destDir, file));
  }
  for (const dir of ['data', 'emotes', 'motions']) {
    fs.cpSync(path.join(ROOT, dir), path.join(destDir, dir), { recursive: true });
  }
}

function runListInTempPlugin(dir) {
  const input =
    JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'call', params: { action: 'list', arguments: {} } }) +
    '\n' +
    JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'shutdown' }) +
    '\n';
  const result = spawnSync(process.execPath, ['index.js'], {
    cwd: dir,
    input,
    encoding: 'utf8',
    timeout: 20000,
  });
  const lines = (result.stdout || '').split('\n').filter((line) => line.trim().length > 0);
  return { result, lines };
}

function parseFirstReply(lines) {
  if (!lines.length) return null;
  try {
    return JSON.parse(lines[0]);
  } catch {
    return null;
  }
}

function checkMetadataTolerance() {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'gqy-emote-pack-metadata-'));
  try {
    const pluginDir = path.join(tempRoot, 'emote-pack-default');
    copyPluginForTempTest(pluginDir);

    // 1) 只放系统元数据（macOS Finder / 拷盘常见）：list 必须成功。
    fs.writeFileSync(path.join(pluginDir, 'emotes', '.DS_Store'), 'dummy');
    fs.writeFileSync(path.join(pluginDir, 'emotes', '._01-平静.png'), 'dummy');
    fs.writeFileSync(path.join(pluginDir, 'motions', '.localized'), 'dummy');
    fs.writeFileSync(path.join(pluginDir, 'motions', 'Thumbs.db'), 'dummy');
    const tolerantRun = runListInTempPlugin(pluginDir);
    const tolerantReply = parseFirstReply(tolerantRun.lines);
    check(
      tolerantRun.result.status === 0,
      '临时副本：含系统元数据时进程应 0 退出（实际 ' +
        String(tolerantRun.result.status) +
        '，stderr=' +
        String(tolerantRun.result.stderr || '').trim() +
        '）'
    );
    check(
      Boolean(tolerantReply && tolerantReply.result && typeof tolerantReply.result.content === 'string'),
      '临时副本：含系统元数据时 list 应成功（实际 ' + String(tolerantRun.lines[0] || '无回包') + '）'
    );
    if (tolerantReply && tolerantReply.result && typeof tolerantReply.result.content === 'string') {
      let content = null;
      try {
        content = JSON.parse(tolerantReply.result.content);
      } catch {
        content = null;
      }
      check(Boolean(content) && content.count === 28, '临时副本：忽略系统元数据后 list.count 应为 28');
    }

    // 2) 再放一个真正的多余文件：list 必须报错且消息含「未登记」。
    fs.writeFileSync(path.join(pluginDir, 'emotes', 'foo.txt'), 'dummy');
    const strictRun = runListInTempPlugin(pluginDir);
    const strictReply = parseFirstReply(strictRun.lines);
    check(
      Boolean(strictReply && strictReply.error && strictReply.error.code === -32000),
      '临时副本：出现 foo.txt 时 list 应返回 -32000（实际 ' + String(strictRun.lines[0] || '无回包') + '）'
    );
    check(
      Boolean(strictReply && strictReply.error && /未登记/.test(strictReply.error.message)),
      '临时副本：foo.txt 的错误消息应含「未登记」'
    );
    check(strictRun.result.status === 0, '临时副本：多余文件报错后进程仍应 0 退出（实际 ' + String(strictRun.result.status) + '）');
  } catch (err) {
    problems.push('临时副本元数据用例执行失败：' + (err && err.message ? err.message : String(err)));
  } finally {
    try {
      fs.rmSync(tempRoot, { recursive: true, force: true });
    } catch {
      // 清理失败不影响断言结果
    }
  }
}

checkMetadataTolerance();

// ---------- 结果 ----------

if (problems.length > 0) {
  console.error('FAIL: manifest 与资产清单校验未通过（' + problems.length + ' 项）：');
  for (const problem of problems) console.error('  - ' + problem);
  process.exit(1);
}

console.log('OK: manifest.json 与 data/assets.json 校验通过（' + checks + ' 项检查，含临时副本系统元数据用例）');
