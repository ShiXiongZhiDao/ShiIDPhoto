/* 师兄证照（ShiIDPhoto）前端。只做三件事：拿配置、传图、把结果放到画布上。 */

const API = {
  state: () => fetch('/api/state').then(async r => ({ status: r.status, data: await r.json().catch(() => ({})) })),
  process: (blob, name, params) =>
    fetch('/api/process?' + new URLSearchParams({ ...params, name }), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: blob,
    }).then(async r => {
      const data = await r.json().catch(() => ({ ok: false, error: '服务返回异常' }));
      if (!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);
      return data;
    }),
  openFolder: () => fetch('/api/open-folder', { method: 'POST' }).then(r => r.json()),
  setupInstall: () => fetch('/api/setup/install', { method: 'POST' })
    .then(async r => ({ status: r.status, ...(await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }))) })),
  setupCancel: () => fetch('/api/setup/cancel', { method: 'POST' })
    .then(async r => ({ status: r.status, ...(await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }))) })),
  setupLocal: blob => fetch('/api/setup/install-local', {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: blob,
  }).then(async r => ({ status: r.status, ...(await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }))) })),
};

const $ = id => document.getElementById(id);

const CUSTOM_KEY = '__custom__';

const state = {
  catalog: null,
  version: '',
  presets: [],
  customSizes: [],
  customDraft: { w: 390, h: 567, dpi: 300, mm_w: 33, mm_h: 48, name: '' },
  size: '一寸',
  color: '白色',
  sheet: '六寸',
  mode: 'sheet',
  file: null,
  result: null,
  selected: 'single',
  dirty: false,
  busy: false,
  layout: { rows: 4, cols: 4, count: 16, util: 0.9 },
};

const PRIORITY = ['一寸', '二寸 (证件照)', '小二寸', '中国居民身份证', '中国美国签证', '中国驾驶证'];

/* ---------------- 渲染 ---------------- */
function setStatus(text, kind = '') {
  $('statusText').textContent = text;
  $('status').className = 'status' + (kind ? ' ' + kind : '');
}

function toast(text, isError = false) {
  const el = $('toast');
  el.textContent = text;
  el.className = 'toast on' + (isError ? ' err' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.className = 'toast'; }, 2600);
}

/* 服务端的「结果可能不可用」提示：常驻显示，不随 toast 消失 */
function showNotice(items) {
  const el = $('notice');
  const list = (items || []).filter(Boolean);
  el.textContent = list.join(' ');
  el.hidden = list.length === 0;
}

/* 品牌串。唯一来源是 config/portable.json（经 /api/state 的 brand 字段到达这里）；
   HTML 里那份同名字面量只是服务还没连上时的兜底，两者由 BrandTests 钉成同源。
   以前 product_title 一路发到前端却没人读，改配置对界面毫无影响——这次接上。 */
function applyBrand(brand) {
  if (!brand || !brand.title) return;
  const name = $('brandName');
  const tag = $('brandTag');
  if (name) name.textContent = brand.title;
  if (tag && brand.tagline) tag.textContent = brand.tagline;
  document.title = brand.tagline ? `${brand.title} · ${brand.tagline}` : brand.title;
}

/* ---------------- modal 的公共出口 ---------------- */
/* × 由各卡自己绑，遮罩点击与 Esc 走这里。自定义尺寸那页原来是写死元素 id 的两份 handler，
   关于页再加一份就是三处要同步的关闭逻辑。 */
function openModal(el) { el.hidden = false; }
function closeModal(el) { el.hidden = true; }
function wireModalDismiss(el) {
  el.addEventListener('click', ev => { if (ev.target === el) closeModal(el); });
  document.addEventListener('keydown', ev => {
    if (ev.key === 'Escape' && !el.hidden) closeModal(el);
  });
}

/* ---------------- 步骤引导 ---------------- */
/* 文案只有 GUIDE_STEPS 这一处：常驻清单和手动触发的分步引导都从它渲染。
   写两遍就会漂移——上一轮刚为这件事立了测试（不许两处各说一套）。
   常驻清单不关闭、不写 localStorage（既有约定把那几个键列为"改了会悄悄重置用户偏好"）；
   引导也不记"看过没有"，随时可重播。
   完成态一律从 state 派生，不自记一份：state.file / state.result / state.dirty 已经在
   acceptFile、run、railReset、markDirty 里被改过，自记就要在同一个位置再改一次，
   漏一处（比如清空按钮）就会说假话。只有「下载过了」是本块自己的、会话内的一次性事实。 */
const GUIDE_STEPS = [
  { title: '把照片拖进来', short: '照片', sel: '#dropzone', fallback: '#paper',
    hints: ['单人 · 正面 · 光线均匀', '头顶上方要留出空间——头发贴到边就补不出白边'],
    body: '拖进中间这块，或点它选文件。手机拍的也行，但别用美颜过的原图。' },
  { title: '规格 · 底色', short: '规格', sel: '#sec-spec', value: true,
    body: '打开页面时已经替你选好，不改就直接下一步。要换就在下面点，其余规格收在「更多规格」里。' },
  { title: '生成证件照', short: '生成', sel: '#run', dirty: true,
    body: '右下角这颗，几秒出结果。照片不合格时画布上方会出现一条黄色提示，先看提示再决定要不要用。' },
  { title: '下载所选 / 打开文件夹', short: '下载', sel: '.outbar',
    body: '电子版走浏览器下载；要拿去冲印就点「打开文件夹」取那张相纸图。' },
];

const RAIL_MARKS = ['1', '2', '3', '4'];   // 带圈数字在 11px 下糊成点（实测截图），折叠版用阿拉伯数字
let stepDownloaded = false;

/* 半折叠：清单只占一行（缩写 + 当前值），完整句子交给「新手引导」那颗。
   取景提醒不能因为折叠就消失——它搬到画布空状态，见 renderHints。 */
function renderRail() {
  const box = $('steps');
  if (!box) return;
  box.innerHTML = GUIDE_STEPS.map((s, i) => `<li class="step" data-step="${i + 1}">
      <span class="k">${RAIL_MARKS[i]}</span><span class="t">${escHtml(s.short)}</span>
      ${s.value ? '<span class="v" id="stepSpec">—</span>' : ''}
      ${s.dirty ? '<span class="v warn" id="stepDirty" hidden>待重新生成</span>' : ''}
    </li>`).join('');
}

