/**
 * 界面行为自测：用 jsdom 真的点击、真的触发轮询。
 *
 * 覆盖两类只有在"真的点界面"时才暴露的问题：
 *  1. 未保存的编辑被轮询冲掉（历史上真出过：点「添加任务」卡片过几秒自己消失）
 *  2. 没勾选的备份模式，它下面的选项必须收起；勾选后才展开
 *
 * 用法
 * ----
 *   node tools/uitest_ui.js [index.html 路径]     默认测 src/ui/index.html
 * 需要 jsdom（只在开发机上装一次）：  npm install jsdom
 * 没装 jsdom 时以退出码 2 结束并提示，不会误报成功。
 */
const fs = require('fs');
const path = require('path');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  console.error('缺少 jsdom，请先安装：  npm install jsdom');
  process.exit(2);
}

const target = process.argv[2] || path.join(__dirname, '..', 'src', 'ui', 'index.html');
const html = fs.readFileSync(target, 'utf8');

let failed = 0;
function check(cond, label, extra) {
  const tail = extra === undefined ? '' : `  (${extra})`;
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${tail}`);
  if (!cond) failed++;
}

/* ---------------- 假的后台 ---------------- */
const server = {
  app: {port: 45700},
  jobs: [],
  runtime: {
    version: '0.2.0', url: 'http://127.0.0.1:45700/',
    config_path: 'C:\\假\\config.json', logs_dir: 'C:\\假\\logs',
    autostart: false, watch_paused: false,
    running: [], results: {}, restores: {}, status: {}, server_time: 0,
  },
};
const calls = {save: null, run: [], restore: null, open: []};
let pickResult = {path: 'D:\\假装选中的文件夹', error: ''};

const fakeBackups = [{
  name: '工作文件备份202609150938', created: 1, files: 2, bytes: 2048, ok: true,
  source: 'D:\\src',
}];
const fakeTree = {
  name: '工作文件备份202609150938', type: 'folder', children: [
    {name: '图纸', type: 'folder', rel: '图纸', children: [
      {name: '客厅.dwg', type: 'file', rel: '图纸/客厅.dwg', size: 2048, mtime: 1},
    ]},
    {name: '报价单.xlsx', type: 'file', rel: '报价单.xlsx', size: 512, mtime: 1},
  ],
};

function fakeFetch(url, opt) {
  const u = String(url);
  const body = opt && opt.body ? JSON.parse(opt.body) : null;
  if (u.includes('/api/state')) {
    return Promise.resolve({json: () => Promise.resolve(JSON.parse(JSON.stringify(server)))});
  }
  if (u.includes('/api/save')) {
    calls.save = body;
    server.jobs = JSON.parse(JSON.stringify(body.jobs || []));
    return Promise.resolve({json: () => Promise.resolve({ok: true})});
  }
  if (u.includes('/api/run')) {
    calls.run.push(body);
    return Promise.resolve({json: () => Promise.resolve({ok: true})});
  }
  if (u.includes('/api/pick')) {
    return Promise.resolve({json: () => Promise.resolve(pickResult)});
  }
  if (u.includes('/api/backups')) {
    if (u.includes('name=')) {
      return Promise.resolve({json: () => Promise.resolve({ok: true, tree: fakeTree, files: 2})});
    }
    return Promise.resolve({json: () => Promise.resolve({ok: true, target: 'F:\\备份', backups: fakeBackups})});
  }
  if (u.includes('/api/restore')) {
    calls.restore = body;
    return Promise.resolve({json: () => Promise.resolve(
      {ok: true, restored: 1, renamed: 0, dest: 'F:\\恢复出来的', summary: '恢复 1 项'})});
  }
  if (u.includes('/api/open')) {
    calls.open.push(body);
    return Promise.resolve({json: () => Promise.resolve({ok: true})});
  }
  if (u.includes('/api/log')) {
    return Promise.resolve({json: () => Promise.resolve({lines: []})});
  }
  return Promise.resolve({json: () => Promise.resolve({ok: true})});
}

(async () => {
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = fakeFetch;
      window.confirm = () => true;
      window.alert = () => {};
    },
  });
  const w = dom.window;
  const $ = (s, r) => (r || w.document).querySelector(s);
  const $$ = (s, r) => Array.from((r || w.document).querySelectorAll(s));
  const cards = () => $$('#jobs .card').length;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const dirtyShown = () => {
    const el = $('#dirtyPill');
    return el ? el.style.display !== 'none' : false;
  };
  const page = () => w.document.documentElement.innerHTML;
  const modeBody = (m) => $(`#jobs .card .mode[data-mode="${m}"] .mode-body`);

  console.log('界面行为自测：' + target);
  await sleep(80);

  /* ① 页面结构：新布局该有的都在 */
  check(page().indexOf('完整备份') >= 0 && page().indexOf('镜像备份') >= 0,
    '页面上有两种备份模式');
  check(page().indexOf('文件留存') >= 0, '有「文件留存」设置');
  check(page().indexOf('版本控制') < 0, '不再出现旧的「版本控制」说法');
  check(page().indexOf('打开备份夹') < 0, '顶部不再有「打开备份夹」');
  check(page().indexOf('历史版本') < 0, '不再有「历史版本」入口');
  check(page().indexOf('模式') >= 0 && !/镜像[\s\S]{0,40}<option/.test(page()),
    '备份方式不是下拉框（改成了勾选）');

  /* ② 全流程：添加 → 轮询不冲掉 → 保存 */
  check(cards() === 0, '没有任务时显示空状态');
  $('#btnAdd').dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(30);
  check(cards() === 1, '点「添加任务」后出现 1 张卡片', '卡片数=' + cards());
  check(dirtyShown(), '顶部出现「有改动没保存」提示');
  await w.load();
  await sleep(30);
  check(cards() === 1, '★ 轮询后，还没保存的卡片不能被冲掉', '卡片数=' + cards());

  const nameInput = $('#jobs .card input.job-name');
  nameInput.value = '我的任务';
  nameInput.dispatchEvent(new w.Event('input', {bubbles: true}));
  await w.load();
  await sleep(30);
  const after = $('#jobs .card input.job-name');
  check(!!after && after.value === '我的任务', '★ 轮询后，正在填的内容不能被冲掉',
    after ? after.value : '(输入框没了)');

  /* ③ 模式收起/展开（用户特别要求的行为） */
  const fullCb = $('#jobs .card input[data-k="full.enabled"]');
  const mirrorCb = $('#jobs .card input[data-k="mirror.enabled"]');
  check(!!fullCb && !!mirrorCb, '卡片上有「完整备份」「镜像备份」两个勾选');
  check(!!fullCb && fullCb.checked === true, '新任务默认勾选完整备份');
  check(!!mirrorCb && mirrorCb.checked === false, '新任务默认不勾镜像备份');
  check(!!modeBody('full') && modeBody('full').style.display !== 'none',
    '勾选了完整备份 → 它下面的选项展开');
  check(!!modeBody('mirror') && modeBody('mirror').style.display === 'none',
    '★ 没勾镜像备份 → 它下面的选项收起');
  mirrorCb.checked = true;
  mirrorCb.dispatchEvent(new w.Event('change', {bubbles: true}));
  await sleep(30);
  check(modeBody('mirror').style.display !== 'none', '★ 勾上镜像备份 → 选项展开');
  fullCb.checked = false;
  fullCb.dispatchEvent(new w.Event('change', {bubbles: true}));
  await sleep(30);
  check(modeBody('full').style.display === 'none', '★ 取消勾选完整备份 → 选项收起');
  fullCb.checked = true;
  fullCb.dispatchEvent(new w.Event('change', {bubbles: true}));
  await sleep(30);

  /* ④ 保存：结构要进到新字段里 */
  $('#btnSave').dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(120);
  check(calls.save !== null, '点「保存并应用」会把配置发给后台');
  const saved = calls.save && (calls.save.jobs || [])[0];
  check(!!saved && !!saved.full && !!saved.mirror, '保存的是新结构（full / mirror）');
  check(!!saved && saved.full.keep_recent === 5, '文件留存默认保留 5 份',
    saved ? String(saved.full.keep_recent) : '');
  check(!!saved && saved.mirror.watch.check_interval_minutes === 5,
    '变动触发默认「每 5 分钟检查一次」',
    saved ? String(saved.mirror.watch.check_interval_minutes) : '');
  check(!dirtyShown(), '保存后「有改动没保存」提示消失');
  check(cards() === 1, '保存后卡片仍在（此时来自服务器）');

  /* ⑤ 立即备份：两种模式都开着就应该分别触发 */
  $('#jobs .card button[data-act="run"]').dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(150);
  check(calls.run.length === 2, '两种模式各触发一次', JSON.stringify(calls.run));
  check(calls.run.every((c) => c.id && c.mode), '触发时带了任务 id 和模式');

  /* ⑥ 选择文件夹失败必须给提示 */
  const oldPick = pickResult;
  pickResult = {path: '', error: '没有选择文件夹（可能取消了）'};
  $('#toast').textContent = '';
  $('#jobs .card button[data-act="pick"]').dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(150);
  check(($('#toast').textContent || '').length > 0,
    '★ 选不到文件夹时必须给出提示，不能静默', JSON.stringify($('#toast').textContent));
  pickResult = oldPick;
  $('#toast').textContent = '';
  $('#jobs .card button[data-act="pick"]').dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(150);
  const picked = $('#jobs .card input[data-k="source"]');
  check(!!picked && picked.value === 'D:\\假装选中的文件夹', '选到文件夹时把路径填进输入框');

  /* ⑦ 恢复弹层：列出备份、树能展开、能选文件 */
  $('#jobs .card button[data-act="restore"]').dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(150);
  check($('#modal').hidden === false, '点「恢复」打开弹层');
  check($$('#vBackups .vitem').length === 1, '列出了可选的备份',
    String($$('#vBackups .vitem').length));
  check(page().indexOf('202609150938') >= 0, '备份按时间展示');
  const rows = $$('#vTree .trow');
  check(rows.length >= 3, '树里有文件夹和文件', String(rows.length));
  const caret = $('#vTree .caret[data-toggle]');
  const kids = caret ? caret.closest('.tnode').querySelector('.tkids') : null;
  check(!!kids && kids.style.display === 'none', '★ 文件夹默认是合上的');
  check(!!caret && caret.textContent === '▸', '合上时小三角是 ▸', caret ? caret.textContent : '');
  caret.dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(20);
  check(kids.style.display !== 'none', '★ 点小三角才展开');
  check(caret.textContent === '▾', '展开时小三角变成 ▾', caret.textContent);
  caret.dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(20);
  check(kids.style.display === 'none', '再点一次又合上');

  check($('#vRestorePicked').disabled === true, '没选文件时「恢复选中的」不可点');
  check($('#vRestoreAll').disabled === false, '「整批恢复」可用');
  const fbox = $('#vTree input[data-rel]');
  check(!!fbox, '文件前面有勾选框', fbox ? fbox.dataset.rel : '');
  fbox.checked = true;
  fbox.dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(30);
  check($('#vRestorePicked').disabled === false, '勾上文件后按钮变可用');
  check(($('#vRestorePicked').textContent || '').indexOf('1') >= 0,
    '按钮上显示选了几项', $('#vRestorePicked').textContent);

  calls.restore = null;
  $('#vRestorePicked').dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(200);
  check(calls.restore !== null, '点「恢复选中的」会调恢复接口');
  check(calls.restore && Array.isArray(calls.restore.rels) && calls.restore.rels.length === 1,
    '带上了选中的文件', JSON.stringify(calls.restore && calls.restore.rels));
  check(calls.restore && calls.restore.name === '工作文件备份202609150938',
    '带上了备份名');

  calls.restore = null;
  $('#vRestoreAll').dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(200);
  check(calls.restore !== null && !calls.restore.rels, '「整批恢复」不带 rels（整份恢复）');

  $('#vClose').dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(20);
  check($('#modal').hidden === true, '点关闭收起弹层');

  /* ⑧ 删除 */
  $('#jobs .card button[data-act="del"]').dispatchEvent(new w.Event('click', {bubbles: true}));
  await sleep(150);
  check(calls.save && (calls.save.jobs || []).length === 0,
    '★ 删除后发给后台的配置里没有任务了');
  check(cards() === 0, '★ 删除后卡片消失');

  console.log(failed ? `\n结果：${failed} 项未通过` : '\n结果：全部通过');
  dom.window.close();
  process.exit(failed ? 1 : 0);
})();
