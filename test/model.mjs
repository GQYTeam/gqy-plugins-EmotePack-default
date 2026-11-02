#!/usr/bin/env node
// GQY 表情包插件「语境模型」验收（零依赖）：
// A. 模型 JSON 结构：classes 25（顺序=表情清单+none）、vocab≤1600、w1/w2/b1/b2 形状、无 NaN、metrics 齐全、parity≥8、体积≤2MB；
// B. Python↔JS parity：用 index.js 的真实推理逐条跑 parity，argmax 必须一致、概率最大误差 ≤1e-3、softmax 和≈1；
// C. suggest 动作级：emote/none 决策、limit、字段、list 的 model 元信息、非法参数 -32602、延迟；
// D. 模型缺失/损坏：/tmp 副本里删掉/写坏 data/emote-model.json，suggest 回 -32000 且进程不崩。
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const MODEL_FILE = path.join(ROOT, 'data', 'emote-model.json');
const ASSETS_FILE = path.join(ROOT, 'data', 'assets.json');
const HINT_NO_EMOTE = '该语境不适合发表情';
const MAX_MODEL_BYTES = 600 * 1024;
const METRIC_KEYS = [
  'val_acc1',
  'val_acc3',
  'train_acc1',
  'none_precision',
  'none_recall',
  'holdout_acc1',
  'holdout_acc3',
  'holdout_none_f1',
  'baseline_acc1',
  'baseline_acc3',
  'holdout_acc5',
  'coverage_70_top1_precision',
];

const problems = [];
let checks = 0;
const clients = [];

function check(cond, why) {
  checks += 1;
  if (!cond) problems.push(why);
}

function fail(why) {
  console.error('FAIL: ' + why);
  for (const client of clients) {
    try {
      client.proc.kill('SIGKILL');
    } catch {
      // 已退出则忽略
    }
  }
  process.exit(1);
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

function allFinite(values) {
  if (!Array.isArray(values)) return false;
  for (const value of values) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return false;
  }
  return true;
}

// ---------- 轻客户端（spawn 插件，按 id 收包） ----------

class Client {
  constructor(label, cwd = ROOT) {
    this.label = label;
    this.cwd = cwd;
    this.buf = '';
    this.nextId = 1;
    this.waiters = new Map();
    this.received = [];
    this.exitInfo = null;
    this.exitWaiters = [];
    this.proc = spawn(process.execPath, ['index.js'], { cwd, stdio: ['pipe', 'pipe', 'pipe'] });
    clients.push(this);
    this.proc.on('error', (err) => fail(this.label + ' 进程启动失败：' + err.message));
    this.proc.stdout.on('data', (chunk) => this.onData(chunk));
    this.proc.stderr.on('data', (chunk) => process.stderr.write(chunk));
    this.proc.stdin.on('error', () => {});
    this.proc.on('close', (code, signal) => this.onClose(code, signal));
  }

  onData(chunk) {
    this.buf += chunk.toString('utf8');
    let index;
    while ((index = this.buf.indexOf('\n')) >= 0) {
      const line = this.buf.slice(0, index).trim();
      this.buf = this.buf.slice(index + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        fail(this.label + ' stdout 出现非 JSON 行：' + JSON.stringify(line));
      }
      if (!msg || typeof msg !== 'object' || Array.isArray(msg)) {
        fail(this.label + ' 回包不是 JSON 对象：' + line);
      }
      if (msg.jsonrpc !== '2.0') fail(this.label + ' 回包 jsonrpc 应为 2.0');
      this.received.push(msg);
      const waiter = this.waiters.get(msg.id);
      if (!waiter) fail(this.label + ' 收到未请求的回包 id=' + JSON.stringify(msg.id));
      this.waiters.delete(msg.id);
      clearTimeout(waiter.timer);
      waiter.resolve(msg);
    }
  }

  onClose(code, signal) {
    this.exitInfo = { code, signal };
    for (const waiter of this.waiters.values()) {
      clearTimeout(waiter.timer);
      waiter.reject(new Error(this.label + ' 进程提前退出（code=' + code + ', signal=' + signal + '）'));
    }
    this.waiters.clear();
    const exitWaiters = this.exitWaiters;
    this.exitWaiters = [];
    for (const waiter of exitWaiters) {
      clearTimeout(waiter.timer);
      waiter.resolve(this.exitInfo);
    }
  }

