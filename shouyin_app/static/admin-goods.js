/* 管理后台 · goods 模块页面。Owner：模块①。用 AdminShell.register({...}) 注册页面（见 admin.js 末尾说明）。
   接口：api/goods*（{ok,data}）。M1 = 模块①三个页面文件共用的小工具（本文件最先加载）。 */
'use strict';
const M1 = {
  pg: d => ({ page: d.page, pages: Math.max(1, Math.ceil(d.total / d.size)), total: d.total, pageSize: d.size }),
  /* 多行文本 -> 行数组：每行「编码或条码 数量 [单位] [单价]」，空格/逗号/Tab 分隔 */
  parseLines(text, withPrice) {
    return String(text || '').split(/\n/).map(s => s.trim()).filter(Boolean).map((s, i) => {
      const a = s.split(/[\s,，\t]+/);
      if (a.length < 2) throw new Error('第 ' + (i + 1) + ' 行格式不对：编码 数量 [单位]' + (withPrice ? ' [单价]' : ''));
      const ln = { code: a[0], qty: a[1] };
      if (a[2] && isNaN(Number(a[2]))) ln.unit = a[2]; else if (a[2] && withPrice) ln.price = a[2];
      if (a[3] && withPrice) ln.price = a[3];
      return ln;
    });
  },
  whs: null,
  async whList() { if (!M1.whs) M1.whs = (await api('api/stock/warehouses')).data; return M1.whs; },
  async whSel(id, cur, all) {
    const ws = await M1.whList();
    return sel(id, (all ? [['', '全部仓库']] : []).concat(ws.map(w => [w.code, w.code + ' ' + w.name + '（' + w.kind_name + '）'])), cur);
  },
  tag(s, name) {
    const c = { draft: '', submitted: 'orange', approved: 'blue', partial: 'orange', shipped: 'blue', received: 'green', posted: 'green', closed: '',
      counting: 'blue', rejected: 'red', cancelled: '', open: 'orange', normal: 'green', locked: 'red', recalled: 'red' }[s];
    return `<span class="tag ${c || ''}">${esc(name || s)}</span>`;
  },
  importResult(r) {
    modal('导入结果', `<div style="font-size:15px;margin-bottom:10px">新增 <b>${r.created}</b>，更新 <b>${r.updated}</b>，失败 <b>${r.error_count}</b> 行。</div>` +
      (r.errors.length ? `<table class="tbl"><tr><th>行</th><th>名称</th><th>原因</th></tr>${r.errors.map(x => `<tr><td>${x.row_no}</td><td>${esc(x.name)}</td><td>${esc(x.errors.join('；'))}</td></tr>`).join('')}</table>` : ''), null);
  },
  upload(accept, url, done) {
    const f = document.createElement('input'); f.type = 'file'; f.accept = accept;
    f.onchange = async () => {
      if (!f.files[0]) return;
      const fd = new FormData(); fd.append('file', f.files[0]);
      try { const r = await api(url, { form: fd }); M1.importResult(r.data); done && done(r.data); } catch (e) { toast(e.message, 'err'); }
    };
    f.click();
  },
  dl(url) { location.href = url; },
};
window.M1 = M1;

