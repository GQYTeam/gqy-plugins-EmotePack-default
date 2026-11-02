#!/usr/bin/env node
// GQY「表情包」插件：stdio 换行 JSON-RPC 2.0（gqy-jsonrpc-v1）。
// 动作：list 列清单；show 按名称/文件路径展示（inline=true 时带 data URI）；match 确定性打分匹配。
// 零依赖，只用 Node 内置模块（readline / fs / path）。
// 资产索引只认 data/assets.json（启动时读取）；每次 call 都按磁盘重新校验（缺失/多余/空文件/尺寸不符都报清晰错误）。
// show 只在清单内查找，不接受任意路径（防路径穿越）。stdin EOF 时自退（04 P8 的最后一道防线）。
'use strict';

const readline = require('readline');
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const PLUGIN_ID = 'emote-pack-default';
const VERSION = '1.2.0';
const ASSETS_FILE = path.join(ROOT, 'data', 'assets.json');
const MODEL_FILE = path.join(ROOT, 'data', 'emote-model.json');
const ACTIONS = ['list', 'show', 'match', 'suggest'];
const HINT_NO_MATCH = '没有匹配的表情，可用 list 查看全部';
const HINT_NO_EMOTE = '该语境不适合发表情';
const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

// macOS / Windows 在目录里自动生成的系统元数据：不是资产，也不算「多余文件」。
// Finder 打开 emotes/ 或 motions/ 就会写 .DS_Store；拷到 U 盘/网络盘还会出现 ._ 开头的 AppleDouble 与 Thumbs.db。
const IGNORED_METADATA_NAMES = new Set(['.DS_Store', '.localized', 'Thumbs.db', 'thumbs.db']);

function isIgnoredMetadata(name) {
  return IGNORED_METADATA_NAMES.has(name) || name.startsWith('._');
}

// 参数形状错误（-32602）与业务错误（-32000）分开，便于调用方区分。
class ParamError extends Error {}

const state = {
  emotes: [],
  motions: [],
  all: [],
  byName: new Map(),
  byFile: new Map(),
  error: null,
  // 语境模型：首次使用时懒加载，之后常驻缓存（只解析一次）。
  model: null,
};

function logStderr(text) {
  try {
    process.stderr.write('[emote-pack] ' + text + '\n');
  } catch {
    // stderr 也写不进去就放弃，绝不影响主流程。
  }
}

// ---------- 启动时读取并校验清单结构 ----------

function requireString(value, where) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error('资产清单字段非法：' + where + ' 必须是非空字符串');
  }
  return value.trim();
}

function requireStringArray(value, where) {
  if (!Array.isArray(value)) {
    throw new Error('资产清单字段非法：' + where + ' 必须是字符串数组');
  }
  return value.map((item, index) => requireString(item, where + '[' + index + ']'));
}

function requirePositiveInt(value, where) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error('资产清单字段非法：' + where + ' 必须是正整数');
  }
  return value;
}

// 只接受插件目录内的相对路径；绝对路径、.. 穿越一律拒绝。
function requireSafeFile(value, where) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error('资产清单字段非法：' + where + ' 必须是非空字符串');
  }
  const raw = value.trim();
  if (raw.includes('\0')) {
    throw new Error('资产清单字段非法：' + where + ' 含非法字符');
  }
  if (path.isAbsolute(raw)) {
    throw new Error('资产清单字段非法：' + where + ' 不允许绝对路径：' + raw);
  }
  const normalized = path.normalize(raw);
  const resolved = path.resolve(ROOT, normalized);
  if (
    normalized === '..' ||
    normalized.startsWith('..' + path.sep) ||
    (resolved !== ROOT && !resolved.startsWith(ROOT + path.sep))
  ) {
    throw new Error('资产清单字段非法：' + where + ' 越出插件目录：' + raw);
  }
  return normalized.split(path.sep).join('/');
}