  request(method, params = {}) {
    const id = this.nextId++;
    const message = { jsonrpc: '2.0', id, method, params };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters.delete(id);
        reject(new Error(this.label + ' 回包超时 id=' + id));
      }, 15000);
      this.waiters.set(id, {
        timer,
        resolve: (reply) => {
          if (reply.id !== id) fail(this.label + ' 回包 id 不匹配：请求 ' + id + '，回包 ' + reply.id);
          resolve(reply);
        },
        reject,
      });
      try {
        this.proc.stdin.write(JSON.stringify(message) + '\n');
      } catch (err) {
        this.waiters.delete(id);
        clearTimeout(timer);
        reject(err);
      }
    });
  }

  waitExit(timeoutMs = 5000) {
    if (this.exitInfo) return Promise.resolve(this.exitInfo);
    return new Promise((resolve) => {
      const waiter = {
        resolve,
        timer: setTimeout(() => {
          this.exitWaiters = this.exitWaiters.filter((item) => item !== waiter);
          resolve(this.exitInfo || { code: null, signal: 'timeout' });
        }, timeoutMs),
      };
      this.exitWaiters.push(waiter);
    });
  }
}

async function callAction(client, action, args = {}) {
  const reply = await client.request('call', { action, arguments: args });
  if (reply.error) return { error: reply.error };
  if (!reply.result || typeof reply.result.content !== 'string') {
    problems.push(action + ' 应返回 result.content 字符串（实际 ' + JSON.stringify(reply.result) + '）');
    return { error: { code: null, message: 'no content' } };
  }
  try {
    return { data: JSON.parse(reply.result.content) };
  } catch (err) {
    problems.push(action + ' 的 content 不是合法 JSON：' + err.message);
    return { error: { code: null, message: 'bad json' } };
  }
}

async function expectErrorCode(client, action, args, code, why) {
  const reply = await client.request('call', { action, arguments: args });
  check(Boolean(reply.error), why + '：应返回 error');
  if (reply.error) check(reply.error.code === code, why + '：错误码应为 ' + code + '，实际 ' + reply.error.code);
}

async function shutdown(client) {
  const reply = await client.request('shutdown', {});
  check(Boolean(reply.result && reply.result.ok === true), client.label + ' shutdown 应返回 ok:true');
  const exitInfo = await client.waitExit(5000);
  check(exitInfo.code === 0, client.label + ' 进程应以 0 退出，实际 ' + JSON.stringify(exitInfo));
  check(client.buf.trim() === '', client.label + ' 退出时 stdout 不应残留半行');
}

// 排序工具：与 index.js 的 suggest 排序口径一致（分数降序、同分按下标）。
function rankedEmoteIndexes(probs, count) {
  const indexes = [];
  for (let i = 0; i < probs.length - 1; i += 1) indexes.push(i);
  indexes.sort((a, b) => probs[b] - probs[a] || a - b);
  return indexes.slice(0, count);
}

function copyPluginForTempTest(destDir) {
  fs.mkdirSync(destDir, { recursive: true });
  for (const file of ['index.js', 'manifest.json', 'package.json']) {
    fs.copyFileSync(path.join(ROOT, file), path.join(destDir, file));
  }
  for (const dir of ['data', 'emotes', 'motions']) {
    fs.cpSync(path.join(ROOT, dir), path.join(destDir, dir), { recursive: true });
  }
}