(function () {
  const yesno = x => x ? '是' : '否';

  async function goodsPage(box) {
    const f = { kw: '', cat: '', status: '', page: 1 };
    const canEdit = AdminShell.can('goods.edit');
    const cats = (await api('api/goods/categories')).data;
    box.innerHTML = `<div class="card"><div class="bar">
      <input id="g-kw" placeholder="名称/简称/拼音首字母/货号/条码"><select id="g-cat"><option value="">全部分类</option>${cats.map(c => `<option>${esc(c.name)}</option>`).join('')}</select>
      <select id="g-st"><option value="">全部状态</option><option>在售</option><option>停售</option></select>
      <button class="btn pri" id="g-go">查询</button><div class="sp"></div>
      ${AdminShell.can('goods.import') ? '<button class="btn" id="g-tpl">导入模板</button><button class="btn" id="g-imp">导入 Excel</button>' : ''}
      ${AdminShell.can('goods.export') ? '<button class="btn" id="g-x">导出</button>' : ''}
      ${AdminShell.can('goods.price.edit') ? '<button class="btn" id="g-bp">批量调价</button>' : ''}
      ${AdminShell.can('goods.listing') ? '<button class="btn" id="g-ls">门店铺货/停售</button>' : ''}
      ${canEdit ? '<button class="btn pri" id="g-new">新增商品</button>' : ''}</div>
      <div class="tbl-wrap" id="g-tbl"></div></div>`;
    let rows = [];
    const checked = () => [...box.querySelectorAll('[data-ck]:checked')].map(x => +x.dataset.ck);
    const load = async () => {
      f.kw = v('g-kw'); f.cat = v('g-cat'); f.status = v('g-st');
      const d = (await api('api/goods?' + qs({ kw: f.kw, cat: f.cat, status: f.status, page: f.page, size: 20 }))).data;
      rows = d.items;
      $('g-tbl').innerHTML = `<table class="tbl"><tr><th><input type="checkbox" id="g-all"></th><th>货号</th><th>商品名称</th><th>规格</th><th>分类/品牌</th><th>单位（换算）</th><th>条码</th><th class="r">零售价</th><th class="r">会员价</th><th class="r">成本</th><th>批次/效期</th><th>状态</th><th></th></tr>` +
        (rows.map(p => `<tr><td><input type="checkbox" data-ck="${p.id}"></td><td>${esc(p.code)}</td><td>${esc(p.name)}${p.combo_mode !== 'none' ? ' <span class="tag blue">组合装</span>' : ''}</td><td>${esc(p.spec)}</td>
          <td>${esc(p.category)}<br><span style="color:var(--ink2)">${esc(p.brand)}</span></td><td>${p.units.map(u => esc(u.unit) + (u.rate > 1 ? '=' + u.rate : '')).join('，')}</td>
          <td style="font-size:12px">${p.barcodes.map(b => esc(b.barcode) + (b.unit !== p.base_unit ? '（' + esc(b.unit) + '）' : '')).join('<br>')}</td>
          <td class="r num">${fx(p.retail)}</td><td class="r num">${fx(p.member_price)}</td><td class="r num">${p.cost == null ? '<span style="color:var(--ink2)">无权限</span>' : fx(p.cost)}</td>
          <td>${p.batch_managed ? '批次 · 保质 ' + p.shelf_life_days + ' 天' : '-'}</td><td>${M1.tag(p.status === '在售' ? 'normal' : 'closed', p.status)}</td>
          <td style="white-space:nowrap"><button class="btn sm" data-view="${p.id}">详情</button>${canEdit ? ` <button class="btn sm" data-edit="${p.id}">编辑</button> <button class="btn sm" data-dis="${p.id}">${p.status === '在售' ? '停用' : '启用'}</button>` : ''}</td></tr>`).join('') ||
          '<tr><td colspan="13" class="empty">没有商品</td></tr>') + '</table>' + pager(M1.pg(d));
      bindPager($('g-tbl'), p => { f.page = p; load(); });
      $('g-all').onchange = e => box.querySelectorAll('[data-ck]').forEach(x => x.checked = e.target.checked);
      box.querySelectorAll('[data-view]').forEach(b => b.onclick = () => detail(+b.dataset.view));
      box.querySelectorAll('[data-edit]').forEach(b => b.onclick = async () => editModal((await api('api/goods/' + b.dataset.edit)).data, load));
      box.querySelectorAll('[data-dis]').forEach(b => b.onclick = () => {
        const p = rows.find(x => x.id == b.dataset.dis), en = p.status !== '在售';
        modal(en ? '启用商品' : '停用商品', fld('原因', inp('g-dr', '')) + '<div class="hint">停用不删除，历史单据照常可查。</div>' + errBox, async m => {
          await api('api/goods/' + p.id + '/disable', { body: { enable: en ? 1 : 0, reason: v('g-dr') } }); m.close(); toast('已' + (en ? '启用' : '停用'), 'ok'); load();
        });
      });
    };
    $('g-go').onclick = () => { f.page = 1; load(); };
    $('g-kw').addEventListener('keydown', e => { if (e.key === 'Enter') { f.page = 1; load(); } });
    if ($('g-new')) $('g-new').onclick = () => editModal(null, load);
    if ($('g-tpl')) $('g-tpl').onclick = () => M1.dl('api/goods/template');
    if ($('g-imp')) $('g-imp').onclick = () => M1.upload('.xlsx', 'api/goods/import', load);
    if ($('g-x')) $('g-x').onclick = () => M1.dl('api/goods/export');
    if ($('g-bp')) $('g-bp').onclick = () => {
      const ids = checked(); if (!ids.length) return toast('先勾选商品', 'err');
      modal('批量调价（' + ids.length + ' 个商品）', fld('零售价、会员价同比例调整（%）', inp('g-pct', '', 'type="number" step="0.1"'), '例：5 表示上调 5%，-10 表示下调 10%') + fld('原因', inp('g-pr', '')) + errBox, async m => {
        await api('api/goods/batch-price', { body: { ids, percent: v('g-pct'), reason: v('g-pr') } }); m.close(); toast('已调价', 'ok'); load();
      });
    };
    if ($('g-ls')) $('g-ls').onclick = () => {
      const ids = checked(); if (!ids.length) return toast('先勾选商品', 'err');
      modal('门店铺货/停售（' + ids.length + ' 个商品）', fld('门店', S.stores.map(s => `<label style="margin-right:10px"><input type="checkbox" data-st="${esc(s.code)}"> ${esc(s.name)}</label>`).join('')) +
        fld('设置为', sel('g-lst', ['在售', '停售', '跟随总部'], '停售')) + errBox, async m => {
        const st = [...m.root.querySelectorAll('[data-st]:checked')].map(x => x.dataset.st);
        await api('api/goods/listing', { body: { product_ids: ids, store_codes: st, status: v('g-lst') } }); m.close(); toast('已更新', 'ok'); load();
      });
    };
    await load();
  }

  async function detail(id) {
    const p = (await api('api/goods/' + id)).data;
    const attrs = Object.keys(p.spec_attrs || {}).map(k => esc(k) + '：' + esc(p.spec_attrs[k])).join('，');
    modal('商品详情 · ' + p.name, `<div class="kv"><span>货号</span><b>${esc(p.code)}</b><span>SPU</span><b>${esc(p.spu_code || '-')}</b><span>规格</span><b>${esc(p.spec || '-')} ${attrs}</b>
      <span>允许折扣/积分</span><b>${yesno(p.allow_discount)} / ${yesno(p.allow_points)}</b><span>批次管理</span><b>${yesno(p.batch_managed)}（保质 ${p.shelf_life_days} 天，临期 ${p.near_expiry_days} 天）</b>
      <span>组合装</span><b>${esc(p.combo_mode)}${p.bom.length ? '：' + p.bom.map(b => esc(b.name) + '×' + b.qty).join('，') : ''}</b></div>
      <h4>单位</h4><table class="tbl"><tr><th>单位</th><th>换算率</th><th class="r">零售价</th><th>销售/采购</th></tr>${p.units.map(u => `<tr><td>${esc(u.unit)}</td><td>${u.rate}</td><td class="r">${u.retail == null ? '-' : fx(u.retail)}</td><td>${yesno(u.for_sale)}/${yesno(u.for_purchase)}</td></tr>`).join('')}</table>
      <h4>门店铺货</h4><div>${p.listings.length ? p.listings.map(l => esc(l.store_code) + '：' + esc(l.status)).join('，') : '全部门店跟随总部'}</div>
      ${p.purchase_prices ? `<h4>历史采购价</h4><table class="tbl"><tr><th>时间</th><th>供应商</th><th>单位</th><th class="r">单价</th><th>单号</th></tr>${p.purchase_prices.map(x => `<tr><td>${esc(x.created_at)}</td><td>${esc(x.supplier)}</td><td>${esc(x.unit)}</td><td class="r">${fx(x.price)}</td><td>${esc(x.src_no)}</td></tr>`).join('')}</table>` : ''}
      <h4>变更记录</h4><table class="tbl"><tr><th>时间</th><th>操作人</th><th>动作</th><th>变更</th></tr>${p.history.map(h => `<tr><td>${esc(h.created_at)}</td><td>${esc(h.username)}</td><td>${esc(h.action)}</td><td style="font-size:12px">${esc(JSON.stringify(h.after))}</td></tr>`).join('')}</table>`, null, { wide: 'wide' });
  }

  function editModal(p, done) {
    const n = !p;
    p = p || { status: '在售', units: [], barcodes: [], bom: [], spec_attrs: {}, combo_mode: 'none', allow_discount: 1, allow_points: 1, batch_managed: 0 };
    const units = (p.units || []).filter(u => !u.is_base).map(u => u.unit + ' ' + u.rate + (u.retail != null ? ' ' + u.retail : '')).join('\n');
    const bcs = (p.barcodes || []).map(b => b.barcode + (b.unit && b.unit !== p.base_unit ? ' ' + b.unit : '')).join('\n');
    const attrs = Object.keys(p.spec_attrs || {}).map(k => k + '=' + p.spec_attrs[k]).join('，');
    const bom = (p.bom || []).map(b => b.code + ' ' + b.qty).join('\n');
    const costOk = p.cost !== null || n;
    modal(n ? '新增商品' : '编辑商品 · ' + p.name, `<div class="frow">${fld('商品名称*', inp('e-name', p.name))}${fld('货号', inp('e-code', p.code), n ? '不填自动生成' : '')}${fld('简称', inp('e-short', p.short_name))}</div>
      <div class="frow">${fld('分类', inp('e-cat', p.category))}${fld('品牌', inp('e-brand', p.brand))}${fld('SPU（同款不同规格填同一个）', inp('e-spu', p.spu_code))}</div>
      <div class="frow">${fld('规格', inp('e-spec', p.spec))}${fld('规格属性', inp('e-attrs', attrs), '例：段位=1段，容量=800g')}${fld('图片地址', inp('e-img', p.image))}</div>
      <div class="frow">${fld('基本单位*', inp('e-unit', p.base_unit || p.unit))}${fld('其他单位（每行：单位 换算率 [零售价]）', `<textarea id="e-units" rows="2" placeholder="箱 6 1700">${esc(units)}</textarea>`)}</div>
      ${fld('条码（每行一个；箱码在后面写单位：16900… 箱）', `<textarea id="e-bc" rows="2">${esc(bcs)}</textarea>`)}
      <div class="frow">${fld('零售价', inp('e-retail', p.retail, 'type="number" step="0.01" min="0"'))}${fld('会员价', inp('e-mp', p.member_price, 'type="number" step="0.01" min="0"'))}${costOk ? fld('成本价', inp('e-cost', p.cost, 'type="number" step="0.01" min="0"')) : ''}</div>
      <div class="frow">${fld('批次管理', sel('e-batch', [['0', '否'], ['1', '是']], p.batch_managed ? '1' : '0'))}${fld('保质期天数', inp('e-shelf', p.shelf_life_days || 0, 'type="number" min="0"'))}${fld('临期预警天数', inp('e-near', p.near_expiry_days || 0, 'type="number" min="0"'))}</div>
      <div class="frow">${fld('允许折扣', sel('e-disc', [['1', '是'], ['0', '否']], p.allow_discount ? '1' : '0'))}${fld('允许积分', sel('e-pts', [['1', '是'], ['0', '否']], p.allow_points ? '1' : '0'))}${fld('状态', sel('e-st', ['在售', '停售'], p.status))}</div>
      <div class="frow">${fld('组合装', sel('e-combo', [['none', '普通商品'], ['self', '组合装（扣自身库存，可拆零/组装）'], ['components', '组合装（卖出扣组件库存）']], p.combo_mode))}${fld('组件 BOM（每行：组件编码 数量）', `<textarea id="e-bom" rows="2">${esc(bom)}</textarea>`)}</div>${errBox}`, async m => {
      const body = { id: p.id, name: v('e-name'), code: v('e-code'), short_name: v('e-short'), category: v('e-cat'), brand: v('e-brand'), spu_code: v('e-spu'),
        spec: v('e-spec'), image: v('e-img'), base_unit: v('e-unit'), retail: v('e-retail'), member_price: v('e-mp'), status: v('e-st'),
        batch_managed: +v('e-batch'), shelf_life_days: v('e-shelf'), near_expiry_days: v('e-near'), allow_discount: +v('e-disc'), allow_points: +v('e-pts'),
        combo_mode: v('e-combo'), spec_attrs: {} };
      if (costOk && v('e-cost') !== '') body.cost = v('e-cost');
      v('e-attrs').split(/[，,;；]/).map(s => s.split('=')).filter(a => a[0] && a[1]).forEach(a => { body.spec_attrs[a[0].trim()] = a[1].trim(); });
      body.units = $('e-units').value.split('\n').map(s => s.trim().split(/\s+/)).filter(a => a[0]).map(a => ({ unit: a[0], rate: a[1], retail: a[2] || null }));
      body.barcodes = $('e-bc').value.split('\n').map(s => s.trim().split(/\s+/)).filter(a => a[0]).map(a => ({ barcode: a[0], unit: a[1] || '' }));
      body.bom = $('e-bom').value.split('\n').map(s => s.trim().split(/\s+/)).filter(a => a[0]).map(a => ({ code: a[0], qty: a[1] }));
      await api('api/goods', { body }); m.close(); toast('已保存', 'ok'); done();
    }, { wide: 'wide' });
  }

  async function catPage(box) {
    const canEdit = AdminShell.can('goods.edit');
    const load = async () => {
      const cats = (await api('api/goods/categories')).data, brands = (await api('api/goods/brands')).data;
      box.innerHTML = `<div class="card"><div class="bar"><b>分类</b><div class="sp"></div>${canEdit ? '<button class="btn pri" id="c-new">新增分类</button>' : ''}</div>
        <table class="tbl"><tr><th>编码</th><th>名称</th><th class="r">临期预警天数</th><th class="r">安全库存</th><th>状态</th><th></th></tr>${cats.map(c => `<tr><td>${esc(c.code)}</td><td>${esc(c.name)}</td><td class="r">${c.near_expiry_days}</td><td class="r">${c.safety_stock}</td><td>${esc(c.status)}</td><td>${canEdit ? `<button class="btn sm" data-c="${c.id}">编辑</button>` : ''}</td></tr>`).join('')}</table></div>
        <div class="card"><div class="bar"><b>品牌</b><div class="sp"></div>${canEdit ? '<input id="b-name" placeholder="品牌名称"><button class="btn pri" id="b-add">添加</button>' : ''}</div>
        <div>${brands.map(b => `<span class="tag" style="margin:3px">${esc(b.name)}</span>`).join('')}</div></div>`;
      const edit = c => modal(c ? '编辑分类' : '新增分类', fld('名称', inp('c-name', c && c.name)) + `<div class="frow">${fld('临期预警天数', inp('c-near', c ? c.near_expiry_days : 0, 'type="number"'))}${fld('安全库存', inp('c-safe', c ? c.safety_stock : 0, 'type="number"'))}</div>` + errBox, async m => {
        await api('api/goods/categories', { body: { id: c && c.id, name: v('c-name'), near_expiry_days: v('c-near'), safety_stock: v('c-safe') } }); m.close(); load();
      });
      if ($('c-new')) $('c-new').onclick = () => edit(null);
      box.querySelectorAll('[data-c]').forEach(b => b.onclick = () => edit(cats.find(x => x.id == b.dataset.c)));
      if ($('b-add')) $('b-add').onclick = async () => { try { await api('api/goods/brands', { body: { name: v('b-name') } }); load(); } catch (e) { toast(e.message, 'err'); } };
    };
    await load();
  }

  AdminShell.register({ key: 'goods2', title: '商品档案', icon: '🍼', group: '商品与库存', perm: 'goods.view', render: goodsPage });
  AdminShell.register({ key: 'goods-cat', title: '分类与品牌', icon: '🏷️', group: '商品与库存', perm: 'goods.view', render: catPage });
})();