function mediaForFile(file, where) {
  const ext = path.extname(file).toLowerCase();
  if (ext === '.png') return 'image/png';
  if (ext === '.gif') return 'image/gif';
  throw new Error('资产清单字段非法：' + where + ' 只支持 .png / .gif：' + file);
}

function normalizeSection(list, where, kind) {
  if (!Array.isArray(list)) {
    throw new Error('data/assets.json 缺少数组字段 ' + where);
  }
  return list.map((raw, index) => {
    const at = where + '[' + index + ']';
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new Error('资产清单节点必须是对象：' + at);
    }
    const item = {
      kind,
      name: requireString(raw.name, at + '.name'),
      label: requireString(raw.label, at + '.label'),
      tags: requireStringArray(raw.tags, at + '.tags'),
      file: requireSafeFile(raw.file, at + '.file'),
      width: requirePositiveInt(raw.width, at + '.width'),
      height: requirePositiveInt(raw.height, at + '.height'),
    };
    item.media = mediaForFile(item.file, at + '.file');
    item.labelLower = item.label.toLowerCase();
    item.tagsLower = item.tags.map((tag) => tag.toLowerCase());
    if (kind === 'motion') {
      item.frames = requirePositiveInt(raw.frames, at + '.frames');
      item.frame_ms = requirePositiveInt(raw.frame_ms, at + '.frame_ms');
      item.duration_ms = requirePositiveInt(raw.duration_ms, at + '.duration_ms');
      if (typeof raw.note !== 'string') {
        throw new Error('资产清单字段非法：' + at + '.note 必须是字符串');
      }
      item.note = raw.note;
    }
    return item;
  });
}

function buildState() {
  let text;
  try {
    text = fs.readFileSync(ASSETS_FILE, 'utf8');
  } catch (err) {
    throw new Error('无法读取 data/assets.json（' + err.message + '）');
  }
  let data;
  try {
    data = JSON.parse(text);
  } catch (err) {
    throw new Error('data/assets.json 不是合法 JSON（' + err.message + '）');
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('data/assets.json 根节点必须是对象');
  }
  const emotes = normalizeSection(data.emotes, 'emotes', 'emote');
  const motions = normalizeSection(data.motions, 'motions', 'motion');
  const all = emotes.concat(motions);
  const byName = new Map();
  const byFile = new Map();
  for (const item of all) {
    if (byName.has(item.name)) throw new Error('清单内 name 重复：' + item.name);
    if (byFile.has(item.file)) throw new Error('清单内 file 重复：' + item.file);
    byName.set(item.name, item);
    byFile.set(item.file, item);
  }
  state.emotes = emotes;
  state.motions = motions;
  state.all = all;
  state.byName = byName;
  state.byFile = byFile;
  state.error = null;
}

// ---------- 磁盘校验（每次 call 重新做，不缓存） ----------

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

// 返回 {media,width,height}；无法识别返回 null；PNG 头损坏返回 {bad:true}。
function probeImage(head) {
  if (head.length >= 8 && head.subarray(0, 8).equals(PNG_MAGIC)) {
    if (head.length < 33 || head.subarray(12, 16).toString('latin1') !== 'IHDR') {
      return { media: 'image/png', bad: true };
    }
    return { media: 'image/png', width: head.readUInt32BE(16), height: head.readUInt32BE(20) };
  }
  if (head.length >= 10) {
    const sig = head.subarray(0, 6).toString('latin1');
    if (sig === 'GIF87a' || sig === 'GIF89a') {
      return { media: 'image/gif', width: head.readUInt16LE(6), height: head.readUInt16LE(8) };
    }
  }
  return null;
}

function fileBytes(file) {
  try {
    return fs.statSync(path.resolve(ROOT, file)).size;
  } catch (err) {
    throw new Error('读取文件大小失败：' + file + '（' + err.message + '）');
  }
}

