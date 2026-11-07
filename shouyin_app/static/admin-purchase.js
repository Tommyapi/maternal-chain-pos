/* 管理后台 · purchase 模块页面。Owner：模块①。用 AdminShell.register({...}) 注册页面（见 admin.js 末尾说明）。
   接口：api/purchase/*（{ok,data}）；共用工具 M1 在 admin-goods.js。 */
'use strict';
(function () {
  async function supOpts(id, cur) {
    const d = (await api('api/purchase/suppliers?size=200&status=启用')).data.items;
    return sel(id, d.map(s => [s.id, s.code + ' ' + s.name]), cur);
  }

  async function supPage(box) {
    const canEdit = AdminShell.can('supplier.edit');
    const f = { kw: '', page: 1 };
    box.innerHTML = `<div class="card"><div class="bar"><input id="s-kw" placeholder="名称/编码/联系人"><button class="btn pri" id="s-go">查询</button><div class="sp"></div>
      ${canEdit ? '<button class="btn pri" id="s-new">新增供应商</button>' : ''}</div><div class="tbl-wrap" id="s-tbl"></div></div>`;
    let rows = [];
    const load = async () => {
      const d = (await api('api/purchase/suppliers?' + qs({ kw: v('s-kw'), page: f.page }))).data;
      rows = d.items;
      $('s-tbl').innerHTML = `<table class="tbl"><tr><th>编码</th><th>名称</th><th>联系人</th><th>电话</th><th>结算资料</th><th>状态</th><th></th></tr>` +
        (rows.map(s => `<tr><td>${esc(s.code)}</td><td>${esc(s.name)}</td><td>${esc(s.contact)}</td><td>${esc(s.phone)}</td><td>${esc(s.settle_info)}</td><td>${M1.tag(s.status === '启用' ? 'normal' : 'closed', s.status)}</td>
          <td>${canEdit ? `<button class="btn sm" data-e="${s.id}">编辑</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="7" class="empty">没有供应商</td></tr>') + '</table>' + pager(M1.pg(d));
      bindPager($('s-tbl'), p => { f.page = p; load(); });
      box.querySelectorAll('[data-e]').forEach(b => b.onclick = () => edit(rows.find(x => x.id == b.dataset.e)));
    };
    const edit = s => {
      s = s || { status: '启用' };
      modal(s.id ? '编辑供应商' : '新增供应商', `<div class="frow">${fld('名称*', inp('s-name', s.name))}${fld('状态', sel('s-st', ['启用', '停用'], s.status), '停用不删除历史采购记录')}</div>
        <div class="frow">${fld('联系人', inp('s-ct', s.contact))}${fld('电话', inp('s-ph', s.phone))}</div>${fld('地址', inp('s-ad', s.address))}${fld('结算资料（开户行/账号/账期）', inp('s-se', s.settle_info))}${errBox}`, async m => {
        await api('api/purchase/suppliers', { body: { id: s.id, name: v('s-name'), status: v('s-st'), contact: v('s-ct'), phone: v('s-ph'), address: v('s-ad'), settle_info: v('s-se') } });
        m.close(); toast('已保存', 'ok'); load();
      });
    };
    $('s-go').onclick = () => { f.page = 1; load(); };
    if ($('s-new')) $('s-new').onclick = () => edit(null);
    await load();
  }

  async function poPage(box) {
    const f = { status: '', page: 1 };
    const st = [['', '全部状态'], ['draft', '草稿'], ['submitted', '待审核'], ['approved', '已审核'], ['partial', '部分到货'], ['received', '已到货'], ['closed', '已关闭'], ['cancelled', '已取消']];
    box.innerHTML = `<div class="card"><div class="bar">${sel('o-st', st, '')}<input id="o-kw" placeholder="采购单号"><button class="btn pri" id="o-go">查询</button><div class="sp"></div>
      ${AdminShell.can('purchase.receive') ? '<button class="btn" id="o-rcv">无单收货</button>' : ''}${AdminShell.can('purchase.edit') ? '<button class="btn pri" id="o-new">新建采购单</button>' : ''}</div>
      <div class="tbl-wrap" id="o-tbl"></div></div>`;
    const load = async () => {
      const d = (await api('api/purchase/orders?' + qs({ status: v('o-st'), kw: v('o-kw'), page: f.page }))).data;
      $('o-tbl').innerHTML = `<table class="tbl"><tr><th>采购单号</th><th>供应商</th><th>收货仓</th><th>预计到货</th><th class="r">金额</th><th>状态</th><th>创建</th><th></th></tr>` +
        (d.items.map(o => `<tr><td>${esc(o.po_no)}</td><td>${esc(o.supplier_name)}</td><td>${esc(o.wh_code)}</td><td>${esc(o.expect_date)}</td><td class="r">${fx(o.total)}</td><td>${M1.tag(o.status, o.status_name)}</td>
          <td>${esc(o.created_by)} ${esc(o.created_at)}</td><td><button class="btn sm" data-o="${esc(o.po_no)}">打开</button></td></tr>`).join('') || '<tr><td colspan="8" class="empty">没有采购单</td></tr>') + '</table>' + pager(M1.pg(d));
      bindPager($('o-tbl'), p => { f.page = p; load(); });
      box.querySelectorAll('[data-o]').forEach(b => b.onclick = () => open(b.dataset.o));
    };
    const open = async no => {
      const o = (await api('api/purchase/orders/' + no)).data;
      const act = (url, body, msg) => async m => { await api('api/purchase/orders/' + no + '/' + url, { body: body ? body() : {} }); m.close(); toast(msg, 'ok'); load(); };
      const ex = [];
      if (o.status === 'draft' && AdminShell.can('purchase.edit')) ex.push({ text: '提交审核', fn: act('submit', null, '已提交') });
      if (o.status === 'submitted' && AdminShell.can('purchase.approve')) {
        ex.push({ text: '审核通过', fn: act('approve', null, '已审核') });
        ex.push({ text: '驳回', fn: m => { const r = prompt('驳回原因'); if (r) act('approve', () => ({ reject: 1, reason: r }), '已驳回')(m); } });
      }
      if (['approved', 'partial'].indexOf(o.status) >= 0 && AdminShell.can('purchase.receive')) ex.push({ text: '收货', fn: m => { m.close(); receive(o); } });
      if (['partial', 'approved'].indexOf(o.status) >= 0) ex.push({ text: '关闭', fn: m => { const r = prompt('关闭原因（剩余不再到货）'); if (r) act('close', () => ({ reason: r }), '已关闭')(m); } });
      if (['draft', 'submitted', 'approved'].indexOf(o.status) >= 0 && AdminShell.can('purchase.edit')) ex.push({ text: '取消', fn: act('cancel', null, '已取消') });
      modal('采购单 ' + no, `<div class="kv"><span>供应商</span><b>${esc(o.supplier_name)}</b><span>收货仓</span><b>${esc(o.wh_code)}</b><span>状态</span><b>${esc(o.status_name)}</b><span>金额</span><b>${fx(o.total)}</b><span>收货单</span><b>${esc(o.receipts.join('，') || '-')}</b></div>
        <table class="tbl"><tr><th>商品</th><th>单位</th><th class="r">数量</th><th class="r">单价</th><th class="r">已收(基本单位)</th><th class="r">拒收</th><th class="r">未到</th></tr>${o.lines.map(l => `<tr><td>${esc(l.code)} ${esc(l.name)}</td><td>${esc(l.unit)}${l.rate > 1 ? '(' + l.rate + ')' : ''}</td><td class="r">${l.qty}</td><td class="r">${fx(l.price)}</td><td class="r">${l.received_base_qty}</td><td class="r">${l.rejected_base_qty}</td><td class="r">${l.remain_base_qty}</td></tr>`).join('')}</table>`, null, { wide: 'wide', extra: ex });
    };
    const receive = (o) => {
      modal('收货 · ' + (o ? o.po_no : '无单收货'), (o ? '' : '<div class="frow" id="r-head"></div>') +
        `<table class="tbl"><tr><th>商品</th><th>本次实收</th><th>拒收</th><th>差异/拒收原因</th><th>批号</th><th>生产日期</th><th>有效期</th></tr>` +
        (o ? o.lines.filter(l => l.remain_base_qty > 0).map(l => `<tr data-l="${l.id}"><td>${esc(l.name)}（${esc(l.unit)}，未到 ${l.remain_base_qty / l.rate}）</td><td><input data-k="qty" style="width:60px" value="${l.remain_base_qty / l.rate}"></td>
          <td><input data-k="rejected_qty" style="width:50px" value="0"></td><td><input data-k="diff_reason"></td><td><input data-k="batch_no" style="width:90px"></td><td><input data-k="prod_date" type="date"></td><td><input data-k="expiry_date" type="date"></td></tr>`).join('') : '') + '</table>' +
        (o ? '' : fld('收货明细（每行：编码 数量 [单位] [单价]，批次商品用有单收货或下方批号同一批）', '<textarea id="r-lines" rows="3"></textarea>') + `<div class="frow">${fld('批号', inp('r-bno', ''))}${fld('有效期', inp('r-exp', '', 'type="date"'))}</div>`) + errBox, async m => {
        let body;
        if (o) {
          body = { po_no: o.po_no, lines: [...m.root.querySelectorAll('[data-l]')].map(tr => { const x = { po_line_id: +tr.dataset.l }; tr.querySelectorAll('[data-k]').forEach(i => { x[i.dataset.k] = i.value; }); return x; }) };
        } else {
          body = { supplier_id: v('r-sup'), wh_code: v('r-wh'), lines: M1.parseLines($('r-lines').value, true).map(x => Object.assign(x, { batch_no: v('r-bno'), expiry_date: v('r-exp') })) };
        }
        const r = (await api('api/purchase/receipts', { body })).data; m.close(); toast('已收货过账：' + r.gr_no, 'ok'); load();
      }, { wide: 'wide', onOpen: async root => { if (!o) root.querySelector('#r-head').innerHTML = fld('供应商', await supOpts('r-sup')) + fld('收货仓库', await M1.whSel('r-wh')); } });
    };
    const create = async () => {
      modal('新建采购单', `<div class="frow">${fld('供应商*', await supOpts('n-sup'))}${fld('收货仓库*', await M1.whSel('n-wh'))}${fld('预计到货日', inp('n-exp', '', 'type="date"'))}</div>
        ${fld('商品明细（每行：编码或条码 数量 [单位] [采购单价]）', '<textarea id="n-lines" rows="6" placeholder="P1 10 箱 1800"></textarea>')}${fld('备注', inp('n-rm', ''))}
        <label><input type="checkbox" id="n-sub" checked> 保存后直接提交审核</label>${errBox}`, async m => {
        const r = (await api('api/purchase/orders', { body: { supplier_id: v('n-sup'), wh_code: v('n-wh'), expect_date: v('n-exp'), remark: v('n-rm'), submit: $('n-sub').checked, lines: M1.parseLines($('n-lines').value, true) } })).data;
        m.close(); toast('已保存 ' + r.po_no, 'ok'); load();
      }, { wide: 'wide' });
    };
    $('o-go').onclick = () => { f.page = 1; load(); };
    if ($('o-new')) $('o-new').onclick = create;
    if ($('o-rcv')) $('o-rcv').onclick = () => receive(null);
    await load();
  }

  async function retPage(box) {
    const f = { page: 1 };
    box.innerHTML = `<div class="card"><div class="bar"><b>采购退货</b><div class="sp"></div>${AdminShell.can('purchase.return') ? '<button class="btn pri" id="t-new">新建采购退货</button>' : ''}</div><div class="tbl-wrap" id="t-tbl"></div></div>`;
    const load = async () => {
      const d = (await api('api/purchase/returns?page=' + f.page)).data;
      $('t-tbl').innerHTML = `<table class="tbl"><tr><th>退货单号</th><th>原收货单</th><th>仓库</th><th>明细</th><th class="r">金额</th><th>原因</th><th>状态</th><th></th></tr>` +
        (d.items.map(r => `<tr><td>${esc(r.pr_no)}</td><td>${esc(r.gr_no)}</td><td>${esc(r.wh_code)}</td><td style="font-size:12px">${r.lines.map(l => esc(l.name) + ' ' + l.qty + esc(l.unit) + (l.batch_no ? '（' + esc(l.batch_no) + '）' : '')).join('<br>')}</td>
          <td class="r">${fx(r.total)}</td><td>${esc(r.reason)}</td><td>${M1.tag(r.status, r.status_name)}</td>
          <td>${r.status === 'submitted' && AdminShell.can('purchase.approve') ? `<button class="btn sm" data-a="${esc(r.pr_no)}">审核出库</button> <button class="btn sm red" data-r="${esc(r.pr_no)}">驳回</button>` : ''}</td></tr>`).join('') ||
          '<tr><td colspan="8" class="empty">没有采购退货</td></tr>') + '</table>' + pager(M1.pg(d));
      bindPager($('t-tbl'), p => { f.page = p; load(); });
      box.querySelectorAll('[data-a]').forEach(b => b.onclick = async () => { try { await api('api/purchase/returns/' + b.dataset.a + '/approve', { body: {} }); toast('已出库', 'ok'); load(); } catch (e) { toast(e.message, 'err'); } });
      box.querySelectorAll('[data-r]').forEach(b => b.onclick = async () => { const r = prompt('驳回原因'); if (!r) return; await api('api/purchase/returns/' + b.dataset.r + '/approve', { body: { reject: 1, reason: r } }); load(); });
    };
    if ($('t-new')) $('t-new').onclick = () => modal('新建采购退货', fld('原收货单号（填了按原单校验可退数量）', inp('t-gr', '')) + '<div class="frow" id="t-head"></div>' +
      fld('退货明细（每行：编码 数量 [单位]）', '<textarea id="t-lines" rows="4"></textarea>') + fld('指定批次 ID（召回批次退回时填写，可空）', inp('t-bid', '')) + fld('退货原因*', inp('t-rs', '')) + errBox, async m => {
      const bid = v('t-bid');
      await api('api/purchase/returns', { body: { gr_no: v('t-gr'), supplier_id: v('t-sup'), wh_code: v('t-wh'), reason: v('t-rs'), lines: M1.parseLines($('t-lines').value).map(x => Object.assign(x, bid ? { batch_id: bid } : {})) } });
      m.close(); toast('已提交审核', 'ok'); load();
    }, { onOpen: async root => { root.querySelector('#t-head').innerHTML = fld('供应商（无原单时）', await supOpts('t-sup')) + fld('出库仓库（无原单时）', await M1.whSel('t-wh')); } });
    await load();
  }

  async function pricePage(box) {
    if (!AdminShell.can('goods.cost.view')) { box.innerHTML = '<div class="card"><div class="empty">查看采购价需要成本价权限</div></div>'; return; }
    const d = (await api('api/purchase/prices?size=200')).data;
    box.innerHTML = `<div class="card"><table class="tbl"><tr><th>时间</th><th>商品</th><th>供应商</th><th>单位</th><th class="r">单价</th><th>来源单</th></tr>${d.items.map(x => `<tr><td>${esc(x.created_at)}</td><td>${esc(x.code)} ${esc(x.name)}</td><td>${esc(x.supplier_name)}</td><td>${esc(x.unit)}${x.rate > 1 ? '(' + x.rate + ')' : ''}</td><td class="r">${fx(x.price)}</td><td>${esc(x.src_no)}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">暂无</td></tr>'}</table></div>`;
  }

  AdminShell.register({ key: 'pur-po', title: '采购单与收货', icon: '🚚', group: '采购', perm: ['purchase.view', 'purchase.receive'], render: poPage });
  AdminShell.register({ key: 'pur-ret', title: '采购退货', icon: '↩️', group: '采购', perm: ['purchase.return', 'purchase.view'], render: retPage });
  AdminShell.register({ key: 'pur-sup', title: '供应商', icon: '🏭', group: '采购', perm: ['supplier.view', 'purchase.view'], render: supPage });
  AdminShell.register({ key: 'pur-price', title: '历史采购价', icon: '💹', group: '采购', perm: 'purchase.view', render: pricePage });
})();
