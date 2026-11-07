/* 管理后台 · stock 模块页面。Owner：模块①。用 AdminShell.register({...}) 注册页面（见 admin.js 末尾说明）。
   接口：api/stock/*（{ok,data}）；共用工具 M1 在 admin-goods.js。 */
'use strict';
(function () {
  const can = p => AdminShell.can(p);
  const ask = (title, label) => new Promise(res => modal(title, fld(label, inp('ask-v', '')) + errBox, async m => { const x = v('ask-v'); if (!x) throw new Error('请填写' + label); m.close(); res(x); }));

  async function balPage(box) {
    const f = { page: 1 };
    box.innerHTML = `<div class="card"><div class="bar">${await M1.whSel('b-wh', '', true)}<input id="b-kw" placeholder="商品名称/编码/拼音/条码">
      <label><input type="checkbox" id="b-bt"> 按批次</label><button class="btn pri" id="b-go">查询</button><div class="sp"></div>
      ${can('stock.adjust') ? '<button class="btn" id="b-otpl">期初模板</button><button class="btn" id="b-oimp">期初导入</button>' : ''}${can('stock.export') ? '<button class="btn" id="b-x">导出</button>' : ''}</div>
      <div class="alert">账面 = 全部质量状态；可售 = 合格品 − 锁定/召回 − 过期；在途 = 调入本仓已出库未收货。</div><div class="tbl-wrap" id="b-tbl"></div></div>`;
    const load = async () => {
      const bt = $('b-bt').checked;
      const d = (await api('api/stock?' + qs({ wh: v('b-wh'), kw: v('b-kw'), by_batch: bt ? 1 : '', page: f.page }))).data;
      $('b-tbl').innerHTML = `<table class="tbl"><tr><th>仓库</th><th>商品</th>${bt ? '<th>批号</th><th>有效期</th>' : ''}<th class="r">账面</th><th class="r">可售</th><th class="r">锁定</th><th class="r">过期</th><th class="r">残次/待处理</th><th class="r">在途</th><th class="r">库存金额</th></tr>` +
        (d.items.map(x => `<tr><td>${esc(x.wh_code)}</td><td>${esc(x.code)} ${esc(x.name)}<span style="color:var(--ink2)"> ${esc(x.unit)}</span></td>${bt ? `<td>${esc(x.batch_no || '-')}${x.batch_status && x.batch_status !== 'normal' ? ' ' + M1.tag(x.batch_status, x.batch_status === 'locked' ? '锁定' : '召回') : ''}</td><td>${esc(x.expiry_date || '')}</td>` : ''}
          <td class="r num">${x.book}</td><td class="r num">${x.sellable}</td><td class="r num">${x.locked || ''}</td><td class="r num">${x.expired || ''}</td><td class="r num">${x.defect || ''}</td><td class="r num">${x.in_transit || ''}</td><td class="r num">${x.amount == null ? '-' : fx(x.amount)}</td></tr>`).join('') ||
          '<tr><td colspan="11" class="empty">没有库存</td></tr>') + '</table>' + pager(M1.pg(d));
      bindPager($('b-tbl'), p => { f.page = p; load(); });
    };
    $('b-go').onclick = () => { f.page = 1; load(); };
    if ($('b-x')) $('b-x').onclick = () => M1.dl('api/stock/export?' + qs({ wh: v('b-wh') }));
    if ($('b-otpl')) $('b-otpl').onclick = () => M1.dl('api/stock/opening/template');
    if ($('b-oimp')) $('b-oimp').onclick = () => M1.upload('.xlsx', 'api/stock/opening/import', load);
    await load();
  }

  async function movePage(box) {
    const f = { page: 1 };
    box.innerHTML = `<div class="card"><div class="bar">${await M1.whSel('m-wh', '', true)}<input id="m-kw" placeholder="商品"><input id="m-no" placeholder="来源单号">
      <input type="date" id="m-s" value="${addDays(-30)}"><input type="date" id="m-e" value="${localDate()}"><button class="btn pri" id="m-go">查询</button></div><div class="tbl-wrap" id="m-tbl"></div></div>`;
    const load = async () => {
      const d = (await api('api/stock/moves?' + qs({ wh: v('m-wh'), kw: v('m-kw'), ref_no: v('m-no'), start: v('m-s'), end: v('m-e'), page: f.page }))).data;
      $('m-tbl').innerHTML = `<table class="tbl"><tr><th>时间</th><th>仓库</th><th>商品</th><th>类型</th><th>来源单号</th><th>批号</th><th>质量</th><th class="r">变动</th><th class="r">前</th><th class="r">后</th><th>交易单位</th><th>操作人</th></tr>` +
        (d.items.map(x => `<tr><td>${esc(x.created_at)}</td><td>${esc(x.wh_code)}</td><td>${esc(x.name)}</td><td>${esc(x.type_name)}</td><td>${esc(x.ref_no)}</td><td>${esc(x.batch_no)}</td><td>${esc(x.quality)}</td>
          <td class="r num" style="color:${x.change < 0 ? 'var(--pri)' : 'inherit'}">${x.change > 0 ? '+' : ''}${x.change}</td><td class="r">${x.qty_before == null ? '' : x.qty_before}</td><td class="r">${x.qty_after == null ? '' : x.qty_after}</td>
          <td>${x.unit ? esc(x.unit_qty + x.unit) + (x.rate > 1 ? '×' + x.rate : '') : ''}</td><td>${esc(x.operator)}</td></tr>`).join('') || '<tr><td colspan="12" class="empty">没有流水</td></tr>') + '</table>' + pager(M1.pg(d));
      bindPager($('m-tbl'), p => { f.page = p; load(); });
    };
    $('m-go').onclick = () => { f.page = 1; load(); };
    await load();
  }

  async function traceModal(id) {
    const t = (await api('api/stock/batches/' + id + '/trace')).data;
    modal('批次追溯 · ' + t.name + ' ' + t.batch_no, `<div class="kv"><span>有效期</span><b>${esc(t.expiry_date || '-')}</b><span>状态</span><b>${esc(t.status)} ${esc(t.lock_reason || '')}</b><span>供应商</span><b>${esc(t.supplier_name || '-')}</b><span>在库</span><b>${t.qty}</b><span>已售</span><b>${t.sold_qty}</b></div>
      <h4>采购来源</h4><table class="tbl"><tr><th>单号</th><th>类型</th><th>供应商</th><th>仓库</th><th class="r">数量</th><th class="r">单价</th><th>时间</th></tr>${t.purchases.map(p => `<tr><td>${esc(p.gr_no)}</td><td>${esc(p.kind)}</td><td>${esc(p.supplier)}</td><td>${esc(p.wh_code)}</td><td class="r">${p.qty}</td><td class="r">${p.price == null ? '-' : fx(p.price)}</td><td>${esc(p.posted_at)}</td></tr>`).join('')}</table>
      <h4>库存分布</h4><div>${t.distribution.map(x => esc(x.wh_code) + '（' + esc(x.quality) + '）' + x.qty).join('，') || '无'}</div>
      <h4>销售去向</h4><table class="tbl"><tr><th>单号</th><th>类型</th><th>门店</th><th class="r">数量</th><th>会员</th><th>手机号</th><th>时间</th></tr>${t.sales.map(s => `<tr><td>${esc(s.order_no)}</td><td>${esc(s.type)}</td><td>${esc(s.store_code)}</td><td class="r">${s.qty}</td><td>${esc(s.member_name)}</td><td>${esc(s.member_phone)}</td><td>${esc(s.created_at)}</td></tr>`).join('')}</table>`, null, { wide: 'wide' });
  }

  async function batchPage(box) {
    const f = { page: 1 };
    box.innerHTML = `<div class="card"><div class="bar"><input id="t-kw" placeholder="商品"><input id="t-bn" placeholder="批号"><input type="date" id="t-ef" title="有效期从"><input type="date" id="t-et" title="有效期到">
      ${sel('t-st', [['', '全部状态'], ['normal', '正常'], ['locked', '锁定'], ['recalled', '召回']], '')}<label><input type="checkbox" id="t-in" checked> 只看有库存</label><button class="btn pri" id="t-go">查询</button><div class="sp"></div>
      ${can('stock.recall') ? '<button class="btn" id="t-rcl">召回单</button>' : ''}</div><div class="tbl-wrap" id="t-tbl"></div></div>`;
    const load = async () => {
      const d = (await api('api/stock/batches?' + qs({ kw: v('t-kw'), batch_no: v('t-bn'), exp_from: v('t-ef'), exp_to: v('t-et'), status: v('t-st'), in_stock: $('t-in').checked ? 1 : '', page: f.page }))).data;
      $('t-tbl').innerHTML = `<table class="tbl"><tr><th><input type="checkbox" id="t-all"></th><th>商品</th><th>批号</th><th>生产日期</th><th>有效期</th><th>供应商</th><th class="r">在库</th><th>分布</th><th>状态</th><th></th></tr>` +
        (d.items.map(b => `<tr><td><input type="checkbox" data-ck="${b.id}"></td><td>${esc(b.code)} ${esc(b.name)}</td><td>${esc(b.batch_no)}</td><td>${esc(b.prod_date)}</td><td>${esc(b.expiry_date)}</td><td>${esc(b.supplier_name)}</td><td class="r">${b.qty}</td>
          <td style="font-size:12px">${b.distribution.map(x => esc(x.wh_code) + ':' + x.qty).join(' ')}</td><td>${M1.tag(b.status, { normal: '正常', locked: '锁定', recalled: '召回' }[b.status])}${b.sellable_reason ? '<br><span style="font-size:12px;color:var(--pri)">' + esc(b.sellable_reason) + '</span>' : ''}</td>
          <td style="white-space:nowrap"><button class="btn sm" data-tr="${b.id}">追溯</button>${b.status === 'normal' && can('stock.batch.lock') ? ` <button class="btn sm red" data-lk="${b.id}">锁定</button>` : ''}${b.status === 'locked' && can('stock.batch.unlock') ? ` <button class="btn sm" data-ul="${b.id}">申请解锁</button>` : ''}</td></tr>`).join('') ||
          '<tr><td colspan="10" class="empty">没有批次</td></tr>') + '</table>' + pager(M1.pg(d));
      bindPager($('t-tbl'), p => { f.page = p; load(); });
      $('t-all').onchange = e => box.querySelectorAll('[data-ck]').forEach(x => x.checked = e.target.checked);
      box.querySelectorAll('[data-tr]').forEach(b => b.onclick = () => traceModal(b.dataset.tr));
      box.querySelectorAll('[data-lk]').forEach(b => b.onclick = async () => { const r = await ask('锁定批次（禁止销售和调出）', '锁定原因'); await api('api/stock/batches/' + b.dataset.lk + '/lock', { body: { reason: r } }); toast('已锁定', 'ok'); load(); });
      box.querySelectorAll('[data-ul]').forEach(b => b.onclick = async () => { const r = await ask('申请解锁（需审批）', '解锁原因'); const x = (await api('api/stock/batches/' + b.dataset.ul + '/unlock', { body: { reason: r } })).data; toast('已提交审批 ' + x.approval_no, 'ok'); });
    };
    $('t-go').onclick = () => { f.page = 1; load(); };
    if ($('t-rcl')) $('t-rcl').onclick = () => recallModal([...box.querySelectorAll('[data-ck]:checked')].map(x => +x.dataset.ck), load);
    await load();
  }

  async function recallModal(ids, done) {
    const list = (await api('api/stock/recalls?size=50')).data.items;
    modal('召回处理', (ids.length ? `<div class="frow">${fld('对勾选的 ' + ids.length + ' 个批次新建召回', inp('rc-t', '', 'placeholder="召回原因/标题"'))}<div class="fi"><label>&nbsp;</label><button class="btn red" id="rc-new">召回并锁定</button></div></div>` : '<div class="hint">在批次列表勾选批次后可新建召回。</div>') +
      `<table class="tbl"><tr><th>召回单</th><th>标题</th><th>状态</th><th>时间</th><th></th></tr>${list.map(r => `<tr><td>${esc(r.rc_no)}</td><td>${esc(r.title)}</td><td>${M1.tag(r.status, r.status === 'open' ? '处理中' : '已关闭')}</td><td>${esc(r.created_at)}</td><td><button class="btn sm" data-rc="${esc(r.rc_no)}">处理</button></td></tr>`).join('') || '<tr><td colspan="5" class="empty">没有召回单</td></tr>'}</table>`, null, {
      wide: 'wide', onOpen: (root, m) => {
        const nb = root.querySelector('#rc-new');
        if (nb) nb.onclick = async () => { try { await api('api/stock/recalls', { body: { title: v('rc-t'), batch_ids: ids } }); m.close(); toast('已召回锁定', 'ok'); done(); } catch (e) { toast(e.message, 'err'); } };
        root.querySelectorAll('[data-rc]').forEach(b => b.onclick = () => { m.close(); recallDetail(b.dataset.rc, done); });
      } });
  }

  async function recallDetail(no, done) {
    const r = (await api('api/stock/recalls/' + no)).data;
    const acts = [['notify', '通知顾客/门店'], ['return', '退回供应商'], ['loss', '报损销毁'], ['supplier', '供应商处理'], ['other', '其他']];
    modal('召回单 ' + no + ' · ' + r.title, `<h4>批次</h4><table class="tbl"><tr><th>商品</th><th>批号</th><th class="r">在库</th><th class="r">已售</th><th>已售订单</th></tr>${r.batches.map(b => `<tr><td>${esc(b.name)}</td><td>${esc(b.batch_no)}</td><td class="r">${b.qty}</td><td class="r">${b.sold_qty}</td><td style="font-size:12px">${b.sales.map(s => esc(s.order_no + ' ' + s.store_code + ' ' + (s.member_name || '') + ' ' + (s.member_phone || ''))).join('<br>')}</td></tr>`).join('')}</table>
      <h4>处理记录</h4><table class="tbl"><tr><th>时间</th><th>动作</th><th>说明</th><th>单号</th><th>操作人</th></tr>${r.events.map(e => `<tr><td>${esc(e.created_at)}</td><td>${esc(e.action_name)}</td><td>${esc(e.detail)}</td><td>${esc(e.ref_no)}</td><td>${esc(e.operator)}</td></tr>`).join('')}</table>
      ${r.status === 'open' ? `<div class="frow">${fld('处理动作', sel('re-a', acts, 'notify'))}${fld('说明', inp('re-d', ''))}${fld('关联单号', inp('re-n', '', 'placeholder="退货单/报损单号"'))}</div>` : ''}${errBox}`, r.status === 'open' ? async m => {
      await api('api/stock/recalls/' + no + '/events', { body: { action: v('re-a'), detail: v('re-d'), ref_no: v('re-n') } }); m.close(); recallDetail(no, done);
    } : null, { wide: 'wide', okText: '登记处理', extra: [{ text: '导出清单', fn: () => M1.dl('api/stock/recalls/' + no + '/export') }].concat(r.status === 'open' ? [{ text: '关闭召回', fn: async m => { await api('api/stock/recalls/' + no + '/close', { body: {} }); m.close(); done(); } }] : []) });
  }

  /* -------- 要货 / 调拨 -------- */
  async function tfPage(box) {
    let kind = can('stock.request') && !can('stock.transfer') ? 'requests' : 'transfers';
    const f = { page: 1 };
    box.innerHTML = `<div class="card"><div class="bar"><button class="btn" data-k="requests">要货单</button><button class="btn" data-k="transfers">调拨单</button>
      ${sel('f-st', [['', '全部状态'], ['submitted', '待审核'], ['approved', '待出库'], ['shipped', '在途'], ['partial', '部分收货'], ['received', '已收货'], ['closed', '已完结']], '')}<button class="btn pri" id="f-go">查询</button><div class="sp"></div>
      ${can('stock.request') ? '<button class="btn pri" id="f-rq">门店要货</button>' : ''}${can('stock.transfer') ? '<button class="btn pri" id="f-tf">新建调拨</button>' : ''}</div><div class="tbl-wrap" id="f-tbl"></div></div>`;
    const load = async () => {
      box.querySelectorAll('[data-k]').forEach(b => b.classList.toggle('pri', b.dataset.k === kind));
      const d = (await api('api/stock/' + kind + '?' + qs({ status: v('f-st'), page: f.page }))).data;
      $('f-tbl').innerHTML = `<table class="tbl"><tr><th>单号</th><th>调出/供货</th><th>调入/要货</th><th>来源</th><th>状态</th><th>创建</th><th></th></tr>` +
        (d.items.map(t => `<tr><td>${esc(t.tf_no)}</td><td>${esc(t.from_wh)}</td><td>${esc(t.to_wh)}</td><td>${esc(t.src_no)}</td><td>${M1.tag(t.status, t.status_name)}</td><td>${esc(t.created_by)} ${esc(t.created_at)}</td><td><button class="btn sm" data-o="${esc(t.tf_no)}">打开</button></td></tr>`).join('') ||
          '<tr><td colspan="7" class="empty">没有单据</td></tr>') + '</table>' + pager(M1.pg(d));
      bindPager($('f-tbl'), p => { f.page = p; load(); });
      box.querySelectorAll('[data-o]').forEach(b => b.onclick = () => open(b.dataset.o));
    };
    const post = async (no, act, body, m) => { await api('api/stock/' + kind + '/' + no + '/' + act, { body: body || {} }); m && m.close(); toast('已完成', 'ok'); load(); };
    const open = async no => {
      const t = (await api('api/stock/' + kind + '/' + no)).data;
      const ex = [], isRq = kind === 'requests';
      if (t.status === 'draft') ex.push({ text: '提交', fn: m => post(no, 'submit', null, m) });
      if (t.status === 'submitted' && can('stock.transfer.approve')) {
        ex.push({ text: '审核通过', fn: m => post(no, 'approve', isRq ? { lines: [...m.root.querySelectorAll('[data-aq]')].map(i => ({ id: +i.dataset.aq, qty: i.value })) } : {}, m) });
        ex.push({ text: '驳回', fn: async m => { const r = await ask('驳回', '原因'); post(no, 'approve', { reject: 1, reason: r }, m); } });
      }
      if (!isRq && t.status === 'approved' && can('stock.ship')) ex.push({ text: '出库（在途）', fn: m => post(no, 'ship', null, m) });
      if (!isRq && ['shipped', 'partial'].indexOf(t.status) >= 0 && can('stock.receive')) ex.push({ text: '确认收货', fn: m => post(no, 'receive', { lines: [...m.root.querySelectorAll('[data-in]')].map(i => ({ id: +i.dataset.in, in_base_qty: i.value || 0, diff_reason: (m.root.querySelector('[data-dr="' + i.dataset.in + '"]') || {}).value || '' })) }, m) });
      if (!isRq && t.status === 'received' && can('stock.transfer.approve')) ex.push({ text: '差异处理完结', fn: m => post(no, 'close', { lines: [...m.root.querySelectorAll('[data-se]')].map(s => ({ id: +s.dataset.se, settle: s.value })) }, m) });
      if (!isRq && ['draft', 'submitted', 'approved'].indexOf(t.status) >= 0 && can('stock.transfer')) ex.push({ text: '取消', fn: m => post(no, 'cancel', null, m) });
      const recv = !isRq && ['shipped', 'partial'].indexOf(t.status) >= 0;
      modal((isRq ? '要货单 ' : '调拨单 ') + no, `<div class="kv"><span>调出/供货</span><b>${esc(t.from_wh)}</b><span>调入</span><b>${esc(t.to_wh)}</b><span>状态</span><b>${esc(t.status_name)}</b></div>
        <table class="tbl"><tr><th>商品</th><th>批号</th><th>单位</th><th class="r">申请</th><th class="r">出库</th><th class="r">已收</th><th class="r">差异</th><th class="r">在途</th><th>差异原因/处理</th>${recv ? '<th>本次收(基本单位)</th><th>剩余记差异原因</th>' : ''}</tr>` +
        t.lines.map(l => `<tr><td>${esc(l.name)}</td><td>${esc(l.batch_no || '')}</td><td>${esc(l.unit)}${l.rate > 1 ? '(' + l.rate + ')' : ''}</td>
          <td class="r">${isRq && t.status === 'submitted' && can('stock.transfer.approve') ? `<input data-aq="${l.id}" value="${l.req_qty}" style="width:60px">` : l.req_qty}</td>
          <td class="r">${l.out_base_qty}</td><td class="r">${l.in_base_qty}</td><td class="r">${l.diff_base_qty || ''}</td><td class="r">${l.in_transit || ''}</td>
          <td>${esc(l.diff_reason)} ${esc(l.diff_settle || '')}${t.status === 'received' && l.diff_base_qty > 0 ? sel('se-' + l.id, [['return', '退回调出方'], ['loss', '运输损耗'], ['claim', '索赔']], 'loss').replace('<select', '<select data-se="' + l.id + '"') : ''}</td>
          ${recv ? `<td><input data-in="${l.id}" value="${l.in_transit}" style="width:60px"></td><td><input data-dr="${l.id}" placeholder="少到/破损…"></td>` : ''}</tr>`).join('') + '</table>' + errBox, null, { wide: 'wide', extra: ex });
    };
    const create = async rq => {
      modal(rq ? '门店要货' : '新建调拨', `<div class="frow">${rq ? fld('要货门店仓', await M1.whSel('c-to', S.me.store_code)) + fld('供货仓（空=总部仓）', inp('c-from', '')) : fld('调出仓库', await M1.whSel('c-from')) + fld('调入仓库编码', inp('c-to', ''))}</div>
        ${fld('商品明细（每行：编码或条码 数量 [单位]）', '<textarea id="c-lines" rows="6"></textarea>')}${fld('备注', inp('c-rm', ''))}${errBox}`, async m => {
        const r = (await api('api/stock/' + (rq ? 'requests' : 'transfers'), { body: { from_wh: v('c-from'), to_wh: v('c-to'), remark: v('c-rm'), submit: 1, lines: M1.parseLines($('c-lines').value) } })).data;
        m.close(); toast('已提交 ' + r.tf_no, 'ok'); kind = rq ? 'requests' : 'transfers'; load();
      }, { wide: 'wide' });
    };
    box.querySelectorAll('[data-k]').forEach(b => b.onclick = () => { kind = b.dataset.k; f.page = 1; load(); });
    $('f-go').onclick = () => { f.page = 1; load(); };
    if ($('f-rq')) $('f-rq').onclick = () => create(true);
    if ($('f-tf')) $('f-tf').onclick = () => create(false);
    await load();
  }

  /* -------- 盘点 -------- */
  async function stPage(box) {
    const f = { page: 1 };
    box.innerHTML = `<div class="card"><div class="bar"><b>盘点单</b><div class="sp"></div>${can('stock.take') ? '<button class="btn pri" id="s-new">新建盘点</button>' : ''}</div>
      <div class="alert">盘点期间可以照常营业：差异 = 实盘 − (建单时账面 + 建单到录入之间的流水)，审核后才调整库存。</div><div class="tbl-wrap" id="s-tbl"></div></div>`;
    const load = async () => {
      const d = (await api('api/stock/stocktakes?page=' + f.page)).data;
      $('s-tbl').innerHTML = `<table class="tbl"><tr><th>盘点单</th><th>仓库</th><th>范围</th><th>状态</th><th>建单</th><th></th></tr>` +
        (d.items.map(s => `<tr><td>${esc(s.st_no)}</td><td>${esc(s.wh_code)}</td><td>${esc({ full: '全盘', category: '分类', spot: '抽盘', dynamic: '动态' }[s.scope])} ${esc(s.scope_value)}</td><td>${M1.tag(s.status, s.status_name)}</td><td>${esc(s.created_by)} ${esc(s.created_at)}</td>
          <td><button class="btn sm" data-o="${esc(s.st_no)}">打开</button></td></tr>`).join('') || '<tr><td colspan="6" class="empty">没有盘点单</td></tr>') + '</table>' + pager(M1.pg(d));
      bindPager($('s-tbl'), p => { f.page = p; load(); });
      box.querySelectorAll('[data-o]').forEach(b => b.onclick = () => open(b.dataset.o));
    };
    const open = async no => {
      const s = (await api('api/stock/stocktakes/' + no)).data, counting = s.status === 'counting', ex = [];
      const call = async (act, body, m) => { await api('api/stock/stocktakes/' + no + '/' + act, { body: body || {} }); m.close(); toast('已完成', 'ok'); load(); };
      if (counting && can('stock.take')) {
        ex.push({ text: '保存录入', fn: async m => { await api('api/stock/stocktakes/' + no + '/lines', { body: { lines: [...m.root.querySelectorAll('[data-c]')].filter(i => i.value !== '').map(i => ({ product_id: +i.dataset.p, batch_id: +i.dataset.b, base_qty: i.value })) } }); m.close(); open(no); } });
        ex.push({ text: '提交审核', fn: m => call('submit', { skip_uncounted: confirm('未录入的行不调整？') ? 1 : 0 }, m) });
        ex.push({ text: '取消盘点', fn: m => call('cancel', null, m) });
      }
      if (s.status === 'submitted' && can('stock.take.approve')) {
        ex.push({ text: '审核并调整库存', fn: m => call('approve', null, m) });
        ex.push({ text: '驳回重盘', fn: async m => { const r = await ask('驳回', '原因'); call('approve', { reject: 1, reason: r }, m); } });
      }
      modal('盘点单 ' + no + '（' + s.status_name + '）', (counting ? fld('扫码/编码快速录入（每行：编码或条码 数量，累加）', '<textarea id="st-scan" rows="2"></textarea>') : '') +
        `<table class="tbl"><tr><th>商品</th><th>批号</th><th class="r">快照账面</th><th class="r">期间流水</th><th class="r">实盘</th><th class="r">差异</th><th class="r">差异金额</th></tr>${s.lines.map(l => `<tr><td>${esc(l.code)} ${esc(l.name)}</td><td>${esc(l.batch_no || '')}</td><td class="r">${l.book_qty}</td><td class="r">${l.moved_qty || ''}</td>
          <td class="r">${counting ? `<input data-c="1" data-p="${l.product_id}" data-b="${l.batch_id}" value="${l.counted_qty == null ? '' : l.counted_qty}" style="width:70px">` : (l.counted_qty == null ? '未盘' : l.counted_qty)}</td>
          <td class="r" style="color:${l.diff_qty < 0 ? 'var(--pri)' : 'inherit'}">${l.diff_qty || ''}</td><td class="r">${l.diff_amount == null ? '-' : fx(l.diff_amount)}</td></tr>`).join('')}</table>${errBox}`,
      counting ? async m => { const t = $('st-scan').value; if (!t.trim()) return; await api('api/stock/stocktakes/' + no + '/lines', { body: { mode: 'add', lines: M1.parseLines(t) } }); m.close(); open(no); } : null,
      { wide: 'wide', okText: '录入扫码', extra: ex });
    };
    if ($('s-new')) $('s-new').onclick = async () => modal('新建盘点', `<div class="frow">${fld('仓库', await M1.whSel('n-wh'))}${fld('范围', sel('n-sc', [['dynamic', '动态盘点（不停业）'], ['full', '全盘'], ['category', '分类盘点'], ['spot', '抽盘']], 'dynamic'))}</div>
      ${fld('分类名称 / 抽盘商品编码（逗号分隔）', inp('n-sv', ''))}${fld('备注', inp('n-rm', ''))}${errBox}`, async m => {
      const s = (await api('api/stock/stocktakes', { body: { wh_code: v('n-wh'), scope: v('n-sc'), scope_value: v('n-sv'), remark: v('n-rm') } })).data; m.close(); load(); open(s.st_no);
    });
    await load();
  }

  /* -------- 报损报溢 / 调整 / 拆零组装 / 质量转移 -------- */
  async function adjPage(box) {
    const f = { page: 1 };
    const kinds = [['loss', '报损'], ['overflow', '报溢'], ['adjust', '库存调整'], ['split', '拆零'], ['assemble', '组装'], ['quality', '质量状态转移']];
    box.innerHTML = `<div class="card"><div class="bar">${sel('a-k', [['', '全部类型']].concat(kinds), '')}<button class="btn pri" id="a-go">查询</button><div class="sp"></div>${can('stock.adjust') ? '<button class="btn pri" id="a-new">新建单据</button>' : ''}</div>
      <div class="alert">提交后生成审批单（审批人需「审批：库存调整/报损/调拨」权限），审批通过自动过账生成库存流水。</div><div class="tbl-wrap" id="a-tbl"></div></div>`;
    const load = async () => {
      const d = (await api('api/stock/adjusts?' + qs({ kind: v('a-k'), page: f.page }))).data;
      $('a-tbl').innerHTML = `<table class="tbl"><tr><th>单号</th><th>类型</th><th>仓库</th><th>原因</th><th class="r">成本金额</th><th>审批单</th><th>状态</th><th>创建</th><th></th></tr>` +
        (d.items.map(x => `<tr><td>${esc(x.adj_no)}</td><td>${esc(x.kind_name)}</td><td>${esc(x.wh_code)}</td><td>${esc(x.reason)}</td><td class="r">${x.total == null ? '-' : fx(x.total)}</td><td>${esc(x.approval_no)}</td><td>${M1.tag(x.status, x.status_name)}</td><td>${esc(x.created_by)} ${esc(x.created_at)}</td>
          <td><button class="btn sm" data-o="${esc(x.adj_no)}">明细</button></td></tr>`).join('') || '<tr><td colspan="9" class="empty">没有单据</td></tr>') + '</table>' + pager(M1.pg(d));
      bindPager($('a-tbl'), p => { f.page = p; load(); });
      box.querySelectorAll('[data-o]').forEach(b => b.onclick = async () => {
        const x = (await api('api/stock/adjusts/' + b.dataset.o)).data;
        modal(x.kind_name + ' ' + x.adj_no, `<table class="tbl"><tr><th>商品</th><th>批号</th><th>质量</th><th class="r">数量</th><th class="r">成本</th></tr>${x.lines.map(l => `<tr><td>${esc(l.name)}</td><td>${esc(l.batch_no || '')}</td><td>${esc(l.quality)}${l.to_quality ? '→' + esc(l.to_quality) : ''}</td><td class="r">${l.base_qty}</td><td class="r">${l.cost == null ? '-' : fx(l.cost)}</td></tr>`).join('')}</table>`, null);
      });
    };
    if ($('a-new')) $('a-new').onclick = async () => modal('新建库存单据', `<div class="frow">${fld('类型', sel('n-k', kinds, 'loss'))}${fld('仓库', await M1.whSel('n-wh'))}</div>
      ${fld('明细（每行：编码或条码 数量 [单位]；调整可填负数）', '<textarea id="n-lines" rows="5"></textarea>')}
      <div class="frow">${fld('批次 ID（批次商品报损/转移必填，可在批次页查看）', inp('n-bid', ''))}${fld('报溢新批号', inp('n-bno', ''))}${fld('新批次有效期', inp('n-exp', '', 'type="date"'))}</div>
      <div class="frow">${fld('质量状态', sel('n-q', [['ok', '合格'], ['defect', '残次'], ['pending', '待处理']], 'ok'))}${fld('转为（质量转移）', sel('n-tq', [['', '-'], ['ok', '合格'], ['defect', '残次'], ['pending', '待处理']], ''))}</div>
      ${fld('原因*', inp('n-rs', ''))}${fld('附件地址（照片等）', inp('n-at', ''))}${errBox}`, async m => {
      const extra = { batch_id: v('n-bid') || undefined, batch_no: v('n-bno') || undefined, expiry_date: v('n-exp') || undefined, quality: v('n-q'), to_quality: v('n-tq') || undefined };
      const r = (await api('api/stock/adjusts', { body: { kind: v('n-k'), wh_code: v('n-wh'), reason: v('n-rs'), attachment: v('n-at'), lines: M1.parseLines($('n-lines').value).map(x => Object.assign(x, extra)) } })).data;
      m.close(); toast('已提交审批 ' + r.approval_no, 'ok'); load();
    }, { wide: 'wide' });
    $('a-go').onclick = () => { f.page = 1; load(); };
    await load();
  }

  /* -------- 预警 -------- */
  async function alertPage(box) {
    const names = { out: '缺货', low: '低于安全库存', near_expiry: '临期', expired: '过期', slow: '滞销' };
    box.innerHTML = `<div class="card"><div class="bar">${await M1.whSel('w-wh', '', true)}<button class="btn pri" id="w-go">查询</button><div class="sp"></div>
      ${can('stock.adjust.approve') || can('stock.transfer.approve') ? '<button class="btn" id="w-scan">推送提醒</button>' : ''}${can('goods.edit') ? '<button class="btn" id="w-rule">预警规则</button>' : ''}</div><div id="w-body"></div></div>`;
    const load = async () => {
      const d = (await api('api/stock/alerts?' + qs({ wh: v('w-wh') }))).data;
      $('w-body').innerHTML = Object.keys(names).map(k => `<h4>${names[k]}（${d.counts[k] || 0}）</h4><table class="tbl"><tr><th>仓库</th><th>商品</th>${k === 'near_expiry' || k === 'expired' ? '<th>批号</th><th>有效期</th><th class="r">剩余天数</th><th>级别</th>' : ''}<th class="r">数量</th>${k === 'low' || k === 'out' ? '<th class="r">安全库存</th>' : ''}</tr>` +
        ((d[k] || []).map(x => `<tr><td>${esc(x.wh_code)}</td><td>${esc(x.code)} ${esc(x.name)}</td>${k === 'near_expiry' || k === 'expired' ? `<td>${esc(x.batch_no)}</td><td>${esc(x.expiry_date)}</td><td class="r">${x.days_left}</td><td>${x.level ? x.level + ' 天内' : ''}</td>` : ''}<td class="r">${x.qty}</td>${k === 'low' || k === 'out' ? `<td class="r">${x.safety || ''}</td>` : ''}</tr>`).join('') || '<tr><td colspan="8" class="empty">无</td></tr>') + '</table>').join('');
    };
    $('w-go').onclick = load;
    if ($('w-scan')) $('w-scan').onclick = async () => { const r = (await api('api/stock/alerts/scan', { body: {} })).data; toast('已推送 ' + r.notified + ' 条提醒', 'ok'); };
    if ($('w-rule')) $('w-rule').onclick = async () => {
      const rules = (await api('api/stock/alert-rules')).data, mr = rules.find(r => r.target_type === 'merchant') || {};
      modal('预警规则（商户默认；商品/分类单独设置填对象 ID）', `<div class="frow">${fld('对象', sel('r-t', [['merchant', '商户默认'], ['category', '分类'], ['product', '商品']], 'merchant'))}${fld('对象 ID', inp('r-id', ''))}</div>
        <div class="frow">${fld('安全库存', inp('r-s', mr.safety_qty == null ? '' : mr.safety_qty, 'type="number"'))}${fld('临期级别（天）', inp('r-n', mr.near_expiry_days || '30,60,90'))}${fld('滞销天数', inp('r-sl', mr.slow_days || 60, 'type="number"'))}</div>
        <table class="tbl"><tr><th>对象</th><th>ID</th><th>安全库存</th><th>临期级别</th><th>滞销天数</th></tr>${rules.map(r => `<tr><td>${esc(r.target_type)}</td><td>${r.target_id}</td><td>${r.safety_qty == null ? '' : r.safety_qty}</td><td>${esc(r.near_expiry_days)}</td><td>${r.slow_days || ''}</td></tr>`).join('')}</table>${errBox}`, async m => {
        await api('api/stock/alert-rules', { body: { target_type: v('r-t'), target_id: v('r-id'), safety_qty: v('r-s'), near_expiry_days: v('r-n'), slow_days: v('r-sl') } }); m.close(); load();
      });
    };
    await load();
  }

  /* -------- 仓库 -------- */
  async function whPage(box) {
    const load = async () => {
      M1.whs = null;
      const ws = await M1.whList();
      box.innerHTML = `<div class="card"><div class="bar"><b>仓库</b><div class="sp"></div>${can('org.edit') ? '<button class="btn pri" id="h-new">新建仓库</button>' : ''}</div>
        <table class="tbl"><tr><th>编码</th><th>名称</th><th>类型</th><th>所属门店</th><th>可售</th><th>状态</th><th></th></tr>${ws.map(w => `<tr><td>${esc(w.code)}</td><td>${esc(w.name)}</td><td>${esc(w.kind_name)}</td><td>${esc(w.store_code)}</td><td>${w.sellable ? '是' : '否'}</td><td>${esc(w.status)}</td><td>${can('org.edit') ? `<button class="btn sm" data-e="${esc(w.code)}">编辑</button>` : ''}</td></tr>`).join('')}</table></div>`;
      const edit = w => { w = w || { kind: 'hq', sellable: 1, status: '启用' }; modal(w.code ? '编辑仓库' : '新建仓库', `<div class="frow">${fld('编码', inp('h-c', w.code, w.code ? 'readonly' : ''), '门店仓编码 = 门店编码')}${fld('名称', inp('h-n', w.name))}</div>
        <div class="frow">${fld('类型', sel('h-k', [['hq', '总部仓'], ['store', '门店仓'], ['defect', '残次仓']], w.kind))}${fld('所属门店（残次仓可选）', inp('h-s', w.store_code))}${fld('状态', sel('h-st', ['启用', '停用'], w.status))}</div>${errBox}`, async m => {
        await api('api/stock/warehouses', { body: { code: v('h-c'), name: v('h-n'), kind: v('h-k'), store_code: v('h-s'), status: v('h-st') } }); m.close(); load();
      }); };
      if ($('h-new')) $('h-new').onclick = () => edit(null);
      box.querySelectorAll('[data-e]').forEach(b => b.onclick = () => edit(ws.find(x => x.code === b.dataset.e)));
    };
    await load();
  }

  const G = '库存';
  AdminShell.register({ key: 'stock2', title: '库存查询', icon: '📦', group: G, perm: 'stock.view', render: balPage });
  AdminShell.register({ key: 'stock-moves', title: '库存流水', icon: '🧾', group: G, perm: 'stock.view', render: movePage });
  AdminShell.register({ key: 'stock-batch', title: '批次与召回', icon: '🔒', group: G, perm: 'stock.view', render: batchPage });
  AdminShell.register({ key: 'stock-tf', title: '要货与调拨', icon: '🔁', group: G, perm: ['stock.request', 'stock.transfer', 'stock.ship', 'stock.receive', 'stock.transfer.approve'], render: tfPage });
  AdminShell.register({ key: 'stock-st', title: '盘点', icon: '📋', group: G, perm: ['stock.take', 'stock.take.approve'], render: stPage });
  AdminShell.register({ key: 'stock-adj', title: '报损报溢/调整', icon: '⚖️', group: G, perm: ['stock.adjust', 'stock.adjust.approve'], render: adjPage });
  AdminShell.register({ key: 'stock-alert', title: '库存预警', icon: '⏰', group: G, perm: 'stock.view', render: alertPage });
  AdminShell.register({ key: 'stock-wh', title: '仓库', icon: '🏬', group: G, perm: 'stock.view', render: whPage });
})();