function validateDisk() {
  const issues = [];
  for (const item of state.all) {
    const abs = path.resolve(ROOT, item.file);
    let stat;
    try {
      stat = fs.statSync(abs);
    } catch {
      issues.push('清单登记的文件不存在：' + item.file);
      continue;
    }
    if (!stat.isFile()) {
      issues.push('不是普通文件：' + item.file);
      continue;
    }
    if (stat.size <= 0) {
      issues.push('文件为空：' + item.file);
      continue;
    }
    let head;
    try {
      head = readHead(abs);
    } catch (err) {
      issues.push('无法读取文件 ' + item.file + '（' + err.message + '）');
      continue;
    }
    const info = probeImage(head);
    if (!info) {
      issues.push('不是有效的 PNG/GIF 图片：' + item.file);
      continue;
    }
    if (info.bad) {
      issues.push('PNG 文件头不完整或损坏：' + item.file);
      continue;
    }
    if (info.media !== item.media) {
      issues.push('文件内容与扩展名不符：' + item.file + '（清单按 ' + item.media + '，实际 ' + info.media + '）');
      continue;
    }
    if (info.width !== item.width || info.height !== item.height) {
      issues.push(
        '图片尺寸与清单不符：' + item.file + '（清单 ' + item.width + '×' + item.height + '，实际 ' + info.width + '×' + info.height + '）'
      );
    }
  }

  // 资产目录内不允许出现未登记的文件或子目录；系统元数据（.DS_Store 等）忽略，避免 Finder 一打开就整体报错。
  const dirs = new Map();
  for (const item of state.all) {
    const dir = path.posix.dirname(item.file);
    if (!dirs.has(dir)) dirs.set(dir, new Set());
    dirs.get(dir).add(item.file);
  }
  for (const [dir, registered] of dirs) {
    let entries;
    try {
      entries = fs.readdirSync(path.resolve(ROOT, dir), { withFileTypes: true });
    } catch (err) {
      issues.push('清单登记的目录不可读：' + dir + '（' + err.message + '）');
      continue;
    }
    for (const entry of entries) {
      if (isIgnoredMetadata(entry.name)) continue;
      const rel = dir === '.' ? entry.name : dir + '/' + entry.name;
      if (entry.isDirectory()) {
        issues.push('资产目录内出现未登记的目录：' + rel);
        continue;
      }
      if (!registered.has(rel)) {
        issues.push('资产目录内出现未登记的文件：' + rel);
      }
    }
  }

  if (issues.length > 0) {
    throw new Error('资产清单与磁盘文件不一致（' + issues.length + ' 处）：' + issues.join('；'));
  }
}

function ensureReady() {
  if (state.error) {
    throw new Error('资产清单不可用：' + state.error.message);
  }
  validateDisk();
}

// ---------- 动作 ----------

function toListEntry(item) {
  const out = {
    name: item.name,
    label: item.label,
    tags: item.tags.slice(),
    file: item.file,
    bytes: fileBytes(item.file),
    width: item.width,
    height: item.height,
  };
  if (item.kind === 'motion') {
    out.frames = item.frames;
    out.frame_ms = item.frame_ms;
    out.duration_ms = item.duration_ms;
    out.note = item.note;
  }
  return out;
}

function actionList() {
  const emotes = state.emotes.map(toListEntry);
  const motions = state.motions.map(toListEntry);
  // 语境模型元信息是只读展示字段；模型缺失/损坏不影响 list（既有行为不回退）。
  let model = null;
  try {
    model = modelMetadata();
  } catch {
    model = null;
  }
  return { count: emotes.length + motions.length, emotes, motions, model };
}

function toDataUri(item) {
  let buf;
  try {
    buf = fs.readFileSync(path.resolve(ROOT, item.file));
  } catch (err) {
    throw new Error('读取文件失败：' + item.file + '（' + err.message + '）');
  }
  return 'data:' + item.media + ';base64,' + buf.toString('base64');
}