/* 拍照要求那两行：只在还没放照片时出现，那时它才是"事前"的。
   与引导卡片共用 GUIDE_STEPS[0].hints 这一处文案。 */
function renderHints() {
  const box = $('dzHints');
  if (!box) return;
  box.innerHTML = (GUIDE_STEPS[0].hints || []).map(h => `<div>${escHtml(h)}</div>`).join('');
}

function stepSpecLabel() {
  if (state.size === CUSTOM_KEY) return `自定义 ${state.customDraft.w}×${state.customDraft.h}`;
  // 用与卡片、色卡同一套显示口径，免得引导念的名字屏幕上找不到
  return `${shortName(state.size)} · ${String(state.color).replace('色', '')}`;
}

function renderSteps() {
  const box = $('steps');
  if (!box) return;
  const ready = !!state.catalog;
  const done = [!!state.file, ready, !!state.result && !state.dirty, stepDownloaded];
  box.querySelectorAll('.step').forEach((el, i) => {
    el.classList.toggle('done', done[i]);
    // 当前步 = 第一条未完成、且前面都完成的步骤
    el.classList.toggle('now', !done[i] && done.slice(0, i).every(Boolean));
  });
  // 清单由 renderRail 生成，所以这两格在 boot 早期可能还不存在
  const spec = $('stepSpec');
  if (spec) spec.textContent = ready ? stepSpecLabel() : '—';
  const dirty = $('stepDirty');
  if (dirty) dirty.hidden = !(state.result && state.dirty);
}

/* ---------------- 新手引导：手动触发的分步高亮 ---------------- */
/* 四块遮条围住被点亮的控件：空白处的点击被吃掉（点它=退出），
   控件本身照常能点——把控件盖住再"转发一次点击"是会咬人的做法，不做。 */
let guideIdx = 0;

function guideTarget(step) {
  const main = document.querySelector(step.sel);
  if (main && main.offsetParent !== null) return main;
  const alt = step.fallback && document.querySelector(step.fallback);
  return alt && alt.offsetParent !== null ? alt : null;
}

function layoutGuide() {
  const step = GUIDE_STEPS[guideIdx];
  const target = guideTarget(step);
  if (!target) { closeGuide(); return; }   // 目标不在屏上就别挡住界面
  const r = target.getBoundingClientRect();
  const pad = 6;
  const spot = $('guideSpot');
  const left = Math.max(4, r.left - pad);
  const top = Math.max(4, r.top - pad);
  const right = Math.min(window.innerWidth - 4, r.right + pad);
  const bottom = Math.min(window.innerHeight - 4, r.bottom + pad);
  spot.style.left = left + 'px';
  spot.style.top = top + 'px';
  spot.style.width = (right - left) + 'px';
  spot.style.height = (bottom - top) + 'px';

  const vw = window.innerWidth, vh = window.innerHeight;
  $('guideHitTop').style.cssText = `left:0;top:0;width:${vw}px;height:${top}px`;
  $('guideHitBottom').style.cssText = `left:0;top:${bottom}px;width:${vw}px;height:${Math.max(0, vh - bottom)}px`;
  $('guideHitLeft').style.cssText = `left:0;top:${top}px;width:${left}px;height:${bottom - top}px`;
  $('guideHitRight').style.cssText = `left:${right}px;top:${top}px;width:${Math.max(0, vw - right)}px;height:${bottom - top}px`;

  // 卡片优先落在高亮区下方；放不下就上方；再放不下就贴着视口
  const card = $('guideCard');
  const cardH = card.offsetHeight;
  let cardTop = bottom + 14;
  if (cardTop + cardH > vh - 12) cardTop = top - cardH - 14;
  if (cardTop < 12) cardTop = Math.min(Math.max(12, vh - cardH - 12), top);
  const cardW = card.offsetWidth;
  let cardLeft = Math.min(left, vw - cardW - 12);
  card.style.left = Math.max(12, cardLeft) + 'px';
  card.style.top = cardTop + 'px';
}

function renderGuide() {
  const step = GUIDE_STEPS[guideIdx];
  $('guideIdx').textContent = `第 ${guideIdx + 1} / ${GUIDE_STEPS.length} 步`;
  $('guideTitle').textContent = step.title;
  $('guideBody').textContent = step.body;
  const hints = $('guideHints');
  hints.innerHTML = (step.hints || []).map(h => `<li>${escHtml(h)}</li>`).join('');
  hints.hidden = !(step.hints || []).length;
  $('guideNext').textContent = guideIdx + 1 === GUIDE_STEPS.length ? '知道了' : '下一步';
  layoutGuide();
}

function openGuide() {
  guideIdx = 0;
  openModal($('guide'));
  renderGuide();
}

function closeGuide() {
  closeModal($('guide'));
}

function nextGuide() {
  if (guideIdx + 1 >= GUIDE_STEPS.length) { closeGuide(); return; }
  guideIdx += 1;
  renderGuide();
}

function bindGuide() {
  const guide = $('guide');
  if (!guide) return;
  $('guideBtn').addEventListener('click', openGuide);
  $('guideNext').addEventListener('click', nextGuide);
  $('guideSkip').addEventListener('click', closeGuide);
  guide.querySelectorAll('.tour-hit').forEach(el => el.addEventListener('click', closeGuide));
  wireModalDismiss(guide);   // Esc 与点遮罩走同一出口，不再写第三份关闭逻辑
  window.addEventListener('resize', () => { if (!guide.hidden) layoutGuide(); });
}

/* ---------------- 首次使用向导 ---------------- */
const fmtBytes = n => {
  if (!n) return '0 MB';
  const mb = n / 1048576;
  return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : (mb >= 100 ? mb.toFixed(0) : mb.toFixed(1)) + ' MB';
};

// 输出体积用：KB 级精度。复用上面的 fmtBytes 会把 31 KB 显示成「0.0 MB」
const fmtSize = n => (!n ? '—'
  : n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB'
  : Math.round(n / 1024) + ' KB');

const escHtml = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* 三张试印版固定对应三个模型；顺序与下载顺序一致（小的先完成） */
const SETUP_PLATES = [
  { model: 'face_yunet', fallbackName: '人脸检测 · YuNet', fallbackSize: '0.2 MB' },
  { model: 'pose', fallbackName: '姿态识别 · YOLOv8-pose', fallbackSize: '12.9 MB' },
  { model: 'background_removal', fallbackName: '抠图换底 · RMBG-1.4', fallbackSize: '168 MB' },
];

