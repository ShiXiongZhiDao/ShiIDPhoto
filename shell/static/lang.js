/* 界面语言运行时。
 *
 * 形状和后端那条出口一致：**中文原串就是词条表的键**，中文态完全不查表，
 * 所以"中文界面一字未变"不是靠测试守着，是结构上碰不到。
 * 切语言 = 还原原文 → 扫一遍 DOM → 按整句模板替换；节点原文存在 WeakMap 里，
 * 所以能来回切，不用刷新页面，也不丢已经载入的照片（决策 4 的"即时生效"）。
 *
 * 不去改 app.js 里那 180 个字面量：那些字符串的赋值点散在全文件，改一遍的风险
 * 远大于收益；边界扫描只认"整句等于词条"或"整句匹配模板"，认不动就原样留着中文。
 */
const I18N = {
  en: {},
  lang: 'zh',
  intent: 'auto',
  originalText: new WeakMap(),
  originalAttr: new WeakMap(),
  pairs: [],
  patterns: null,
  app: null,          // app.js 末尾挂上来的 {state, render}
  title: '',
  loaded: false,      // 词条表是否已经到手
  pending: null,      // boot 比词条表早到时，把那一次切换挂在这里
};

const HAN = /[㐀-䶿一-鿿]/;
const LITE_ATTRS = ['placeholder', 'title', 'alt', 'aria-label'];

/* ---------- 词条与模板 ---------- */

async function loadLexicon() {
  try {
    const r = await fetch('/static/i18n.json', { cache: 'no-store' });
    if (r.ok) {
      const doc = await r.json();
      I18N.en = doc && doc.en ? doc.en : {};
    }
  } catch (err) {
    /* 词条表拿不到就一直是中文：不拦启动，也不白屏（既有约定的失败形状） */
  }
  I18N.patterns = null;
}