function actionShow(args) {
  const rawName = args.name;
  const inline = args.inline;
  if (typeof rawName !== 'string' || !rawName.trim()) {
    throw new ParamError('参数非法：name 必须是非空字符串');
  }
  if (inline !== undefined && typeof inline !== 'boolean') {
    throw new ParamError('参数非法：inline 必须是布尔值');
  }
  const key = rawName.trim();
  // 只查清单（按 name 或清单内的 file 精确匹配），绝不把用户输入拼进路径。
  const item = state.byName.get(key) || state.byFile.get(key);
  if (!item) {
    throw new Error('表情或动图不存在：' + key);
  }
  const out = {
    name: item.name,
    label: item.label,
    media: item.media,
    file: item.file,
    bytes: fileBytes(item.file),
  };
  if (inline === true) {
    out.data_uri = toDataUri(item);
  }
  return out;
}

// 确定性打分：label 全等 +100；tag 全等 +60；label 包含 query +40；query 包含 label +30；
// tag 与 query 互相包含各 +20。全等是包含的特例，只在非全等时叠加包含分，保证分数可预期。
function scoreItem(item, queryLower) {
  let score = 0;
  const label = item.labelLower;
  if (label === queryLower) {
    score += 100;
  } else if (label.includes(queryLower)) {
    score += 40;
  } else if (queryLower.includes(label)) {
    score += 30;
  }
  for (const tag of item.tagsLower) {
    if (tag === queryLower) {
      score += 60;
      continue;
    }
    if (tag.includes(queryLower)) score += 20;
    if (queryLower.includes(tag)) score += 20;
  }
  return score;
}

function actionMatch(args) {
  const rawQuery = args.query;
  if (typeof rawQuery !== 'string' || !rawQuery.trim()) {
    throw new ParamError('参数非法：query 必须是非空字符串');
  }
  let limit = 3;
  if (args.limit !== undefined) {
    if (!Number.isInteger(args.limit) || args.limit < 1 || args.limit > 24) {
      throw new ParamError('参数非法：limit 必须是 1–24 的整数');
    }
    limit = args.limit;
  }
  const query = rawQuery.trim();
  const queryLower = query.toLowerCase();
  const scored = [];
  // match 只在 24 张表情上打分（limit 上限 24 与之一致）；动图按名称用 show 取。
  state.emotes.forEach((item, index) => {
    const score = scoreItem(item, queryLower);
    if (score > 0) scored.push({ item, score, index });
  });
  // 分数降序；同分保持 data/assets.json 里 emotes 的原始顺序。
  scored.sort((a, b) => b.score - a.score || a.index - b.index);
  const matches = scored.slice(0, limit).map(({ item, score }) => ({
    name: item.name,
    label: item.label,
    score,
    file: item.file,
  }));
  const out = { query, matches };
  if (matches.length === 0) {
    out.hint = HINT_NO_MATCH;
  }
  return out;
}

// ---------- 语境模型（语境 → 表情，零依赖 JS 推理） ----------
// 契约：train/model-format.md。权重来自 data/emote-model.json（训练产物），插件只读、不联网、不引第三方库。

function round6(value) {
  return Math.round(value * 1e6) / 1e6;
}