function renderSetup(s) {
  if (!s) return;
  if (!s.installing) setupPausing = false;
  const pausing = setupPausing && s.installing;
  const items = s.missing || [];
  const byId = {};
  items.forEach(item => { byId[item.model_id] = item; });

  const total = items.reduce((sum, item) => sum + item.size, 0);
  const done = items.reduce((sum, item) => sum + (item.done || 0), 0);
  const pct = total ? Math.min(100, done / total * 100) : 100;
  const installing = !!s.installing;

  $('setupPct').textContent = pct > 0 && pct < 10 ? pct.toFixed(1) : Math.floor(pct);
  $('setupBar').style.width = pct + '%';
  $('setupStat').textContent = pausing ? '正在暂停'
    : s.paused ? '已暂停'
    : s.error ? '显影中断'
    : installing ? '显影中'
    : pct >= 100 ? '三张已定影' : '等待装片';
  $('setupTime').textContent = total ? `${fmtBytes(done)} / ${fmtBytes(total)}` : '—';

  SETUP_PLATES.forEach((plate, i) => {
    const item = byId[plate.model];
    const state = item ? (item.state || 'pending') : 'done';
    const el = $('setupPrint' + i);
    el.classList.toggle('on', state === 'done');
    el.classList.toggle('run', state === 'downloading');
    if (item) $('setupSize' + i).textContent = fmtBytes(item.size);
  });

  $('setupList').innerHTML = SETUP_PLATES.map(plate => {
    const item = byId[plate.model];
    const state = item ? (item.state || 'pending') : 'done';
    const cls = state === 'downloading' ? 'on'
      : state === 'done' ? 'done'
      : state === 'failed' ? 'failed' : '';
    const name = plate.fallbackName;
    const size = item ? fmtBytes(item.size) : plate.fallbackSize;
    return `<div class="setup-li ${cls}">
      <span class="d"></span><span class="n">${escHtml(name)}</span><span class="sz">${size}</span>
    </div>`;
  }).join('');

  $('setupStart').disabled = installing || s.disk_ok === false;
  $('setupStart').textContent = pausing ? '暂停中…'
    : installing ? '正在下载…'
    : s.disk_ok === false ? '磁盘空间不足'
    : s.paused ? '继续下载'
    : s.error ? '重试下载'
    : pct >= 100 ? '正在进入…'
    : done > 0 ? '继续下载' : '开始下载';
  $('setupCancel').hidden = !installing;
  $('setupLocal').disabled = installing;

  const warn = setupClientError || s.error || (s.warnings && s.warnings[0]) || '';
  $('setupError').hidden = !warn;
  $('setupError').textContent = warn;
  $('setupError').className = 'setup-warn' + (s.paused ? ' paused' : '');
  $('setupNote').innerHTML = `${fmtBytes(total)} · 网络正常约 1 分钟 · 装好后离线<br>`
    + `位置 user-data\\models` + (s.free_bytes ? ` · 磁盘剩余 ${fmtBytes(s.free_bytes)}` : '');
  $('termDisk').textContent = s.free_bytes ? fmtBytes(s.free_bytes) : 'ok';
}

/* 页面侧的请求失败（409/网络错误）与下载错误共用一个提示位 */
let setupClientError = '';
let setupPausing = false;

function setSetupClientError(text) {
  setupClientError = text || '';
  $('setupError').hidden = !setupClientError;
  $('setupError').textContent = setupClientError;
}

function showSetup(s) {
  $('setup').hidden = false;
  setStatus('等待首次设置', 'busy');
  renderSetup(s);
}

function hideSetup() {
  $('setup').hidden = true;
}

function bindSetup() {
  $('setupStart').addEventListener('click', async () => {
    setSetupClientError('');
    setupPausing = false;
    const r = await API.setupInstall();
    if (!r.ok) setSetupClientError(r.error || '启动下载失败');
  });
  $('setupCancel').addEventListener('click', async () => {
    // 连接建立阶段无法立刻中断，先给一个"正在暂停"的即时反馈
    setupPausing = true;
    $('setupStart').disabled = true;
    $('setupStart').textContent = '暂停中…';
    $('setupStat').textContent = '正在暂停';
    const r = await API.setupCancel();
    if (!r.ok) {
      setupPausing = false;
      setSetupClientError(r.error || '暂停失败');
    }
  });
  $('setupLocal').addEventListener('click', () => $('setupFile').click());
  $('setupFile').addEventListener('change', async event => {
    const file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (!file) return;
    setSetupClientError('');
    $('setupStat').textContent = '校验文件…';
    const r = await API.setupLocal(file);
    if (!r.ok) setSetupClientError(r.error || '安装失败');
  });
}

function renderSpecs() {
  const photos = state.catalog.photos;
  const chips = PRIORITY.filter(n => photos.some(p => p.name === n));
  const rest = photos.filter(p => !chips.includes(p.name));

  const customOn = state.size === CUSTOM_KEY;
  const customCard = `<button class="spec spec-add${customOn ? ' on' : ''}" id="specCustom">
      <span class="n">${customOn ? '自定义' : '+ 自定义'}</span>
      <span class="d">${customOn
        ? `${state.customDraft.mm_w ? `${state.customDraft.mm_w}×${state.customDraft.mm_h}mm · ` : ''}`
          + `${state.customDraft.w}×${state.customDraft.h}`
        : '按机构要求填写'}</span>
    </button>`;

  $('specs').innerHTML = chips.map(name => {
    const p = photos.find(x => x.name === name);
    return `<button class="spec" data-size="${escapeAttr(name)}">
      <span class="n">${shortName(name)}</span>
      <span class="d">${fmtMm(p.print_w)}×${fmtMm(p.print_h)}cm · ${p.w}×${p.h}</span>
    </button>`;
  }).join('') + customCard;

  $('moreSpecs').innerHTML = '<option value="">更多规格…</option>' +
    rest.map(p => `<option value="${escapeAttr(p.name)}">${p.name} · ${p.w}×${p.h}</option>`).join('');

  $('specs').querySelectorAll('.spec[data-size]').forEach(btn => {
    btn.addEventListener('click', () => selectSize(btn.dataset.size));
  });
  $('specCustom').addEventListener('click', () => openCustom());
  syncSpecActive();
}

