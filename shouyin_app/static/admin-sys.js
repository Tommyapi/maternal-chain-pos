/* 管理后台 · 系统配置（模块④）：编号规则与预警参数、打印模板、数据字典、初始化检查、导入模板中心。
   接口 api/sys/*。收银参数（api/sys/config）集成轮从 admin.js 旧页面迁到这里。 */
'use strict';
(function () {
  const G = '系统配置';

  /* ---------- 收银参数（商户级，api/sys/config；保存写审计） ---------- */
  AdminShell.register({
    key: 'sys-pos', title: '收银参数', icon: '🧾', group: G, perm: 'sys.config', render: async box => {
      const d = (await api('api/sys/config')).data, c = d.config; S.cfg = c;
      const payName = {}; (d.pay_methods || []).forEach(p => { payName[p.code] = p.name; });
      const rules = (c.recharge_rules || []).map(r => r.threshold + ',' + r.gift).join('\n');
      box.innerHTML = `<div class="card"><h3>积分与抹零</h3><div class="frow">${fld('每实付 1 元积几分', inp('c-ppy', c.points_per_yuan, 'type="number" step="0.1" min="0"'))}${fld('多少积分抵 1 元', inp('c-p2y', c.points_to_yuan, 'type="number" min="1"'))}</div>
        <div class="frow">${fld('积分最多抵订单金额的比例（0-1）', inp('c-pmr', c.points_max_ratio, 'type="number" step="0.05" min="0" max="1"'))}${fld('抹零规则', sel('c-round', [['none', '不抹零'], ['jiao', '抹分（保留到角）'], ['yuan', '抹角（保留到元）']], c.round_mode))}</div></div>
        <div class="card"><h3>收银控制</h3><div class="hint" style="color:var(--mut)">改价、折扣、退款、现金支出的额度以「角色权限」里的操作额度为准；下面两项是 v1 兼容参数。</div>
        <div class="frow">${fld('收银员可直接改价（v1 兼容）', sel('c-chp', [['0', '否，需授权'], ['1', '是']], c.cashier_can_change_price ? '1' : '0'))}${fld('收银员最低折扣（v1 兼容，0.9=九折）', inp('c-disc', c.cashier_max_discount, 'type="number" step="0.01" min="0" max="1"'))}</div>
        <div class="frow">${fld('库存不足时', sel('c-neg', [['0', '不允许开单'], ['1', '允许（还需角色有「负库存销售」权限）']], c.allow_negative_stock ? '1' : '0'))}${fld('无单退货', sel('c-nrr', [['1', '允许（按角色权限与额度）'], ['0', '整体禁用']], c.allow_no_receipt_refund ? '1' : '0'))}</div>
        <div class="frow">${fld('原路退款拆分顺序（逗号分隔）', inp('c-rmo', (c.refund_method_order || []).join(',')), (c.refund_method_order || []).map(x => payName[x] || x).join(' → '))}${fld('临期提示默认天数', inp('c-ned', c.near_expiry_days, 'type="number" min="0"'), '商品/分类没有单独设置时使用')}</div></div>
        <div class="card"><h3>小票</h3>${fld('小票抬头', inp('c-title', c.receipt_title))}${fld('小票底部文字', `<textarea id="c-foot" rows="3">${esc(c.receipt_footer)}</textarea>`)}${fld('小票纸宽', sel('c-w', [['58', '58 毫米'], ['80', '80 毫米']], c.receipt_width))}<div class="hint" style="color:var(--mut)">门店/商户自定义模板在「打印模板」里维护，优先于这里的抬头和底部文字。</div></div>
        <div class="card"><h3>会员</h3>${fld('储值赠送规则（每行一条：充多少,送多少）', `<textarea id="c-rules" rows="4" placeholder="500,50\n1000,120">${esc(rules)}</textarea>`, '例：500,50 表示一次充 500 送 50；多条规则取满足的最高档')}${fld('会员等级（用逗号分隔）', inp('c-lv', (c.member_levels || []).join(',')))}</div>
        <div class="card">${fld('修改原因（写入审计）', inp('c-reason', ''))}<div class="err" id="c-err"></div><button class="btn pri" id="c-save" style="height:38px;padding:0 28px">保存设置</button></div>`;
      $('c-save').onclick = async () => {
        try {
          const r = await api('api/sys/config', { body: { points_per_yuan: v('c-ppy'), points_to_yuan: v('c-p2y'), points_max_ratio: v('c-pmr'), round_mode: v('c-round'),
            cashier_can_change_price: v('c-chp') === '1', cashier_max_discount: v('c-disc'), allow_negative_stock: v('c-neg') === '1', allow_no_receipt_refund: v('c-nrr') === '1',
            refund_method_order: v('c-rmo').split(/[,，\s]+/).filter(Boolean), near_expiry_days: v('c-ned'), receipt_title: v('c-title'), receipt_footer: $('c-foot').value, receipt_width: +v('c-w'),
            recharge_rules: parseLines($('c-rules').value, ['threshold', 'gift']), member_levels: v('c-lv').split(/[,，]+/).map(x => x.trim()).filter(Boolean), reason: v('c-reason') } });
          S.cfg = r.data.config; $('c-err').textContent = ''; toast(r.data.changed.length ? '已保存 ' + r.data.changed.length + ' 项，收银台刷新后生效' : '没有改动', 'ok');
        } catch (e) { $('c-err').textContent = e.message; }
      };
    }
  });

  /* ---------- 编号规则 + 预警参数（ERP-901 / ERP-903） ---------- */
  AdminShell.register({
    key: 'sys-config', title: '编号与预警', icon: '⚙️', group: G, perm: 'sys.config', render: async box => {
      const [num, al] = await Promise.all([api('api/sys/numbering'), api('api/sys/alert-params')]);
      const n = num.data, a = al.data;
      box.innerHTML = `<div class="card"><h3>预警参数</h3><div class="frow">${fld('临期分级（天，逗号分隔）', inp('al-near', a.near_expiry_days.join(',')))}${fld('默认安全库存（基本单位，0=不预警）', inp('al-safe', a.safety_stock_default, 'type="number" min="0"'))}</div>
        <div class="frow">${fld('滞销天数', inp('al-slow', a.slow_moving_days, 'type="number" min="7"'))}${fld('每日推送时间', inp('al-time', a.notify_time, 'placeholder="09:00"'))}</div>
        ${fld('接收人（拥有这些权限码、且门店在范围内的人）', inp('al-rc', a.receivers.join(',')), '例：stock.view,app.store')}
        ${fld('按分类/商品覆盖（每行：category 或 product,名称或编码,安全库存,临期天数）', `<textarea id="al-ov" rows="4">${esc(a.overrides.map(o => [o.scope, o.value, o.safety_stock == null ? '' : o.safety_stock, o.near_expiry_days == null ? '' : o.near_expiry_days].join(',')).join('\n'))}</textarea>`)}
        <div class="err" id="al-err"></div><button class="btn pri" id="al-save">保存预警参数</button></div>
        <div class="card"><h3>单号规则</h3><div class="hint">${esc(n.rule)}</div><ul>${n.notes.map(x => `<li>${esc(x)}</li>`).join('')}</ul>
        <table class="tbl"><tr><th>类型</th><th>单据</th><th>示例</th></tr>${n.types.map(t => `<tr><td>${esc(t.code)}</td><td>${esc(t.name)}</td><td><code>${esc(t.example)}</code></td></tr>`).join('')}</table></div>`;
      $('al-save').onclick = async () => {
        const overrides = $('al-ov').value.split('\n').map(x => x.trim()).filter(Boolean).map(l => { const p = l.split(/[,，]/); return { scope: p[0], value: p[1], safety_stock: p[2], near_expiry_days: p[3] }; });
        try {
          await api('api/sys/alert-params', { body: { near_expiry_days: v('al-near').split(/[,，]/).filter(Boolean).map(Number), safety_stock_default: +v('al-safe'), slow_moving_days: +v('al-slow'), notify_time: v('al-time'), receivers: v('al-rc').split(/[,，]/).map(x => x.trim()).filter(Boolean), overrides } });
          $('al-err').textContent = ''; toast('已保存', 'ok');
        } catch (e) { $('al-err').textContent = e.message; }
      };
    }
  });

  /* ---------- 打印模板（ERP-902） ---------- */
  AdminShell.register({
    key: 'sys-print', title: '打印模板', icon: '🖨️', group: G, perm: 'sys.print', render: async box => {
      const d = (await api('api/sys/print-templates')).data;
      const kinds = Object.keys(d.kinds);
      box.innerHTML = `<div class="card"><div class="bar"><span class="hint" style="color:var(--mut)">${esc(d.note)}；门店模板优先于商户默认模板。</span><div class="sp"></div><button class="btn pri" id="pt-new">新建/修改模板</button></div>
        <table class="tbl"><tr><th>适用</th><th>类型</th><th>纸宽</th><th>份数</th><th>抬头</th><th>版本</th><th>状态</th><th>更新时间</th><th></th></tr>${d.items.map(t => `<tr><td>${esc(t.store_code || '商户默认')}</td><td>${esc(t.kind_name)}</td><td>${t.width}mm</td><td>${t.copies}</td><td>${esc(t.content.title || '')}</td><td>v${t.version}</td><td>${esc(t.status)}</td><td>${esc(t.updated_at || '')}</td><td><button class="btn sm" data-t="${t.id}">编辑</button></td></tr>`).join('') || '<tr><td colspan="9" class="empty">还没有自定义模板，当前使用内置模板（取收银参数里的抬头/底部文字）</td></tr>'}</table></div>`;
      const tplModal = t => {
        t = t || { store_code: '', kind: 'sale', width: 58, copies: 1, status: '启用', content: d.effective.sale.content };
        const c = t.content || {};
        modal('打印模板', `<div class="frow">${fld('适用门店', sel('pt-store', [['', '商户默认']].concat((S.me.stores || []).map(s => [s, s])), t.store_code))}${fld('类型', sel('pt-kind', kinds.map(k => [k, d.kinds[k]]), t.kind))}</div>
          <div class="frow">${fld('纸宽', sel('pt-w', [['58', '58 毫米'], ['80', '80 毫米']], t.width))}${fld('份数', sel('pt-cp', ['1', '2', '3'], t.copies))}</div>
          <div class="frow">${fld('抬头', inp('pt-title', c.title))}${fld('门店电话', inp('pt-phone', c.phone))}</div>${fld('门店地址', inp('pt-addr', c.address))}
          ${fld('底部文字', `<textarea id="pt-foot" rows="2">${esc(c.footer || '')}</textarea>`)}${fld('售后说明', `<textarea id="pt-as" rows="2">${esc(c.after_sale || '')}</textarea>`)}
          <h4>打印字段</h4><div style="display:flex;flex-wrap:wrap;gap:4px 14px">${Object.keys(d.fields).map(f => `<label style="font-weight:normal"><input type="checkbox" class="pf" value="${f}" ${(c.fields || []).indexOf(f) >= 0 ? 'checked' : ''}> ${esc(d.fields[f])}</label>`).join('')}</div>
          ${fld('状态', sel('pt-st', ['启用', '停用'], t.status))}${errBox}`, async mm => {
          await api('api/sys/print-templates', { body: { store_code: v('pt-store'), kind: v('pt-kind'), width: +v('pt-w'), copies: +v('pt-cp'), status: v('pt-st'), content: { title: v('pt-title'), phone: v('pt-phone'), address: v('pt-addr'), footer: $('pt-foot').value, after_sale: $('pt-as').value, fields: Array.from(mm.root.querySelectorAll('.pf:checked')).map(x => x.value) } } });
          mm.close(); toast('已保存（版本 +1）', 'ok'); go('sys-print');
        }, { wide: 'wide' });
      };
      $('pt-new').onclick = () => tplModal(null);
      box.querySelectorAll('[data-t]').forEach(b => b.onclick = () => tplModal(d.items.find(x => x.id == b.dataset.t)));
    }
  });

  /* ---------- 数据字典（ERP-904） ---------- */
  AdminShell.register({
    key: 'sys-dicts', title: '数据字典', icon: '📚', group: G, perm: 'sys.dict', render: async box => {
      const d = (await api('api/sys/dicts')).data;
      box.innerHTML = d.types.map(t => `<div class="card"><div class="bar"><h3 style="margin:0">${esc(t.name)}</h3><div class="sp"></div><input id="dn-${t.code}" placeholder="新增一项" style="width:160px"><button class="btn sm" data-add="${t.code}">添加</button></div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">${(d.items[t.code] || []).map(x => `<span class="tag ${x.status === '启用' ? 'green' : 'red'}">${esc(x.name)} <a href="javascript:void 0" data-tg="${t.code}|${esc(x.code)}|${esc(x.name)}|${x.status}" style="margin-left:4px">${x.status === '启用' ? '停用' : '启用'}</a></span>`).join('') || '<span class="hint">暂无</span>'}</div></div>`).join('') + '<div class="hint" style="color:var(--mut)">停用后新单据不能再选，历史单据保留原文字，不受影响。</div>';
      box.querySelectorAll('[data-add]').forEach(b => b.onclick = async () => { try { await api('api/sys/dicts', { body: { dict_type: b.dataset.add, name: v('dn-' + b.dataset.add) } }); go('sys-dicts'); } catch (e) { toast(e.message, 'err'); } });
      box.querySelectorAll('[data-tg]').forEach(a => a.onclick = async () => { const p = a.dataset.tg.split('|'); try { await api('api/sys/dicts', { body: { dict_type: p[0], code: p[1], name: p[2], status: p[3] === '启用' ? '停用' : '启用' } }); go('sys-dicts'); } catch (e) { toast(e.message, 'err'); } });
    }
  });

  /* ---------- 初始化检查（ERP-905） ---------- */
  AdminShell.register({
    key: 'sys-init', title: '初始化检查', icon: '✅', group: G, perm: 'sys.init_check', render: async box => {
      const d = (await api('api/sys/init-check')).data;
      box.innerHTML = `<div class="card"><div class="bar"><h3 style="margin:0">上线前检查：通过 ${d.passed}/${d.total}</h3><div class="sp"></div><span class="hint">检查时间 ${esc(d.checked_at)}</span><button class="btn sm" id="ic-re">重新检查</button></div>
        <table class="tbl"><tr><th>项目</th><th>结果</th><th>说明</th><th></th></tr>${d.items.map(x => `<tr><td>${esc(x.name)}</td><td>${x.ok ? '<span class="tag green">通过</span>' : '<span class="tag red">待处理</span>'}</td><td>${esc(x.detail)}</td><td>${x.link && TITLES[x.link] ? `<button class="btn sm" data-go="${esc(x.link)}">去处理</button>` : ''}</td></tr>`).join('')}</table></div>`;
      $('ic-re').onclick = () => go('sys-init');
      box.querySelectorAll('[data-go]').forEach(b => b.onclick = () => go(b.dataset.go));
    }
  });

  /* ---------- 导入模板中心（DATA-001/002） ---------- */
  AdminShell.register({
    key: 'sys-import', title: '导入模板', icon: '📥', group: G, perm: ['goods.import', 'member.import', 'stock.adjust', 'org.edit', 'goods.edit'], render: async box => {
      const d = (await api('api/sys/import-templates')).data;
      box.innerHTML = d.map(t => `<div class="card"><div class="bar"><h3 style="margin:0">${esc(t.title)}</h3><div class="sp"></div><a class="btn" href="${esc(t.download)}">下载模板</a>
        ${t.kind === 'stores' ? '<input type="file" id="imp-st" accept=".xlsx" style="width:200px"><button class="btn pri" id="imp-go">导入门店</button>' : `<span class="hint" style="margin-left:8px">导入入口：对应模块页面</span>`}</div>
        <div class="hint" style="color:var(--mut)">${t.columns.map(c => esc(c.title) + (c.required ? '*' : '') + (c.note ? '（' + esc(c.note) + '）' : '')).join('；')}</div></div>`).join('') + '<div class="card"><div id="imp-res"></div></div>';
      if ($('imp-go')) $('imp-go').onclick = async () => {
        const f = $('imp-st').files[0]; if (!f) { toast('请选择 .xlsx 文件', 'err'); return; }
        const fd = new FormData(); fd.append('file', f);
        try {
          const r = (await api('api/sys/import/stores', { form: fd })).data;
          $('imp-res').innerHTML = `<div>新增 ${r.created} · 更新 ${r.updated} · 失败 ${r.error_count}</div>${r.errors.length ? `<table class="tbl"><tr><th>行号</th><th>门店</th><th>问题</th></tr>${r.errors.map(e => `<tr><td>${e.row_no}</td><td>${esc(e.name)}</td><td>${esc(e.errors.join('；'))}</td></tr>`).join('')}</table>` : ''}`;
        } catch (e) { toast(e.message, 'err'); }
      };
    }
  });
})();