function escapeRe(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function buildPatterns() {
  const pairs = [];
  for (const zh of Object.keys(I18N.en)) {
    if (!zh.includes('{x}')) continue;
    const parts = zh.split('{x}').map(escapeRe);
    pairs.push([new RegExp('^' + parts[0] + parts.slice(1).map(p => '(.*?)' + p).join('') + '$', 's'),
                I18N.en[zh]]);
  }
  // 占位符少的先试，同长度按模板更长者先，避免短模板把长句吞掉
  pairs.sort((a, b) => (a[0].source.split('(.*?)').length - b[0].source.split('(.*?)').length)
             || (b[0].source.length - a[0].source.length));
  I18N.patterns = pairs;
}

/* 插值进来的规格名/底色名换成显示名，否则英文句子里会夹 `(大一寸)`。
 * canonical 值本身没动（决策 8），这里只动最终显示的那串字。 */
function rebuildValuePairs() {
  const pairs = [];
  const cat = I18N.app && I18N.app.state ? I18N.app.state.catalog : null;
  for (const list of (cat ? [cat.photos, cat.colors, cat.sheets] : [])) {
    for (const item of (list || [])) {
      if (item.name && item.label && item.name !== item.label) pairs.push([item.name, item.label]);
    }
  }
  pairs.push(['抠图', 'cutout'], ['人脸', 'face'], ['姿态', 'pose']);
  pairs.sort((a, b) => b[0].length - a[0].length);  // `大一寸` 必须抢在 `一寸` 前面替
  I18N.pairs = pairs;
}

function localizeValues(text) {
  let outText = text;
  for (const [zh, en] of I18N.pairs) {
    if (outText.includes(zh)) outText = outText.split(zh).join(en);
  }
  return outText;
}

function out(text, depth) {
  if (I18N.lang !== 'en' || !text) return text;
  if (I18N.patterns === null) buildPatterns();
  const exact = I18N.en[text];
  if (exact !== undefined) return localizeValues(exact);
  const d = depth || 0;
  for (const [re, en] of I18N.patterns) {
    const m = re.exec(text);
    if (m) {
      let i = 1;
      // 捕获组可能本身又是一条拼接出来的消息（提示：+ 正文），递归翻，
      // 否则会出现"前缀英文、正文中文"那种半截态
      const fill = () => {
        const g = m[i++];
        return g === undefined ? '' : (d > 3 ? g : out(g, d + 1));
      };
      return localizeValues(en.replace(/\{x\}/g, fill));
    }
  }
  return text;
}

/* ---------- DOM 扫描与还原 ---------- */

function sweepNode(node) {
  if (node.nodeType === Node.TEXT_NODE) {
    const src = node.nodeValue;
    if (!src || !src.trim()) return;
    if (!I18N.originalText.has(node)) I18N.originalText.set(node, src);
    const raw = I18N.originalText.get(node);
    const core = raw.trim();          // HTML 里 <button>\n  生成证件照\n</button> 带着首尾空白，
    let wanted = raw;                 // 整串去查词条永远查不到（扫过却翻不动就是这条）
    if (I18N.lang === 'en' && core) {
      const tr = out(core);
      if (tr !== core) wanted = raw.replace(core, tr);
    }
    if (node.nodeValue !== wanted) node.nodeValue = wanted;   // 同值不写，免得观察器死循环
    return;
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return;
  for (const attr of LITE_ATTRS) {
    if (!node.hasAttribute(attr)) continue;
    let store = I18N.originalAttr.get(node);
    if (!store) { store = {}; I18N.originalAttr.set(node, store); }
    const value = node.getAttribute(attr);
    if (!(attr in store)) store[attr] = value;
    const wanted = I18N.lang === 'en' ? out(store[attr]) : store[attr];
    if (node.getAttribute(attr) !== wanted) node.setAttribute(attr, wanted);
  }
  const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  let cur = walker.nextNode();
  while (cur) {
    if (cur !== node) sweepNode(cur);
    cur = walker.nextNode();
  }
}

function sweepAll() {
  rebuildValuePairs();
  sweepNode(document.body);
  I18N.title = I18N.title || document.title;
  const next = I18N.lang === 'en' ? out(I18N.title) : I18N.title;
  if (document.title !== next) document.title = next;
}

/* ---------- 切换 ---------- */

function setLanguage(lang, intent) {
  I18N.lang = lang === 'en' ? 'en' : 'zh';
  if (intent) I18N.intent = intent;
  document.documentElement.lang = I18N.lang === 'en' ? 'en' : 'zh-CN';
  syncControl();
  // 规格名/色名走的是 catalog 的 label，那是 app.js 渲染出来的——只重扫 DOM 翻不动契约值。
  // 词条表比 boot 晚到手时，这一句就是让卡片按新语言重画一遍。
  if (I18N.app && I18N.app.render) {
    try {
      I18N.app.render();
    } catch (err) {
      /* boot 还没走完时渲染会缺数据；等 boot 自己再渲染一次即可 */
    }
  }
  sweepAll();
}

/* 界面语言 = 三态单选 + auto 态那行"当前显示…"（决策 6）。
   控件由这里建，不去 index.html 里加行——那个文件正被另一批改动占着。
   逐节点建、逐节点 setText：用 innerHTML 拼字符串的话，抽取器只看到整段模板，
   `跟随系统` 这些字面量进不了词条表，运行时也就匹配不上（踩过一次）。 */
const LANG_CHOICES = [['auto', '跟随系统'], ['zh', '中文'], ['en', 'English']];

function makeLangRow(scope) {
  const row = document.createElement('div');
  row.className = 'langrow sec';
  row.dataset.langRow = scope;
  const key = document.createElement('span');
  key.className = 'k';
  key.textContent = '界面语言';
  const seg = document.createElement('span');
  seg.className = 'seg';
  seg.dataset.langSeg = scope;
  for (const [value, label] of LANG_CHOICES) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.dataset.lang = value;
    btn.textContent = label;
    seg.appendChild(btn);
  }
  const note = document.createElement('div');
  note.className = 'langnote';
  note.dataset.langNote = scope;
  note.hidden = true;
  row.appendChild(key);
  row.appendChild(seg);
  seg.addEventListener('click', chooseLanguage);
  return { row, note };
}

/* 真正做切换的那一步：存盘 → 本地换语言 → 重取目录（label 是服务端按语言给的）→ 重渲染 */
async function pickLanguage(value) {
  try {
    await fetch('/api/prefs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ language: value }),
    });
  } catch (err) {
    /* 存不下时界面照样切，服务端那条说明会出现在 prefsWarn 里 */
  }
  // 顺序不能反：先问服务端"这次生效的是哪种语言"，再按它渲染。
  // 之前先 setLanguage(旧 effective) 再重取目录，切到 auto 时出现过
  // "英文 chrome + 中文卡片"的劈开态——正是这套设计要防的那一种。
  let fresh = null;
  try {
    const r = await fetch('/api/state');
    fresh = await r.json();
    I18N.effectiveFromServer = fresh.language_effective || I18N.effectiveFromServer;
  } catch (err) {
    fresh = null;   // 取不到就按显式档位切，目录留旧 label，至少不会中英劈开
  }
  const effective = value === 'auto' ? (I18N.effectiveFromServer || 'zh') : value;
  setLanguage(effective, value);
  if (I18N.app && I18N.app.state && fresh && fresh.catalog) {
    I18N.app.state.catalog = fresh.catalog;
  }
  if (I18N.app && I18N.app.render) {
    try {
      I18N.app.render();
    } catch (err) { /* boot 还没走完时渲染会缺数据，等 boot 自己渲染 */ }
    sweepAll();
  }
  syncControl();
  if (window.toast) {
    // toast 里给"切到哪一档"，auto 档再补一句当前实际显示的语言（决策 6）
    const pair = LANG_NAMES[I18N.intent] || LANG_NAMES.auto;
    const base = I18N.lang === 'en' ? pair[1] : pair[0];
    const autoNote = I18N.lang === 'en' ? '当前显示英文（跟随系统）' : '当前显示中文（跟随系统）';
    toast(I18N.intent === 'auto' ? out('跟随系统') + ' · ' + out(autoNote) : base);
  }
}

