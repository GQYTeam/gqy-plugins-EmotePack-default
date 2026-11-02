#!/usr/bin/env node
// GQY 表情包插件的本地握手测试（零依赖）：
// spawn node index.js，走 initialize → list → show（普通/文件路径/内联 PNG/动图/内联 GIF）
// → match（命中/limit/无匹配）→ 各类错误 → shutdown，断言回包 id 一一对应、stdout 全是合法 JSON、
// 错误不崩进程；再单独 spawn 一次验证关闭 stdin 后自退（04 P8）。
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const MANIFEST = JSON.parse(fs.readFileSync(new URL('../manifest.json', import.meta.url), 'utf8'));

const ROOT = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const HINT_NO_MATCH = '没有匹配的表情，可用 list 查看全部';
const clients = [];

function fail(why) {
  console.error('FAIL: ' + why);
  for (const client of clients) {
    try {
      client.proc.kill('SIGKILL');
    } catch {
      // 已经退出则忽略
    }
  }
  process.exit(1);
}

function assert(cond, why) {
  if (!cond) fail(why);
}

function expectedBytes(file) {
  try {
    return fs.statSync(path.join(ROOT, file)).size;
  } catch (err) {
    fail('测试读取文件失败：' + file + '（' + err.message + '）');
  }
  return -1;
}