// 第 4 节步骤 1：Unicode NFKC → 全部转小写 → 删除所有空白。
// 空白集合与 Python 的 \s / str.isspace 对齐（含 U+001C–U+001F、U+0085；不含 BOM U+FEFF 与零宽空格 U+200B），保证 Python↔JS parity。
const WHITESPACE_RE = /[\t\n\v\f\r \u001c-\u001f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/g;

function normalizeContext(text) {
  return [...text.normalize('NFKC').toLowerCase().replace(WHITESPACE_RE, '')];
}

function badModel(why) {
  throw new Error('语境模型损坏：' + why);
}

function requireFiniteNumberArray(value, where) {
  if (!Array.isArray(value)) badModel(where + ' 必须是数组');
  for (let i = 0; i < value.length; i += 1) {
    if (typeof value[i] !== 'number' || !Number.isFinite(value[i])) {
      badModel(where + '[' + i + '] 不是有限数字');
    }
  }
  return value;
}

function compileModel(raw, sizeBytes) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) badModel('根节点必须是对象');
  if (raw.schema !== 1) badModel('schema 应为 1，实际 ' + JSON.stringify(raw.schema));
  if (raw.task !== 'context-emote-clf') badModel('task 应为 context-emote-clf');
  if (typeof raw.trained_at !== 'string' || !raw.trained_at.trim()) badModel('trained_at 缺失');
  if (typeof sizeBytes !== 'number' || sizeBytes <= 0) badModel('文件大小异常');
  if (sizeBytes > 600 * 1024) badModel('模型文件超过 600KB 预算（实际 ' + sizeBytes + ' 字节）');

  const metrics = raw.metrics && typeof raw.metrics === 'object' && !Array.isArray(raw.metrics) ? raw.metrics : null;
  if (!metrics) badModel('metrics 缺失');
  for (const key of ['val_acc1', 'holdout_acc1', 'none_recall']) {
    if (typeof metrics[key] !== 'number' || !Number.isFinite(metrics[key])) {
      badModel('metrics.' + key + ' 缺失或不是数字');
    }
  }

  const input = raw.input && typeof raw.input === 'object' && !Array.isArray(raw.input) ? raw.input : null;
  if (!input) badModel('input 缺失');
  if (input.normalize !== 'nfkc-lower-strip-all-whitespace') {
    badModel('input.normalize 不受支持：' + JSON.stringify(input.normalize));
  }
  if (input.max_chars !== 120) badModel('input.max_chars 应为 120');
  if (JSON.stringify(input.ngram_order) !== JSON.stringify([1, 2, 3])) badModel('input.ngram_order 应为 [1,2,3]');
  if (input.feature_value !== 'binary') badModel('input.feature_value 应为 binary');

  const vocab = input.vocab;
  if (!Array.isArray(vocab) || vocab.length === 0 || vocab.length > 1600) {
    badModel('input.vocab 应为 1–1200 项的数组');
  }
  const vocabIndex = new Map();
  for (let i = 0; i < vocab.length; i += 1) {
    const gram = vocab[i];
    if (typeof gram !== 'string' || gram.length === 0) badModel('input.vocab[' + i + '] 必须是非空字符串');
    if (!vocabIndex.has(gram)) vocabIndex.set(gram, []);
    vocabIndex.get(gram).push(i);
  }

  const classes = raw.classes;
  const emoteCount = state.emotes.length;
  if (!Array.isArray(classes) || classes.length !== emoteCount + 1) {
    badModel('classes 长度应为 ' + (emoteCount + 1) + '（24 表情 + none）');
  }
  for (let i = 0; i < emoteCount; i += 1) {
    if (classes[i] !== state.emotes[i].name) {
      badModel('classes 顺序与 data/assets.json 表情清单不一致（第 ' + i + ' 项：' + JSON.stringify(classes[i]) + '）');
    }
  }
  if (classes[emoteCount] !== 'none') badModel('classes 末位应为 none');

  const m = raw.model && typeof raw.model === 'object' && !Array.isArray(raw.model) ? raw.model : null;
  if (!m) badModel('model 缺失');
  if (m.type !== 'mlp-tanh-softmax') badModel('model.type 应为 mlp-tanh-softmax');
  if (m.hidden !== 32) badModel('model.hidden 应为 32（预算硬上限），实际 ' + JSON.stringify(m.hidden));
  const hidden = m.hidden;
  const classesCount = classes.length;

  const b1 = requireFiniteNumberArray(m.b1, 'model.b1');
  if (b1.length !== hidden) badModel('model.b1 长度应为 ' + hidden);
  const b2 = requireFiniteNumberArray(m.b2, 'model.b2');
  if (b2.length !== classesCount) badModel('model.b2 长度应为 ' + classesCount);

  const w1 = m.w1;
  if (!Array.isArray(w1) || w1.length !== vocab.length) badModel('model.w1 行数应为 vocab 长度 ' + vocab.length);
  const w1Flat = new Float64Array(vocab.length * hidden);
  for (let i = 0; i < w1.length; i += 1) {
    const row = requireFiniteNumberArray(w1[i], 'model.w1[' + i + ']');
    if (row.length !== hidden) badModel('model.w1[' + i + '] 长度应为 ' + hidden);
    for (let j = 0; j < hidden; j += 1) w1Flat[i * hidden + j] = row[j];
  }

  const w2 = m.w2;
  if (!Array.isArray(w2) || w2.length !== hidden) badModel('model.w2 行数应为 hidden ' + hidden);
  const w2Flat = new Float64Array(hidden * classesCount);
  for (let j = 0; j < w2.length; j += 1) {
    const row = requireFiniteNumberArray(w2[j], 'model.w2[' + j + ']');
    if (row.length !== classesCount) badModel('model.w2[' + j + '] 长度应为 ' + classesCount);
    for (let k = 0; k < classesCount; k += 1) w2Flat[j * classesCount + k] = row[k];
  }

  return {
    schema: raw.schema,
    task: raw.task,
    trained_at: raw.trained_at,
    classes,
    classesCount,
    vocabIndex,
    maxChars: 120,
    hidden,
    w1Flat,
    b1: Float64Array.from(b1),
    w2Flat,
    b2: Float64Array.from(b2),
    sizeBytes,
    metrics,
  };
}