async function main() {
  // ---------- A. 模型结构 ----------

  let modelStat = null;
  try {
    modelStat = fs.statSync(MODEL_FILE);
  } catch {
    problems.push('data/emote-model.json 不存在（语境模型训练产物未就绪）');
  }
  const modelBytes = modelStat ? modelStat.size : 0;
  if (modelStat) {
    check(modelStat.isFile(), 'data/emote-model.json 应为普通文件');
    check(modelBytes > 0 && modelBytes <= MAX_MODEL_BYTES, '模型体积应在 (0, 600KB] 内（实际 ' + modelBytes + ' 字节）');
  }

  const raw = modelStat ? readJson(MODEL_FILE, 'data/emote-model.json') : null;
  const assets = readJson(ASSETS_FILE, 'data/assets.json');
  const emoteNames = assets && Array.isArray(assets.emotes) ? assets.emotes.map((item) => item && item.name) : [];
  const assetsEmotes = assets && Array.isArray(assets.emotes) ? assets.emotes : [];

  let parity = [];
  let classes = [];

  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    check(raw.schema === 1, '语境模型 schema 应为 1');
    check(raw.task === 'context-emote-clf', '语境模型 task 应为 context-emote-clf');
    check(
      typeof raw.trained_at === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(raw.trained_at),
      '语境模型 trained_at 应为 YYYY-MM-DD'
    );

    const dataset = raw.dataset && typeof raw.dataset === 'object' ? raw.dataset : {};
    check(
      [dataset.train, dataset.val, dataset.holdout].every((value) => Number.isInteger(value) && value > 0),
      '语境模型 dataset.train/val/holdout 应为正整数'
    );
    check(Number.isInteger(dataset.seed), '语境模型 dataset.seed 应为整数');
    check(typeof dataset.source === 'string' && dataset.source.trim().length > 0, '语境模型 dataset.source 应为非空字符串');

    classes = Array.isArray(raw.classes) ? raw.classes : [];
    check(classes.length === 25, '语境模型 classes 应恰好 25 项');
    if (classes.length === 25) {
      check(new Set(classes).size === 25, '语境模型 classes 不应重复');
      check(JSON.stringify(classes.slice(0, 24)) === JSON.stringify(emoteNames), '语境模型 classes 前 24 项顺序应等于 data/assets.json 的表情清单');
      check(classes[24] === 'none', '语境模型 classes 末位应为 none');
    }

    const input = raw.input && typeof raw.input === 'object' ? raw.input : {};
    check(input.normalize === 'nfkc-lower-strip-all-whitespace', '语境模型 input.normalize 应为 nfkc-lower-strip-all-whitespace');
    check(input.max_chars === 120, '语境模型 input.max_chars 应为 120');
    check(JSON.stringify(input.ngram_order) === JSON.stringify([1, 2, 3]), '语境模型 input.ngram_order 应为 [1,2,3]');
    check(input.feature_value === 'binary', '语境模型 input.feature_value 应为 binary');

    const vocab = Array.isArray(input.vocab) ? input.vocab : [];
    check(Array.isArray(input.vocab) && vocab.length > 0 && vocab.length <= 1200, '语境模型 vocab 应为 1–1200 项');
    let vocabProblem = '';
    const seenGram = new Set();
    for (let i = 0; i < vocab.length; i += 1) {
      const gram = vocab[i];
      if (typeof gram !== 'string' || gram.length === 0) {
        vocabProblem = 'vocab[' + i + '] 必须是非空字符串';
        break;
      }
      if (seenGram.has(gram)) {
        vocabProblem = 'vocab 含重复 gram：' + gram;
        break;
      }
      seenGram.add(gram);
      const normalized = [...gram.normalize('NFKC').toLowerCase().replace(/\s+/gu, '')].join('');
      if (normalized !== gram) {
        vocabProblem = 'vocab[' + i + '] 未按 normalize 规格归一：' + JSON.stringify(gram);
        break;
      }
    }
    check(vocabProblem === '', '语境模型 vocab 不合法：' + vocabProblem);

    const net = raw.model && typeof raw.model === 'object' ? raw.model : {};
    const hidden = net.hidden;
    check(net.type === 'mlp-tanh-softmax', '语境模型 model.type 应为 mlp-tanh-softmax');
    check(hidden === 32, '语境模型 model.hidden 应为 32（预算硬上限）');

    check(Array.isArray(net.b1) && net.b1.length === hidden && allFinite(net.b1), '语境模型 b1 长度应为 hidden 且全为有限数');
    check(Array.isArray(net.b2) && net.b2.length === 25 && allFinite(net.b2), '语境模型 b2 长度应为 25 且全为有限数');

    let w1Problem = '';
    if (!Array.isArray(net.w1) || net.w1.length !== vocab.length) {
      w1Problem = 'w1 行数应为 vocab 长度 ' + vocab.length;
    } else {
      for (let i = 0; i < net.w1.length; i += 1) {
        const row = net.w1[i];
        if (!Array.isArray(row) || row.length !== hidden) {
          w1Problem = 'w1[' + i + '] 长度应为 hidden ' + hidden;
          break;
        }
        if (!allFinite(row)) {
          w1Problem = 'w1[' + i + '] 含 NaN/Infinity 或非数字';
          break;
        }
      }
    }
    check(w1Problem === '', '语境模型 w1 不合法：' + w1Problem);

    let w2Problem = '';
    if (!Array.isArray(net.w2) || net.w2.length !== hidden) {
      w2Problem = 'w2 行数应为 hidden ' + hidden;
    } else {
      for (let j = 0; j < net.w2.length; j += 1) {
        const row = net.w2[j];
        if (!Array.isArray(row) || row.length !== 25) {
          w2Problem = 'w2[' + j + '] 长度应为 25';
          break;
        }
        if (!allFinite(row)) {
          w2Problem = 'w2[' + j + '] 含 NaN/Infinity 或非数字';
          break;
        }
      }
    }
    check(w2Problem === '', '语境模型 w2 不合法：' + w2Problem);

    const metrics = raw.metrics && typeof raw.metrics === 'object' ? raw.metrics : {};
    let metricsProblem = '';
    for (const key of METRIC_KEYS) {
      const value = metrics[key];
      if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) {
        metricsProblem = 'metrics.' + key + ' 应为 0–1 的有限数（实际 ' + JSON.stringify(value) + '）';
        break;
      }
    }
    check(metricsProblem === '', '语境模型 metrics 不合法：' + metricsProblem);

    parity = Array.isArray(raw.parity) ? raw.parity : [];
    check(Array.isArray(raw.parity) && parity.length >= 8, '语境模型 parity 应 ≥ 8 条');
    let parityProblem = '';
    for (let i = 0; i < parity.length; i += 1) {
      const item = parity[i];
      if (!item || typeof item !== 'object' || typeof item.text !== 'string' || item.text.trim().length === 0) {
        parityProblem = 'parity[' + i + '].text 应为非空字符串';
        break;
      }
      if (!Array.isArray(item.probs) || item.probs.length !== 25 || !allFinite(item.probs)) {
        parityProblem = 'parity[' + i + '].probs 应为 25 个有限数';
        break;
      }
      if (item.probs.some((value) => value < 0 || value > 1)) {
        parityProblem = 'parity[' + i + '].probs 应在 [0,1] 内';
        break;
      }
      const sum = item.probs.reduce((acc, value) => acc + value, 0);
      if (Math.abs(sum - 1) > 1e-3) {
        parityProblem = 'parity[' + i + '].probs 之和应≈1（实际 ' + sum + '）';
        break;
      }
    }
    check(parityProblem === '', '语境模型 parity 不合法：' + parityProblem);
  }

  // ---------- B. Python ↔ JS parity ----------

  let plugin = null;
  try {
    plugin = createRequire(import.meta.url)('../index.js');
  } catch (err) {
    problems.push('无法加载 index.js 导出的推理函数（parity 无法执行）：' + err.message);
  }

  let compiled = null;
  if (plugin) {
    try {
      compiled = plugin.loadModel();
    } catch (err) {
      problems.push('index.js loadModel() 失败：' + err.message);
    }
  }

  let parityMaxDiff = 0;
  let parityChecked = 0;
  if (plugin && compiled && parity.length >= 1) {
    for (let i = 0; i < parity.length; i += 1) {
      const item = parity[i];
      if (!item || typeof item.text !== 'string' || !Array.isArray(item.probs) || item.probs.length !== 25) continue;
      parityChecked += 1;
      let result;
      try {
        result = plugin.inferProbabilities(compiled, item.text);
      } catch (err) {
        problems.push('parity[' + i + '] JS 推理抛错：' + err.message);
        continue;
      }
      let expectedArgmax = 0;
      for (let k = 1; k < item.probs.length; k += 1) {
        if (item.probs[k] > item.probs[expectedArgmax]) expectedArgmax = k;
      }
      check(
        result.argmax === expectedArgmax,
        'parity[' + i + '] argmax 不一致：JS=' + result.argmax + '，Python=' + expectedArgmax + '（text=' + JSON.stringify(item.text.slice(0, 40)) + '）'
      );
      let maxDiff = 0;
      for (let k = 0; k < item.probs.length; k += 1) {
        maxDiff = Math.max(maxDiff, Math.abs(result.probs[k] - item.probs[k]));
      }
      parityMaxDiff = Math.max(parityMaxDiff, maxDiff);
      check(maxDiff <= 1e-3, 'parity[' + i + '] 概率最大误差 ' + maxDiff.toExponential(3) + ' > 1e-3');
      const jsSum = result.probs.reduce((acc, value) => acc + value, 0);
      check(Math.abs(jsSum - 1) <= 1e-6, 'parity[' + i + '] softmax 和应≈1（实际 ' + jsSum + '）');
    }
  }

  // 归一化/截断语义（model-format §4 步骤 1–2；不依赖具体模型权重）。
  if (plugin && typeof plugin.normalizeContext === 'function') {
    const normalize = plugin.normalizeContext;
    check(
      JSON.stringify(normalize('Ａ　Ｂ\tＣ')) === JSON.stringify(['a', 'b', 'c']),
      'normalizeContext 应做 NFKC + 小写 + 删空白（含全角空格/制表）'
    );
    check(
      JSON.stringify(normalize('a\u001c\u001f\u0085\u00a0b')) === JSON.stringify(['a', 'b']),
      'normalizeContext 的空白集合应与 Python \\s 对齐（U+001C–U+001F / U+0085 / NBSP）'
    );
    check(
      JSON.stringify(normalize('a\ufeffb')) === JSON.stringify(['a', '\ufeff', 'b']),
      'normalizeContext 不应删除 BOM（Python 口径）'
    );
    check(
      JSON.stringify(normalize('a\u200bb')) === JSON.stringify(['a', '\u200b', 'b']),
      'normalizeContext 不应删除零宽空格（Python 口径）'
    );
    if (compiled) {
      const longText = '乙'.repeat(180) + '甲'.repeat(120);
      const tailText = '甲'.repeat(300);
      const a = plugin.inferProbabilities(compiled, longText);
      const b = plugin.inferProbabilities(compiled, tailText);
      let truncationDiff = 0;
      for (let k = 0; k < a.probs.length; k += 1) {
        truncationDiff = Math.max(truncationDiff, Math.abs(a.probs[k] - b.probs[k]));
      }
      check(truncationDiff < 1e-12, '超长语境应只保留末尾 120 码点（截断前后概率应一致）');
    }
  }

  // 推理延迟（预算：<10ms/次，M2）。用最长的 parity 文本跑 50 次取平均。
  let inferenceMs = null;
  if (plugin && compiled && parity.length >= 1) {
    const texts = parity.filter((item) => item && typeof item.text === 'string').map((item) => item.text);
    const sampleText = texts.sort((a, b) => b.length - a.length)[0] || '今天加班到很晚';
    const iterations = 50;
    const startedAt = process.hrtime.bigint();
    for (let i = 0; i < iterations; i += 1) plugin.inferProbabilities(compiled, sampleText);
    inferenceMs = Number(process.hrtime.bigint() - startedAt) / 1e6 / iterations;
    check(inferenceMs < 10, '单次推理延迟应 <10ms（实际 ' + inferenceMs.toFixed(2) + 'ms）');
  }

  // ---------- C. suggest 动作级 ----------

  let latencyAvg = null;
  if (plugin && compiled && classes.length === 25 && parity.length >= 1) {
    // 用 parity 里选一条 emote 语境与一条 none 语境（契约要求 parity 覆盖 none）。
    let emoteCase = null;
    let noneCase = null;
    for (const item of parity) {
      if (!item || typeof item.text !== 'string') continue;
      const result = plugin.inferProbabilities(compiled, item.text);
      if (result.argmax === 24) {
        if (!noneCase) noneCase = { text: item.text, probs: result.probs };
      } else if (!emoteCase) {
        emoteCase = { text: item.text, probs: result.probs };
      }
      if (emoteCase && noneCase) break;
    }
    check(Boolean(emoteCase), 'parity 未覆盖 emote 语境（无法做动作级 emote 用例）');
    check(Boolean(noneCase), 'parity 未覆盖 none 语境（model-format 第 3 节要求覆盖 none）');

    const client = new Client('模型用例');
    const init = await client.request('initialize', {});
    check(Boolean(init.result && init.result.ok === true), 'initialize 应返回 ok:true');

    if (emoteCase) {
      const expectedTop = rankedEmoteIndexes(emoteCase.probs, 3);
      const got = await callAction(client, 'suggest', { context: emoteCase.text });
      const data = got.data || {};
      check(data.decision === 'emote', 'suggest 应判定为 emote（实际 ' + data.decision + '）');
      check(
        typeof data.none_score === 'number' && Math.abs(data.none_score - emoteCase.probs[24]) <= 1e-6,
        'suggest.none_score 应等于模型 none 概率'
      );
      check(Array.isArray(data.predictions) && data.predictions.length === 3, 'suggest 默认 limit 应返回 3 条');
      check(data.hint === undefined, 'decision=emote 时不应返回 hint');
      if (Array.isArray(data.predictions)) {
        for (let i = 0; i < data.predictions.length; i += 1) {
          const prediction = data.predictions[i];
          const index = expectedTop[i];
          check(prediction && prediction.name === classes[index], 'suggest 第 ' + (i + 1) + ' 条 name 不符：' + JSON.stringify(prediction && prediction.name));
          check(Boolean(prediction && prediction.label === assetsEmotes[index].label), 'suggest 第 ' + (i + 1) + ' 条 label 不符');
          check(Boolean(prediction && prediction.file === assetsEmotes[index].file), 'suggest 第 ' + (i + 1) + ' 条 file 不符');
          check(
            Boolean(prediction && typeof prediction.score === 'number' && Math.abs(prediction.score - emoteCase.probs[index]) <= 1e-6),
            'suggest 第 ' + (i + 1) + ' 条 score 应等于模型概率'
          );
        }
        for (let i = 1; i < data.predictions.length; i += 1) {
          check(data.predictions[i - 1].score >= data.predictions[i].score, 'suggest predictions 应按分数降序');
        }
      }

      // limit 只影响返回条数，不影响决策。
      const one = await callAction(client, 'suggest', { context: emoteCase.text, limit: 1 });
      check(Boolean(one.data && one.data.matches === undefined), 'suggest 返回不应含 matches 字段');
      check(Boolean(one.data && one.data.decision === 'emote'), 'limit=1 时决策仍应为 emote');
      check(Boolean(one.data && Array.isArray(one.data.predictions) && one.data.predictions.length === 1), 'limit=1 应只返回 1 条');
      if (one.data && Array.isArray(one.data.predictions) && one.data.predictions.length === 1 && data.predictions && data.predictions.length === 3) {
        check(one.data.predictions[0].name === data.predictions[0].name, 'limit=1 的首条应与默认首条一致');
      }
      const five = await callAction(client, 'suggest', { context: emoteCase.text, limit: 5 });
      check(Boolean(five.data && Array.isArray(five.data.predictions) && five.data.predictions.length === 5), 'limit=5 应返回 5 条');

      // 延迟：M2 目标 < 50ms/次（含进程内校验与推理，不含进程启动）。
      const samples = [];
      for (let i = 0; i < 10; i += 1) {
        const startedAt = Date.now();
        const reply = await client.request('call', { action: 'suggest', arguments: { context: emoteCase.text } });
        samples.push(Date.now() - startedAt);
        if (!reply.result) fail('延迟采样时 suggest 返回错误：' + JSON.stringify(reply.error));
      }
      latencyAvg = samples.reduce((acc, value) => acc + value, 0) / samples.length;
      check(latencyAvg < 50, 'suggest 平均延迟应 <50ms（实际 ' + latencyAvg.toFixed(1) + 'ms）');
    }

    if (noneCase) {
      const expected = plugin.inferProbabilities(compiled, noneCase.text);
      const got = await callAction(client, 'suggest', { context: noneCase.text });
      const data = got.data || {};
      check(data.decision === 'none', 'suggest 应判定为 none（实际 ' + data.decision + '）');
      check(Array.isArray(data.predictions) && data.predictions.length === 0, 'decision=none 时 predictions 应为空数组');
      check(data.hint === HINT_NO_EMOTE, 'decision=none 时 hint 不符：' + data.hint);
      check(
        typeof data.none_score === 'number' && Math.abs(data.none_score - expected.probs[24]) <= 1e-6,
        'decision=none 时 none_score 应等于模型 none 概率'
      );
    }

    // list 的只读 model 元字段。
    const listed = await callAction(client, 'list', {});
    const listedModel = listed.data ? listed.data.model : null;
    check(Boolean(listedModel && typeof listedModel === 'object'), 'list 应返回 model 元字段');
    if (listedModel && raw) {
      check(listedModel.schema === raw.schema, 'list.model.schema 不符');
      check(listedModel.trained_at === raw.trained_at, 'list.model.trained_at 不符');
      check(listedModel.classes === 25, 'list.model.classes 应为 25');
      let listedMetricsOk = Boolean(listedModel.metrics && typeof listedModel.metrics === 'object');
      for (const key of ['val_acc1', 'holdout_acc1', 'none_recall', 'holdout_acc5', 'coverage_70_top1_precision']) {
        if (!listedModel.metrics || listedModel.metrics[key] !== raw.metrics[key]) listedMetricsOk = false;
      }
      check(listedMetricsOk, 'list.model.metrics 应与模型 metrics 一致（含 holdout_acc5 / coverage_70_top1_precision）');
      check(listedModel.size_bytes === modelBytes, 'list.model.size_bytes 应等于模型文件字节数');
    }

    // 非法参数 → -32602，且进程继续服务。
    const invalidCases = [
      [{}, '缺 context'],
      [{ context: '' }, '空 context'],
      [{ context: '   ' }, '纯空白 context'],
      [{ context: 'a'.repeat(2001) }, '超长 context（2001 字符）'],
      [{ context: 123 }, '非字符串 context'],
      [{ context: '你好', limit: 0 }, 'limit=0'],
      [{ context: '你好', limit: 6 }, 'limit=6'],
      [{ context: '你好', limit: 1.5 }, 'limit=1.5'],
    ];
    for (const [args, why] of invalidCases) {
      await expectErrorCode(client, 'suggest', args, -32602, why);
    }
    const afterErrors = await callAction(client, 'list', {});
    check(Boolean(afterErrors.data && afterErrors.data.count === 28), '参数错误后进程应继续服务');

    await shutdown(client);
  }

  // ---------- D. 模型缺失/损坏 → -32000 且不崩（/tmp 副本） ----------

  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'gqy-emote-model-'));
  try {
    const pluginDir = path.join(tempRoot, 'emote-pack-default');
    copyPluginForTempTest(pluginDir);
    const tempModel = path.join(pluginDir, 'data', 'emote-model.json');
    fs.rmSync(tempModel, { force: true });

    const missingClient = new Client('模型缺失用例', pluginDir);
    const init = await missingClient.request('initialize', {});
    check(Boolean(init.result && init.result.ok === true), '模型缺失时 initialize 仍应成功');
    const missingReply = await missingClient.request('call', {
      action: 'suggest',
      arguments: { context: '今天加班到很晚，好累啊' },
    });
    check(Boolean(missingReply.error), '模型缺失时 suggest 应返回 error');
    if (missingReply.error) {
      check(missingReply.error.code === -32000, '模型缺失时 suggest 错误码应为 -32000（实际 ' + missingReply.error.code + '）');
      check(/语境模型/.test(missingReply.error.message), '模型缺失时的错误消息应说明语境模型不可用：' + missingReply.error.message);
    }
    const listWithoutModel = await callAction(missingClient, 'list', {});
    check(Boolean(listWithoutModel.data && listWithoutModel.data.count === 28), '模型缺失时 list 仍应成功');
    check(Boolean(listWithoutModel.data && listWithoutModel.data.model === null), '模型缺失时 list.model 应为 null');
    const secondMissing = await missingClient.request('call', { action: 'suggest', arguments: { context: '今天天气不错' } });
    check(Boolean(secondMissing.error && secondMissing.error.code === -32000), '模型缺失时再次 suggest 仍应 -32000（进程未崩）');
    await shutdown(missingClient);

    // 损坏模型：写坏 JSON。
    fs.writeFileSync(tempModel, '{broken');
    const brokenClient = new Client('模型损坏用例', pluginDir);
    const brokenReply = await brokenClient.request('call', { action: 'suggest', arguments: { context: '今天天气不错' } });
    check(Boolean(brokenReply.error), '模型损坏时 suggest 应返回 error');
    if (brokenReply.error) {
      check(brokenReply.error.code === -32000, '模型损坏时 suggest 错误码应为 -32000（实际 ' + brokenReply.error.code + '）');
      check(/语境模型|JSON/.test(brokenReply.error.message), '模型损坏时的错误消息应说明原因：' + brokenReply.error.message);
    }
    await shutdown(brokenClient);

    // 预算硬上限（model-format §0/§3）：hidden≠32、文件 >600KB 时运行时必须拒绝（-32000）。
    const baseModel = readJson(MODEL_FILE, 'data/emote-model.json');
    if (baseModel) {
      const hiddenBroken = JSON.parse(JSON.stringify(baseModel));
      hiddenBroken.model.hidden = 64;
      fs.writeFileSync(tempModel, JSON.stringify(hiddenBroken));
      const hiddenClient = new Client('hidden 预算用例', pluginDir);
      const hiddenReply = await hiddenClient.request('call', { action: 'suggest', arguments: { context: '今天加班好累' } });
      check(Boolean(hiddenReply.error && hiddenReply.error.code === -32000), 'hidden≠32 时 suggest 应 -32000');
      check(Boolean(hiddenReply.error && /hidden/.test(hiddenReply.error.message)), 'hidden≠32 的错误消息应含 hidden');
      await shutdown(hiddenClient);

      const oversized = JSON.parse(JSON.stringify(baseModel));
      oversized.padding = 'x'.repeat(700 * 1024);
      fs.writeFileSync(tempModel, JSON.stringify(oversized));
      const sizeClient = new Client('体积预算用例', pluginDir);
      const sizeReply = await sizeClient.request('call', { action: 'suggest', arguments: { context: '今天加班好累' } });
      check(Boolean(sizeReply.error && sizeReply.error.code === -32000), '模型超过 600KB 时 suggest 应 -32000');
      check(Boolean(sizeReply.error && /600KB/.test(sizeReply.error.message)), '超体积的错误消息应含 600KB');
      await shutdown(sizeClient);
    }
  } catch (err) {
    problems.push('/tmp 模型缺失用例执行失败：' + (err && err.message ? err.message : String(err)));
  } finally {
    try {
      fs.rmSync(tempRoot, { recursive: true, force: true });
    } catch {
      // 清理失败不影响结论
    }
  }

  // ---------- 结果 ----------

  if (problems.length > 0) {
    console.error('FAIL: 语境模型校验未通过（' + problems.length + ' 项）：');
    for (const problem of problems) console.error('  - ' + problem);
    process.exit(1);
  }
  const latencyText = latencyAvg === null ? '未测量' : latencyAvg.toFixed(1) + 'ms';
  const inferenceText = inferenceMs === null ? '未测量' : inferenceMs.toFixed(2) + 'ms/次';
  const metricText =
    raw && raw.metrics
      ? 'holdout_acc1=' + raw.metrics.holdout_acc1 + '，holdout_acc3=' + raw.metrics.holdout_acc3 + '，holdout_none_f1=' + raw.metrics.holdout_none_f1
      : '未读取';
  console.log(
    'OK: 语境模型校验通过（' + checks + ' 项检查；parity ' + parityChecked + ' 条 argmax 全一致，最大概率误差 ' +
      parityMaxDiff.toExponential(2) + '；推理 ' + inferenceText + '，suggest 端到端 ' + latencyText + '；' + metricText + '）'
  );
}

main().catch((err) => fail(err && err.stack ? err.stack : String(err)));