function chooseLanguage(ev) {
  const btn = ev.target.closest('button[data-lang]');
  if (!btn) return;
  pickLanguage(btn.dataset.lang);
}

const LANG_ORDER = ['auto', 'zh', 'en'];

function buildControl() {
  /* 入口在左侧栏（railLang）：三态靠点击轮换，当前档写进 title 与 toast。
     右侧 inspector 不放这一行——rail 是"界面级开关"那一层，深浅开关也在那儿。 */
  const rail = document.getElementById('railLang');
  if (rail && !rail.dataset.langBound) {
    rail.dataset.langBound = '1';
    rail.addEventListener('click', () => {
      const next = LANG_ORDER[(LANG_ORDER.indexOf(I18N.intent) + 1) % LANG_ORDER.length];
      pickLanguage(next);
    });
  }
  /* 首次启动设置向导里给一排明确的选项（决策 5）：同一个 prefs 键，安装态才看得见 */
  if (!document.querySelector('[data-lang-row]')) {
    const firstPlate = document.querySelector('#setup .print');
    if (firstPlate && firstPlate.parentElement) {
      const setup = makeLangRow('setup');
      firstPlate.parentElement.insertBefore(setup.row, firstPlate);
      firstPlate.parentElement.insertBefore(setup.note, firstPlate);
    }
  }
  syncControl();
}

function syncControl() {
  document.querySelectorAll('[data-lang-seg]').forEach(seg =>
    seg.querySelectorAll('button').forEach(b =>
      b.classList.toggle('on', b.dataset.lang === I18N.intent)));
  document.querySelectorAll('[data-lang-note]').forEach(note => {
    // 显式选了中文/英文就没有"解析结果"可报，那行字留着会让人以为没生效（决策 6）
    if (I18N.intent !== 'auto') { note.hidden = true; return; }
    note.hidden = false;
    const shown = I18N.lang === 'en' ? '当前显示英文（跟随系统）' : '当前显示中文（跟随系统）';
    note.textContent = I18N.lang === 'en' ? out(shown) : shown;
  });
  const rail = document.getElementById('railLang');
  if (rail) {
    const wanted = railTip();
    if (rail.getAttribute('title') !== wanted) rail.setAttribute('title', wanted);
  }
}

/* 语言档位的显示名：'中文' 与 'English' 是语言自己的名字，两种界面里都照写（skip 里登过） */
const LANG_NAMES = { auto: ['跟随系统', 'Follow system'], zh: ['中文', '中文'], en: ['English', 'English'] };

function railTip() {
  const pair = LANG_NAMES[I18N.intent] || LANG_NAMES.auto;
  const label = I18N.lang === 'en' ? pair[1] : pair[0];
  return out('界面语言') + (I18N.lang === 'en' ? ': ' : '：') + label;
}

/* ---------- 接线 ---------- */

function observeRenders() {
  // 后续动态文案（toast、状态条、结果元信息）也是 JS 写进去的，跟着扫
  const observer = new MutationObserver(records => {
    for (const rec of records) {
      const target = rec.target.nodeType === Node.ELEMENT_NODE
        ? rec.target : rec.target.parentElement;
      if (target) sweepNode(target);
    }
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true });
}

async function startI18N() {
  I18N.title = document.title;
  await loadLexicon();
  buildControl();
  observeRenders();
  I18N.loaded = true;
  // 词条表到手前 boot 可能已经报过语言了（那时 en 是空的，扫了等于没扫）
  if (I18N.pending) { I18N.pending(); I18N.pending = null; }
}

/* app.js 的 boot 拿到 /api/state 之后调这里（那边只加了两行） */
function applyServerLanguage(data, app) {
  I18N.app = app;
  I18N.intent = data.language || 'auto';
  I18N.effectiveFromServer = data.language_effective || 'zh';
  const apply = () => setLanguage(data.language_effective || 'zh', I18N.intent);
  if (I18N.loaded) apply();
  else I18N.pending = apply;   // 等 startI18N 把词条表读回来再扫
}

window.I18N = I18N;
window.startI18N = startI18N;
window.applyServerLanguage = applyServerLanguage;
window.i18nOut = out;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => { startI18N(); });
} else {
  startI18N();
}