// 懒加载：首次建议/list 取元信息时读盘解析，之后常驻缓存（只解析一次）。
function loadModel() {
  if (state.model) return state.model;
  let text;
  try {
    text = fs.readFileSync(MODEL_FILE, 'utf8');
  } catch (err) {
    throw new Error('语境模型不可用：无法读取 data/emote-model.json（' + err.message + '）');
  }
  let raw;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    throw new Error('语境模型不可用：data/emote-model.json 不是合法 JSON（' + err.message + '）');
  }
  let sizeBytes = Buffer.byteLength(text, 'utf8');
  try {
    sizeBytes = fs.statSync(MODEL_FILE).size;
  } catch {
    // 用文本字节数兜底
  }
  state.model = compileModel(raw, sizeBytes);
  return state.model;
}

// 第 4 节步骤 1–6：末尾 120 码点 → 1/2/3-gram → binary 特征 → tanh MLP → softmax。
function inferProbabilities(model, text) {
  const chars = normalizeContext(text).slice(-model.maxChars);
  const hit = new Set();
  for (let i = 0; i < chars.length; i += 1) {
    for (let n = 1; n <= 3 && i + n <= chars.length; n += 1) {
      const indexes = model.vocabIndex.get(chars.slice(i, i + n).join(''));
      if (indexes) {
        for (const index of indexes) hit.add(index);
      }
    }
  }
  const h = new Float64Array(model.hidden);
  for (let j = 0; j < model.hidden; j += 1) h[j] = model.b1[j];
  for (const index of hit) {
    const base = index * model.hidden;
    for (let j = 0; j < model.hidden; j += 1) h[j] += model.w1Flat[base + j];
  }
  for (let j = 0; j < model.hidden; j += 1) h[j] = Math.tanh(h[j]);
  const logits = new Float64Array(model.classesCount);
  for (let k = 0; k < model.classesCount; k += 1) logits[k] = model.b2[k];
  for (let j = 0; j < model.hidden; j += 1) {
    const hValue = h[j];
    if (hValue === 0) continue;
    const base = j * model.classesCount;
    for (let k = 0; k < model.classesCount; k += 1) logits[k] += hValue * model.w2Flat[base + k];
  }
  let max = -Infinity;
  for (let k = 0; k < model.classesCount; k += 1) {
    if (logits[k] > max) max = logits[k];
  }
  const probs = new Float64Array(model.classesCount);
  let sum = 0;
  for (let k = 0; k < model.classesCount; k += 1) {
    const value = Math.exp(logits[k] - max);
    probs[k] = value;
    sum += value;
  }
  for (let k = 0; k < model.classesCount; k += 1) probs[k] /= sum;
  let argmax = 0;
  for (let k = 1; k < model.classesCount; k += 1) {
    if (probs[k] > probs[argmax]) argmax = k;
  }
  return { probs: Array.from(probs), argmax };
}