// 一个长驻插件进程的轻客户端：按 id 分发回包，同时收集 id=null 的杂项回包。
class Client {
  constructor(label) {
    this.label = label;
    this.buf = '';
    this.nextId = 1;
    this.sent = [];
    this.received = [];
    this.waiters = new Map();
    this.strays = [];
    this.strayWaiters = [];
    this.exitInfo = null;
    this.exitWaiters = [];
    this.proc = spawn(process.execPath, ['index.js'], { cwd: ROOT, stdio: ['pipe', 'pipe', 'pipe'] });
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
      assert(msg.jsonrpc === '2.0', this.label + ' 回包 jsonrpc 应为 2.0：' + line);
      this.received.push(msg);
      if (msg.id === null || msg.id === undefined) {
        const waiter = this.strayWaiters.shift();
        if (waiter) {
          clearTimeout(waiter.timer);
          waiter.resolve(msg);
        } else {
          this.strays.push(msg);
        }
        continue;
      }
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
    this.sent.push(id);
    const message = { jsonrpc: '2.0', id, method, params };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters.delete(id);
        reject(new Error(this.label + ' 回包超时 id=' + id));
      }, 15000);
      this.waiters.set(id, {
        timer,
        resolve: (reply) => {
          assert(reply.id === id, this.label + ' 回包 id 不匹配：请求 ' + id + '，回包 ' + reply.id);
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

  writeRaw(text) {
    try {
      this.proc.stdin.write(text);
    } catch (err) {
      fail(this.label + ' 写 stdin 失败：' + err.message);
    }
  }

  nextStray() {
    if (this.strays.length > 0) return Promise.resolve(this.strays.shift());
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(this.label + ' 等待 id=null 回包超时')), 15000);
      this.strayWaiters.push({
        timer,
        resolve: (msg) => {
          clearTimeout(timer);
          resolve(msg);
        },
      });
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

  endStdin() {
    try {
      this.proc.stdin.end();
    } catch {
      // 已关闭则忽略
    }
  }
}

async function callAction(client, action, args = {}) {
  const reply = await client.request('call', { action, arguments: args });
  assert(
    reply.result && typeof reply.result.content === 'string',
    action + ' 应返回 result.content 字符串（实际：' + JSON.stringify(reply.error || reply.result) + '）'
  );
  try {
    return JSON.parse(reply.result.content);
  } catch (err) {
    fail(action + ' 的 content 不是合法 JSON：' + err.message);
  }
  return null;
}

async function expectError(client, action, args, expectedCode, why, messagePattern) {
  const reply = await client.request('call', { action, arguments: args });
  assert(Boolean(reply.error), why + '：应返回 error，实际 ' + JSON.stringify(reply.result));
  assert(reply.error.code === expectedCode, why + '：错误码应为 ' + expectedCode + '，实际 ' + reply.error.code);
  if (messagePattern) {
    assert(messagePattern.test(reply.error.message), why + '：错误消息不符：' + reply.error.message);
  }
  return reply.error;
}

async function runScenario() {
  const client = new Client('main');

  // 1. initialize
  const init = await client.request('initialize', {});
  assert(init.result && init.result.ok === true, 'initialize 应返回 ok:true');
  assert(init.result.name === 'emote-pack-default', 'initialize 返回的 name 不符：' + init.result.name);
  assert(init.result.version === MANIFEST.version, 'initialize 返回的 version 不符：' + init.result.version);

  // 2. list：24 张表情 + 4 个动图，字段齐全，bytes 与磁盘一致（现读不缓存）
  const listed = await callAction(client, 'list', {});
  assert(listed.count === 28, 'list.count 应为 28，实际 ' + listed.count);
  assert(Array.isArray(listed.emotes) && listed.emotes.length === 24, 'list.emotes 应 24 条');
  assert(Array.isArray(listed.motions) && listed.motions.length === 4, 'list.motions 应 4 条');
  for (const item of listed.emotes) {
    assert(typeof item.name === 'string' && item.name, '表情缺少 name');
    assert(typeof item.label === 'string' && item.label, '表情缺少 label：' + item.name);
    assert(Array.isArray(item.tags) && item.tags.length > 0, '表情缺少 tags：' + item.name);
    assert(typeof item.file === 'string' && item.file.startsWith('emotes/'), '表情 file 应在 emotes/ 下：' + item.name);
    assert(Number.isInteger(item.bytes) && item.bytes === expectedBytes(item.file), '表情 bytes 与磁盘不一致：' + item.name);
    assert(item.width === 512 && item.height === 512, '表情尺寸应为 512×512：' + item.name);
    assert(item.data_uri === undefined, 'list 不应返回 data_uri');
  }
  for (const item of listed.motions) {
    assert(typeof item.name === 'string' && item.name, '动图缺少 name');
    assert(typeof item.label === 'string' && item.label, '动图缺少 label：' + item.name);
    assert(Array.isArray(item.tags) && item.tags.length > 0, '动图缺少 tags：' + item.name);
    assert(typeof item.file === 'string' && item.file.startsWith('motions/'), '动图 file 应在 motions/ 下：' + item.name);
    assert(Number.isInteger(item.bytes) && item.bytes === expectedBytes(item.file), '动图 bytes 与磁盘不一致：' + item.name);
    assert(Number.isInteger(item.width) && item.width > 0 && Number.isInteger(item.height) && item.height > 0, '动图尺寸字段缺失：' + item.name);
    assert(Number.isInteger(item.frames) && item.frames > 0, '动图缺少 frames：' + item.name);
    assert(Number.isInteger(item.frame_ms) && item.frame_ms > 0, '动图缺少 frame_ms：' + item.name);
    assert(Number.isInteger(item.duration_ms) && item.duration_ms > 0, '动图缺少 duration_ms：' + item.name);
    assert(typeof item.note === 'string' && item.note.length > 0, '动图缺少 note：' + item.name);
    assert(item.data_uri === undefined, 'list 不应返回 data_uri');
  }

  // 3. show：普通表情（不内联）
  const shown = await callAction(client, 'show', { name: '05-困倦' });
  assert(shown.name === '05-困倦', 'show 返回的 name 不符：' + shown.name);
  assert(shown.label === '困倦', 'show 返回的 label 不符：' + shown.label);
  assert(shown.media === 'image/png', 'show 表情的 media 应为 image/png，实际 ' + shown.media);
  assert(shown.file === 'emotes/05-困倦.png', 'show 返回的 file 不符：' + shown.file);
  assert(shown.bytes === expectedBytes(shown.file), 'show 返回的 bytes 与磁盘不一致');
  assert(shown.data_uri === undefined, '未要求 inline 时不应返回 data_uri');

  // 4. show：接受清单内的 file 路径精确匹配
  const shownByFile = await callAction(client, 'show', { name: 'emotes/05-困倦.png' });
  assert(shownByFile.name === '05-困倦', 'show 用 file 路径匹配时应返回规范 name，实际 ' + shownByFile.name);
  assert(shownByFile.file === 'emotes/05-困倦.png', 'show 用 file 路径匹配时 file 不符');

  // 5. show：inline PNG
  const inlinePng = await callAction(client, 'show', { name: '01-平静', inline: true });
  assert(typeof inlinePng.data_uri === 'string', 'inline:true 应返回 data_uri');
  assert(inlinePng.data_uri.startsWith('data:image/png;base64,'), 'PNG data_uri 前缀不符：' + inlinePng.data_uri.slice(0, 32));
  const pngBuf = Buffer.from(inlinePng.data_uri.slice('data:image/png;base64,'.length), 'base64');
  assert(pngBuf.length === inlinePng.bytes, 'PNG 内联数据长度与 bytes 不一致');
  assert(pngBuf.subarray(0, 8).equals(PNG_MAGIC), 'PNG 内联数据魔术不符');

  // 6. show：动图（GIF，不内联）
  const gif = await callAction(client, 'show', { name: '动作六态' });
  assert(gif.media === 'image/gif', 'show 动图的 media 应为 image/gif，实际 ' + gif.media);
  assert(gif.file === 'motions/表情包-动作六态.gif', 'show 动图返回的 file 不符：' + gif.file);
  assert(gif.bytes === expectedBytes(gif.file), 'show 动图返回的 bytes 与磁盘不一致');
  assert(gif.data_uri === undefined, '未要求 inline 时不应返回 data_uri');

  // 7. show：inline GIF（用最小的微信 240 版，避免测试塞大包）
  const inlineGif = await callAction(client, 'show', { name: '微信240-24态', inline: true });
  assert(inlineGif.media === 'image/gif', 'inline GIF 的 media 应为 image/gif');
  assert(inlineGif.data_uri.startsWith('data:image/gif;base64,'), 'GIF data_uri 前缀不符：' + inlineGif.data_uri.slice(0, 32));
  const gifBuf = Buffer.from(inlineGif.data_uri.slice('data:image/gif;base64,'.length), 'base64');
  assert(gifBuf.length === inlineGif.bytes, 'GIF 内联数据长度与 bytes 不一致');
  assert(['GIF87a', 'GIF89a'].includes(gifBuf.subarray(0, 6).toString('latin1')), 'GIF 内联数据魔术不符');

  // 8. match：「累」应命中 05-困倦（或 09-打哈欠）且分数最高
  const matched = await callAction(client, 'match', { query: '累' });
  assert(matched.query === '累', 'match 返回的 query 不符：' + matched.query);
  assert(Array.isArray(matched.matches), 'match 应返回 matches 数组');
  assert(matched.matches.length >= 1 && matched.matches.length <= 3, '默认 limit 应为 3，实际 ' + matched.matches.length);
  const topName = matched.matches[0].name;
  assert(topName === '05-困倦' || topName === '09-打哈欠', '「累」的最高分应为 05-困倦 或 09-打哈欠，实际 ' + topName);
  assert(matched.matches.some((item) => item.name === '05-困倦'), '「累」结果应包含 05-困倦（标签「累」完全相等）');
  for (const item of matched.matches) {
    assert(typeof item.name === 'string' && item.name, 'match 项缺少 name');
    assert(typeof item.label === 'string' && item.label, 'match 项缺少 label');
    assert(typeof item.file === 'string' && item.file, 'match 项缺少 file');
    assert(Number.isFinite(item.score) && item.score > 0, 'match 项 score 应为正数：' + JSON.stringify(item));
    assert(fs.existsSync(path.join(ROOT, item.file)), 'match 返回的 file 应存在：' + item.file);
  }
  for (let index = 1; index < matched.matches.length; index += 1) {
    assert(matched.matches[index - 1].score >= matched.matches[index].score, 'match 结果应按分数降序');
  }
  assert(matched.hint === undefined, '有匹配时不应返回 hint');

  // 9. match：limit 生效
  const limited = await callAction(client, 'match', { query: '累', limit: 1 });
  assert(limited.matches.length === 1, 'limit:1 应只返回 1 条，实际 ' + limited.matches.length);
  assert(limited.matches[0].name === topName, 'limit:1 的首条应与默认首条一致');

  // 10. match：无匹配 → 空数组 + hint
  const noMatch = await callAction(client, 'match', { query: '不存在词xyz' });
  assert(Array.isArray(noMatch.matches) && noMatch.matches.length === 0, '无匹配时 matches 应为空数组');
  assert(noMatch.hint === HINT_NO_MATCH, '无匹配 hint 不符：' + noMatch.hint);

  // 11. show 不存在 / 路径穿越：error.message 含「不存在」，进程继续服务
  const missing = await expectError(client, 'show', { name: '不存在' }, -32000, '不存在的表情应报错', /不存在/);
  assert(typeof missing.message === 'string', '不存在的表情应返回 error.message');
  await expectError(client, 'show', { name: '../../etc/passwd' }, -32000, '路径穿越必须被拒绝', /不存在/);
  await expectError(client, 'show', { name: '/etc/passwd' }, -32000, '绝对路径必须被拒绝', /不存在/);

  // 12. 未知动作 / 未知方法 → -32601
  await expectError(client, 'nope', {}, -32601, '未知动作应返回 -32601', /未知动作/);
  const unknownMethod = await client.request('no_such_method', {});
  assert(unknownMethod.error && unknownMethod.error.code === -32601, '未知方法应返回 -32601');
  assert(/未知方法/.test(unknownMethod.error.message), '未知方法的 error.message 应说明「未知方法」');

  // 13. 参数非法 → -32602（不崩进程）
  await expectError(client, 'show', {}, -32602, 'show 缺 name 应返回 -32602', /name/);
  await expectError(client, 'match', { query: '累', limit: 0 }, -32602, 'limit=0 应返回 -32602', /limit/);
  await expectError(client, 'match', { query: '累', limit: 25 }, -32602, 'limit=25 应返回 -32602', /limit/);
  await expectError(client, 'match', { query: '' }, -32602, '空 query 应返回 -32602', /query/);
  await expectError(client, 'show', { name: '01-平静', inline: 'yes' }, -32602, 'inline 非布尔应返回 -32602', /inline/);

  // 14. 非法 JSON 行 → id:null + -32700，且不崩进程
  client.writeRaw('{not json}\n');
  const stray = await client.nextStray();
  assert(stray.id === null, '非法 JSON 的回包 id 应为 null，实际 ' + JSON.stringify(stray.id));
  assert(stray.error && stray.error.code === -32700, '非法 JSON 应返回 -32700');
  const afterBadJson = await callAction(client, 'list', {});
  assert(afterBadJson.count === 28, '非法 JSON 之后进程应继续服务');

  // 15. shutdown → ok:true，进程 0 退出，stdout 无杂音
  const shutdown = await client.request('shutdown', {});
  assert(shutdown.result && shutdown.result.ok === true, 'shutdown 应返回 ok:true');
  const exitInfo = await client.waitExit(5000);
  assert(exitInfo.code === 0, 'shutdown 后进程应以 0 退出，实际 ' + JSON.stringify(exitInfo));
  assert(client.buf.trim() === '', '退出时 stdout 不应残留半行：' + JSON.stringify(client.buf));
  assert(client.waiters.size === 0, 'shutdown 后不应有未回包请求');

  // 回包 id 一一对应：每个请求恰好一个回包，无重复、无多余。
  const repliedIds = client.received.filter((msg) => msg.id !== null && msg.id !== undefined).map((msg) => msg.id);
  assert(repliedIds.length === client.sent.length, '回包数量应与请求数量一致（请求 ' + client.sent.length + '，回包 ' + repliedIds.length + '）');
  assert(new Set(repliedIds).size === repliedIds.length, '回包 id 不应重复');
  const sentSorted = client.sent.slice().sort((a, b) => a - b);
  const repliedSorted = repliedIds.slice().sort((a, b) => a - b);
  assert(JSON.stringify(sentSorted) === JSON.stringify(repliedSorted), '回包 id 应与请求 id 一一对应');
  const strays = client.received.filter((msg) => msg.id === null || msg.id === undefined);
  assert(strays.length === 1, '应恰好收到 1 个 id:null 的非法 JSON 回包，实际 ' + strays.length);

  // 16. 单独 spawn 一次：关闭 stdin（EOF）应自退 0（04 P8 最后一道防线）
  const eofClient = new Client('eof');
  const eofInit = await eofClient.request('initialize', {});
  assert(eofInit.result && eofInit.result.ok === true, 'EOF 用例的 initialize 应成功');
  eofClient.endStdin();
  const eofExit = await eofClient.waitExit(5000);
  assert(eofExit.code === 0, '关闭 stdin 后进程应以 0 自退，实际 ' + JSON.stringify(eofExit));
  assert(eofClient.buf.trim() === '', 'EOF 自退时 stdout 不应残留半行：' + JSON.stringify(eofClient.buf));
}

async function main() {
  const guard = setTimeout(() => fail('测试总时长超过 60s'), 60000);
  await runScenario();
  clearTimeout(guard);
  console.log('OK: 握手测试通过（initialize/list/show/inline/match/错误处理/shutdown/EOF 自退）');
}

main().catch((err) => fail(err && err.stack ? err.stack : String(err)));