function shortName(name) {
  return name.replace(' (证件照)', '').replace(' (标准2寸)', '').replace('中国', '');
}

function fmtMm(v) {
  return Number.isInteger(v) ? v : v.toFixed(1);
}

function escapeAttr(s) {
  return String(s).replace(/"/g, '&quot;');
}

function syncSpecActive() {
  $('specs').querySelectorAll('.spec').forEach(b => {
    // 自定义卡片没有 data-size，按伪规格名单独判断
    b.classList.toggle('on', b.id === 'specCustom'
      ? state.size === CUSTOM_KEY
      : b.dataset.size === state.size);
  });
  renderSteps();
}

function renderColors() {
  $('colors').innerHTML = state.catalog.colors.map(c =>
    `<div class="sw-item" data-color="${escapeAttr(c.name)}">
       <span class="sw-col" style="background:${c.hex}"></span>
       <span>${c.name.replace('色', '')}</span>
     </div>`).join('');
  $('colors').querySelectorAll('.sw-item').forEach(el => {
    el.addEventListener('click', () => {
      state.color = el.dataset.color;
      syncColorActive();
      markDirty();
    });
  });
  syncColorActive();
}

function syncColorActive() {
  $('colors').querySelectorAll('.sw-item').forEach(el => {
    el.classList.toggle('on', el.dataset.color === state.color);
  });
  renderSteps();
}

function renderSheets() {
  $('sheetSel').innerHTML = state.catalog.sheets.map(s =>
    `<option value="${escapeAttr(s.name)}" ${s.name === state.sheet ? 'selected' : ''}>${s.name} · ${s.w}×${s.h}</option>`
  ).join('');
}

/* ---------------- 排版计算 ---------------- */
function recomputeLayout() {
  const photo = currentPhoto();
  const paper = state.catalog.sheets.find(s => s.name === state.sheet);
  if (!photo || !paper) return;

  // 自定义尺寸可能比整张相纸还大。Math.max(1, ...) 会算出 1 列，
  // 但一张都放不下——不说清楚的话，用户点了生成才看到上游的报错。
  if (photo.w > paper.w || photo.h > paper.h) {
    state.layout = { rows: 1, cols: 1, count: 1, util: 0 };
    $('layoutInfo').textContent = `${state.sheet} · 放不下`;
    $('layoutCount').textContent = '请改用单张';
    $('footDpi').textContent = `${photo.dpi} dpi`;
    return;
  }

  const cols = Math.max(1, Math.floor(paper.w / photo.w));
  const rows = Math.max(1, Math.floor(paper.h / photo.h));
  const util = (cols * photo.w * rows * photo.h) / (paper.w * paper.h);
  state.layout = { rows, cols, count: rows * cols, util };

  $('layoutInfo').textContent = `${state.sheet} · ${cols}×${rows}`;
  $('layoutCount').textContent = `${rows * cols} 张 · ${Math.round(util * 100)}% 利用`;
  $('footDpi').textContent = `${photo.dpi} dpi`;
}

function selectSize(name) {
  state.size = name;
  syncSpecActive();
  $('moreSpecs').value = '';
  recomputeLayout();
  markDirty();
}

function markDirty() {
  if (!state.result) return;
  state.dirty = true;
  const sub = $('sub');
  sub.className = 'sub dirty';
  sub.textContent = '参数已改动，点一下重新生成';
  renderSteps();
}

/* ---------------- 文件 ---------------- */
function acceptFile(file) {
  if (!file) return;
  if (!file.type.startsWith('image/')) {
    toast('这不是图片文件', true);
    return;
  }
  if (file.size > 40 * 1024 * 1024) {
    toast('图片超过 40MB，请先压缩', true);
    return;
  }
  state.file = file;
  state.result = null;
  state.dirty = false;
  showNotice([]);

  const url = URL.createObjectURL(file);
  showPaper(url, file.name, img => {
    $('fileMeta').textContent = `· ${img.naturalWidth} × ${img.naturalHeight} · ${(file.size / 1048576).toFixed(1)} MB`;
  });
  $('fileName').textContent = file.name.length > 34 ? file.name.slice(0, 32) + '…' : file.name;
  $('stageStat').textContent = '待生成';
  $('run').disabled = false;
  $('sub').className = 'sub';
  $('sub').textContent = '点击「生成证件照」，约 4 秒';
  resetThumbs();
  setStatus('照片已载入', '');
  stepDownloaded = false;
  renderSteps();
}

function showPaper(src, name, onLoaded) {
  const paper = $('paper');
  paper.hidden = false;
  $('dropzone').style.display = 'none';
  const img = $('preview');
  img.onload = () => {
    fitPaper();
    if (onLoaded) onLoaded(img);
  };
  img.src = src;
  img.alt = name || '预览';
  if (img.complete && img.naturalWidth) {
    fitPaper();
    if (onLoaded) onLoaded(img);
  }
}

/* 相纸盒子按图片比例贴合容器：不用 aspect-ratio，避免溢出盖住底部输出条 */
function fitPaper() {
  const wrap = $('canvasWrap');
  const img = $('preview');
  const paper = $('paper');
  if (paper.hidden || !img.naturalWidth) return;
  const pad = 68;
  const availW = Math.max(80, wrap.clientWidth - pad);
  const availH = Math.max(80, wrap.clientHeight - pad);
  const scale = Math.min(availW / img.naturalWidth, availH / img.naturalHeight);
  paper.style.width = Math.round(img.naturalWidth * scale) + 'px';
  paper.style.height = Math.round(img.naturalHeight * scale) + 'px';
}

function resetThumbs() {
  for (const key of ['thumbSingle', 'thumbSheet']) {
    const el = $(key);
    el.classList.remove('ready', 'on');
    const img = el.querySelector('img');
    if (img) img.remove();
    if (!el.querySelector('.ph')) {
      const ph = document.createElement('div');
      ph.className = 'ph';
      el.prepend(ph);
    }
    el.querySelector('.m').textContent = '—';
  }
  $('dl').classList.add('off');
}

function setThumb(key, url, meta, label) {
  const el = $(key);
  const ph = el.querySelector('.ph');
  if (ph) ph.remove();
  let img = el.querySelector('img');
  if (!img) {
    img = document.createElement('img');
    el.prepend(img);
  }
  img.src = url;
  el.querySelector('.t').textContent = label;
  el.querySelector('.m').textContent = meta;
  el.classList.add('ready');
}

/* ---------------- 生成 ---------------- */
async function run() {
  if (state.busy) return;
  if (!state.file) {
    $('fileInput').click();
    return;
  }
  state.busy = true;
  $('run').disabled = true;
  setStatus('正在处理…', 'busy');
  $('overlay').classList.add('on');

  const started = performance.now();
  const timer = setInterval(() => {
    $('overlayTxt').textContent = `正在处理… ${((performance.now() - started) / 1000).toFixed(1)}s`;
  }, 100);

  try {
    const draft = state.size === CUSTOM_KEY ? state.customDraft : null;
    const data = await API.process(state.file, state.file.name, {
      size: state.size,
      // 自定义规格把数值一并带上：服务端不认 id，只认这次要用的尺寸
      ...(draft ? { w: draft.w, h: draft.h, dpi: draft.dpi, mm_w: draft.mm_w, mm_h: draft.mm_h } : {}),
      bg: state.color,
      sheet: state.sheet,
      rows: state.layout.rows,
      cols: state.layout.cols,
      mode: state.mode,
      fmt: $('fmtSel').value,
      quality: $('qualitySel').value,
      // 没勾选就传空，服务端按"不限制"处理，而不是按 0 处理
      max_kb: $('maxKbOn').checked ? $('maxKb').value : '',
    });
    state.result = data;
    state.dirty = false;
    state.selected = state.mode === 'sheet' ? 'sheet' : 'single';
    const notes = data.warnings || [];
    const sizeNotes = data.size_notes || [];
    const allNotes = [...notes, ...sizeNotes];
    applyResult();
    showNotice(allNotes);
    setStatus(allNotes.length ? '处理完成 · 有提示' : '处理完成', allNotes.length ? 'warn' : '');
    $('stageStat').textContent = `${data.ms} ms`;
    $('sub').className = 'sub' + (allNotes.length ? ' warn' : '');
    $('sub').textContent = notes.length ? '结果可能不合格，建议换一张照片'
      : sizeNotes.length ? '已按体积上限输出，见上方提示'
      : `已输出到 user-data\\output`;
  } catch (err) {
    showNotice([]);
    setStatus('处理失败', 'err');
    toast(String(err.message || err), true);
    $('sub').className = 'sub err';
    $('sub').textContent = '处理失败，详见日志 user-data\\logs\\webui.log';
  } finally {
    clearInterval(timer);
    $('overlay').classList.remove('on');
    $('run').disabled = !state.file;
    state.busy = false;
    renderSteps();
  }
}

function applyResult() {
  const r = state.result;
  const single = r.single;
  const sheet = r.sheet_image;

  const fmtLabel = (r.format || 'jpg').toUpperCase();
  $('footFmt').textContent = fmtLabel;
  // 体积是这次的诉求，直接写在元信息里，用户不用去文件夹看属性
  const meta = (w, h, extra, bytes) =>
    `${w}×${h} · ${extra} · ${fmtLabel}` + (bytes ? ` · ${fmtSize(bytes)}` : '');
  setThumb('thumbSingle', single.url, meta(single.w, single.h, `${single.dpi}dpi`, single.bytes), '单张电子版');
  if (sheet) {
    setThumb('thumbSheet', sheet.url, meta(sheet.w, sheet.h, `${sheet.count} 张`, sheet.bytes), '相纸排版');
  } else {
    const el = $('thumbSheet');
    el.classList.remove('ready');
    el.querySelector('.m').textContent = '未生成';
  }
  $('dl').classList.remove('off');
  selectThumb(state.selected);
}

function selectThumb(which) {
  state.selected = which;
  const single = state.result?.single;
  const sheet = state.result?.sheet_image;
  const useSheet = which === 'sheet' && sheet;

  $('thumbSingle').classList.toggle('on', which === 'single');
  $('thumbSheet').classList.toggle('on', which === 'sheet');

  if (useSheet) {
    showPaper(sheet.url);
    $('guides').classList.add('off');
    $('toggleGuides').classList.add('disabled');
    $('toggleSafe').classList.add('disabled');
    $('stageStat').textContent = `${sheet.w}×${sheet.h} · ${sheet.count} 张`
      + (sheet.bytes ? ` · ${fmtSize(sheet.bytes)}` : '');
  } else if (single) {
    showPaper(single.url);
    $('toggleGuides').classList.remove('disabled');
    $('toggleSafe').classList.remove('disabled');
    if ($('toggleGuides').classList.contains('on')) $('guides').classList.remove('off');
    $('stageStat').textContent = `${single.w}×${single.h} · ${single.dpi}dpi`
      + (single.bytes ? ` · ${fmtSize(single.bytes)}` : '');
  }
  $('dl').classList.remove('off');
}

/* PNG 是无损的：质量档与体积上限对它无效，禁掉比留着好看却没用要诚实。
   maxKbWanted 记住用户的意图——切到 PNG 再切回来，不该把设好的上限弄丢。 */
let maxKbWanted = false;

function syncSizeControls() {
  const lossless = $('fmtSel').value === 'png';
  $('maxKbOn').checked = lossless ? false : maxKbWanted;
  $('qualitySel').disabled = lossless;
  $('maxKbOn').disabled = lossless;
  $('maxKb').disabled = lossless || !$('maxKbOn').checked;
  // 控件与说明二选一：PNG 下留着灰掉的复选框，只会让人以为"再点点就能用"
  $('maxKbBox').hidden = lossless;
  $('maxKb').hidden = lossless;
  $('maxKbUnit').hidden = lossless;
  $('sizeHint').hidden = !lossless;
}

/* ---------------- 下载 ---------------- */
function download() {
  const r = state.result;
  if (!r) return;
  const target = state.selected === 'sheet' && r.sheet_image ? r.sheet_image : r.single;
  const ext = r.format || 'jpg';
  const name = state.selected === 'sheet' && r.sheet_image
    ? `证件照_${r.size}_${r.color}_相纸.${ext}`
    : `证件照_${r.size}_${r.color}.${ext}`;
  const a = document.createElement('a');
  a.href = target.url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  stepDownloaded = true;
  renderSteps();
}

/* ---------------- 事件绑定 ---------------- */
/* ---------------- 自定义尺寸 ---------------- */
let cmUnit = 'px';

// 与 shell/custom_sizes.py 保持一致：四舍五入，不是截断。
// 截断会让 33mm@300dpi 变成 389 而不是 390，和机构给的数字差 1 像素。
const mmToPx = (mm, dpi) => Math.round(mm / 25.4 * dpi);
const pxToMm = (px, dpi) => Math.round(px / dpi * 25.4 * 10) / 10;

function currentPhoto() {
  if (state.size === CUSTOM_KEY) {
    const d = state.customDraft;
    return {
      name: '自定义', w: d.w, h: d.h, dpi: d.dpi,
      print_w: d.mm_w ? d.mm_w / 10 : null,
      print_h: d.mm_h ? d.mm_h / 10 : null,
    };
  }
  return state.catalog.photos.find(p => p.name === state.size);
}

function cmDpiValue() {
  const v = Number($('cmDpi').value);
  return Number.isFinite(v) && v > 0 ? Math.round(v) : 300;
}

function readCmForm() {
  const dpi = cmDpiValue();
  const w = Number($('cmW').value);
  const h = Number($('cmH').value);
  if (!Number.isFinite(w) || !Number.isFinite(h) || !w || !h) {
    return { dpi, error: '宽和高都要填数字' };
  }

  let pxW, pxH, mmW, mmH;
  if (cmUnit === 'mm') {
    mmW = w; mmH = h;
    pxW = mmToPx(w, dpi); pxH = mmToPx(h, dpi);
  } else {
    pxW = Math.round(w); pxH = Math.round(h);
    mmW = pxToMm(pxW, dpi); mmH = pxToMm(pxH, dpi);
  }

  // 前端先挡一遍，理由只是"立刻反馈"；服务端仍会独立校验，不依赖这里
  let error = null;
  if (pxW < 100 || pxW > 2000 || pxH < 100 || pxH > 2000) {
    error = `宽高需在 100–2000 像素之间（当前 ${pxW}×${pxH}）`;
  } else {
    const ratio = pxH / pxW;
    if (ratio < 0.5 || ratio > 1.5) {
      error = `宽高比 ${ratio.toFixed(2)} 超出 0.5–1.5，这个比例裁出来会变形`;
    } else if (dpi < 72 || dpi > 1200) {
      error = `DPI 需在 72–1200 之间（当前 ${dpi}）`;
    }
  }
  return { dpi, pxW, pxH, mmW, mmH, error };
}

function updateCmHint() {
  const f = readCmForm();
  if (f.error) {
    $('cmHint').textContent = '—';
    return;
  }
  $('cmHint').textContent = cmUnit === 'mm'
    ? `= ${f.pxW}×${f.pxH} px @${f.dpi}dpi`
    : `= 约 ${f.mmW}×${f.mmH} mm @${f.dpi}dpi`;
}

function syncCmUnit() {
  $('cmUnit').querySelectorAll('button').forEach(b => {
    b.classList.toggle('on', b.dataset.unit === cmUnit);
  });
  $('cmUnitTag').textContent = cmUnit === 'mm' ? 'mm' : 'px';
}

function switchCmUnit(next) {
  if (next === cmUnit) return;
  const dpi = cmDpiValue();
  const w = Number($('cmW').value) || 0;
  const h = Number($('cmH').value) || 0;
  if (w && h) {
    if (next === 'mm') {
      $('cmW').value = pxToMm(w, dpi);
      $('cmH').value = pxToMm(h, dpi);
    } else {
      $('cmW').value = mmToPx(w, dpi);
      $('cmH').value = mmToPx(h, dpi);
    }
  }
  cmUnit = next;
  syncCmUnit();
  updateCmHint();
}

function showCmError(text) {
  $('cmError').textContent = text || '';
  $('cmError').hidden = !text;
}

function renderCustomList() {
  const items = state.customSizes || [];
  $('cmSaved').hidden = items.length === 0;
  if (!items.length) {
    $('cmList').innerHTML = '';
    return;
  }
  $('cmList').innerHTML = items.map(it => `
    <div class="cm-item" data-id="${escapeAttr(it.id)}">
      <span class="nm">${escHtml(it.name)}</span>
      <span class="sz">${it.width}×${it.height}${it.mm_w ? ` · ${it.mm_w}×${it.mm_h}mm` : ''}</span>
      <button class="del" data-del="${escapeAttr(it.id)}" title="删除">×</button>
    </div>`).join('');

  $('cmList').querySelectorAll('.cm-item').forEach(el => {
    el.addEventListener('click', () => {
      const item = items.find(x => x.id === el.dataset.id);
      if (item) openCustom(item);
    });
  });
  $('cmList').querySelectorAll('[data-del]').forEach(btn => {
    btn.addEventListener('click', ev => {
      ev.stopPropagation();
      deleteCustom(btn.dataset.del);
    });
  });
}

function openCustom(item) {
  showCmError('');
  renderCustomList();

  if (item) {
    cmUnit = 'px';
    $('cmW').value = item.width;
    $('cmH').value = item.height;
    $('cmDpi').value = item.dpi;
    $('cmName').value = item.name || '';
  } else if (state.size === CUSTOM_KEY) {
    const d = state.customDraft;
    cmUnit = 'px';
    $('cmW').value = d.w;
    $('cmH').value = d.h;
    $('cmDpi').value = d.dpi;
    $('cmName').value = d.name || '';
  } else {
    cmUnit = 'px';
    $('cmW').value = '';
    $('cmH').value = '';
    $('cmDpi').value = 300;
    $('cmName').value = '';
  }

  syncCmUnit();
  updateCmHint();
  openModal($('customModal'));
  setTimeout(() => $('cmW').focus(), 40);
}

function closeCustom() {
  closeModal($('customModal'));
}

function applyCustom() {
  const f = readCmForm();
  if (f.error) {
    showCmError(f.error);
    return;
  }
  state.customDraft = {
    w: f.pxW, h: f.pxH, dpi: f.dpi,
    mm_w: f.mmW, mm_h: f.mmH,
    name: $('cmName').value.trim(),
  };
  state.size = CUSTOM_KEY;
  closeCustom();
  renderSpecs();
  recomputeLayout();
  markDirty();
}

async function saveCustom() {
  const f = readCmForm();
  if (f.error) {
    showCmError(f.error);
    return;
  }
  try {
    const r = await fetch('/api/custom-sizes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        w: f.pxW, h: f.pxH, dpi: f.dpi,
        mm_w: f.mmW, mm_h: f.mmH,
        name: $('cmName').value.trim(),
      }),
    }).then(x => x.json());
    if (!r.ok) throw new Error(r.error || '保存失败');
    state.customSizes = r.sizes || [];
    applyCustom();
    toast('已保存这个尺寸');
  } catch (err) {
    showCmError(String(err.message || err));
  }
}