function modelMetadata() {
  const model = loadModel();
  const metrics = {
    val_acc1: model.metrics.val_acc1,
    holdout_acc1: model.metrics.holdout_acc1,
    none_recall: model.metrics.none_recall,
  };
  // 模型卡扩展指标：训练产物提供时才暴露（保持向后兼容）。
  for (const key of ['holdout_acc5', 'coverage_70_top1_precision']) {
    if (typeof model.metrics[key] === 'number' && Number.isFinite(model.metrics[key])) {
      metrics[key] = model.metrics[key];
    }
  }
  return {
    schema: model.schema,
    trained_at: model.trained_at,
    classes: model.classesCount,
    metrics,
    size_bytes: model.sizeBytes,
  };
}

// suggest：语境 → 表情。limit 只影响返回条数，不影响决策（第 4 节步骤 7）。
function actionSuggest(args) {
  const rawContext = args.context;
  if (typeof rawContext !== 'string') {
    throw new ParamError('参数非法：context 必须是 1–2000 字符的字符串');
  }
  const contextChars = [...rawContext].length;
  if (contextChars < 1 || contextChars > 2000) {
    throw new ParamError('参数非法：context 长度必须是 1–2000 字符（实际 ' + contextChars + '）');
  }
  if (rawContext.trim() === '') {
    throw new ParamError('参数非法：context 不能只有空白字符');
  }
  let limit = 3;
  if (args.limit !== undefined) {
    if (!Number.isInteger(args.limit) || args.limit < 1 || args.limit > 5) {
      throw new ParamError('参数非法：limit 必须是 1–5 的整数');
    }
    limit = args.limit;
  }
  const model = loadModel(); // 缺失/损坏时抛 Error，由 handleCall 映射成 -32000
  const { probs, argmax } = inferProbabilities(model, rawContext);
  const noneIndex = model.classesCount - 1;
  const out = {
    decision: argmax === noneIndex ? 'none' : 'emote',
    none_score: round6(probs[noneIndex]),
  };
  if (argmax === noneIndex) {
    out.predictions = [];
    out.hint = HINT_NO_EMOTE;
    return out;
  }
  const ranked = [];
  for (let index = 0; index < noneIndex; index += 1) {
    ranked.push({ index, score: probs[index] });
  }
  ranked.sort((a, b) => b.score - a.score || a.index - b.index);
  out.predictions = ranked.slice(0, limit).map(({ index, score }) => ({
    name: model.classes[index],
    label: state.emotes[index].label,
    score: round6(score),
    file: state.emotes[index].file,
  }));
  return out;
}

const HANDLERS = {
  list: actionList,
  show: actionShow,
  match: actionMatch,
  suggest: actionSuggest,
};


// ---------- JSON-RPC ----------

function errorReply(id, code, message) {
  return { jsonrpc: '2.0', id: id === undefined ? null : id, error: { code, message } };
}

function handleCall(id, params) {
  const p = params && typeof params === 'object' && !Array.isArray(params) ? params : {};
  const action = p.action;
  if (typeof action !== 'string' || !action.trim()) {
    return errorReply(id, -32602, '参数非法：缺少 action');
  }
  const name = action.trim();
  if (!ACTIONS.includes(name)) {
    return errorReply(id, -32601, '未知动作: ' + name);
  }
  const args = p.arguments && typeof p.arguments === 'object' && !Array.isArray(p.arguments) ? p.arguments : {};
  let payload;
  try {
    ensureReady();
    payload = HANDLERS[name](args);
  } catch (err) {
    if (err instanceof ParamError) {
      return errorReply(id, -32602, err.message);
    }
    return errorReply(id, -32000, err && err.message ? err.message : '调用失败');
  }
  return { jsonrpc: '2.0', id, result: { content: JSON.stringify(payload) } };
}

