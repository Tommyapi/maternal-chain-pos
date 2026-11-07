/* 萌贝管理后台 —— 全部接口走相对路径 api/...，经 nginx 反代 /shouyin/ 后也能用 */
'use strict';
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fx = n => Number(n || 0).toFixed(2);
const money = n => '¥' + Number(n || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const localDate = (d) => { d = d || new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
const addDays = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return localDate(d); };
const S = { me: null, page: '', stores: [], cfg: null, modals: [] };

function toast(msg, type) {
  const t = $('toast'); t.textContent = msg; t.className = 'toast show ' + (type || '');
  clearTimeout(t._t); t._t = setTimeout(() => { t.className = 'toast'; }, 2800);
}
async function api(path, opts) {
  opts = opts || {};
  const init = { credentials: 'same-origin', headers: {} };
  if (opts.form) { init.method = 'POST'; init.body = opts.form; }
  else if (opts.body !== undefined) { init.method = opts.method || 'POST'; init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
  else if (opts.method) init.method = opts.method;
  let r;
  try { r = await fetch(path, init); } catch (e) { throw new Error('网络连接失败，请检查网络'); }
  if (r.status === 401) { showLogin(); throw new Error('请先登录'); }
  let d = {};
  try { d = await r.json(); } catch (e) { throw new Error('服务器返回异常（' + r.status + '）'); }
  if (!r.ok || d.ok === false) { const e = new Error(d.error || ('请求失败 ' + r.status)); e.status = r.status; throw e; }
  return d;
}
const qs = o => Object.keys(o).filter(k => o[k] !== '' && o[k] != null).map(k => encodeURIComponent(k) + '=' + encodeURIComponent(o[k])).join('&');
/* v2：后台一律按权限码判断（AdminShell.can），不再看 v1 粗角色 admin/manager/cashier/staff。
   收银台也要用的只读码：只有这些（加 pos./app./platform. 前缀的码）不算后台权限，等于收银员，不能进后台 */
const POS_ONLY_PERMS = ['goods.view', 'stock.view', 'member.view', 'member.stored.view'];
const hasBackendPerm = me => (me.perms || []).some(c => !/^(pos|app|platform)\./.test(c) && POS_ONLY_PERMS.indexOf(c) < 0);

/* ---------------- 弹窗 ---------------- */
function modal(title, html, onOk, opt) {
  opt = opt || {};
  const root = document.createElement('div'); root.className = 'mask';
  const extra = (opt.extra || []).map((x, i) => `<button class="btn" data-ex="${i}">${esc(x.text)}</button>`).join('');
  root.innerHTML = `<div class="modal ${opt.wide || ''}"><div class="mh"><h3>${esc(title)}</h3><button class="x" data-close>×</button></div><div class="mb">${html}</div>
    <div class="mf">${extra}<button class="btn" data-close>${onOk ? '取消' : '关闭'}</button>${onOk ? `<button class="btn pri" data-ok>${esc(opt.okText || '保存')}</button>` : ''}</div></div>`;
  $('modal-root').appendChild(root);
  const m = { root, close() { root.remove(); S.modals = S.modals.filter(x => x !== m); } };
  S.modals.push(m);
  root.querySelectorAll('[data-close]').forEach(b => b.onclick = () => m.close());
  root.querySelectorAll('[data-ex]').forEach(b => b.onclick = () => opt.extra[+b.dataset.ex].fn(m));
  const ok = root.querySelector('[data-ok]');
  if (ok) ok.onclick = async () => {
    ok.disabled = true;
    try { await onOk(m); } catch (e) { const er = root.querySelector('.err'); if (er) er.textContent = e.message; else toast(e.message, 'err'); }
    ok.disabled = false;
  };
  if (opt.onOpen) opt.onOpen(root, m);
  return m;
}
document.addEventListener('keydown', e => { if (e.key === 'Escape' && S.modals.length) S.modals[S.modals.length - 1].close(); });
const errBox = '<div class="err"></div>';
const fld = (label, html, hint) => `<div class="fi"><label>${label}</label>${html}${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;
const inp = (id, v, extra) => `<input id="${id}" value="${esc(v == null ? '' : v)}" ${extra || ''}>`;
const sel = (id, opts, cur) => `<select id="${id}">${opts.map(o => { const v = Array.isArray(o) ? o[0] : o, t = Array.isArray(o) ? o[1] : o; return `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`; }).join('')}</select>`;
const v = id => ($(id) ? $(id).value.trim() : '');

function pager(d, goto) {
  if (d.pages <= 1 && d.total <= d.pageSize) return `<div class="pager"><span>共 ${d.total} 条</span></div>`;
  return `<div class="pager"><span>共 ${d.total} 条 · 第 ${d.page}/${d.pages} 页</span><div class="btns">
    <button class="btn sm" data-pg="1" ${d.page <= 1 ? 'disabled' : ''}>首页</button><button class="btn sm" data-pg="${d.page - 1}" ${d.page <= 1 ? 'disabled' : ''}>上一页</button>
    <button class="btn sm" data-pg="${d.page + 1}" ${d.page >= d.pages ? 'disabled' : ''}>下一页</button><button class="btn sm" data-pg="${d.pages}" ${d.page >= d.pages ? 'disabled' : ''}>末页</button></div></div>`;
}
function bindPager(box, goto) { box.querySelectorAll('[data-pg]').forEach(b => b.onclick = () => goto(+b.dataset.pg)); }
const storeOpts = (all, label) => (all ? [['', label || '全部门店']] : []).concat(S.stores.map(s => [s.code, s.name]));

/* ---------------- 登录 / 启动 ---------------- */
function showLogin() { $('login').classList.remove('hidden'); $('app').classList.add('hidden'); setTimeout(() => $('l-user').focus(), 50); }
async function doLogin() {
  const u = $('l-user').value.trim(), p = $('l-pass').value;
  if (!u || !p) { $('l-err').textContent = '请填写账号和密码'; return; }
  try {
    const r = await fetch('api/login', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: p }) });
    const d = await r.json();
    if (!r.ok || !d.ok) { $('l-err').textContent = d.error || '登录失败'; $('l-pass').value = ''; return; }
    $('l-err').textContent = ''; $('l-pass').value = '';
    await boot(!!(d.data && d.data.must_change_pw));
  } catch (e) { $('l-err').textContent = '网络错误'; }
}
$('l-btn').onclick = doLogin;
['l-user', 'l-pass'].forEach(id => $(id).addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); }));

const TITLES = {};
function buildNav() {
  let html = '';
  // v2：模块页面（static/admin-<模块>.js 用 AdminShell.register 注册，按权限码显示）
  let lastGrp = '';
  EXT_PAGES.forEach(p => {
    if (p.perm && !(S.me.perms || []).some(x => [].concat(p.perm).indexOf(x) >= 0)) return;
    TITLES[p.key] = p.title;
    if (p.group !== lastGrp) { html += `<div class="grp">${esc(p.group)}</div>`; lastGrp = p.group; }
    html += `<a data-p="${esc(p.key)}"><span>${p.icon || '•'}</span>${esc(p.title)}</a>`;
  });
  $('nav').innerHTML = html;
  $('nav').querySelectorAll('a').forEach(a => a.onclick = () => { go(a.dataset.p); $('side').classList.remove('open'); });
}
/* v2 扩展点：模块 agent 只改自己的 static/admin-<模块>.js，在里面调用
   AdminShell.register({key:'goods2', title:'商品档案', icon:'🍼', group:'商品与库存', perm:'goods.view', render: async box => {...}})
   可直接使用本文件的 api/modal/fld/inp/sel/v/esc/money/qs/toast/pager/bindPager/S 等全局函数。 */
const EXT_PAGES = [];
const AdminShell = {
  register(p) {
    const i = EXT_PAGES.findIndex(x => x.key === p.key);
    if (i >= 0) EXT_PAGES.splice(i, 1);
    EXT_PAGES.push(p); PAGES[p.key] = p.render;
    if (S.me) buildNav();
  },
  can(perm) { return !!(S.me && (S.me.perms || []).indexOf(perm) >= 0); },
};
window.AdminShell = AdminShell;
function noBackend(me) {
  $('login').classList.add('hidden');
  $('app').classList.add('hidden');
  const plat = me.is_platform;
  document.body.insertAdjacentHTML('beforeend', `<div style="padding:60px 20px;text-align:center"><h2>${plat ? '平台运营账号请使用平台后台' : '当前账号没有管理后台权限'}</h2><p style="margin:14px 0;color:#666">${plat ? '平台账号不能查看商户经营数据。' : '收银员请使用收银台；需要后台权限请联系管理员分配角色。'}</p><a class="btn pri" href="${plat ? 'platform' : './'}">${plat ? '去平台后台' : '去收银台'}</a> <button class="btn" onclick="fetch('api/logout',{method:'POST'}).then(()=>location.reload())">换账号</button></div>`);
}
async function boot(mustChangePw) {
  let me;
  try { me = await api('api/me'); } catch (e) { return; }
  me = me.data || me;
  S.me = me;
  if (me.is_platform || !hasBackendPerm(me)) { noBackend(me); return; }
  $('login').classList.add('hidden'); $('app').classList.remove('hidden');
  $('who').textContent = me.name + ' · ' + me.role_name + (me.store_name ? ' · ' + me.store_name : '');
  $('f-user').textContent = me.username;
  // 门店：api/me 只给编码（已按数据范围过滤）；有 org.view 再补门店名，失败不阻塞
  S.stores = (me.stores || []).map(c => ({ code: c, name: c }));
  if (AdminShell.can('org.view')) {
    try { const st = (await api('api/org/stores')).data || []; S.stores = S.stores.map(s => { const x = st.find(y => y.code === s.code); return x ? { code: s.code, name: x.name } : s; }); } catch (e) { }
  }
  // 收银参数：只有 sys.config 能读；读不到用空对象，各页面自带默认值
  S.cfg = {};
  if (AdminShell.can('sys.config')) { try { S.cfg = (await api('api/sys/config')).data.config || {}; } catch (e) { } }
  buildNav();
  const hash = (location.hash || '').replace('#', '');
  const first = ($('nav').querySelector('a') || { dataset: {} }).dataset.p;
  go(TITLES[hash] ? hash : first);
  if (mustChangePw) pwModal(true);
}
$('menu-btn').onclick = () => $('side').classList.toggle('open');
$('out-btn').onclick = async () => { await fetch('api/logout', { method: 'POST', credentials: 'same-origin' }); location.reload(); };
function pwModal(force) {
  modal(force ? '首次登录请先修改密码' : '修改密码', (force ? '<div class="alert">管理员重置或新建的账号，必须先改成自己的密码再使用。</div>' : '') + fld('原密码', inp('pw-o', '', 'type="password"')) + fld('新密码（至少 8 位）', inp('pw-n', '', 'type="password"')) + errBox, async m => {
    await api('api/password', { body: { old: $('pw-o').value, new: $('pw-n').value } }); m.close(); toast('密码已修改', 'ok');
  });
}
$('pw-btn').onclick = () => pwModal(false);

function go(p) {
  S.page = p; location.hash = p;
  $('title').textContent = TITLES[p] || '';
  $('nav').querySelectorAll('a').forEach(a => a.classList.toggle('on', a.dataset.p === p));
  const box = $('content'); box.innerHTML = '<div class="empty">加载中…</div>';
  if (!PAGES[p]) { box.innerHTML = '<div class="card"><div class="empty">当前账号没有可用的后台菜单</div></div>'; return; }
  PAGES[p](box).catch(e => { box.innerHTML = `<div class="card"><div class="empty">${esc(e.message)}</div></div>`; });
}

/* ================= 页面注册表（全部由 static/admin-<模块>.js 用 AdminShell.register 注册） ================= */
const PAGES = {};
/* 多行文本粘贴解析（采购/库存页面用）：每行用逗号或 Tab 分隔，按 cols 取字段 */
function parseLines(text, cols) {
  return text.split(/\n+/).map(l => l.trim()).filter(Boolean).map(l => { const a = l.split(/[,，\t]+/).map(x => x.trim()); const o = {}; cols.forEach((c, i) => { o[c] = a[i] || ''; }); return o; });
}

api('api/me').then(boot).catch(() => { });