async function deleteCustom(id) {
  try {
    const r = await fetch('/api/custom-sizes/' + encodeURIComponent(id), { method: 'DELETE' })
      .then(x => x.json());
    if (!r.ok) throw new Error(r.error || '删除失败');
    state.customSizes = r.sizes || [];
    renderCustomList();
  } catch (err) {
    showCmError(String(err.message || err));
  }
}

function bindCustomSize() {
  $('cmClose').addEventListener('click', closeCustom);
  $('cmCancel').addEventListener('click', closeCustom);
  $('cmUse').addEventListener('click', applyCustom);
  $('cmSave').addEventListener('click', saveCustom);
  $('cmUnit').querySelectorAll('button').forEach(b => {
    b.addEventListener('click', () => switchCmUnit(b.dataset.unit));
  });
  ['cmW', 'cmH', 'cmDpi'].forEach(id => {
    $(id).addEventListener('input', updateCmHint);
  });
  $('cmW').addEventListener('keydown', ev => { if (ev.key === 'Enter') applyCustom(); });
  $('cmH').addEventListener('keydown', ev => { if (ev.key === 'Enter') applyCustom(); });
  wireModalDismiss($('customModal'));
}

/* ---------------- 关于 ---------------- */
/* 这一页唯一的出站请求就是那三张二维码图。两个镜像目录结构一致，HTML 只写文件名。
   不打进包换的是包体积；代价是完整包用户可能一张都取不到，
   所以失败态自带「在微信里搜『师兄知道』」这句纯文字兜底。
   Gitee 那个签名地址只认空 Referer：页面里带着 http://127.0.0.1:7860/ 去要就是 403，
   所以每个 <img> 都必须写 referrerpolicy="no-referrer"，否则第二个镜像形同不存在。 */
