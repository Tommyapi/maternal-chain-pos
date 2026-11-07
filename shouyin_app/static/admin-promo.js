/* 管理后台 · promo 模块页面。Owner：模块②。用 AdminShell.register({...}) 注册页面（见 admin.js 末尾说明）。
   接口：api/promo/*（新接口 {ok,data}）。全部函数包在 IIFE 里，避免与 admin.js 旧函数重名。 */
'use strict';
(function () {
  const can = p => AdminShell.can(p);
  const pg = (d, box, goto) => {
    box.insertAdjacentHTML('beforeend', pager({ total: d.total, page: d.page, pages: Math.max(1, Math.ceil(d.total / d.size)), pageSize: d.size }));
    bindPager(box, goto);
  };
  const tag = (t, cls) => `<span class="tag ${cls || ''}">${esc(t)}</span>`;
  const ST_CLS = { approved: 'green', submitted: '', paused: '', draft: '', ended: '', cancelled: '' };
  const TYPES = [['full_reduce', '满减'], ['order_discount', '整单折扣'], ['special_price', '特价'], ['item_discount', '单品折扣'], ['nth_discount', '第N件折扣'],
    ['bundle_price', '套餐价'], ['redeem', '换购'], ['full_gift', '满赠']];
  const RULE_SAMPLE = {
    full_reduce: { tiers: [{ threshold: 300, reduce: 20 }, { threshold: 600, reduce: 50 }], repeat: false },
    order_discount: { threshold: 0, rate: '0.95' }, special_price: { items: [{ code: '商品编码', price: 99 }] }, item_discount: { rate: '0.9' },
    nth_discount: { n: 2, rate: '0.5' }, bundle_price: { codes: ['编码1', '编码2'], price: 199 }, redeem: { code: '换购商品编码', threshold: 200, price: 9.9, max_qty: 1 },
    full_gift: { threshold: 300, gift_code: '赠品编码', gift_qty: 1, repeat: false },
  };
  const pj = (id, label) => { const s = v(id); if (!s) return {}; try { return JSON.parse(s); } catch (e) { throw new Error(label + ' 不是合法 JSON'); } };
  const now = () => localDate() + ' 00:00:00';

  /* ---------------- 促销活动 ---------------- */
  async function actsPage(box) {
    const f = { page: 1 };
    box.innerHTML = `<div class="card"><div class="bar"><input id="pa-kw" placeholder="活动号 / 名称"><select id="pa-st"><option value="">全部状态</option>
      ${[['draft', '草稿'], ['submitted', '审批中'], ['approved', '已审核'], ['paused', '已暂停'], ['ended', '已终止'], ['cancelled', '已作废']].map(o => `<option value="${o[0]}">${o[1]}</option>`).join('')}</select>
      <select id="pa-ty"><option value="">全部类型</option>${TYPES.map(o => `<option value="${o[0]}">${o[1]}</option>`).join('')}</select><button class="btn pri" id="pa-go">查询</button><div class="sp"></div>
      ${can('promo.edit') ? '<button class="btn" id="pa-new">新建活动</button>' : ''}</div><div class="tbl-wrap" id="pa-tbl"></div></div>`;
    const load = async () => {
      const d = (await api('api/promo/activities?' + qs({ kw: v('pa-kw'), status: v('pa-st'), type: v('pa-ty'), page: f.page, size: 20 }))).data;
      $('pa-tbl').innerHTML = `<table class="tbl"><tr><th>活动号</th><th>名称</th><th>类型</th><th>规则说明</th><th>时间</th><th>状态</th><th></th></tr>` +
        (d.items.map(a => `<tr><td>${esc(a.act_no)}</td><td>${esc(a.name)}</td><td>${esc(a.type_name)}</td><td style="font-size:12px;max-width:360px">${esc(a.explain)}</td>
          <td style="font-size:12px">${esc(a.start_at)}<br>${esc(a.end_at)}</td><td>${tag(a.status_name, ST_CLS[a.status])}</td><td style="white-space:nowrap">${actBtns(a)}</td></tr>`).join('')
          || '<tr><td colspan="7" class="empty">还没有活动</td></tr>') + '</table>';
      pg(d, $('pa-tbl'), p => { f.page = p; load(); });
      $('pa-tbl').querySelectorAll('[data-op]').forEach(b => b.onclick = () => actOp(b.dataset.no, b.dataset.op, d.items.find(x => x.act_no === b.dataset.no), load));
    };
    $('pa-go').onclick = () => { f.page = 1; load().catch(e => toast(e.message, 'err')); };
    if ($('pa-new')) $('pa-new').onclick = () => actModal(null, load);
    await load();
  }
  function actBtns(a) {
    const b = (op, t, cls) => `<button class="btn sm ${cls || ''}" data-no="${esc(a.act_no)}" data-op="${op}">${t}</button> `;
    let h = '';
    if (a.status === 'draft' && can('promo.edit')) h += b('edit', '编辑') + b('submit', '提交审核') + b('cancel', '作废', 'red');
    if (a.status === 'submitted' && can('promo.approve')) h += b('approve', '同意') + b('reject', '驳回', 'red');
    if (a.status === 'submitted' && can('promo.edit')) h += b('cancel', '撤回作废', 'red');
    if (a.status === 'approved' && can('promo.edit')) h += b('pause', '暂停') + b('end', '终止', 'red');
    if (a.status === 'paused' && can('promo.approve')) h += b('resume', '恢复');
    if (a.status === 'paused' && can('promo.edit')) h += b('end', '终止', 'red');
    if (can('promo.edit')) h += b('copy', '复制');
    return h;
  }
  async function actOp(no, op, a, done) {
    try {
      if (op === 'edit') return actModal(a, done);
      if (['pause', 'end', 'reject'].indexOf(op) >= 0) {
        const mm = modal({ pause: '暂停活动', end: '终止活动', reject: '驳回活动' }[op], fld('原因*', inp('ao-r', '')) + errBox, async () => {
          await api('api/promo/activities/' + no + '/' + op, { body: { reason: v('ao-r') } }); mm.close(); toast('已处理', 'ok'); done();
        });
        return;
      }
      await api('api/promo/activities/' + no + '/' + op, { body: {} });
      toast('已处理', 'ok'); done();
    } catch (e) { toast(e.message, 'err'); }
  }
  function actModal(a, done) {
    a = a || { type: 'full_reduce', priority: 100, stack_coupon: 1, stack_member_price: 0, rule: RULE_SAMPLE.full_reduce, scope: {}, start_at: now(), end_at: addDays(30) + ' 23:59:59' };
    const sc = a.scope || {};
    const j = x => (x || []).join(',');
    const mm = modal(a.act_no ? '编辑活动 ' + a.act_no : '新建活动', `<div class="frow">${fld('名称*', inp('ac-n', a.name))}${fld('类型*', sel('ac-t', TYPES, a.type))}</div>
      <div class="frow">${fld('开始*', inp('ac-s', a.start_at))}${fld('结束*', inp('ac-e', a.end_at))}${fld('优先级（小的先算）', inp('ac-p', a.priority, 'type="number"'))}</div>
      <div class="frow">${fld('互斥组（同组一单只享一个）', inp('ac-g', a.exclusive_group))}${fld('与会员价叠加', sel('ac-m', [['0', '不叠加（会员价更优则不用活动）'], ['1', '叠加（在会员价上再优惠）']], String(a.stack_member_price || 0)))}
      ${fld('与优惠券同享', sel('ac-c', [['1', '可同享'], ['0', '不可同享']], String(a.stack_coupon == null ? 1 : a.stack_coupon)))}</div>
      ${fld('规则（JSON，金额单位元）', `<textarea id="ac-r" rows="4" style="width:100%;font-family:monospace">${esc(JSON.stringify(a.rule))}</textarea>`, '<a href="#" id="ac-sample">填入该类型示例</a>')}
      <div class="frow">${fld('适用门店（编码，逗号分隔；空=全部）', inp('ac-ss', j(sc.stores)))}${fld('适用区域', inp('ac-sr', j(sc.regions)))}${fld('会员等级', inp('ac-sl', j(sc.levels)))}</div>
      <div class="frow">${fld('商品编码', inp('ac-sp', j(sc.product_codes)))}${fld('分类', inp('ac-sc', j(sc.categories)))}${fld('品牌', inp('ac-sb', j(sc.brands)))}${fld('排除商品', inp('ac-sx', j(sc.exclude_codes)))}</div>
      ${fld('积分倍率（选填，如 2 = 双倍积分）', inp('ac-pr', a.points_rate || ''))}${errBox}`, async () => {
      const L = id => v(id).split(/[,，\s]+/).filter(Boolean);
      const body = { act_no: a.act_no, name: v('ac-n'), type: v('ac-t'), start_at: v('ac-s'), end_at: v('ac-e'), priority: v('ac-p'), exclusive_group: v('ac-g'),
        stack_member_price: v('ac-m') === '1', stack_coupon: v('ac-c') === '1', rule: pj('ac-r', '规则'), points_rate: v('ac-pr'),
        scope: { stores: L('ac-ss'), regions: L('ac-sr'), levels: L('ac-sl'), product_codes: L('ac-sp'), categories: L('ac-sc'), brands: L('ac-sb'), exclude_codes: L('ac-sx') } };
      const r = (await api('api/promo/activities', { body: body })).data;
      toast('已保存：' + r.explain, 'ok'); mm.close(); done();
    }, { wide: 'wide', onOpen: () => { $('ac-sample').onclick = e => { e.preventDefault(); $('ac-r').value = JSON.stringify(RULE_SAMPLE[v('ac-t')]); }; } });
  }

  /* ---------------- 促销试算（与收银同一引擎） ---------------- */
  async function tryPage(box) {
    box.innerHTML = `<div class="card"><div class="hint">与收银台同一计价引擎；可指定时间试算未来生效的活动和调价。</div>
      <div class="frow">${fld('门店', sel('pt-st', storeOpts(false), S.me.store_code || (S.stores[0] || {}).code))}${fld('会员（号/手机，选填）', inp('pt-m', ''))}${fld('试算时间（选填）', inp('pt-at', '', 'placeholder="YYYY-MM-DD HH:MM:SS"'))}</div>
      ${fld('商品（每行：编码或条码 数量 [单位]）', '<textarea id="pt-items" rows="4" style="width:100%" placeholder="6900000000011 2\nP2 1 箱"></textarea>')}
      <div class="frow">${fld('券号（逗号分隔）', inp('pt-c', ''))}${fld('抵扣积分', inp('pt-p', '', 'type="number"'))}${fld('整单折扣率', inp('pt-r', '', 'placeholder="0.95"'))}${fld('抹零', sel('pt-ro', [['', '按收银参数'], ['none', '不抹零'], ['jiao', '抹分'], ['yuan', '抹角']], ''))}</div>
      <button class="btn pri" id="pt-go">试算</button><div id="pt-res" style="margin-top:12px"></div></div>`;
    $('pt-go').onclick = async () => {
      try {
        const items = v('pt-items').split('\n').map(s => s.trim()).filter(Boolean).map(s => { const p = s.split(/\s+/); return { code: p[0], qty: p[1] || 1, unit: p[2] || '' }; });
        const cart = { store_code: v('pt-st'), items: items, member_code: v('pt-m'), coupon_nos: v('pt-c').split(/[,，\s]+/).filter(Boolean), points_used: v('pt-p') || 0, at: v('pt-at') };
        if (v('pt-r')) cart.order_discount = { rate: v('pt-r') };
        if (v('pt-ro')) cart.round_mode = v('pt-ro');
        const r = (await api('api/promo/try', { body: cart })).data;
        $('pt-res').innerHTML = `<div class="kpis"><div class="kpi"><span>原价合计</span><b>${fx(r.retail_total)}</b></div><div class="kpi"><span>应付</span><b>${fx(r.payable)}</b></div>
          <div class="kpi"><span>优惠合计</span><b>${fx(r.retail_total - r.payable)}</b></div><div class="kpi"><span>本单积分</span><b>${r.points_earned}</b></div></div>
          <table class="tbl"><tr><th>#</th><th>商品</th><th>单位</th><th class="r">数量</th><th class="r">原价</th><th class="r">成交价</th><th class="r">行金额</th><th class="r">分摊</th><th class="r">实付</th><th>优惠来源</th></tr>
          ${r.lines.map(l => `<tr><td>${l.line_no}</td><td>${esc(l.name)}${l.is_gift ? tag('赠品', 'green') : ''}</td><td>${esc(l.unit)}${l.unit_rate > 1 ? '×' + l.unit_rate : ''}</td><td class="r">${l.qty}</td><td class="r num">${fx(l.retail)}</td><td class="r num">${fx(l.price)}</td><td class="r num">${fx(l.line_amount)}</td><td class="r num">${fx(l.share)}</td><td class="r num">${fx(l.amount)}</td><td style="font-size:12px">${l.promos.map(p => esc(p.name + ' -' + fx(p.amount))).join('<br>')}</td></tr>`).join('')}</table>
          <h4>优惠明细</h4><table class="tbl"><tr><th>类型</th><th>行</th><th>来源</th><th class="r">金额</th></tr>${r.discounts.map(x => `<tr><td>${esc(x.kind)}</td><td>${x.line_no}</td><td>${esc(x.source_name || x.source_no)}</td><td class="r num">${fx(x.amount)}</td></tr>`).join('')}</table>
          ${r.need_auth.length ? `<div class="alert">需授权：${r.need_auth.map(x => esc(x.label)).join('；')}</div>` : ''}
          ${r.explain.length ? `<div class="hint">${r.explain.map(esc).join('<br>')}</div>` : ''}
          ${r.member_coupons ? `<h4>会员可用券</h4>${r.member_coupons.map(c => `<div class="hint">${esc(c.coupon_no)} ${esc(c.name)}：${c.usable ? '可用，优惠 ' + fx(c.amount) : '不可用：' + esc(c.reason)}</div>`).join('') || '<div class="hint">无</div>'}` : ''}`;
      } catch (e) { $('pt-res').innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
    };
  }

  /* ---------------- 优惠券 ---------------- */
  async function couponsPage(box) {
    let tab = 't';
    const draw = async () => {
      box.innerHTML = `<div class="card"><div class="tabs" id="cp-tabs"><button data-t="t" class="${tab === 't' ? 'on' : ''}">券模板</button><button data-t="c" class="${tab === 'c' ? 'on' : ''}">券实例与流水</button></div><div id="cp-body"></div></div>`;
      box.querySelectorAll('#cp-tabs button').forEach(b => b.onclick = () => { tab = b.dataset.t; draw(); });
      if (tab === 't') {
        const d = (await api('api/promo/coupon-templates?size=200')).data;
        $('cp-body').innerHTML = (can('coupon.edit') ? '<div class="bar"><button class="btn pri" id="ct2-new">新建券模板</button></div>' : '') +
          `<table class="tbl"><tr><th>名称</th><th>类型</th><th class="r">面额/折扣</th><th class="r">门槛</th><th>有效期</th><th>限领/总量</th><th class="r">已发</th><th>整单退回</th><th>状态</th><th></th></tr>${d.items.map(t => `<tr><td>${esc(t.name)}</td><td>${esc(t.type_name)}</td>
          <td class="r num">${t.type === 'discount' ? esc(t.discount_rate) + ' 折' : fx(t.face_value)}</td><td class="r num">${fx(t.threshold)}</td><td style="font-size:12px">${t.end_date ? '至 ' + esc(t.end_date) : t.valid_days + ' 天'}</td>
          <td>${t.per_member_limit || '不限'} / ${t.total_limit || '不限'}</td><td class="r">${t.issued_count}</td><td>${t.return_on_refund ? '是' : '否'}</td><td>${tag(t.status, t.status === '启用' ? 'green' : '')}</td>
          <td style="white-space:nowrap">${can('coupon.issue') ? `<button class="btn sm" data-iss="${t.id}">发券</button>` : ''} ${can('coupon.edit') ? `<button class="btn sm" data-ed="${t.id}">编辑</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="10" class="empty">还没有券模板</td></tr>'}</table>`;
        if ($('ct2-new')) $('ct2-new').onclick = () => tplModal(null, draw);
        $('cp-body').querySelectorAll('[data-ed]').forEach(b => b.onclick = () => tplModal(d.items.find(x => x.id == b.dataset.ed), draw));
        $('cp-body').querySelectorAll('[data-iss]').forEach(b => b.onclick = () => issueModal(d.items.find(x => x.id == b.dataset.iss), draw));
      } else {
        const f = { page: 1 };
        $('cp-body').innerHTML = `<div class="bar"><input id="cc-m" placeholder="会员号"><select id="cc-s"><option value="">全部状态</option>${['未使用', '已锁定', '已核销', '已作废', '已过期'].map(s => `<option>${s}</option>`).join('')}</select><button class="btn pri" id="cc-go">查询</button>
          <div class="sp"></div>${can('coupon.void') ? '<button class="btn" id="cc-exp">处理过期券</button>' : ''}</div><div id="cc-tbl"></div>`;
        const load = async () => {
          const d = (await api('api/promo/coupons?' + qs({ member_code: v('cc-m'), status: v('cc-s'), page: f.page, size: 30 }))).data;
          $('cc-tbl').innerHTML = `<table class="tbl"><tr><th>券号</th><th>名称</th><th>会员</th><th class="r">面额</th><th>有效期至</th><th>状态</th><th>核销/占用单号</th><th></th></tr>${d.items.map(c => `<tr><td>${esc(c.coupon_no)}</td><td>${esc(c.template_name)}</td><td>${esc(c.member_code)}</td>
            <td class="r num">${fx(c.face_value)}</td><td>${esc(c.expire_date)}</td><td>${tag(c.status, c.status === '未使用' ? 'green' : '')}</td><td>${esc(c.order_no || c.locked_order_no || '')}</td>
            <td style="white-space:nowrap"><button class="btn sm" data-lg="${esc(c.coupon_no)}">流水</button> ${can('coupon.void') && c.status === '未使用' ? `<button class="btn sm red" data-vd="${esc(c.coupon_no)}">作废</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="8" class="empty">没有券</td></tr>'}</table>`;
          pg(d, $('cc-tbl'), p => { f.page = p; load(); });
          $('cc-tbl').querySelectorAll('[data-lg]').forEach(b => b.onclick = async () => {
            const c = (await api('api/promo/coupons/' + b.dataset.lg)).data;
            modal('券流水 ' + c.coupon_no, `<table class="tbl"><tr><th>时间</th><th>从</th><th>到</th><th>单号</th><th>操作人</th><th>说明</th></tr>${c.logs.map(l => `<tr><td>${esc(l.created_at)}</td><td>${esc(l.from_status || '')}</td><td>${esc(l.to_status)}</td><td>${esc(l.ref_no)}</td><td>${esc(l.operator)}</td><td>${esc(l.note)}</td></tr>`).join('')}</table>`);
          });
          $('cc-tbl').querySelectorAll('[data-vd]').forEach(b => b.onclick = () => {
            const mm = modal('作废优惠券 ' + b.dataset.vd, fld('原因*', inp('cv-r', '')) + errBox, async () => { await api('api/promo/coupons/' + b.dataset.vd + '/void', { body: { reason: v('cv-r') } }); mm.close(); load(); });
          });
        };
        $('cc-go').onclick = () => { f.page = 1; load().catch(e => toast(e.message, 'err')); };
        if ($('cc-exp')) $('cc-exp').onclick = async () => { try { const r = (await api('api/promo/coupons/expire', { body: {} })).data; toast('已过期 ' + r.expired + ' 张', 'ok'); load(); } catch (e) { toast(e.message, 'err'); } };
        await load();
      }
    };
    await draw();
  }
  function tplModal(t, done) {
    t = t || { type: 'cash', valid_days: 30, status: '启用', return_on_refund: 1, scope: {} };
    const sc = t.scope || {};
    const mm = modal(t.id ? '编辑券模板' : '新建券模板', `<div class="frow">${fld('名称*', inp('tp-n', t.name))}${fld('类型', sel('tp-t', [['cash', '满减券'], ['discount', '折扣券'], ['free', '无门槛券'], ['product', '商品券']], t.type))}</div>
      <div class="frow">${fld('面额（元）', inp('tp-f', t.face_value, 'type="number" step="0.01"'))}${fld('使用门槛（元）', inp('tp-h', t.threshold, 'type="number" step="0.01"'))}${fld('折扣率（折扣券）', inp('tp-r', t.discount_rate || ''))}${fld('折扣封顶（元）', inp('tp-x', t.max_discount || ''))}</div>
      <div class="frow">${fld('有效天数', inp('tp-d', t.valid_days, 'type="number"'))}${fld('发放截止日', inp('tp-e', t.end_date || '', 'type="date"'))}${fld('每人限领（0=不限）', inp('tp-pl', t.per_member_limit || 0, 'type="number"'))}${fld('发放总量（0=不限）', inp('tp-tl', t.total_limit || 0, 'type="number"'))}</div>
      <div class="frow">${fld('适用门店（编码，逗号分隔）', inp('tp-ss', (sc.stores || []).join(',')))}${fld('适用商品编码', inp('tp-sp', (sc.product_codes || []).join(',')))}${fld('适用分类', inp('tp-sc', (sc.categories || []).join(',')))}${fld('适用品牌', inp('tp-sb', (sc.brands || []).join(',')))}</div>
      <div class="frow">${fld('整单退货退回券', sel('tp-rr', [['1', '是'], ['0', '否']], String(t.return_on_refund)))}${fld('状态', sel('tp-st', ['启用', '停用'], t.status))}</div>${errBox}`, async () => {
      const L = id => v(id).split(/[,，\s]+/).filter(Boolean);
      await api('api/promo/coupon-templates', { body: { id: t.id, name: v('tp-n'), type: v('tp-t'), face_value: v('tp-f') || 0, threshold: v('tp-h') || 0, discount_rate: v('tp-r'),
        max_discount: v('tp-x') || 0, valid_days: v('tp-d'), end_date: v('tp-e'), per_member_limit: v('tp-pl'), total_limit: v('tp-tl'),
        scope: { stores: L('tp-ss'), product_codes: L('tp-sp'), categories: L('tp-sc'), brands: L('tp-sb') }, return_on_refund: v('tp-rr') === '1', status: v('tp-st') } });
      mm.close(); toast('已保存', 'ok'); done();
    }, { wide: 'wide' });
  }
  function issueModal(t, done) {
    const mm = modal('发券：' + t.name, `${fld('会员号（多个用逗号/换行分隔）*', '<textarea id="is-m" rows="4" style="width:100%"></textarea>')}${fld('备注', inp('is-n', ''))}<div id="is-res"></div>${errBox}`, async () => {
      const codes = v('is-m').split(/[,，\s]+/).filter(Boolean);
      const r = (await api('api/promo/coupons/issue', { body: { template_id: t.id, member_codes: codes, note: v('is-n'), request_key: 'ui-' + t.id + '-' + Date.now() } })).data;
      $('is-res').innerHTML = `<div class="alert">发出 ${r.issued.length} 张，跳过 ${r.skipped.length}</div>` + r.skipped.map(s => `<div class="hint">${esc(s.member_code)}：${esc(s.reason)}</div>`).join('');
      done && done();
    }, { okText: '发放' });
  }

  /* ---------------- 价格体系 / 调价单 ---------------- */
  async function pricePage(box) {
    const f = { page: 1 };
    box.innerHTML = `<div class="card"><div class="bar">${can('price.adjust') ? '<button class="btn pri" id="pp-new">新建调价单</button>' : ''}<div class="hint">优先级：门店价 &gt; 区域价 &gt; 总部调价 &gt; 商品档案零售价；会员：等级价 &gt; 会员价。审核后到生效时间自动生效，不影响历史订单。</div></div><div id="pp-tbl"></div></div>`;
    const load = async () => {
      const d = (await api('api/promo/price-adjusts?' + qs({ page: f.page, size: 20 }))).data;
      $('pp-tbl').innerHTML = `<table class="tbl"><tr><th>调价单</th><th>范围</th><th>生效时间</th><th>明细</th><th>状态</th><th>原因</th><th></th></tr>${d.items.map(p => `<tr><td>${esc(p.pa_no)}</td><td>${esc(p.scope_type)} ${esc(p.scope_refs)}</td><td>${esc(p.effective_at)}</td>
        <td style="font-size:12px">${p.lines.slice(0, 5).map(l => esc(l.code + ' ' + l.name + ' ' + l.field + (l.level ? '(' + l.level + ')' : '') + '：' + fx(l.old_price) + '→' + fx(l.new_price))).join('<br>')}${p.lines.length > 5 ? '<br>…共 ' + p.lines.length + ' 行' : ''}</td>
        <td>${tag(p.status_name, p.status === 'effective' ? 'green' : '')}</td><td>${esc(p.reason)}</td><td style="white-space:nowrap">${p.status === 'draft' && can('price.adjust') ? `<button class="btn sm" data-op="submit" data-no="${p.pa_no}">提交审核</button>` : ''}
        ${p.status === 'submitted' && can('price.adjust.approve') ? `<button class="btn sm" data-op="approve" data-no="${p.pa_no}">同意</button> <button class="btn sm red" data-op="reject" data-no="${p.pa_no}">驳回</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="7" class="empty">还没有调价单</td></tr>'}</table>`;
      pg(d, $('pp-tbl'), p => { f.page = p; load(); });
      $('pp-tbl').querySelectorAll('[data-op]').forEach(b => b.onclick = async () => {
        try {
          let reason = '';
          if (b.dataset.op === 'reject') { reason = prompt('驳回原因') || ''; if (!reason) return; }
          await api('api/promo/price-adjusts/' + b.dataset.no + '/' + b.dataset.op, { body: { reason: reason } }); toast('已处理', 'ok'); load();
        } catch (e) { toast(e.message, 'err'); }
      });
    };
    if ($('pp-new')) $('pp-new').onclick = () => {
      const mm = modal('新建调价单', `<div class="frow">${fld('范围', sel('pn-s', [['hq', '总部（全部门店）'], ['region', '区域'], ['store', '门店']], 'hq'))}${fld('区域/门店编码（逗号分隔）', inp('pn-r', ''))}${fld('生效时间', inp('pn-e', localDate() + ' 00:00:00'))}</div>
        ${fld('明细（每行：商品编码 新价 [retail|member_price|level_price] [等级]）', '<textarea id="pn-l" rows="6" style="width:100%" placeholder="P1 399\nP1 379 member_price\nP2 88 level_price 金卡会员"></textarea>')}${fld('原因', inp('pn-why', ''))}${errBox}`, async () => {
        const lines = v('pn-l').split('\n').map(s => s.trim()).filter(Boolean).map(s => { const p = s.split(/\s+/); return { code: p[0], new_price: p[1], field: p[2] || 'retail', level: p[3] || '' }; });
        await api('api/promo/price-adjusts', { body: { scope_type: v('pn-s'), scope_refs: v('pn-r').split(/[,，\s]+/).filter(Boolean), effective_at: v('pn-e'), lines: lines, reason: v('pn-why') } });
        mm.close(); toast('已保存为草稿', 'ok'); load();
      }, { wide: 'wide' });
    };
    await load();
  }

  AdminShell.register({ key: 'promo-acts', title: '促销活动', icon: '🎉', group: '会员与营销', perm: 'promo.view', render: actsPage });
  AdminShell.register({ key: 'promo-try', title: '促销试算', icon: '🧮', group: '会员与营销', perm: 'promo.view', render: tryPage });
  AdminShell.register({ key: 'promo-coupons', title: '优惠券', icon: '🎟️', group: '会员与营销', perm: ['coupon.edit', 'coupon.issue', 'promo.view'], render: couponsPage });
  AdminShell.register({ key: 'promo-prices', title: '调价单', icon: '🏷️', group: '会员与营销', perm: ['price.adjust', 'price.adjust.approve'], render: pricePage });
})();
