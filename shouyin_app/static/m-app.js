/* 萌贝 · 门店助手（手机端 H5 / PWA）。原生 JS，无构建、无外部依赖；所有请求走相对路径（nginx 反代 /shouyin/）。
 * 页面（hash 路由）：#home 首页 · #dash 经营看板 · #alerts/<类> 库存预警明细 · #store 门店经营 · #goods 商品库存查询
 *   #todo/<页签> 待办与消息 · #approval/<单号> 审批 · #docs/<页签> 店务单据 · #newdoc/<类型> 新建单据 · #transfer/<单号> 调拨收货
 *   #take 盘点任务 · #takeone/<单号> 扫码盘点 · #notice 公告 · #noticeone/<id> · #publish 发公告 · #order/<单号> · #me 我的 · #rules 提醒规则
 * 库存类单据调用模块①的 api/stock/*；接口未上线（404 接口不存在 / 501）时页面给出降级提示，不在手机端写库存。
 */
(function () {
  'use strict';
  var APP_VERSION = '3.0.0';
  var S = { me: null, home: null, scopes: null, scope: '', period: 'day', date: '', rankDim: 'store', todoCat: '' };
  var $app = document.getElementById('app');
  var $tab = document.getElementById('tabbar');

  // ---------------------------------------------------------------- 工具
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function money(v) {
    if (v === null || v === undefined || v === '') return '—';
    var n = Number(v);
    return (n < 0 ? '-¥' : '¥') + Math.abs(n).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }
  function pct(v, label) {
    if (v === null || v === undefined) return '<em>' + esc(label || '') + ' —</em>';
    var cls = v > 0 ? 'up' : (v < 0 ? 'down' : '');
    return '<em class="' + cls + '">' + esc(label || '') + ' ' + (v > 0 ? '↑' : (v < 0 ? '↓' : '')) + Math.abs(v) + '%</em>';
  }
  function qs(o) {
    var a = [];
    Object.keys(o || {}).forEach(function (k) {
      if (o[k] !== '' && o[k] !== null && o[k] !== undefined) a.push(encodeURIComponent(k) + '=' + encodeURIComponent(o[k]));
    });
    return a.length ? '?' + a.join('&') : '';
  }
  function today() {
    var d = new Date();
    return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
  }
  var toastTimer = null;
  function toast(msg) {
    var t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'on';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = ''; }, 2200);
  }
  function $(sel, root) { return (root || $app).querySelector(sel); }
  // 异步结果写回：页面已切走（序号变了）或节点不在了就丢弃，防止旧范围的数据画到新页面上
  function fill(sel, html, seq) {
    if (seq !== undefined && seq !== S.seq) return null;
    var n = $(sel);
    if (n) n.innerHTML = html;
    return n;
  }
  function $$(sel, root) { return Array.prototype.slice.call((root || $app).querySelectorAll(sel)); }
  function on(sel, ev, fn, root) { $$(sel, root).forEach(function (n) { n.addEventListener(ev, fn); }); }
  function go(link) {
    if (!link) return;
    var i = link.indexOf('#');
    location.hash = i >= 0 ? link.slice(i) : '#' + link;
  }
  function lsGet(k, d) { try { var v = localStorage.getItem('m.' + k); return v ? JSON.parse(v) : d; } catch (e) { return d; } }
  function lsSet(k, v) { try { localStorage.setItem('m.' + k, JSON.stringify(v)); } catch (e) { /* 隐私模式 */ } }
  function lsDel(k) { try { localStorage.removeItem('m.' + k); } catch (e) { /* ignore */ } }

  // APP-405：会话失效 / 退出时清空本地缓存（盘点草稿、筛选、离线缓存）
  function clearLocal() {
    try {
      Object.keys(localStorage).forEach(function (k) { if (k.indexOf('m.') === 0) localStorage.removeItem(k); });
    } catch (e) { /* ignore */ }
    if (window.caches && caches.keys) {
      caches.keys().then(function (ks) { ks.forEach(function (k) { caches.delete(k); }); }).catch(function () {});
    }
    S.me = null; S.home = null; S.scopes = null; S.scope = '';
  }

  // ---------------------------------------------------------------- 接口
  function api(path, opt) {
    opt = opt || {};
    var init = { method: opt.method || (opt.body !== undefined ? 'POST' : 'GET'), credentials: 'same-origin', cache: 'no-store', headers: {} };
    if (opt.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opt.body);
    }
    var seq0 = S.seq, keep = opt.keep || opt.body !== undefined;
    return fetch(path, init).then(function (r) {
      return r.json().catch(function () { return { ok: false, msg: '服务器返回异常（' + r.status + '）' }; }).then(function (j) {
        if (r.status === 401 && !opt.noRedirect) {
          clearLocal();
          showLogin('登录已失效，请重新登录');
        }
        if (!j || j.ok === false || r.status >= 400) {
          var e = new Error((j && (j.msg || j.error)) || ('请求失败（' + r.status + '）'));
          e.code = j && j.code; e.status = r.status; e.data = j && j.data;
          throw e;
        }
        if (!keep && seq0 !== S.seq) return new Promise(function () {});   // 页面已切走：丢弃旧结果，防止旧范围数据画到新页面
        return j.data !== undefined ? j.data : j;
      });
    }, function () {
      var e = new Error('网络连接失败，请检查网络后重试');
      e.code = 'NETWORK';
      throw e;
    });
  }
  // 模块①库存接口：接口不存在（404 接口不存在）或未实现（501）= 降级
  function stockApi(path, opt) {
    return api(path, opt).catch(function (e) {
      if (e.status === 501 || (e.status === 404 && /接口不存在/.test(e.message)) || e.status === 405) e.degraded = true;
      throw e;
    });
  }
  function errBox(e) {
    if (e && e.degraded) {
      return '<div class="warnbox">该功能依赖「库存模块」接口，当前服务器版本尚未开放（' + esc(e.message) + '）。上线后此页自动可用，无需更新手机端。</div>';
    }
    return '<div class="warnbox">' + esc(e && e.message || '加载失败') + '</div>';
  }
  function itemsOf(d) { return Array.isArray(d) ? d : ((d && (d.items || d.rows)) || []); }
  function docNo(x) { return x.no || x.req_no || x.tf_no || x.st_no || x.adj_no || x.order_no || x.doc_no || ''; }

  // ---------------------------------------------------------------- 弹层
  function sheet(html) {
    var m = document.createElement('div');
    m.className = 'sheet-mask';
    m.innerHTML = '<div class="sheet">' + html + '</div>';
    document.body.appendChild(m);
    m.addEventListener('click', function (e) { if (e.target === m) close(); });
    function close() { if (m.parentNode) m.parentNode.removeChild(m); }
    return { el: m, close: close };
  }
  function ask(title, opt) {
    opt = opt || {};
    return new Promise(function (resolve) {
      var s = sheet('<h4>' + esc(title) + '</h4><textarea class="inp" placeholder="' + esc(opt.placeholder || '') + '"></textarea>' +
        '<div class="btns"><button class="btn gray" data-x>取消</button><button class="btn" data-ok>' + esc(opt.ok || '确定') + '</button></div>');
      var ta = s.el.querySelector('textarea');
      s.el.querySelector('[data-x]').onclick = function () { s.close(); resolve(null); };
      s.el.querySelector('[data-ok]').onclick = function () {
        var v = ta.value.trim();
        if (opt.required && !v) { toast('请填写' + (opt.label || '内容')); return; }
        s.close(); resolve(v);
      };
      setTimeout(function () { ta.focus(); }, 50);
    });
  }
  function confirmBox(text, ok) {
    return new Promise(function (resolve) {
      var s = sheet('<h4>' + esc(text) + '</h4><div class="btns"><button class="btn gray" data-x>取消</button><button class="btn" data-ok>' +
        esc(ok || '确定') + '</button></div>');
      s.el.querySelector('[data-x]').onclick = function () { s.close(); resolve(false); };
      s.el.querySelector('[data-ok]').onclick = function () { s.close(); resolve(true); };
    });
  }
  function askQty(title, init) {
    return new Promise(function (resolve) {
      var s = sheet('<h4>' + esc(title) + '</h4><div class="qtybox"><button data-m>－</button><input class="inp" type="number" inputmode="decimal" value="' +
        esc(init == null ? 1 : init) + '"><button data-p>＋</button></div><p class="note" style="margin-top:8px">数量按基本单位（罐/包/件）录入；请复核后确认。</p>' +
        '<div class="btns"><button class="btn gray" data-x>取消</button><button class="btn" data-ok>确认</button></div>');
      var inp = s.el.querySelector('input');
      s.el.querySelector('[data-m]').onclick = function () { inp.value = Math.max(0, (Number(inp.value) || 0) - 1); };
      s.el.querySelector('[data-p]').onclick = function () { inp.value = (Number(inp.value) || 0) + 1; };
      s.el.querySelector('[data-x]').onclick = function () { s.close(); resolve(null); };
      s.el.querySelector('[data-ok]').onclick = function () {
        var v = Number(inp.value);
        if (!(v >= 0)) { toast('数量不正确'); return; }
        s.close(); resolve(v);
      };
      setTimeout(function () { inp.select(); }, 50);
    });
  }

  // ---------------------------------------------------------------- 页面骨架
  function head(title, opt) {
    opt = opt || {};
    return '<header><div class="t">' + (opt.back ? '<button class="back" data-back>‹</button>' : '') + '<h1>' + esc(title) + '</h1>' +
      (opt.right || '') + '</div>' + (opt.sub ? '<div class="sub">' + opt.sub + '</div>' : '') + (opt.bar ? '<div class="hbar">' + opt.bar + '</div>' : '') +
      '</header>';
  }
  function render(html) {
    S.seq = (S.seq || 0) + 1;
    $app.innerHTML = html;
    on('[data-back]', 'click', function () { if (history.length > 1) history.back(); else location.hash = '#home'; });
    on('[data-go]', 'click', function (e) { go(e.currentTarget.getAttribute('data-go')); });
    window.scrollTo(0, 0);
  }
  function loading(title, opt) { render(head(title, opt) + '<div class="wrap"><div class="empty">加载中…</div></div>'); }
  function feat(k) { return !!(S.home && S.home.features && S.home.features[k]); }

  function renderTabbar(active) {
    if (!S.me) { $tab.classList.add('hidden'); return; }
    var h = S.home || {};
    var items = [['home', '⌂', '首页']];
    if (feat('dashboard')) items.push(['dash', '▤', '看板']);
    else if (feat('store')) items.push(['store', '▤', '门店']);
    items.push(['todo', '✓', '待办', (h.todo_open || 0) + (h.unread || 0)]);
    items.push(['me', '☺', '我的']);
    $tab.innerHTML = items.map(function (x) {
      return '<a href="#' + x[0] + '" class="' + (x[0] === active ? 'on' : '') + '"><i>' + x[1] + '</i>' + x[2] +
        (x[3] ? '<span class="badge">' + (x[3] > 99 ? '99+' : x[3]) + '</span>' : '') + '</a>';
    }).join('');
    $tab.classList.remove('hidden');
  }
  function refreshHome() {
    return api('api/m/home', { keep: true }).then(function (h) { S.home = h; return h; });
  }
  function scopeArgs() {
    if (!S.scope) return {};
    return S.scope.charAt(0) === 'r' ? { region: S.scope.slice(2) } : { store: S.scope.slice(2) };
  }
  function loadScopes() {
    if (S.scopes) return Promise.resolve(S.scopes);
    return api('api/m/scopes', { keep: true }).then(function (d) { S.scopes = d; return d; });
  }
  function scopeSelect() {
    var sc = S.scopes || { stores: [], regions: [] };
    var opts = [];
    if (sc.all) opts.push(['', '全部门店（总部汇总）']);
    else if (sc.stores.length > 1) opts.push(['', '我负责的全部门店']);
    sc.regions.forEach(function (r) { opts.push(['r:' + r.code, '区域：' + r.name]); });
    sc.stores.forEach(function (s) { opts.push(['s:' + s.code, s.name]); });
    if (!opts.length) return '';
    return '<select data-scope>' + opts.map(function (o) {
      return '<option value="' + esc(o[0]) + '"' + (o[0] === S.scope ? ' selected' : '') + '>' + esc(o[1]) + '</option>';
    }).join('') + '</select>';
  }

  // ---------------------------------------------------------------- 登录 / 启动
  function showLogin(msg) {
    $tab.classList.add('hidden');
    $app.innerHTML = '<div class="login"><h2>门店助手</h2><p>萌贝母婴连锁 · 老板看板与店长店务</p>' +
      (msg ? '<div class="warnbox">' + esc(msg) + '</div>' : '') +
      '<div class="field"><label>账号</label><input class="inp" id="lu" autocomplete="username" autocapitalize="off"></div>' +
      '<div class="field"><label>密码</label><input class="inp" id="lp" type="password" autocomplete="current-password"></div>' +
      '<button class="btn block" id="lb">登录</button><p class="note" style="margin-top:14px">设备丢失可在电脑或其他手机登录后，到「我的 → 登录设备」远程退出。</p></div>';
    var go_ = function () {
      var u = $('#lu').value.trim(), p = $('#lp').value;
      if (!u || !p) { toast('请输入账号和密码'); return; }
      $('#lb').disabled = true;
      api('api/login', { body: { username: u, password: p, client: 'm' }, noRedirect: true }).then(function () {
        if (location.hash === '#login' || !location.hash) history.replaceState(null, '', '#home');
        boot();
      }).catch(function (e) { toast(e.message); $('#lb').disabled = false; });
    };
    $('#lb').onclick = go_;
    $('#lp').addEventListener('keydown', function (e) { if (e.key === 'Enter') go_(); });
  }

  function boot() {
    return api('api/me', { noRedirect: true, keep: true }).then(function (me) {
      if (me.is_platform) {
        $app.innerHTML = '<div class="login"><h2>平台账号</h2><p>手机端只给商户员工使用，请用平台运营后台。</p><button class="btn block" id="lo">退出登录</button></div>';
        $('#lo').onclick = logout;
        return;
      }
      S.me = me;
      return refreshHome().then(function () { route(); });
    }).catch(function (e) {
      if (e.status === 401) { clearLocal(); showLogin(); } else { $app.innerHTML = '<div class="wrap">' + errBox(e) + '<button class="btn block" id="rt">重试</button></div>'; $('#rt').onclick = boot; }
    });
  }

  function logout() {
    api('api/logout', { body: {}, noRedirect: true }).catch(function () {}).then(function () { clearLocal(); location.hash = '#login'; showLogin(); });
  }

  // APP-404：版本检查；低于最低版本且强制 -> 清缓存强制刷新
  function hardReload() {
    var jobs = [];
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
      jobs.push(navigator.serviceWorker.getRegistrations().then(function (rs) { return Promise.all(rs.map(function (r) { return r.unregister(); })); }));
    }
    if (window.caches) jobs.push(caches.keys().then(function (ks) { return Promise.all(ks.map(function (k) { return caches.delete(k); })); }));
    Promise.all(jobs).catch(function () {}).then(function () { location.reload(); });
  }
  function checkVersion(manual) {
    return api('api/m/version' + qs({ v: APP_VERSION }), { noRedirect: true, keep: true }).then(function (d) {
      var b = document.getElementById('banner');
      if (d.must_upgrade) {
        var last = Number(sessionStorage.getItem('m.upg') || 0);
        if (Date.now() - last > 60000) { sessionStorage.setItem('m.upg', String(Date.now())); hardReload(); return d; }
        b.innerHTML = '<div class="banner">当前版本 ' + esc(APP_VERSION) + ' 已停止使用，请联系管理员更新服务器页面</div>';
      } else if (d.outdated) {
        b.innerHTML = '<div class="banner"><span>有新版本 ' + esc(d.version) + (d.notes ? '：' + esc(d.notes) : '') + '</span><button>刷新</button></div>';
        b.querySelector('button').onclick = hardReload;
      } else if (manual) {
        toast('已是最新版本 ' + APP_VERSION);
      }
      return d;
    }).catch(function () {});
  }

  // ---------------------------------------------------------------- 路由
  var routes = {};
  function route() {
    var h = (location.hash || '#home').slice(1);
    var parts = h.split('/');
    var name = parts[0] || 'home';
    var arg = decodeURIComponent(parts.slice(1).join('/'));
    if (name === 'login') { if (S.me) { location.hash = '#home'; return; } showLogin(); return; }
    if (!S.me) { boot(); return; }
    var fn = routes[name] || routes.home;
    renderTabbar({ home: 'home', dash: 'dash', alerts: 'dash', store: feat('dashboard') ? 'home' : 'store', todo: 'todo', approval: 'todo', me: 'me', rules: 'me' }[name] || 'home');
    try { fn(arg); } catch (e) { render(head('出错了', { back: true }) + '<div class="wrap">' + errBox(e) + '</div>'); }
  }
  window.addEventListener('hashchange', route);

  // ---------------------------------------------------------------- 首页
  routes.home = function () {
    var h = S.home || {}, me = S.me || {};
    var tiles = [];
    function t(ok, icon, name, link, badge) { if (ok) tiles.push([icon, name, link, badge]); }
    t(feat('dashboard'), '📊', '经营看板', '#dash');
    t(feat('store'), '🏪', '门店经营', '#store');
    t(feat('goods'), '🔍', '查商品', '#goods');
    t(feat('approval'), '✅', '审批待办', '#todo/approve', h.todo_open);
    t(feat('request'), '📦', '要货', '#docs/request');
    t(feat('receive'), '🚚', '调拨收货', '#docs/receive');
    t(feat('adjust'), '📝', '报损报溢', '#docs/adjust');
    t(feat('take'), '📋', '移动盘点', '#take');
    t(true, '📢', '公告', '#notice');
    t(true, '💬', '消息', '#todo/msg', h.unread);
    t(feat('notify_rules'), '⚙️', '提醒设置', '#rules');
    render(head('门店助手', { sub: esc(me.name) + ' · ' + esc(me.role_name || '') + (me.store_name ? ' · ' + esc(me.store_name) : '') }) +
      '<div class="wrap">' + (h.risk_unread ? '<div class="warnbox" data-go="#todo/risk">⚠️ 有 ' + h.risk_unread + ' 条高风险告警未查看，点此处理</div>' : '') +
      '<div id="hk"></div><div class="tiles">' + tiles.map(function (x) {
        return '<a class="tile" href="' + x[2] + '"><i>' + x[0] + '</i>' + esc(x[1]) + (x[3] ? '<span class="badge">' + x[3] + '</span>' : '') + '</a>';
      }).join('') + '</div></div>');
    if (feat('dashboard')) {
      api('api/m/dashboard').then(function (d) {
        var c = d.current, p = d.compare.prev || {};
        $('#hk') && ($('#hk').innerHTML = '<div class="card" data-go="#dash"><h3>今日经营 <small>' + esc(d.scope.label) + '</small></h3><div class="g4">' +
          '<div class="stat"><b class="num">' + money(c.sales) + '</b><span>销售额</span></div><div class="stat"><b>' + c.orders + '</b><span>订单</span></div>' +
          '<div class="stat"><b class="num">' + money(c.avg) + '</b><span>客单价</span></div><div class="stat"><b class="num">' + money(c.refund) + '</b><span>退款</span></div>' +
          '</div><p class="note" style="margin-top:8px">' + (p.pct ? pct(p.pct.sales, '较昨日') : '') + ' · 更新于 ' + esc(d.updated_at.slice(11, 16)) + '</p></div>');
        on('[data-go]', 'click', function (e) { go(e.currentTarget.getAttribute('data-go')); }, $('#hk'));
      }).catch(function () {});
    }
  };

  // ---------------------------------------------------------------- 经营看板（APP-001~005）
  routes.dash = function () {
    if (!feat('dashboard')) { render(head('经营看板', { back: true }) + '<div class="wrap"><div class="warnbox">没有看板权限</div></div>'); return; }
    S.date = S.date || today();
    loadScopes().then(function () {
      var bar = scopeSelect() + '<div class="seg" data-period>' + [['day', '日'], ['week', '周'], ['month', '月']].map(function (p) {
        return '<button data-p="' + p[0] + '" class="' + (S.period === p[0] ? 'on' : '') + '">' + p[1] + '</button>';
      }).join('') + '</div><input type="date" data-date value="' + esc(S.date) + '">';
      render(head('经营看板', { bar: bar }) + '<div class="wrap" id="dw"><div class="empty">加载中…</div></div>');
      var sel = $('[data-scope]');
      if (sel) sel.onchange = function () { S.scope = sel.value; routes.dash(); };
      on('[data-p]', 'click', function (e) { S.period = e.currentTarget.getAttribute('data-p'); routes.dash(); });
      $('[data-date]').onchange = function (e) { S.date = e.target.value || today(); routes.dash(); };
      var args = Object.assign({ period: S.period, date: S.date }, scopeArgs());
      Promise.all([api('api/m/dashboard' + qs(args)), api('api/m/trend' + qs(args)), api('api/m/overview-stock-member' + qs(args))]).then(function (r) {
        drawDash(r[0], r[1], r[2], args);
      }).catch(function (e) { fill('#dw', errBox(e)); });
    }).catch(function (e) { render(head('经营看板') + '<div class="wrap">' + errBox(e) + '</div>'); });
  };

  function drawDash(d, tr, ov, args) {
    var c = d.current, p = d.compare.prev, s = d.compare.same;
    var pl = p ? p.label : '', sl = s ? s.label : '';
    function cmp(f) { return (p ? pct(p.pct[f], pl) : '') + (s ? pct(s.pct[f], sl) : ''); }
    var html = '<div class="hero"><div class="kpi main"><span>销售额 · ' + esc(d.range.start === d.range.end ? d.range.start : d.range.start + ' ~ ' + d.range.end) +
      '</span><b class="num">' + money(c.sales) + '</b>' + cmp('sales') + '</div>' +
      '<div class="kpi"><span>订单数</span><b class="num">' + c.orders + '</b>' + cmp('orders') + '</div>' +
      '<div class="kpi"><span>客单价</span><b class="num">' + money(c.avg) + '</b>' + cmp('avg') + '</div>' +
      '<div class="kpi"><span>退款（' + c.refund_orders + ' 单）</span><b class="num">' + money(c.refund) + '</b>' + cmp('refund') + '</div>' +
      '<div class="kpi"><span>净额</span><b class="num">' + money(c.net) + '</b>' + cmp('net') + '</div></div>';
    html += '<p class="note" style="margin:-4px 2px 12px">范围：' + esc(d.scope.label) + '（' + d.scope.store_count + ' 家）· 数据更新 ' + esc(d.updated_at) +
      (d.latest_order_at ? ' · 最新订单 ' + esc(d.latest_order_at) : '') + ' <a data-cal style="color:var(--pri)">统计口径</a></p>';
    // 趋势
    var pts = tr.points, mx = Math.max.apply(null, pts.map(function (x) { return x.sales; }).concat([1]));
    html += '<div class="card"><h3>销售趋势 <small>' + ({ day: '近 14 天', week: '近 12 周', month: '近 12 个月' })[d.period] + '</small></h3><div class="bars">' +
      pts.map(function (x, i) {
        return '<div class="' + (i === pts.length - 1 ? 'on' : '') + '" style="height:' + Math.max(2, Math.round(Math.max(0, x.sales) * 100 / mx)) + '%" title="' +
          esc(x.label + ' ' + money(x.sales)) + '"></div>';
      }).join('') + '</div><div class="hx"><span>' + esc(pts[0].label) + '</span><span>' + esc(pts[pts.length - 1].label) + ' ' + money(pts[pts.length - 1].sales) + '</span></div>';
    if (tr.hours) {
      var hm = Math.max.apply(null, tr.hours.concat([1]));
      html += '<h3 style="margin-top:14px">分时段 <small>' + esc(S.date) + '</small></h3><div class="bars" style="height:80px">' + tr.hours.map(function (v) {
        return '<div style="height:' + Math.max(2, Math.round(Math.max(0, v) * 100 / hm)) + '%"></div>';
      }).join('') + '</div><div class="hx"><span>0点</span><span>12点</span><span>23点</span></div>';
    }
    html += '</div>';
    // 排行
    html += '<div class="card"><h3>排行 <small>净销售额</small></h3><div class="tabs" data-rk>' +
      [['store', '门店'], ['product', '商品'], ['category', '分类'], ['brand', '品牌']].map(function (x) {
        return '<button data-d="' + x[0] + '" class="' + (S.rankDim === x[0] ? 'on' : '') + '">' + x[1] + '</button>';
      }).join('') + '</div><div class="list" id="rk"><div class="empty">加载中…</div></div></div>';
    // 库存与会员
    var st = ov.stock, m = ov.member;
    html += '<div class="card"><h3>库存与会员 <small>临期 ' + ov.days + ' 天内 · 滞销 ' + ov.slow_days + ' 天未动销</small></h3><div class="g4">' +
      [['stock_low', '缺货/低库存', 'warn'], ['near_expiry', '临期批次', 'warn'], ['expired', '过期批次', 'bad'], ['slow', '滞销', '']].map(function (x) {
        return '<div class="stat ' + (st[x[0]].count ? x[2] : '') + '" data-go="#alerts/' + x[0] + '"><b>' + st[x[0]].count + '</b><span>' + x[1] + '</span></div>';
      }).join('') + '</div><div class="g4" style="margin-top:8px"><div class="stat"><b>' + m.new + '</b><span>新增会员</span></div><div class="stat"><b>' + m.buyers +
      '</b><span>消费会员</span></div><div class="stat"><b>' + m.repeat + '</b><span>复购会员</span></div><div class="stat"><b>' +
      (m.repeat_rate == null ? '—' : m.repeat_rate + '%') + '</b><span>复购率</span></div></div></div>';
    if (d.can_profit) html += '<div class="card" id="pf"><h3>毛利与异常 <small>仅有毛利权限可见</small></h3><div class="empty">加载中…</div></div>';
    fill('#dw', html);
    on('[data-go]', 'click', function (e) { go(e.currentTarget.getAttribute('data-go')); }, $('#dw'));
    $('[data-cal]').onclick = function () { sheet('<h4>统计口径</h4>' + d.caliber.map(function (x) { return '<p class="note" style="margin-bottom:8px">' + esc(x) + '</p>'; }).join('') + '<button class="btn block gray" onclick="this.closest(\'.sheet-mask\').remove()">知道了</button>'); };
    on('[data-d]', 'click', function (e) { S.rankDim = e.currentTarget.getAttribute('data-d'); $$('[data-d]').forEach(function (b) { b.classList.toggle('on', b === e.currentTarget); }); loadRank(args); });
    loadRank(args);
    if (d.can_profit) {
      api('api/m/profit' + qs(args)).then(function (pf) {
        $('#pf').innerHTML = '<h3>毛利与异常 <small>' + esc(pf.range.start) + (pf.range.end !== pf.range.start ? ' ~ ' + esc(pf.range.end) : '') + '</small></h3><div class="g2">' +
          '<div class="stat"><b class="num">' + money(pf.gross) + '</b><span>毛利</span></div><div class="stat"><b>' + (pf.margin == null ? '—' : pf.margin + '%') +
          '</b><span>毛利率</span></div><div class="stat"><b class="num">' + money(pf.discount) + '</b><span>折扣让利</span></div><div class="stat"><b class="num">' +
          money(pf.refund) + '</b><span>退款</span></div></div>' + (pf.abnormal.length ? '<h3 style="margin-top:12px">异常改价 <small>成交价低于零售价 ' +
          Math.round(pf.threshold * 100) + '%</small></h3><div class="list">' + pf.abnormal.slice(0, 8).map(function (x) {
            return '<div class="it"><div class="l"><div class="n">' + esc(x.name) + '</div><div class="s">' + esc(x.store_code + ' · ' + x.cashier + ' · ' + x.created_at.slice(5, 16)) +
              (x.auth_user ? ' · 授权 ' + esc(x.auth_user) : '') + '</div></div><div class="r"><b class="up">' + money(x.price) + '</b><div class="s">零售 ' + money(x.retail) + '</div></div></div>';
          }).join('') + '</div>' : '<p class="note" style="margin-top:10px">本期没有异常改价</p>');
      }).catch(function (e) { fill('#pf', errBox(e)); });
    }
  }

  function loadRank(args) {
    api('api/m/rank' + qs(Object.assign({ dim: S.rankDim, limit: 10 }, args))).then(function (r) {
      var box = $('#rk');
      if (!box) return;
      box.innerHTML = r.items.length ? r.items.map(function (x, i) {
        return '<div class="it"><span class="rank ' + (i < 3 ? 't' : '') + '">' + (i + 1) + '</span><div class="l"><div class="n">' + esc(x.name) +
          '</div><div class="pbar"><i style="width:' + Math.max(1, Math.min(100, x.share)) + '%"></i></div></div><div class="r"><b class="num">' + money(x.sales) +
          '</b><div class="s">' + x.share + '% · ' + (S.rankDim === 'store' ? x.orders + ' 单' : x.qty + ' 件') + '</div></div></div>';
      }).join('') : '<div class="empty">暂无数据</div>';
    }).catch(function (e) { var box = $('#rk'); if (box) box.innerHTML = errBox(e); });
  }

  routes.alerts = function (kind) {
    var names = { stock_low: '缺货/低库存', near_expiry: '临期批次', expired: '过期批次', slow: '滞销商品' };
    loading(names[kind] || '库存预警', { back: true });
    api('api/m/overview-stock-member' + qs(Object.assign({ detail: kind }, scopeArgs()))).then(function (d) {
      render(head(names[kind] || '库存预警', { back: true, sub: esc(d.scope.label) }) + '<div class="wrap"><div class="card list">' + (d.items.length ? d.items.map(function (x) {
        var r = kind === 'stock_low' ? '<b>' + x.qty + '</b><div class="s">安全库存 ' + x.safety_qty + '</div>'
          : (kind === 'slow' ? '<b>' + x.qty + '</b><div class="s">' + x.no_sale_days + ' 天未动销</div>'
            : '<b class="' + (kind === 'expired' ? 'up' : '') + '">' + x.qty + '</b><div class="s">' + esc(x.expiry_date) + (x.days_left != null ? '（' + x.days_left + ' 天）' : '') + '</div>');
        return '<div class="it"><div class="l"><div class="n">' + esc(x.name) + '</div><div class="s">' + esc(x.store_name + ' · ' + x.code + (x.batch_no ? ' · 批次 ' + x.batch_no : '')) +
          '</div></div><div class="r">' + r + '</div></div>';
      }).join('') : '<div class="empty">没有需要处理的项目</div>') + '</div></div>');
    }).catch(function (e) { render(head(names[kind] || '库存预警', { back: true }) + '<div class="wrap">' + errBox(e) + '</div>'); });
  };

  // ---------------------------------------------------------------- 门店经营（APP-101）
  routes.store = function () {
    loadScopes().then(function (sc) {
      var cur = lsGet('store', '') || (S.me.store_code || (sc.stores[0] && sc.stores[0].code) || '');
      var start = lsGet('store_start', today()), end = lsGet('store_end', today());
      var bar = '<select data-st>' + sc.stores.map(function (s) { return '<option value="' + esc(s.code) + '"' + (s.code === cur ? ' selected' : '') + '>' + esc(s.name) + '</option>'; }).join('') +
        '</select><input type="date" data-s value="' + esc(start) + '"><input type="date" data-e value="' + esc(end) + '">';
      render(head('门店经营', { back: true, bar: bar }) + '<div class="wrap" id="sw"><div class="empty">加载中…</div></div>');
      $('[data-st]').onchange = function (e) { lsSet('store', e.target.value); routes.store(); };
      $('[data-s]').onchange = function (e) { lsSet('store_start', e.target.value || today()); routes.store(); };
      $('[data-e]').onchange = function (e) { lsSet('store_end', e.target.value || today()); routes.store(); };
      api('api/m/store' + qs({ store: cur, start: start, end: end })).then(function (d) {
        var s = d.stats;
        $('#sw').innerHTML = '<div class="card"><h3>' + esc(d.store.name) + ' <small>' + esc(d.start) + (d.end !== d.start ? ' ~ ' + esc(d.end) : '') + '</small></h3><div class="g4">' +
          '<div class="stat"><b class="num">' + money(s.sales) + '</b><span>销售额</span></div><div class="stat"><b>' + s.orders + '</b><span>订单</span></div>' +
          '<div class="stat"><b class="num">' + money(s.avg) + '</b><span>客单价</span></div><div class="stat"><b class="num">' + money(s.refund) + '</b><span>退款 ' + s.refund_orders + ' 单</span></div></div></div>' +
          '<div class="card"><h3>支付方式</h3><div class="list">' + (d.pays.length ? d.pays.map(function (x) { return '<div class="it"><div class="l">' + esc(x.name || x.method) + '</div><div class="r"><b class="num">' + money(x.amount) + '</b></div></div>'; }).join('') : '<div class="empty">暂无</div>') + '</div></div>' +
          '<div class="card"><h3>收银员</h3><div class="list">' + (d.cashiers.length ? d.cashiers.map(function (x) {
            return '<div class="it"><div class="l"><div class="n">' + esc(x.name || x.user) + '</div><div class="s">' + x.orders + ' 单 · 退 ' + x.refund_orders + ' 单 ' + money(x.refund) + '</div></div><div class="r"><b class="num">' + money(x.sales) + '</b></div></div>';
          }).join('') : '<div class="empty">暂无</div>') + '</div></div>' +
          '<div class="card"><h3>班次</h3><div class="list">' + (d.shifts.length ? d.shifts.map(function (x) {
            return '<div class="it"><div class="l"><div class="n">' + esc(x.cashier) + ' <span class="tag ' + (x.close_time ? 'gray' : 'green') + '">' + esc(x.close_time ? '已交班' : '当班') + '</span></div><div class="s">' +
              esc((x.terminal_no ? x.terminal_no + ' · ' : '') + x.open_time.slice(5, 16) + (x.close_time ? ' ~ ' + x.close_time.slice(11, 16) : '')) + '</div></div><div class="r">' +
              (x.close_time ? '<b class="' + (x.diff && Math.abs(x.diff) >= 0.01 ? 'up' : '') + '">' + money(x.diff) + '</b><div class="s">长短款</div>' : '<div class="s">备用金 ' + money(x.opening_cash) + '</div>') + '</div></div>';
          }).join('') : '<div class="empty">暂无</div>') + '</div></div>' +
          '<div class="card"><h3>最近订单</h3><div class="list">' + (d.recent.length ? d.recent.map(function (x) {
            return '<div class="it" data-go="#order/' + esc(x.order_no) + '"><div class="l"><div class="n">' + (x.biz_type === 'refund' ? '<span class="tag red">退</span> ' : '') + esc(x.order_no) +
              '</div><div class="s">' + esc(x.created_at.slice(5, 16) + ' · ' + (x.cashier || '') + (x.member ? ' · ' + x.member : '')) + '</div></div><div class="r"><b class="num">' + money(x.payable) + '</b></div></div>';
          }).join('') : '<div class="empty">暂无</div>') + '</div></div>';
        on('[data-go]', 'click', function (e) { go(e.currentTarget.getAttribute('data-go')); }, $('#sw'));
      }).catch(function (e) { fill('#sw', errBox(e)); });
    }).catch(function (e) { render(head('门店经营', { back: true }) + '<div class="wrap">' + errBox(e) + '</div>'); });
  };

  routes.order = function (no) {
    loading('订单 ' + no, { back: true });
    api('api/pos/orders/' + encodeURIComponent(no)).then(function (d) {
      var o = d.order || d;
      var items = o.items || [];
      render(head('订单详情', { back: true }) + '<div class="wrap"><div class="card"><dl class="kv"><dt>单号</dt><dd class="mono">' + esc(o.order_no) + '</dd><dt>门店</dt><dd>' +
        esc(o.store_name || o.store_code) + '</dd><dt>时间</dt><dd>' + esc(o.created_at) + '</dd><dt>收银员</dt><dd>' + esc(o.cashier_name || '') + '</dd><dt>应收</dt><dd>' +
        money(o.payable) + '</dd><dt>状态</dt><dd>' + esc(o.status || '') + '</dd></dl></div><div class="card list">' + items.map(function (x) {
          return '<div class="it"><div class="l"><div class="n">' + esc(x.name) + '</div><div class="s">× ' + esc(x.qty) + '</div></div><div class="r"><b>' + money(x.amount) + '</b></div></div>';
        }).join('') + '</div></div>');
    }).catch(function (e) { render(head('订单详情', { back: true }) + '<div class="wrap">' + errBox(e) + '</div>'); });
  };

  // ---------------------------------------------------------------- 商品库存查询（APP-102）
  routes.goods = function (kw) {
    render(head('查商品', { back: true }) + '<div class="wrap"><div class="search"><input class="inp" id="gk" placeholder="条码 / 货号 / 名称 / 拼音首字母" enterkeyhint="search" value="' +
      esc(kw || '') + '"><button class="btn" id="gs">📷</button></div><div id="gr"></div></div>');
    var doSearch = function (v) {
      v = (v || '').trim();
      if (!v) return;
      $('#gk').value = v;
      fill('#gr', '<div class="empty">查询中…</div>');
      api('api/m/goods' + qs(Object.assign({ kw: v }, scopeArgs()))).then(function (d) {
        fill('#gr', d.items.length ? d.items.map(goodsCard).join('') : '<div class="empty">没有找到「' + esc(v) + '」</div>');
      }).catch(function (e) { fill('#gr', errBox(e)); });
    };
    $('#gk').addEventListener('keydown', function (e) { if (e.key === 'Enter') doSearch(e.target.value); });
    $('#gs').onclick = function () { window.MScan.open(doSearch, { title: '扫描商品条码 / 箱码' }); };
    if (kw) doSearch(kw);
  };
  function goodsCard(g) {
    return '<div class="card"><h3><span>' + esc(g.name) + (g.status && g.status !== '在售' ? ' <span class="tag gray">' + esc(g.status) + '</span>' : '') + '</span><small class="mono">' + esc(g.code) +
      '</small></h3><div class="s note">' + esc([g.spec, g.brand, g.category, g.barcodes.join(' / ')].filter(Boolean).join(' · ')) + '</div>' +
      '<div class="g4" style="margin-top:10px"><div class="stat"><b class="num">' + money(g.retail) + '</b><span>零售价</span></div><div class="stat"><b class="num">' + money(g.member_price) +
      '</b><span>会员价</span></div><div class="stat"><b>' + g.total_available + '</b><span>可售(' + esc(g.unit || '件') + ')</span></div>' +
      (g.cost !== undefined ? '<div class="stat"><b class="num">' + money(g.cost) + '</b><span>成本</span></div>' : '<div class="stat"><b>' + g.units.length + '</b><span>多单位</span></div>') +
      '</div>' + (g.units.length ? '<p class="note" style="margin-top:8px">单位：' + g.units.map(function (u) { return esc('1' + u.unit + '=' + u.rate + (g.unit || '')); }).join('，') + '</p>' : '') +
      '<div class="list" style="margin-top:6px">' + g.stock.map(function (s) {
        return '<div class="it"><div class="l"><div class="n">' + esc(s.store_name) + '</div>' + (s.defect ? '<div class="s">残次/待处理 ' + s.defect + '</div>' : '') + '</div><div class="r"><b>' + s.available +
          '</b><div class="s">账面 ' + s.book + '</div></div></div>';
      }).join('') + '</div>' + (g.batches.length ? '<h3 style="margin-top:10px">批次效期</h3><div class="list">' + g.batches.map(function (b) {
        var tag = b.unsellable ? '<span class="tag red">' + esc(b.unsellable.indexOf('过期') >= 0 ? '已过期' : (b.unsellable.indexOf('召回') >= 0 ? '已召回' : '锁定')) + '</span>'
          : (b.days_left != null && b.days_left <= 30 ? '<span class="tag">临期 ' + b.days_left + ' 天</span>' : '');
        return '<div class="it"><div class="l"><div class="n mono">' + esc(b.batch_no) + ' ' + tag + '</div><div class="s">' + esc(b.store_name + ' · 有效期 ' + (b.expiry_date || '—')) +
          '</div></div><div class="r"><b>' + b.qty + '</b></div></div>';
      }).join('') + '</div>' : '') + '</div>';
  }

  // ---------------------------------------------------------------- 待办与消息（APP-103 / APP-301 / APP-303）
  routes.todo = function (tab) {
    tab = tab || (feat('approval') ? 'approve' : 'msg');
    var tabs = [];
    if (feat('approval')) tabs.push(['approve', '待我审批']);
    tabs.push(['mine', '我的申请'], ['msg', '消息'], ['risk', '告警']);
    render(head('待办与消息') + '<div class="wrap"><div class="tabs">' + tabs.map(function (t) {
      return '<button data-go="#todo/' + t[0] + '" class="' + (t[0] === tab ? 'on' : '') + '">' + t[1] + '</button>';
    }).join('') + '</div><div id="tw"><div class="empty">加载中…</div></div></div>');
    if (tab === 'approve' || tab === 'mine') {
      api('api/approvals' + qs({ box: tab === 'approve' ? 'todo' : 'mine', status: tab === 'approve' ? 'pending' : '', size: 50 })).then(function (d) {
        $('#tw').innerHTML = '<div class="card list">' + (d.items.length ? d.items.map(function (a) {
          return '<div class="it" data-go="#approval/' + esc(a.approval_no) + '"><div class="l"><div class="n">' + esc(a.title) + '</div><div class="s">' + esc(a.kind_name + ' · ' +
            (a.store_code || '总部') + ' · ' + a.applicant_name + ' · ' + a.created_at.slice(5, 16)) + '</div></div><div class="r"><span class="tag ' +
            ({ pending: '', approved: 'green', rejected: 'red', consumed: 'gray', cancelled: 'gray' }[a.status] || 'gray') + '">' + esc(a.status_name) + '</span></div></div>';
        }).join('') : '<div class="empty">' + (tab === 'approve' ? '没有待审批事项 🎉' : '还没有发起过申请') + '</div>') + '</div>';
        on('[data-go]', 'click', function (e) { go(e.currentTarget.getAttribute('data-go')); }, $('#tw'));
      }).catch(function (e) { fill('#tw', errBox(e)); });
      return;
    }
    api('api/notify' + qs({ size: 50, category: tab === 'risk' ? 'risk' : '' })).then(function (d) {
      $('#tw').innerHTML = '<div class="row" style="justify-content:space-between;margin-bottom:8px"><span class="note">未读 ' + d.unread + ' · 未处理待办 ' + d.todo_open +
        '</span><button class="btn sm ghost" id="ra">全部已读</button></div><div class="card list">' + (d.items.length ? d.items.map(function (n) {
          return '<div class="it ' + (n.status === 'unread' ? 'unread' : '') + '" data-n="' + n.id + '"><div class="l"><div class="n">' + esc(n.title) + '</div><div class="s">' +
            esc(n.category_name + ' · ' + n.created_at.slice(5, 16)) + (n.level !== 'normal' ? ' <span class="tag ' + (n.level === 'urgent' ? 'red' : '') + '">' + esc(n.level_name) + '</span>' : '') +
            '</div></div><div class="r">' + (n.is_todo ? '<span class="tag ' + (n.status === 'done' ? 'gray' : 'blue') + '">' + (n.status === 'done' ? '已处理' : '待处理') + '</span>' : '') + '</div></div>';
        }).join('') : '<div class="empty">暂无消息</div>') + '</div>';
      var map = {};
      d.items.forEach(function (n) { map[n.id] = n; });
      $('#ra').onclick = function () { api('api/notify/read', { body: { all: true } }).then(function () { return refreshHome(); }).then(function () { routes.todo(tab); }); };
      on('[data-n]', 'click', function (e) { openMsg(map[e.currentTarget.getAttribute('data-n')], tab); }, $('#tw'));
    }).catch(function (e) { fill('#tw', errBox(e)); });
  };
  function openMsg(n, tab) {
    var p = n.status === 'unread' ? api('api/notify/read', { body: { ids: [n.id] } }).then(refreshHome).catch(function () {}) : Promise.resolve();
    p.then(function () {
      if (n.link) { go(n.link); return; }
      var s = sheet('<h4>' + esc(n.title) + '</h4><p class="note">' + esc(n.category_name + ' · ' + n.created_at) + '</p><p style="margin:12px 0;white-space:pre-wrap">' + esc(n.body || '') +
        '</p><div class="btns"><button class="btn gray" data-x>关闭</button>' + (n.status !== 'done' ? '<button class="btn" data-ok>标记已处理</button>' : '') + '</div>');
      s.el.querySelector('[data-x]').onclick = function () { s.close(); routes.todo(tab); };
      var okb = s.el.querySelector('[data-ok]');
      if (okb) okb.onclick = function () { api('api/notify/' + n.id + '/done', { body: {} }).then(refreshHome).then(function () { s.close(); toast('已处理'); routes.todo(tab); }).catch(function (e) { toast(e.message); }); };
    });
  }

  routes.approval = function (no) {
    loading('审批详情', { back: true });
    api('api/approvals/' + encodeURIComponent(no)).then(function (a) {
      var pl = a.payload || {};
      var lines = Array.isArray(pl.lines) ? pl.lines : [];
      var extra = Object.keys(pl).filter(function (k) { return k !== 'lines' && k !== '_value'; });
      var mine = S.me && a.applicant_name === S.me.name && a.status === 'pending';
      render(head('审批详情', { back: true }) + '<div class="wrap"><div class="card"><h3>' + esc(a.title) + ' <span class="tag ' + (a.status === 'pending' ? '' : 'gray') + '">' + esc(a.status_name) +
        '</span></h3><dl class="kv"><dt>类型</dt><dd>' + esc(a.kind_name) + '</dd><dt>门店</dt><dd>' + esc(a.store_code || '总部') + '</dd><dt>申请人</dt><dd>' + esc(a.applicant_name) +
        '</dd><dt>申请原因</dt><dd>' + esc(a.apply_reason || '—') + '</dd>' + (pl._value != null ? '<dt>申请额度</dt><dd>' + esc(pl._value) + '</dd>' : '') +
        (a.amount_cents ? '<dt>金额</dt><dd>' + money(a.amount_cents / 100) + '</dd>' : '') + (a.ref_no ? '<dt>关联单号</dt><dd class="mono">' + esc(a.ref_no) + '</dd>' : '') +
        extra.map(function (k) { return '<dt>' + esc(k) + '</dt><dd>' + esc(typeof pl[k] === 'object' ? JSON.stringify(pl[k]) : pl[k]) + '</dd>'; }).join('') +
        '<dt>审批单号</dt><dd class="mono">' + esc(a.approval_no) + '</dd></dl></div>' +
        (lines.length ? '<div class="card"><h3>明细</h3><div class="list">' + lines.map(function (l) {
          return '<div class="it"><div class="l"><div class="n">' + esc(l.name || l.code || l.product_id) + '</div><div class="s">' + esc(l.reason || l.note || '') + '</div></div><div class="r"><b>' +
            esc(l.qty != null ? l.qty : (l.base_qty != null ? l.base_qty : '')) + '</b></div></div>';
        }).join('') + '</div></div>' : '') +
        '<div class="card"><h3>审批记录</h3><div class="list">' + (a.logs || []).map(function (l) {
          return '<div class="it"><div class="l"><div class="n">' + esc(l.username) + ' · ' + esc({ pending: '提交', approved: '同意', rejected: '驳回', cancelled: '撤回', consumed: '已使用' }[l.to_status] || l.to_status) +
            '</div><div class="s">' + esc(l.reason || '') + '</div></div><div class="r s">' + esc(l.created_at.slice(5, 16)) + '</div></div>';
        }).join('') + '</div></div>' +
        (a.can_decide ? '<div class="btns"><button class="btn ghost" id="rj">驳回</button><button class="btn ok" id="ap">同意</button></div>' : '') +
        (mine ? '<div class="btns"><button class="btn gray" id="cc">撤回申请</button></div>' : '') + '</div>');
      var act = function (path, reason) {
        api('api/approvals/' + encodeURIComponent(no) + '/' + path, { body: { reason: reason || '' } }).then(refreshHome).then(function () {
          toast('已处理'); routes.approval(no);
        }).catch(function (e) { toast(e.message); });
      };
      if ($('#ap')) $('#ap').onclick = function () { ask('同意意见（可不填）', { ok: '同意' }).then(function (r) { if (r !== null) act('approve', r); }); };
      if ($('#rj')) $('#rj').onclick = function () { ask('驳回原因', { required: true, label: '驳回原因', ok: '驳回' }).then(function (r) { if (r) act('reject', r); }); };
      if ($('#cc')) $('#cc').onclick = function () { confirmBox('确定撤回这条申请？', '撤回').then(function (y) { if (y) act('cancel'); }); };
    }).catch(function (e) { render(head('审批详情', { back: true }) + '<div class="wrap">' + errBox(e) + '</div>'); });
  };

  // ---------------------------------------------------------------- 店务单据（APP-104）——调用模块① api/stock/*
  var DOC_STATUS = { draft: '草稿', submitted: '待审核', approved: '已审核', rejected: '已驳回', shipped: '在途', partial: '部分收货', received: '已收货', closed: '已完结',
    cancelled: '已取消', posted: '已过账', counting: '盘点中' };
  routes.docs = function (tab) {
    tab = tab || 'request';
    var tabs = [['request', '要货'], ['receive', '调拨收货'], ['adjust', '报损报溢']];
    render(head('店务单据', { back: true }) + '<div class="wrap"><div class="tabs">' + tabs.map(function (t) {
      return '<button data-go="#docs/' + t[0] + '" class="' + (t[0] === tab ? 'on' : '') + '">' + t[1] + '</button>';
    }).join('') + '</div><div id="dw2"><div class="empty">加载中…</div></div></div>');
    var url = { request: 'api/stock/requests', receive: 'api/stock/transfers' + qs({ direction: 'in' }), adjust: 'api/stock/adjusts' }[tab];
    var actions = { request: feat('request') ? '<button class="btn block" data-go="#newdoc/request">＋ 新建要货单</button>' : '',
      receive: '', adjust: feat('adjust') ? '<div class="btns" style="margin-top:0"><button class="btn" data-go="#newdoc/loss">＋ 报损</button><button class="btn ghost" data-go="#newdoc/overflow">＋ 报溢</button></div>' : '' }[tab];
    stockApi(url).then(function (d) {
      var items = itemsOf(d);
      $('#dw2').innerHTML = actions + '<div class="pad"></div><div class="card list">' + (items.length ? items.map(function (x) {
        var no = docNo(x), st = x.status_name || DOC_STATUS[x.status] || x.status || '';
        var link = tab === 'receive' && /shipped|partial/.test(x.status || '') ? '#transfer/' + no : '';
        return '<div class="it"' + (link ? ' data-go="' + esc(link) + '"' : '') + '><div class="l"><div class="n mono">' + esc(no) + '</div><div class="s">' +
          esc([x.kind_name || x.kind || '', x.from_wh || x.from_code || '', x.to_wh || x.to_code || x.wh_code || '', (x.created_at || '').slice(5, 16)].filter(Boolean).join(' · ')) +
          '</div></div><div class="r"><span class="tag ' + (link ? 'blue' : 'gray') + '">' + esc(link ? '去收货' : st) + '</span></div></div>';
      }).join('') : '<div class="empty">暂无单据</div>') + '</div>';
      on('[data-go]', 'click', function (e) { go(e.currentTarget.getAttribute('data-go')); }, $('#dw2'));
    }).catch(function (e) {
      fill('#dw2', actions + '<div class="pad"></div>' + errBox(e));
      on('[data-go]', 'click', function (ev) { go(ev.currentTarget.getAttribute('data-go')); }, $('#dw2'));
    });
  };

  function lookupOne(code) {
    return api('api/m/goods' + qs({ kw: code })).then(function (d) {
      if (!d.items.length) throw new Error('没有找到商品：' + code);
      if (d.items.length > 1 && !d.scan) throw new Error('匹配到多个商品，请扫码或输入完整条码/货号');
      var g = d.items[0];
      return { product_id: g.id, code: g.code, name: g.name, unit: d.scan ? d.scan.unit : g.unit, rate: d.scan ? d.scan.rate : 1 };
    });
  }

  routes.newdoc = function (kind) {
    var title = { request: '新建要货单', loss: '报损', overflow: '报溢' }[kind] || '新建单据';
    loadScopes().then(function (sc) {
      var draftKey = 'doc.' + kind;
      var lines = lsGet(draftKey, []);
      var store = S.me.store_code || (sc.stores[0] && sc.stores[0].code) || '';
      render(head(title, { back: true }) + '<div class="wrap"><div class="card"><div class="field"><label>门店</label><select class="inp" id="ds">' + sc.stores.map(function (s) {
        return '<option value="' + esc(s.code) + '"' + (s.code === store ? ' selected' : '') + '>' + esc(s.name) + '</option>';
      }).join('') + '</select></div><div class="search" style="margin-bottom:0"><input class="inp" id="dk" placeholder="扫码或输入条码/货号" enterkeyhint="done"><button class="btn" id="dsn">📷</button></div></div>' +
        '<div class="card"><h3>明细 <small id="dc"></small></h3><div class="list" id="dl"></div></div><div class="field"><label>' + (kind === 'request' ? '备注' : '原因（必填）') +
        '</label><textarea class="inp" id="dr"></textarea></div><button class="btn block" id="dsub">提交</button><p class="note" style="margin-top:8px">提交后进入审批，可在「店务单据」查看进度。</p></div>');
      function draw() {
        lsSet(draftKey, lines);
        $('#dc').textContent = lines.length + ' 种';
        $('#dl').innerHTML = lines.length ? lines.map(function (l, i) {
          return '<div class="it" data-i="' + i + '"><div class="l"><div class="n">' + esc(l.name) + '</div><div class="s mono">' + esc(l.code) + '</div></div><div class="r"><b>' + l.qty + '</b> ' + esc(l.unit || '') + '</div></div>';
        }).join('') : '<div class="empty">扫码添加商品</div>';
        on('[data-i]', 'click', function (e) {
          var i = Number(e.currentTarget.getAttribute('data-i'));
          askQty('修改数量（0 = 删除）：' + lines[i].name, lines[i].qty).then(function (q) {
            if (q === null) return;
            if (q === 0) lines.splice(i, 1); else lines[i].qty = q;
            draw();
          });
        });
      }
      function add(code) {
        lookupOne(code).then(function (g) {
          return askQty(g.name + '（' + (g.unit || '') + '）', 1).then(function (q) {
            if (!q) return;
            var ex = lines.filter(function (l) { return l.code === g.code && l.unit === g.unit; })[0];
            if (ex) ex.qty += q; else lines.push({ product_id: g.product_id, code: g.code, name: g.name, unit: g.unit, rate: g.rate, qty: q });
            draw();
          });
        }).catch(function (e) { toast(e.message); });
      }
      $('#dk').addEventListener('keydown', function (e) { if (e.key === 'Enter' && e.target.value.trim()) { add(e.target.value.trim()); e.target.value = ''; } });
      $('#dsn').onclick = function () { window.MScan.open(add); };
      $('#dsub').onclick = function () {
        var reason = $('#dr').value.trim(), wh = $('#ds').value;
        if (!lines.length) { toast('请先添加商品'); return; }
        if (kind !== 'request' && !reason) { toast('请填写原因'); return; }
        var body = { store_code: wh, wh_code: wh, remark: reason, reason: reason, client_id: 'm-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8),
          lines: lines.map(function (l) { return { product_id: l.product_id, code: l.code, unit: l.unit, qty: l.qty, reason: reason }; }) };
        var path = kind === 'request' ? 'api/stock/requests' : 'api/stock/adjusts';
        if (kind !== 'request') body.kind = kind;
        $('#dsub').disabled = true;
        stockApi(path, { body: body }).then(function (d) {
          var no = docNo(d || {});
          if (no && (d.status === 'draft' || !d.status)) {
            return stockApi(path + '/' + encodeURIComponent(no) + '/submit', { body: {} }).catch(function (e) { if (!e.degraded) throw e; }).then(function () { return no; });
          }
          return no;
        }).then(function (no) {
          lsDel(draftKey);
          toast('已提交' + (no ? '：' + no : ''));
          location.hash = '#docs/' + (kind === 'request' ? 'request' : 'adjust');
        }).catch(function (e) { $('#dsub').disabled = false; if (e.degraded) { sheet(errBox(e) + '<p class="note">已录入的明细保存在本机草稿中。</p>'); } else toast(e.message); });
      };
      draw();
    }).catch(function (e) { render(head(title, { back: true }) + '<div class="wrap">' + errBox(e) + '</div>'); });
  };

  routes.transfer = function (no) {
    loading('调拨收货', { back: true });
    stockApi('api/stock/transfers/' + encodeURIComponent(no)).then(function (d) {
      var t = d.transfer || d, lines = d.lines || t.lines || [];
      render(head('调拨收货', { back: true, sub: esc(no) }) + '<div class="wrap"><div class="card"><dl class="kv"><dt>调出</dt><dd>' + esc(t.from_wh || t.from_code || '') + '</dd><dt>调入</dt><dd>' +
        esc(t.to_wh || t.to_code || '') + '</dd><dt>状态</dt><dd>' + esc(t.status_name || DOC_STATUS[t.status] || t.status || '') + '</dd></dl></div><div class="card"><h3>逐行确认实收数量</h3>' +
        lines.map(function (l, i) {
          var out = l.out_qty != null ? l.out_qty : (l.out_base_qty != null ? l.out_base_qty : l.qty);
          var got = l.in_qty != null && l.in_qty !== 0 ? l.in_qty : (l.in_base_qty || 0);
          var left = Number(out || 0) - Number(got || 0);
          return '<div class="field" data-l="' + i + '"><label>' + esc(l.name || l.code || l.product_id) + '（发出 ' + esc(out) + (got ? '，已收 ' + esc(got) : '') + '）</label><div class="row">' +
            '<input class="inp" type="number" inputmode="decimal" data-q value="' + esc(left) + '" style="width:110px"><input class="inp" data-r placeholder="差异原因（数量不一致时必填）"></div></div>';
        }).join('') + '</div><button class="btn block" id="trs">确认收货</button></div>');
      $('#trs').onclick = function () {
        var body = { lines: [] }, bad = false;
        $$('[data-l]').forEach(function (f) {
          var l = lines[Number(f.getAttribute('data-l'))];
          var out = l.out_qty != null ? l.out_qty : (l.out_base_qty != null ? l.out_base_qty : l.qty);
          var got = l.in_qty || l.in_base_qty || 0;
          var q = Number($('[data-q]', f).value), r = $('[data-r]', f).value.trim();
          if (q !== Number(out) - Number(got) && !r) bad = true;
          body.lines.push({ id: l.id, in_qty: q, diff_reason: r });
        });
        if (bad) { toast('实收与发出不一致的行请填写差异原因'); return; }
        $('#trs').disabled = true;
        stockApi('api/stock/transfers/' + encodeURIComponent(no) + '/receive', { body: body }).then(function () {
          toast('已确认收货'); location.hash = '#docs/receive';
        }).catch(function (e) { $('#trs').disabled = false; toast(e.message); });
      };
    }).catch(function (e) { render(head('调拨收货', { back: true }) + '<div class="wrap">' + errBox(e) + '</div>'); });
  };

  // ---------------------------------------------------------------- 移动盘点（APP-201）——同一张盘点单（模块① api/stock/stocktakes）
  routes.take = function () {
    render(head('移动盘点', { back: true }) + '<div class="wrap" id="kw"><div class="empty">加载中…</div></div>');
    stockApi('api/stock/stocktakes' + qs({ status: 'counting' })).then(function (d) {
      var items = itemsOf(d);
      $('#kw').innerHTML = '<p class="note" style="margin-bottom:8px">盘点任务由后台创建（全盘/分类/抽盘/动态）。选择任务后扫码录入，录入实时写入同一张盘点单。</p><div class="card list">' +
        (items.length ? items.map(function (x) {
          var no = docNo(x), draft = lsGet('take.' + no, null);
          return '<div class="it" data-go="#takeone/' + esc(no) + '"><div class="l"><div class="n mono">' + esc(no) + '</div><div class="s">' + esc([x.wh_code, { full: '全盘', category: '分类', spot: '抽盘', dynamic: '动态' }[x.scope] || x.scope,
            x.scope_value, (x.created_at || '').slice(5, 16)].filter(Boolean).join(' · ')) + '</div></div><div class="r">' + (draft && Object.keys(draft.lines || {}).length ? '<span class="tag">本机草稿 ' + Object.keys(draft.lines).length + '</span>' : '<span class="tag blue">去盘点</span>') + '</div></div>';
        }).join('') : '<div class="empty">没有进行中的盘点任务</div>') + '</div>';
      on('[data-go]', 'click', function (e) { go(e.currentTarget.getAttribute('data-go')); }, $('#kw'));
    }).catch(function (e) { fill('#kw', errBox(e)); });
  };

  routes.takeone = function (no) {
    var key = 'take.' + no;
    var dr = lsGet(key, { lines: {}, loc: '' });
    loading('盘点 ' + no, { back: true });
    stockApi('api/stock/stocktakes/' + encodeURIComponent(no)).then(function (d) {
      var st = d.stocktake || d;
      var uploaded = d.lines || st.lines || [];
      render(head('扫码盘点', { back: true, sub: esc(no + ' · ' + (st.wh_code || '') + ' · ' + (st.status_name || DOC_STATUS[st.status] || st.status || '')) }) +
        '<div class="wrap"><div class="card"><div class="field"><label>区域 / 货架（可选，便于复核）</label><input class="inp" id="tl" value="' + esc(dr.loc || '') + '" placeholder="如：A 区 3 号货架"></div>' +
        '<div class="search" style="margin-bottom:0"><input class="inp" id="tk" placeholder="扫码或输入条码/货号" enterkeyhint="done"><button class="btn" id="tsn">📷 扫码</button></div></div>' +
        '<div class="card"><h3>本机待上传 <small id="tc"></small></h3><div class="list" id="tls"></div><div class="btns"><button class="btn ghost" id="tup">上传录入</button><button class="btn" id="tsub">提交差异</button></div></div>' +
        '<div class="card"><h3>已上传到盘点单 <small>' + uploaded.filter(function (l) { return l.counted_qty != null; }).length + ' 行</small></h3><div class="list">' +
        (uploaded.filter(function (l) { return l.counted_qty != null; }).slice(0, 100).map(function (l) {
          return '<div class="it"><div class="l"><div class="n">' + esc(l.name || l.code || l.product_id) + '</div><div class="s">' + esc((l.counted_by || '') + ' ' + (l.counted_at || '').slice(5, 16)) + '</div></div><div class="r"><b>' + esc(l.counted_qty) + '</b></div></div>';
        }).join('') || '<div class="empty">还没有上传的录入</div>') + '</div></div><p class="note">不显示账面数量，避免照账填数；差异由后台审核后才调整库存。</p></div>');
      function save() { dr.loc = $('#tl').value.trim(); lsSet(key, dr); }
      function draw() {
        save();
        var ks = Object.keys(dr.lines);
        $('#tc').textContent = ks.length + ' 种';
        $('#tls').innerHTML = ks.length ? ks.map(function (k) {
          var l = dr.lines[k];
          return '<div class="it" data-k="' + esc(k) + '"><div class="l"><div class="n">' + esc(l.name) + '</div><div class="s mono">' + esc(l.code + (l.loc ? ' · ' + l.loc : '')) + '</div></div><div class="r"><b>' + l.qty + '</b></div></div>';
        }).join('') : '<div class="empty">扫码开始盘点</div>';
        on('[data-k]', 'click', function (e) {
          var k = e.currentTarget.getAttribute('data-k');
          askQty('复核数量（0 = 删除本行）：' + dr.lines[k].name, dr.lines[k].qty).then(function (q) {
            if (q === null) return;
            if (q === 0) delete dr.lines[k]; else dr.lines[k].qty = q;
            draw();
          });
        });
      }
      function add(code) {
        lookupOne(code).then(function (g) {
          var k = g.code;
          var cur = dr.lines[k] ? dr.lines[k].qty : 0;
          return askQty((cur ? '累加 · 已录 ' + cur + ' · ' : '') + g.name, g.rate || 1).then(function (q) {
            if (q === null) return;
            dr.lines[k] = { product_id: g.product_id, code: g.code, name: g.name, qty: cur + q, loc: $('#tl').value.trim() };
            draw();
          });
        }).catch(function (e) { toast(e.message); });
      }
      function upload() {
        var ks = Object.keys(dr.lines);
        if (!ks.length) return Promise.resolve(0);
        var body = { lines: ks.map(function (k) { var l = dr.lines[k]; return { product_id: l.product_id, code: l.code, counted_qty: l.qty, location: l.loc || '' }; }),
          client_id: 'm-' + no + '-' + Date.now() };
        return stockApi('api/stock/stocktakes/' + encodeURIComponent(no) + '/lines', { body: body }).then(function () {
          dr.lines = {}; draw(); return ks.length;
        });
      }
      $('#tl').onchange = save;
      $('#tk').addEventListener('keydown', function (e) { if (e.key === 'Enter' && e.target.value.trim()) { add(e.target.value.trim()); e.target.value = ''; } });
      $('#tsn').onclick = function () { window.MScan.open(add, { title: '扫描要盘点的商品' }); };
      $('#tup').onclick = function () { upload().then(function (n) { toast(n ? '已上传 ' + n + ' 行' : '没有待上传的录入'); if (n) routes.takeone(no); }).catch(function (e) { toast(e.degraded ? '库存模块接口未开放，草稿已保存在本机' : e.message); }); };
      $('#tsub').onclick = function () {
        confirmBox('先上传本机录入，再把盘点单提交审核？提交后不能再录入。', '提交').then(function (y) {
          if (!y) return;
          upload().then(function () { return stockApi('api/stock/stocktakes/' + encodeURIComponent(no) + '/submit', { body: {} }); }).then(function () {
            lsDel(key); toast('已提交审核'); location.hash = '#take';
          }).catch(function (e) { toast(e.degraded ? '库存模块接口未开放，草稿已保存在本机' : e.message); });
        });
      };
      draw();
    }).catch(function (e) { render(head('扫码盘点', { back: true }) + '<div class="wrap">' + errBox(e) + '</div>'); });
  };

  // ---------------------------------------------------------------- 公告（APP-105）
  routes.notice = function () {
    loading('公告', { back: true });
    api('api/m/announcements' + qs({ size: 50 })).then(function (d) {
      render(head('公告', { back: true, right: feat('notice_publish') ? '<button class="btn sm gray" data-go="#publish">发布</button>' : '' }) + '<div class="wrap">' +
        (feat('notice_publish') ? '<div class="tabs"><button class="on">收到的</button><button data-mine>我发布的</button></div>' : '') +
        '<div class="card list">' + (d.items.length ? d.items.map(annItem).join('') : '<div class="empty">暂无公告</div>') + '</div></div>');
      var mb = $('[data-mine]');
      if (mb) mb.onclick = function () {
        api('api/m/announcements' + qs({ box: 'mine' })).then(function (x) {
          $$('.tabs button').forEach(function (b) { b.classList.toggle('on', b === mb); });
          $('.card.list').innerHTML = x.items.length ? x.items.map(annItem).join('') : '<div class="empty">还没有发布过公告</div>';
          on('[data-go]', 'click', function (e) { go(e.currentTarget.getAttribute('data-go')); }, $('.card.list'));
        });
      };
    }).catch(function (e) { render(head('公告', { back: true }) + '<div class="wrap">' + errBox(e) + '</div>'); });
  };
  function annItem(a) {
    return '<div class="it ' + (a.read_at ? '' : 'unread') + '" data-go="#noticeone/' + a.id + '"><div class="l"><div class="n">' + esc(a.title) + '</div><div class="s">' + esc(a.created_at.slice(0, 16)) +
      (a.status === 'withdrawn' ? ' · 已撤回' : '') + '</div></div><div class="r">' + (a.level === 'important' ? '<span class="tag red">重要</span>' : '') +
      (a.need_confirm ? '<span class="tag ' + (a.confirmed_at ? 'green' : '') + '">' + (a.confirmed_at ? '已确认' : '需确认') + '</span>' : '') + '</div></div>';
  }
  routes.noticeone = function (id) {
    loading('公告', { back: true });
    api('api/m/announcements/' + encodeURIComponent(id)).then(function (a) {
      var p = (!a.read_at && a.status === 'published' && !a.stats) ? api('api/m/announcements/' + id + '/read', { body: {} }).then(function (x) { refreshHome(); return x; }) : Promise.resolve(a);
      return p.then(function (x) { return Object.assign(a, { read_at: x.read_at || a.read_at }); });
    }).then(function (a) {
      var st = a.stats;
      render(head('公告', { back: true }) + '<div class="wrap"><div class="card"><h3>' + esc(a.title) + '</h3><p class="note">' + esc(a.created_by + ' · ' + a.created_at) +
        (a.level === 'important' ? ' <span class="tag red">重要</span>' : '') + '</p><div style="margin-top:12px;white-space:pre-wrap;line-height:1.7">' + esc(a.body || '') + '</div></div>' +
        (st ? '<div class="card"><h3>阅读情况</h3><div class="g4"><div class="stat"><b>' + st.targets + '</b><span>接收人</span></div><div class="stat"><b>' + st.read + '</b><span>已读</span></div><div class="stat"><b>' +
          (a.need_confirm ? st.confirmed : '—') + '</b><span>已确认</span></div><div class="stat"><b>' + (st.targets - st.read) + '</b><span>未读</span></div></div>' +
          (st.unread_names.length ? '<p class="note" style="margin-top:8px">未读：' + esc(st.unread_names.join('、')) + '</p>' : '') +
          (a.status === 'published' ? '<button class="btn block ghost" id="wd" style="margin-top:12px">撤回公告</button>' : '') + '</div>' : '') +
        (a.need_confirm && !a.confirmed_at && !st ? '<button class="btn block" id="cf">我已阅读并确认</button>' : '') +
        (a.confirmed_at ? '<p class="note" style="text-align:center">已于 ' + esc(a.confirmed_at) + ' 确认</p>' : '') + '</div>');
      if ($('#cf')) $('#cf').onclick = function () { api('api/m/announcements/' + id + '/confirm', { body: {} }).then(refreshHome).then(function () { toast('已确认'); routes.noticeone(id); }).catch(function (e) { toast(e.message); }); };
      if ($('#wd')) $('#wd').onclick = function () { ask('撤回原因', { ok: '撤回' }).then(function (r) { if (r === null) return; api('api/m/announcements/' + id + '/withdraw', { body: { reason: r } }).then(function () { toast('已撤回'); routes.noticeone(id); }).catch(function (e) { toast(e.message); }); }); };
    }).catch(function (e) { render(head('公告', { back: true }) + '<div class="wrap">' + errBox(e) + '</div>'); });
  };
  routes.publish = function () {
    loadScopes().then(function (sc) {
      var roles = [['store_manager', '店长'], ['cashier', '收银员'], ['region_manager', '区域经理'], ['hq_goods', '总部商品/仓管'], ['finance', '财务']];
      render(head('发布公告', { back: true }) + '<div class="wrap"><div class="field"><label>标题</label><input class="inp" id="pt" maxlength="100"></div><div class="field"><label>正文</label><textarea class="inp" id="pb" style="height:150px"></textarea></div>' +
        '<div class="card"><label class="row"><input type="checkbox" id="pi"> 重要通知</label><div class="pad"></div><label class="row"><input type="checkbox" id="pc"> 需要接收人确认</label></div>' +
        '<div class="card"><h3>发给门店 <small>不选 = ' + (sc.all ? '全部门店' : '我负责的全部门店') + '</small></h3>' + sc.stores.map(function (s) {
          return '<label class="row" style="padding:6px 0"><input type="checkbox" data-ps value="' + esc(s.code) + '"> ' + esc(s.name) + '</label>';
        }).join('') + '</div><div class="card"><h3>发给角色 <small>不选 = 全部角色</small></h3>' + roles.map(function (r) {
          return '<label class="row" style="padding:6px 0"><input type="checkbox" data-pr value="' + r[0] + '"> ' + r[1] + '</label>';
        }).join('') + '</div><button class="btn block" id="pp">发布</button></div>');
      $('#pp').onclick = function () {
        var body = { title: $('#pt').value.trim(), body: $('#pb').value.trim(), level: $('#pi').checked ? 'important' : 'normal', need_confirm: $('#pc').checked,
          target_stores: $$('[data-ps]').filter(function (x) { return x.checked; }).map(function (x) { return x.value; }),
          target_roles: $$('[data-pr]').filter(function (x) { return x.checked; }).map(function (x) { return x.value; }) };
        if (!body.title) { toast('请填写标题'); return; }
        $('#pp').disabled = true;
        api('api/m/announcements', { body: body }).then(function (d) { toast('已发布，通知 ' + d.notified + ' 人'); location.hash = '#noticeone/' + d.id; })
          .catch(function (e) { $('#pp').disabled = false; toast(e.message); });
      };
    });
  };

  // ---------------------------------------------------------------- 我的（APP-402 / APP-404 / APP-405）
  routes.me = function () {
    var me = S.me;
    render(head('我的') + '<div class="wrap"><div class="card"><dl class="kv"><dt>姓名</dt><dd>' + esc(me.name) + '</dd><dt>账号</dt><dd>' + esc(me.username) + '</dd><dt>角色</dt><dd>' +
      esc(me.role_name || '') + '</dd><dt>商户</dt><dd>' + esc(me.merchant_name || '') + '</dd><dt>门店</dt><dd>' + esc(me.store_name || (me.data_scope === 'merchant' ? '总部（全部门店）' : '—')) +
      '</dd><dt>可见门店</dt><dd>' + (me.stores || []).length + ' 家</dd></dl></div>' +
      '<div class="card"><h3>登录设备 <small>设备丢失可在此远程退出</small></h3><div class="list" id="ss"><div class="empty">加载中…</div></div><button class="btn block ghost" id="so" style="margin-top:8px">退出其他全部设备</button></div>' +
      '<div class="card list">' + (feat('notify_rules') ? '<div class="it" data-go="#rules"><div class="l"><div class="n">提醒规则设置</div></div><div class="r">›</div></div>' : '') +
      '<div class="it" id="pw"><div class="l"><div class="n">修改密码</div></div><div class="r">›</div></div><div class="it" id="vc"><div class="l"><div class="n">检查更新</div><div class="s">当前版本 ' + APP_VERSION +
      '</div></div><div class="r">›</div></div><div class="it" id="cl"><div class="l"><div class="n">清除本机缓存</div><div class="s">盘点草稿、筛选记录、离线文件</div></div><div class="r">›</div></div></div>' +
      '<button class="btn block gray" id="lo">退出登录</button><p class="note" style="text-align:center;margin-top:12px">可在浏览器菜单选择「添加到主屏幕」，像 App 一样使用</p></div>');
    $('#lo').onclick = function () { confirmBox('确定退出登录？', '退出').then(function (y) { if (y) logout(); }); };
    $('#vc').onclick = function () { checkVersion(true); };
    $('#cl').onclick = function () { confirmBox('清除本机缓存？未上传的盘点草稿会丢失。', '清除').then(function (y) { if (!y) return; var me_ = S.me, h = S.home; clearLocal(); S.me = me_; S.home = h; toast('已清除'); }); };
    $('#pw').onclick = function () {
      var s = sheet('<h4>修改密码</h4><div class="field"><input class="inp" type="password" id="o1" placeholder="原密码"></div><div class="field"><input class="inp" type="password" id="n1" placeholder="新密码（至少 6 位）"></div>' +
        '<p class="note">修改后其他设备会全部下线。</p><div class="btns"><button class="btn gray" data-x>取消</button><button class="btn" data-ok>确定</button></div>');
      s.el.querySelector('[data-x]').onclick = s.close;
      s.el.querySelector('[data-ok]').onclick = function () {
        api('api/password', { body: { old: s.el.querySelector('#o1').value, new: s.el.querySelector('#n1').value } }).then(function () { s.close(); toast('密码已修改'); }).catch(function (e) { toast(e.message); });
      };
    };
    function loadSess() {
      api('api/m/sessions').then(function (d) {
        $('#ss').innerHTML = d.items.map(function (x) {
          return '<div class="it"><div class="l"><div class="n">' + esc(x.client === 'm' ? '手机端' : (x.client === 'pos' ? '收银台' : '网页')) + (x.current ? ' <span class="tag green">本机</span>' : '') +
            '</div><div class="s">' + esc((x.device || '').slice(0, 40)) + '<br>' + esc(x.ip + ' · 登录 ' + x.created_at.slice(5, 16)) + '</div></div><div class="r">' +
            (x.current ? '' : '<button class="btn sm ghost" data-k="' + esc(x.key) + '">退出</button>') + '</div></div>';
        }).join('') || '<div class="empty">无</div>';
        on('[data-k]', 'click', function (e) {
          api('api/m/sessions/revoke', { body: { key: e.currentTarget.getAttribute('data-k') } }).then(function () { toast('该设备已退出'); loadSess(); }).catch(function (er) { toast(er.message); });
        }, $('#ss'));
      }).catch(function (e) { fill('#ss', errBox(e)); });
    }
    $('#so').onclick = function () { confirmBox('退出除本机外的全部设备？', '全部退出').then(function (y) { if (y) api('api/m/sessions/revoke', { body: { others: true } }).then(function (d) { toast('已退出 ' + d.revoked + ' 台设备'); loadSess(); }); }); };
    loadSess();
  };

  // ---------------------------------------------------------------- 提醒规则（APP-302）
  routes.rules = function () {
    loading('提醒规则', { back: true });
    Promise.all([api('api/m/notify-rules'), loadScopes()]).then(function (r) {
      var d = r[0], sc = r[1];
      var opt = function (pairs, cur) { return pairs.map(function (p) { return '<option value="' + esc(p[0]) + '"' + (p[0] === cur ? ' selected' : '') + '>' + esc(p[1]) + '</option>'; }).join(''); };
      var cats = Object.keys(d.categories).map(function (k) { return [k, d.categories[k]]; });
      render(head('提醒规则', { back: true }) + '<div class="wrap"><p class="note" style="margin-bottom:10px">' + esc(d.note) + '</p><div class="card list">' + (d.items.length ? d.items.map(function (x) {
        return '<div class="it"><div class="l"><div class="n">' + esc(x.category_name) + ' · ' + esc(x.role_code ? ((d.roles.filter(function (r_) { return r_.code === x.role_code; })[0] || {}).name || x.role_code) : '所有角色') +
          '</div><div class="s">' + esc((x.store_code || '所有门店') + ' · ' + (x.enabled ? '接收 ' + d.levels[x.min_level] + '及以上' : '不接收')) + '</div></div><div class="r"><button class="btn sm gray" data-del="' + x.id + '">删除</button></div></div>';
      }).join('') : '<div class="empty">未设置规则：所有有权限的人员都会收到提醒</div>') + '</div>' +
        '<div class="card"><h3>新增规则</h3><div class="field"><label>消息类别</label><select class="inp" id="rc">' + opt(cats) + '</select></div><div class="field"><label>角色</label><select class="inp" id="rr">' +
        opt([['', '所有角色']].concat(d.roles.map(function (x) { return [x.code, x.name]; }))) + '</select></div><div class="field"><label>门店</label><select class="inp" id="rs">' +
        opt([['', '所有门店']].concat(sc.stores.map(function (x) { return [x.code, x.name]; }))) + '</select></div><div class="field"><label>接收方式</label><select class="inp" id="rl">' +
        opt([['normal', '接收全部级别'], ['important', '只收重要及紧急'], ['urgent', '只收紧急'], ['off', '不接收']]) + '</select></div><button class="btn block" id="rsv">保存</button></div></div>');
      on('[data-del]', 'click', function (e) { api('api/m/notify-rules', { body: { id: Number(e.currentTarget.getAttribute('data-del')), delete: true } }).then(function () { routes.rules(); }).catch(function (er) { toast(er.message); }); });
      $('#rsv').onclick = function () {
        var lv = $('#rl').value;
        api('api/m/notify-rules', { body: { category: $('#rc').value, role_code: $('#rr').value, store_code: $('#rs').value, min_level: lv === 'off' ? 'normal' : lv, enabled: lv !== 'off' } })
          .then(function () { toast('已保存'); routes.rules(); }).catch(function (e) { toast(e.message); });
      };
    }).catch(function (e) { render(head('提醒规则', { back: true }) + '<div class="wrap">' + errBox(e) + '</div>'); });
  };

  // ---------------------------------------------------------------- 启动
  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('static/sw.js', { scope: './' }).catch(function () { /* 不支持就当普通网页用 */ });
    });
  }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && S.me) {
      // 回到前台：重新校验会话（停用/调店/远程退出后立即失效，APP-405），刷新角标
      refreshHome().then(function () { renderTabbar(($tab.querySelector('a.on') || { getAttribute: function () { return '#home'; } }).getAttribute('href').slice(1)); }).catch(function () {});
    }
  });
  checkVersion(false);
  boot();
})();