const QR_MIRRORS = [
  'https://raw.githubusercontent.com/ShiXiongZhiDao/BrandAssets/main/images/about/',
  'https://gitee.com/ShiXiongZhiDao/BrandAssets/raw/main/images/about/',
];

function qrSlots(root) {
  return Array.prototype.slice.call(root.querySelectorAll('img[data-qr]'));
}

function syncQrError(img) {
  const sec = img.closest('.about-sec');
  if (!sec) return;
  const dead = qrSlots(sec).some(x => x.dataset.qrState === 'dead');
  sec.classList.toggle('qr-error', dead);
}

/* 顺序回退：GitHub 失败换 Gitee，两个都试过就把图撤掉、露出那句文案。
   不做竞速——这边没有 JS 测试运行器，状态越简单越可信。 */
function loadQr(img) {
  const tryIdx = Number(img.dataset.qrTry || 0);
  const fail = img.parentElement.querySelector('.qr-fail');
  if (tryIdx >= QR_MIRRORS.length) {
    img.dataset.qrState = 'dead';
    img.hidden = true;
    if (fail) fail.hidden = false;
    syncQrError(img);
    return;
  }
  img.dataset.qrState = 'loading';
  img.hidden = false;
  if (fail) fail.hidden = true;
  img.onerror = () => {
    img.dataset.qrTry = String(tryIdx + 1);
    loadQr(img);
  };
  img.onload = () => {
    img.dataset.qrState = 'ok';
    img.onerror = null;
    syncQrError(img);
  };
  img.src = QR_MIRRORS[tryIdx] + img.dataset.qr;
}