function handleMessage(msg) {
  if (!msg || typeof msg !== 'object' || Array.isArray(msg)) {
    return { reply: errorReply(null, -32600, '非法请求：消息必须是 JSON 对象') };
  }
  const id = Object.prototype.hasOwnProperty.call(msg, 'id') ? msg.id : null;
  if (typeof msg.method !== 'string' || !msg.method.trim()) {
    return { reply: errorReply(id, -32600, '非法请求：method 缺失') };
  }
  switch (msg.method) {
    case 'initialize':
      return { reply: { jsonrpc: '2.0', id, result: { ok: true, name: PLUGIN_ID, version: VERSION } } };
    case 'call':
      return { reply: handleCall(id, msg.params) };
    case 'shutdown':
      return { reply: { jsonrpc: '2.0', id, result: { ok: true } }, shutdown: true };
    default:
      return { reply: errorReply(id, -32601, '未知方法: ' + msg.method) };
  }
}

function writeLine(obj, cb) {
  let text;
  try {
    text = JSON.stringify(obj) + '\n';
  } catch {
    text = '{"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"响应序列化失败"}}\n';
  }
  try {
    if (cb) {
      process.stdout.write(text, cb);
    } else {
      process.stdout.write(text);
    }
  } catch (err) {
    logStderr('stdout 写入失败：' + (err && err.message ? err.message : String(err)));
    process.exit(0);
  }
}

// ---------- 启动 ----------

try {
  buildState();
} catch (err) {
  state.error = err;
  logStderr('资产清单加载失败：' + (err && err.message ? err.message : String(err)));
}

function startServer() {
  let shuttingDown = false;

  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

  rl.on('line', (line) => {
    if (shuttingDown) return;
    const trimmed = line.trim();
    if (!trimmed) return;
    let msg;
    try {
      msg = JSON.parse(trimmed);
    } catch {
      writeLine(errorReply(null, -32700, '非法 JSON：无法解析该行'));
      return;
    }
    let out;
    try {
      out = handleMessage(msg);
    } catch (err) {
      out = { reply: errorReply(null, -32000, err && err.message ? err.message : '内部错误') };
    }
    if (!out || !out.reply) return;
    if (out.shutdown) {
      shuttingDown = true;
      writeLine(out.reply, () => process.exit(0));
      // 兜底：回包写不出去时也不无限挂住。
      const force = setTimeout(() => process.exit(0), 2000);
      force.unref();
      return;
    }
    writeLine(out.reply);
  });

  // stdin EOF = 父进程已关闭输入，自退（04 P8 最后一道防线）。
  rl.on('close', () => {
    if (shuttingDown) return;
    process.exitCode = 0;
  });

  process.stdin.on('error', (err) => {
    logStderr('stdin 错误：' + (err && err.message ? err.message : String(err)));
    process.exit(0);
  });

  process.stdout.on('error', (err) => {
    // 宿主已经断开管道，正常结束即可。
    if (err && err.code === 'EPIPE') process.exit(0);
    logStderr('stdout 错误：' + (err && err.message ? err.message : String(err)));
  });

  // 最后防线：任何意外都只记 stderr，不让常驻进程崩掉。
  process.on('uncaughtException', (err) => {
    logStderr('未捕获异常：' + (err && err.stack ? err.stack : String(err)));
  });

  process.on('unhandledRejection', (reason) => {
    logStderr('未处理的 Promise 拒绝：' + (reason && reason.stack ? reason.stack : String(reason)));
  });
}

if (require.main === module) {
  startServer();
}

// 仅供 test/model.mjs 做模型结构校验与 Python↔JS parity 对齐；生产入口仍是 `node index.js`。
module.exports = {
  loadModel,
  inferProbabilities,
  normalizeContext,
};