function retryQr(sec) {
  qrSlots(sec).forEach(img => { img.dataset.qrTry = '0'; });
  sec.classList.remove('qr-error');
  qrSlots(sec).forEach(loadQr);
}

/* 版本号只在关于页显示，所以没必要跟着主界面等模型加载完 */
function renderAboutVersion() {
  const el = $('aboutVersion');
  if (!el) return;
  el.textContent = state.version ? `版本 ${state.version}` : '版本读取中…';
}

function bindAbout() {
  const modal = $('aboutModal');
  if (!modal) return;
  $('railAbout').addEventListener('click', () => {
    openModal(modal);
    if (!modal.dataset.qrLoaded) {
      modal.dataset.qrLoaded = '1';
      qrSlots(modal).forEach(loadQr);
    }
  });
  $('aboutClose').addEventListener('click', () => closeModal(modal));
  wireModalDismiss(modal);
  modal.querySelectorAll('.qr-retry').forEach(btn => {
    btn.addEventListener('click', () => retryQr(btn.closest('.about-sec')));
  });
  renderAboutVersion();
}

function bind() {
  bindCustomSize();
  bindGuide();
  $('dropzone').addEventListener('click', () => $('fileInput').click());
  $('railImport').addEventListener('click', () => $('fileInput').click());
  $('fileInput').addEventListener('change', e => acceptFile(e.target.files[0]));

  ['dragenter', 'dragover'].forEach(ev =>
    document.addEventListener(ev, e => {
      e.preventDefault();
      $('dropzone').classList.add('over');
    }));
  ['dragleave', 'drop'].forEach(ev =>
    document.addEventListener(ev, e => {
      e.preventDefault();
      if (ev === 'dragleave' && e.relatedTarget) return;
      $('dropzone').classList.remove('over');
    }));
  document.addEventListener('drop', e => {
    const file = e.dataTransfer?.files?.[0];
    if (file) acceptFile(file);
  });

  $('run').addEventListener('click', run);

  $('modes').querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', () => {
      state.mode = btn.dataset.mode;
      $('modes').querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
      markDirty();
    });
  });

  $('sheetSel').addEventListener('change', e => {
    state.sheet = e.target.value;
    recomputeLayout();
    markDirty();
  });

  $('moreSpecs').addEventListener('change', e => {
    if (e.target.value) selectSize(e.target.value);
  });

  $('toggleGuides').addEventListener('click', e => {
    const on = e.currentTarget.classList.toggle('on');
    $('guides').classList.toggle('off', !on);
  });
  $('toggleSafe').addEventListener('click', e => {
    const on = e.currentTarget.classList.toggle('on');
    const box = document.querySelector('.safebox');
    if (box) box.style.display = on ? '' : 'none';
  });

  $('thumbSingle').addEventListener('click', () => { if (state.result) selectThumb('single'); });
  $('thumbSheet').addEventListener('click', () => {
    if (state.result?.sheet_image) selectThumb('sheet');
  });

  $('dl').addEventListener('click', download);
  $('fmtSel').value = localStorage.getItem('idphoto.fmt') || 'jpg';
  $('qualitySel').value = localStorage.getItem('idphoto.quality') || 'high';
  $('maxKb').value = localStorage.getItem('idphoto.maxkb') || '100';
  $('maxKbOn').checked = localStorage.getItem('idphoto.maxkbOn') === '1';
  maxKbWanted = $('maxKbOn').checked;
  $('footFmt').textContent = $('fmtSel').value.toUpperCase();
  syncSizeControls();

  $('fmtSel').addEventListener('change', () => {
    localStorage.setItem('idphoto.fmt', $('fmtSel').value);
    $('footFmt').textContent = $('fmtSel').value.toUpperCase();
    syncSizeControls();
  });
  $('qualitySel').addEventListener('change', () => {
    localStorage.setItem('idphoto.quality', $('qualitySel').value);
  });
  $('maxKbOn').addEventListener('change', () => {
    maxKbWanted = $('maxKbOn').checked;
    localStorage.setItem('idphoto.maxkbOn', maxKbWanted ? '1' : '0');
    syncSizeControls();
  });
  $('maxKb').addEventListener('change', () => {
    // 夹到服务端接受的区间，避免输错后请求被 400 打回却看不出原因
    const raw = Number($('maxKb').value);
    const clamped = Math.min(8192, Math.max(20, Number.isFinite(raw) ? Math.round(raw) : 100));
    $('maxKb').value = String(clamped);
    localStorage.setItem('idphoto.maxkb', String(clamped));
  });
  $('openFolder').addEventListener('click', async () => {
    const r = await API.openFolder();
    if (!r.ok) toast('打开失败：' + (r.error || ''), true);
  });

  $('railReset').addEventListener('click', () => {
    state.file = null;
    state.result = null;
    showNotice([]);
    $('paper').hidden = true;
    $('dropzone').style.display = '';
    $('fileName').textContent = '未选择照片';
    $('fileMeta').textContent = '';
    $('stageStat').textContent = '';
    $('sub').className = 'sub';
    $('sub').textContent = '先拖入一张照片';
    $('run').disabled = true;
    resetThumbs();
    stepDownloaded = false;
    renderSteps();
  });

  window.addEventListener('resize', fitPaper);
  window.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); run(); }
    if (e.key === 'o' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('fileInput').click(); }
  });
}

/* ---------------- 启动 ---------------- */
const sleep = ms => new Promise(r => setTimeout(r, ms));

function bootText(text) {
  const el = $('overlayTxt');
  if (el) el.textContent = text;
  $('overlay').classList.add('on', 'boot');
  document.body.classList.add('booting');
  setStatus(text.replace(/…\s*\d+s$/, ''), 'busy');
}

function bootHint(text) {
  const el = $('bootHint');
  if (el) el.textContent = text;
}

async function waitForService() {
  const started = Date.now();
  let hinted = false;

  for (;;) {
    const elapsed = (Date.now() - started) / 1000;
    try {
      const { status, data } = await API.state();

      if (status === 503 && data.mode === 'setup') {
        showSetup(data.setup);
        await sleep(600);
        continue;
      }

      if (status === 503 && data.loading) {
        hideSetup();
        bootText(`正在加载模型… ${elapsed.toFixed(0)}s`);
        if (!hinted && elapsed > 6) {
          hinted = true;
          bootHint('首次启动要加载三个模型，大约需要 15 秒，之后每张图只需几秒。');
        }
        await sleep(600);
        continue;
      }

      if (status >= 400 || !data.ok) {
        throw new Error(data.error || `服务返回 ${status}`);
      }
      hideSetup();
      return data;
    } catch (err) {
      // 服务刚起、端口还没通时 fetch 会直接抛错，这里继续等
      if (elapsed < 90) {
        bootText(`正在连接本地服务… ${elapsed.toFixed(0)}s`);
        if (!hinted && elapsed > 6) {
          hinted = true;
          bootHint('如果一直停在这里，请查看启动窗口里的提示。');
        }
        await sleep(700);
        continue;
      }
      throw err;
    }
  }
}

(async function boot() {
  bindSetup();
  bindAbout();
  bootText('正在连接本地服务…');
  try {
    const data = await waitForService();
    applyBrand(data.brand);
    state.version = data.version || '';
    renderAboutVersion();
    state.catalog = data.catalog;
    state.presets = data.presets || [];
    state.customSizes = data.custom_sizes || [];
    const preset = state.presets.find(p => p.id === data.default_preset) || state.presets[0];
    if (preset) {
      state.size = preset.size;
      state.color = preset.background;
      state.sheet = preset.sheet || state.catalog.sheets[0].name;
      state.mode = preset.sheet ? 'sheet' : 'single';
      $('modes').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.mode === state.mode));
    } else if (state.catalog.photos.length) {
      state.size = state.catalog.photos[0].name;
      state.color = state.catalog.colors[0].name;
      state.sheet = state.catalog.sheets[0].name;
    }
    $('addr').textContent = location.host;

    renderRail();
    renderHints();      // 清单先出骨架，后面 renderSpecs/renderColors 才会算到它
    renderSpecs();
    renderColors();
    renderSheets();
    recomputeLayout();
    bind();
    $('run').disabled = true;
    $('sub').textContent = '先拖入一张照片';
    setStatus('模型已就绪', '');
    renderSteps();

    $('overlay').classList.remove('on', 'boot');
    document.body.classList.remove('booting');
    bootHint('');
  } catch (err) {
    hideSetup();
    setStatus('服务不可用', 'err');
    bootText('启动失败：' + (err.message || err));
    $('overlay').classList.remove('boot');
    document.body.classList.remove('booting');
    bootHint('请查看启动窗口里的红色提示，或 user-data\logs\webui.log');
  }
})();
