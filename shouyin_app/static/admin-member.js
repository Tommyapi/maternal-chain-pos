/* 管理后台 · member 模块页面。Owner：模块②。用 AdminShell.register({...}) 注册页面（见 admin.js 末尾说明）。
   接口：api/member/*（新接口 {ok,data}）。全部函数包在 IIFE 里，避免与 admin.js 旧函数重名。 */
'use strict';
(function () {
  const can = p => AdminShell.can(p);
  const pg = (d, box, goto) => {
    const pages = Math.max(1, Math.ceil(d.total / d.size));
    box.insertAdjacentHTML('beforeend', pager({ total: d.total, page: d.page, pages: pages, pageSize: d.size }));
    bindPager(box, goto);
  };
  const yn = (x, t, f) => x ? t : (f || '');
  const tagHtml = (t, cls) => `<span class="tag ${cls || ''}">${esc(t)}</span>`;
  const statusTag = m => m.merged_into ? tagHtml('已合并→' + m.merged_into) : m.blacklisted ? tagHtml('黑名单', 'red') : m.status === '正常' ? tagHtml('正常', 'green') : tagHtml(m.status || '');
  const LEVELS = () => (S.cfg && S.cfg.member_levels) || ['普通会员', '银卡会员', '金卡会员'];

  /* ---------------- 会员档案 ---------------- */
  async function listPage(box) {
    const f = { page: 1 };
    let tags = [];
    try { tags = (await api('api/member/tags')).data; } catch (e) { tags = []; }
    box.innerHTML = `<div class="card"><div class="bar"><input id="mm-kw" placeholder="手机号 / 姓名 / 会员号 / 卡号">
      <select id="mm-lv"><option value="">全部等级</option>${LEVELS().map(l => `<option>${esc(l)}</option>`).join('')}</select>
      <select id="mm-st"><option value="">有效会员</option><option value="正常">正常</option><option value="停用">停用</option><option value="blacklist">黑名单</option><option value="merged">已合并</option></select>
      <select id="mm-tag"><option value="">全部标签</option>${tags.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('')}</select>
      <button class="btn pri" id="mm-go">查询</button><div class="sp"></div>
      ${yn(can('member.edit'), '<button class="btn" id="mm-new">新增会员</button>')}
      ${yn(can('member.merge'), '<button class="btn" id="mm-merge">合并会员</button>')}
      ${yn(can('member.import'), '<button class="btn" id="mm-imp">导入</button>')}
      ${yn(can('member.export'), '<button class="btn" id="mm-exp">导出(脱敏)</button>')}</div><div class="tbl-wrap" id="mm-tbl"></div></div>`;
    const params = () => ({ kw: v('mm-kw'), level: v('mm-lv'), status: v('mm-st'), tag: v('mm-tag') });
    const load = async () => {
      const d = (await api('api/member?' + qs(Object.assign(params(), { page: f.page, size: 20 })))).data;
      $('mm-tbl').innerHTML = `<table class="tbl"><tr><th>会员号</th><th>姓名</th><th>手机</th><th>等级</th><th class="r">积分</th><th class="r">储值</th><th>标签</th><th>注册门店</th><th>状态</th><th></th></tr>` +
        (d.items.map(m => `<tr><td>${esc(m.code)}</td><td>${esc(m.name)}</td><td>${esc(m.phone || '')}</td><td>${esc(m.level)}</td><td class="r num">${m.points}</td>
          <td class="r num">${m.stored_balance == null ? '—' : fx(m.stored_balance)}</td><td>${m.tags.map(t => tagHtml(t)).join(' ')}</td>
          <td>${esc(m.reg_store_name || m.reg_store_code)}</td><td>${statusTag(m)}</td><td><button class="btn sm" data-c="${esc(m.code)}">详情</button></td></tr>`).join('')
          || '<tr><td colspan="10" class="empty">没有会员</td></tr>') + '</table>';
      pg(d, $('mm-tbl'), p => { f.page = p; load(); });
      $('mm-tbl').querySelectorAll('[data-c]').forEach(b => b.onclick = () => detail(b.dataset.c, load));
    };
    $('mm-go').onclick = () => { f.page = 1; load().catch(e => toast(e.message, 'err')); };
    $('mm-kw').addEventListener('keydown', e => { if (e.key === 'Enter') $('mm-go').onclick(); });
    if ($('mm-new')) $('mm-new').onclick = () => edit(null, load);
    if ($('mm-merge')) $('mm-merge').onclick = () => mergeModal(load);
    if ($('mm-imp')) $('mm-imp').onclick = () => importModal(load);
    if ($('mm-exp')) $('mm-exp').onclick = () => { location.href = 'api/member/export?' + qs(params()); };
    await load();
  }

  function babyRows(babies) {
    return (babies || []).map((b, i) => `<div class="frow bb" data-id="${b.id || ''}" style="grid-template-columns:110px 1fr 70px 150px 60px;gap:8px;margin-bottom:6px">
      <select class="b-k"><option value="baby" ${b.kind !== 'pregnancy' ? 'selected' : ''}>宝宝</option><option value="pregnancy" ${b.kind === 'pregnancy' ? 'selected' : ''}>孕期</option></select>
      <input class="b-n" placeholder="昵称（选填）" value="${esc(b.nickname || '')}"><select class="b-g"><option value="">性别</option><option ${b.gender === '男' ? 'selected' : ''}>男</option><option ${b.gender === '女' ? 'selected' : ''}>女</option></select>
      <input class="b-b" type="date" value="${esc(b.birthday || '')}" title="生日/预产期"><label style="font-size:12px"><input type="checkbox" class="b-h" ${b.hidden ? 'checked' : ''}>隐藏</label></div>`).join('');
  }

  function edit(m, done) {
    const mm = modal(m ? '编辑会员 ' + m.code : '新增会员', `<div class="frow">${fld('姓名', inp('me2-name', m && m.name))}${fld('手机号*', inp('me2-phone', m && m.phone, 'maxlength="11"'), m ? '脱敏显示时不修改' : '')}</div>
      <div class="frow">${fld('等级', sel('me2-lv', LEVELS(), m ? m.level : LEVELS()[0]))}${fld('实体卡号', inp('me2-card', m && m.card_no))}</div>
      ${m ? '' : fld('注册门店', sel('me2-store', storeOpts(false), S.me.store_code || (S.stores[0] || {}).code))}
      ${fld('备注', inp('me2-note', m && m.note))}
      <div class="fi"><label>宝宝 / 孕期档案（选填，月龄自动计算）</label><div id="me2-bb">${babyRows(m ? m.babies : [])}</div><button class="btn sm" id="me2-add">+ 添加</button></div>${errBox}`, async () => {
      const babies = Array.from(document.querySelectorAll('#me2-bb .bb')).map(r => ({ id: r.dataset.id ? +r.dataset.id : undefined, kind: r.querySelector('.b-k').value,
        nickname: r.querySelector('.b-n').value.trim(), gender: r.querySelector('.b-g').value, birthday: r.querySelector('.b-b').value, hidden: r.querySelector('.b-h').checked }))
        .filter(b => b.id || b.birthday || b.nickname);
      const body = { name: v('me2-name'), phone: v('me2-phone'), level: v('me2-lv'), card_no: v('me2-card'), note: v('me2-note'), babies: babies };
      if (m) body.code = m.code; else body.reg_store_code = v('me2-store');
      await api('api/member', { body: body });
      toast('已保存', 'ok'); mm.close(); done && done();
    }, { onOpen: () => { $('me2-add').onclick = () => $('me2-bb').insertAdjacentHTML('beforeend', babyRows([{}])); } });
  }

  async function detail(code, done) {
    const m = (await api('api/member/' + encodeURIComponent(code))).data;
    const refresh = () => { mm.close(); detail(code, done); done && done(); };
    const extra = [];
    if (can('member.edit') && !m.merged_into) extra.push({ text: '编辑资料', fn: () => { mm.close(); edit(m, done); } });
    if (can('member.tag') && !m.merged_into) extra.push({ text: '打标签', fn: () => tagModal(m, refresh) });
    if (can('member.points.adjust') && !m.merged_into) extra.push({ text: '调整积分', fn: () => pointsModal(m, refresh) });
    if (can('member.stored.adjust') && !m.merged_into) extra.push({ text: '储值调整', fn: () => storedModal(m, refresh) });
    if (can('member.blacklist') && !m.merged_into) extra.push({ text: m.benefits_ok ? '停用/拉黑' : '启用', fn: () => statusModal(m, refresh) });
    const babies = m.babies.filter(b => !b.hidden).map(b => esc((b.kind === 'pregnancy' ? '孕期 预产期' : (b.nickname || '宝宝')) + ' ' + (b.birthday || '') + ' ' + (b.axis || ''))).join('；') || '未登记';
    const mm = modal('会员 · ' + (m.name || m.code), `<div class="kv"><span>会员号</span><b>${esc(m.code)}</b><span>手机</span><b>${esc(m.phone || '')}</b><span>等级</span><b>${esc(m.level)}</b>
      <span>状态</span><b>${statusTag(m)} ${esc(m.disabled_reason || '')}</b><span>注册</span><b>${esc(m.reg_time || '')} ${esc(m.reg_store_name || '')} ${esc(m.source || '')}</b>
      <span>卡号</span><b>${esc(m.card_no || '-')}</b><span>家庭档案</span><b>${babies}</b><span>标签</span><b>${m.tags.map(t => tagHtml(t.name + (t.source === 'auto' ? '·自动' : ''))).join(' ') || '-'}</b></div>
      <div class="kpis" style="margin-bottom:10px"><div class="kpi"><span>积分</span><b>${m.points}</b>${m.points_debt ? `<em>欠扣 ${m.points_debt}</em>` : ''}</div>
      <div class="kpi"><span>储值余额</span><b>${m.stored_balance == null ? '—' : fx(m.stored_balance)}</b>${m.stored_gift != null ? `<em>本金 ${fx(m.stored_principal)} / 赠送 ${fx(m.stored_gift)}</em>` : ''}</div>
      <div class="kpi"><span>累计消费</span><b>${fx(m.amount)}</b><em>${m.orders} 单</em></div><div class="kpi"><span>可用券</span><b>${m.coupons.length}</b></div></div>
      <div class="tabs" id="md2-tabs"><button class="on" data-t="o">消费记录</button><button data-t="f">购买偏好</button><button data-t="p">积分流水</button><button data-t="s">储值流水</button><button data-t="c">优惠券</button><button data-t="g">合并记录</button></div><div id="md2-body"></div>`, null, {
      wide: 'wide', extra: extra,
      onOpen: () => {
        const T = (heads, rows, n) => `<table class="tbl"><tr>${heads.map(h => `<th>${h}</th>`).join('')}</tr>${rows.join('') || `<tr><td colspan="${n || heads.length}" class="empty">暂无记录</td></tr>`}</table>`;
        const show = t => {
          document.querySelectorAll('#md2-tabs button').forEach(b => b.classList.toggle('on', b.dataset.t === t));
          $('md2-body').innerHTML = t === 'o' ? T(['时间', '单号', '类型', '门店', '金额', '状态'], m.recent_orders.map(o => `<tr><td>${esc(o.created_at)}</td><td>${esc(o.order_no)}</td><td>${o.biz_type === 'refund' ? '退货' : '销售'}</td><td>${esc(o.store_name || '')}</td><td class="r num">${fx(o.payable)}</td><td>${esc(o.status || '')}</td></tr>`))
            : t === 'f' ? T(['分类', '品牌', '规格/段位/尺码', '件数', '单数', '最近'], m.preferences.map(p => `<tr><td>${esc(p.category)}</td><td>${esc(p.brand)}</td><td>${esc(p.spec)}</td><td>${p.qty}</td><td>${p.orders}</td><td>${esc(p.last_time)}</td></tr>`))
              : t === 'p' ? T(['时间', '类型', '变动', '余额', '单号', '说明'], m.points_log.map(o => `<tr><td>${esc(o.created_at)}</td><td>${esc(o.type)}</td><td class="r num">${o.change}</td><td class="r num">${o.balance}</td><td>${esc(o.order_no || '')}</td><td>${esc(o.note || '')}</td></tr>`))
                : t === 's' ? (m.stored_log ? T(['时间', '类型', '变动', '本金', '赠送', '余额', '说明'], m.stored_log.map(o => `<tr><td>${esc(o.created_at)}</td><td>${esc(o.type)}</td><td class="r num">${fx(o.change)}</td><td class="r num">${fx(o.principal)}</td><td class="r num">${fx(o.gift)}</td><td class="r num">${fx(o.balance)}</td><td>${esc(o.note || '')}</td></tr>`)) : '<div class="empty">没有查看储值的权限</div>')
                  : t === 'c' ? T(['券号', '名称', '面额', '有效期', '状态', '核销单'], m.all_coupons.map(c => `<tr><td>${esc(c.coupon_no)}</td><td>${esc(c.template_name)}</td><td class="r num">${fx(c.face_value)}</td><td>${esc(c.expire_date)}</td><td>${esc(c.status)}</td><td>${esc(c.order_no || '')}</td></tr>`))
                    : T(['时间', '被合并', '保留', '操作人', '原因'], m.merges.map(x => `<tr><td>${esc(x.created_at)}</td><td>${esc(x.from_code)}</td><td>${esc(x.to_code)}</td><td>${esc(x.operator)}</td><td>${esc(x.reason)}</td></tr>`));
        };
        document.querySelectorAll('#md2-tabs button').forEach(b => b.onclick = () => show(b.dataset.t)); show('o');
      } });
  }

  function authFields() {
    return `<div class="hint">超出本人额度时：填店长账号密码现场授权，或先到「审批」提交申请，审批通过后填审批单号。</div>
      <div class="frow">${fld('授权人账号', inp('au-u', ''))}${fld('授权人密码', '<input id="au-p" type="password">')}</div>${fld('审批单号', inp('au-no', ''))}`;
  }
  const authBody = () => { const b = {}; if (v('au-u')) { b.auth_user = v('au-u'); b.auth_pass = $('au-p').value; } if (v('au-no')) b.approval_no = v('au-no'); return b; };

  function pointsModal(m, done) {
    const mm = modal('调整积分 · ' + m.code, `${fld('增减积分*（负数为扣减）', inp('pa-d', '', 'type="number"'))}${fld('原因*', inp('pa-r', ''))}${authFields()}${errBox}`, async () => {
      const r = await api('api/member/' + encodeURIComponent(m.code) + '/points', { body: Object.assign({ delta: +v('pa-d'), reason: v('pa-r') }, authBody()) });
      toast('已调整，当前积分 ' + r.data.points, 'ok'); mm.close(); done();
    });
  }

  function storedModal(m, done) {
    const mm = modal('储值调整 · ' + m.code, `<div class="alert">储值调整/退款必须审批：第一次提交生成审批单；审批通过后填审批单号再提交一次执行。</div>
      <div class="frow">${fld('类型', sel('sa-k', ['人工调整', '储值退款'], '人工调整'))}${fld('金额（元）*（负数为扣减）', inp('sa-d', '', 'type="number" step="0.01"'))}</div>
      ${fld('原因*', inp('sa-r', ''))}${fld('审批单号（审批通过后填）', inp('sa-no', ''))}${errBox}`, async () => {
      const r = (await api('api/member/' + encodeURIComponent(m.code) + '/stored', { body: { kind: v('sa-k'), delta: v('sa-d'), reason: v('sa-r'), approval_no: v('sa-no') } })).data;
      if (r.status === 'pending') { toast('已提交审批：' + r.approval_no, 'ok'); $('sa-no').value = r.approval_no; return; }
      toast('已执行，余额 ' + fx(r.stored_balance), 'ok'); mm.close(); done();
    });
  }

  function statusModal(m, done) {
    const enable = !m.benefits_ok;
    const mm = modal((enable ? '启用' : '停用') + '会员 · ' + m.code, `${enable ? '' : `<label><input type="checkbox" id="ms-bl"> 同时列入黑名单</label>`}
      ${fld('原因*', inp('ms-r', ''))}<div class="hint">停用后不能使用会员价、积分、券；历史订单保留。</div>${errBox}`, async () => {
      await api('api/member/' + encodeURIComponent(m.code) + (enable ? '/enable' : '/disable'), { body: { reason: v('ms-r'), blacklist: !enable && $('ms-bl').checked } });
      toast('已处理', 'ok'); mm.close(); done();
    });
  }

  async function tagModal(m, done) {
    const tags = (await api('api/member/tags')).data.filter(t => t.kind === 'manual');
    const has = new Set(m.tags.map(t => t.id));
    const mm = modal('会员标签 · ' + m.code, tags.map(t => `<label style="display:inline-block;margin:4px 10px 4px 0"><input type="checkbox" class="tg" value="${t.id}" ${has.has(t.id) ? 'checked' : ''}> ${esc(t.name)}</label>`).join('') || '<div class="empty">还没有手工标签，请到「会员标签」新建</div>', async () => {
      const now = Array.from(document.querySelectorAll('.tg')).filter(x => x.checked).map(x => +x.value);
      await api('api/member/' + encodeURIComponent(m.code) + '/tags', { body: { add: now.filter(i => !has.has(i)), remove: tags.map(t => t.id).filter(i => has.has(i) && now.indexOf(i) < 0) } });
      toast('已保存', 'ok'); mm.close(); done();
    });
  }

  function mergeModal(done) {
    const mm = modal('合并会员', `<div class="frow">${fld('被合并会员（号/手机）*', inp('mg-f', ''))}${fld('保留会员（号/手机）*', inp('mg-t', ''))}</div>
      <button class="btn" id="mg-pre">预览</button><div id="mg-box" style="margin-top:10px"></div>${fld('合并原因*', inp('mg-r', ''))}${errBox}`, async () => {
      if (!mm.previewed) throw new Error('请先预览确认');
      await api('api/member/merge', { body: { from_code: mm.f, to_code: mm.t, reason: v('mg-r') } });
      toast('已合并', 'ok'); mm.close(); done();
    }, { okText: '确认合并', onOpen: () => {
      $('mg-pre').onclick = async () => {
        try {
          const resolve = async k => { const d = (await api('api/member?' + qs({ kw: k, size: 2 }))).data; if (d.items.length !== 1) throw new Error('找不到唯一会员：' + k); return d.items[0].code; };
          mm.f = await resolve(v('mg-f')); mm.t = await resolve(v('mg-t'));
          const p = (await api('api/member/merge/preview', { body: { from_code: mm.f, to_code: mm.t } })).data;
          const col = x => `<td>${esc(x.code)} ${esc(x.name)}</td><td>${esc(x.phone)}</td><td class="r">${x.points}</td><td class="r">${x.stored_balance == null ? '—' : fx(x.stored_balance)}</td><td class="r">${x.coupon_count}</td><td class="r">${x.babies.length}</td><td class="r">${x.orders}</td>`;
          $('mg-box').innerHTML = `<table class="tbl"><tr><th></th><th>会员</th><th>手机</th><th class="r">积分</th><th class="r">储值</th><th class="r">可用券</th><th class="r">宝宝</th><th class="r">订单</th></tr>
            <tr><td>被合并</td>${col(p.from)}</tr><tr><td>保留</td>${col(p.to)}</tr><tr><td><b>合并后</b></td><td colspan="2"></td><td class="r">${p.after.points}</td><td class="r">${p.after.stored_balance == null ? '—' : fx(p.after.stored_balance)}</td><td class="r">${p.after.coupon_count}</td><td class="r">${p.after.babies}</td><td class="r">${p.after.orders}</td></tr></table><div class="hint">${esc(p.note)}</div>`;
          mm.previewed = true;
        } catch (e) { mm.root.querySelector('.err').textContent = e.message; }
      };
    } });
  }

  function importModal(done) {
    const mm = modal('导入会员', `<div class="hint">先 <a href="api/member/template">下载模板</a>，手机号必填；文件内或库内重复的手机号会被拒绝并列出行号。期初积分/储值会写流水。</div>
      <input type="file" id="mi-f" accept=".xlsx"><div id="mi-res"></div>${errBox}`, async () => {
      const fd = new FormData(); if (!$('mi-f').files[0]) throw new Error('请选择文件');
      fd.append('file', $('mi-f').files[0]);
      const r = (await api('api/member/import', { form: fd })).data;
      $('mi-res').innerHTML = `<div class="alert">新增 ${r.created}，失败 ${r.error_count}</div>` + r.errors.slice(0, 100).map(e => `<div class="hint">第 ${e.row_no} 行 ${esc(e.name)}：${esc(e.errors.join('；'))}</div>`).join('');
      done();
    }, { okText: '导入' });
  }

  /* ---------------- 会员标签 ---------------- */
  async function tagsPage(box) {
    const load = async () => {
      const d = (await api('api/member/tags')).data;
      box.innerHTML = `<div class="card"><div class="bar"><button class="btn pri" id="tg-new">新建标签</button><div class="hint">自动标签只按消费行为（时间范围/单数/金额/分类/品牌）计算，不做医疗或营养判断。</div></div>
        <table class="tbl"><tr><th>名称</th><th>类型</th><th>规则</th><th class="r">会员数</th><th>状态</th><th></th></tr>${d.map(t => `<tr><td>${esc(t.name)}</td><td>${t.kind === 'auto' ? '行为自动' : '手工'}</td>
        <td style="font-size:12px">${t.kind === 'auto' ? esc(`近 ${t.rule.days} 天 ${t.rule.category || ''}${t.rule.brand || ''} ≥${t.rule.min_orders || 0} 单 ≥${fx((t.rule.min_amount_cents || 0) / 100)} 元`) : ''}</td>
        <td class="r">${t.cnt}</td><td>${esc(t.status)}</td><td>${t.kind === 'auto' ? `<button class="btn sm" data-r="${t.id}">重新计算</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">还没有标签</td></tr>'}</table></div>`;
      $('tg-new').onclick = () => {
        const mm = modal('新建标签', `${fld('名称*', inp('tg-n', ''))}${fld('类型', sel('tg-k', [['manual', '手工'], ['auto', '按消费行为自动']], 'manual'))}
          <div class="frow">${fld('近 N 天', inp('tg-d', 90, 'type="number"'))}${fld('最少单数', inp('tg-o', 1, 'type="number"'))}${fld('最少金额（元）', inp('tg-a', 0, 'type="number"'))}</div>
          <div class="frow">${fld('分类（选填）', inp('tg-c', ''))}${fld('品牌（选填）', inp('tg-b', ''))}</div>${errBox}`, async () => {
          await api('api/member/tags', { body: { name: v('tg-n'), kind: v('tg-k'), rule: { days: v('tg-d'), min_orders: v('tg-o'), min_amount: v('tg-a'), category: v('tg-c'), brand: v('tg-b') } } });
          mm.close(); load();
        });
      };
      box.querySelectorAll('[data-r]').forEach(b => b.onclick = async () => { try { const r = (await api('api/member/tags/' + b.dataset.r + '/refresh', { body: {} })).data; toast('命中 ' + r.members + ' 个会员', 'ok'); load(); } catch (e) { toast(e.message, 'err'); } });
    };
    await load();
  }

  /* ---------------- 储值日报 ---------------- */
  async function storedPage(box) {
    box.innerHTML = `<div class="card"><div class="bar"><input type="date" id="sd-s" value="${addDays(-6)}"><input type="date" id="sd-e" value="${localDate()}">
      <select id="sd-st">${storeOpts(true).map(o => `<option value="${esc(o[0])}">${esc(o[1])}</option>`).join('')}</select><button class="btn pri" id="sd-go">查询</button></div><div id="sd-body"></div></div>`;
    const load = async () => {
      const d = (await api('api/member/stored/daily?' + qs({ start: v('sd-s'), end: v('sd-e'), store: v('sd-st') }))).data;
      $('sd-body').innerHTML = `<div class="kpis"><div class="kpi"><span>当前储值总余额</span><b>${fx(d.balance.total)}</b></div><div class="kpi"><span>本金</span><b>${fx(d.balance.principal)}</b></div><div class="kpi"><span>赠送</span><b>${fx(d.balance.gift)}</b></div><div class="kpi"><span>异常会员</span><b>${d.anomalies.length}</b></div></div>
        <table class="tbl"><tr><th>日期</th><th>类型</th><th class="r">笔数</th><th class="r">金额</th><th class="r">本金</th><th class="r">赠送</th></tr>${d.days.map(day => day.items.map((x, i) => `<tr><td>${i ? '' : esc(day.date) + `<div class="hint">当日净额 ${fx(day.net)}，日末余额 ${fx(day.closing_balance)}</div>`}</td><td>${esc(x.type)}</td><td class="r">${x.count}</td><td class="r num">${fx(x.amount)}</td><td class="r num">${fx(x.principal)}</td><td class="r num">${fx(x.gift)}</td></tr>`).join('')).join('') || '<tr><td colspan="6" class="empty">期间没有储值流水</td></tr>'}</table>
        ${d.anomalies.length ? `<h4>异常检查</h4><table class="tbl"><tr><th>会员</th><th class="r">余额</th><th class="r">本金</th><th class="r">赠送</th><th class="r">流水合计</th></tr>${d.anomalies.map(a => `<tr><td>${esc(a.code)}</td><td class="r">${fx(a.balance)}</td><td class="r">${fx(a.principal)}</td><td class="r">${fx(a.gift)}</td><td class="r">${fx(a.log_sum)}</td></tr>`).join('')}</table>` : ''}
        <div class="hint">${esc(d.caliber)}</div>`;
    };
    $('sd-go').onclick = () => load().catch(e => toast(e.message, 'err'));
    await load();
  }

  /* ---------------- 积分规则 / 欠扣处理 ---------------- */
  async function rulesPage(box) {
    const load = async () => {
      const r = (await api('api/member/rules')).data;
      let exc = [];
      if (can('member.points.adjust')) exc = (await api('api/member/points-exceptions')).data;
      box.innerHTML = `<div class="card"><h3>积分与券规则</h3><div class="hint">每元积分 ${r.base.points_per_yuan}、${r.base.points_to_yuan} 积分抵 1 元、最多抵 ${Math.round(r.base.points_max_ratio * 100)}%、抹零 ${esc(r.base.round_mode)}（在「收银参数」修改）</div>
        <div class="frow">${fld('退货扣回积分不足时', sel('ru-p', [['negative', '形成负积分'], ['limit', '扣到 0，记欠扣并限制积分抵扣'], ['manual', '扣到 0，转人工处理']], r.points_refund_policy))}
        ${fld('消费积分有效期（天，0=永久）', inp('ru-e', r.points_expire_days, 'type="number"'))}${fld('每单最多用券', inp('ru-c', r.coupon_max_per_order, 'type="number"'))}</div>
        ${fld('等级积分倍率（JSON，如 {"金卡会员":"1.5"}）', inp('ru-l', JSON.stringify(r.level_points_rate)))}
        ${fld('会员等级（逗号分隔）', inp('ru-lv', r.member_levels.join(',')))}
        ${can('sys.config') ? '<button class="btn pri" id="ru-save">保存规则</button>' : ''} ${can('member.points.adjust') ? '<button class="btn" id="ru-exp">执行积分过期</button>' : ''}</div>
        ${can('member.points.adjust') ? `<div class="card"><h3>退货积分欠扣（待处理）</h3><table class="tbl"><tr><th>时间</th><th>会员</th><th>退货单</th><th>原单</th><th class="r">差额</th><th>策略</th><th></th></tr>${exc.map(e => `<tr><td>${esc(e.created_at)}</td><td>${esc(e.member_code)}</td><td>${esc(e.ref_no)}</td><td>${esc(e.orig_order_no)}</td><td class="r">${e.shortfall}</td><td>${esc(e.policy)}</td><td><button class="btn sm" data-x="${e.id}" data-a="deduct">扣除</button> <button class="btn sm" data-x="${e.id}" data-a="waive">豁免</button></td></tr>`).join('') || '<tr><td colspan="7" class="empty">没有待处理</td></tr>'}</table></div>` : ''}`;
      if ($('ru-save')) $('ru-save').onclick = async () => {
        try {
          let lr; try { lr = JSON.parse(v('ru-l') || '{}'); } catch (e) { throw new Error('等级积分倍率不是合法 JSON'); }
          await api('api/member/rules', { body: { points_refund_policy: v('ru-p'), points_expire_days: v('ru-e'), coupon_max_per_order: v('ru-c'), level_points_rate: lr, member_levels: v('ru-lv').split(/[,，]/) } });
          toast('已保存', 'ok'); load();
        } catch (e) { toast(e.message, 'err'); }
      };
      if ($('ru-exp')) $('ru-exp').onclick = async () => { try { const d = (await api('api/member/points/expire', { body: {} })).data; toast('过期 ' + d.points + ' 分（' + d.members + ' 人）', 'ok'); } catch (e) { toast(e.message, 'err'); } };
      box.querySelectorAll('[data-x]').forEach(b => b.onclick = () => {
        const mm = modal(b.dataset.a === 'deduct' ? '扣除欠扣积分' : '豁免欠扣积分', fld('原因*', inp('px-r', '')) + errBox, async () => {
          await api('api/member/points-exceptions/' + b.dataset.x + '/resolve', { body: { action: b.dataset.a, reason: v('px-r') } }); mm.close(); load();
        });
      });
    };
    await load();
  }

  AdminShell.register({ key: 'member2', title: '会员档案', icon: '👶', group: '会员与营销', perm: 'member.view', render: listPage });
  AdminShell.register({ key: 'member-tags', title: '会员标签', icon: '🏷️', group: '会员与营销', perm: 'member.tag', render: tagsPage });
  AdminShell.register({ key: 'member-stored', title: '储值日报', icon: '💳', group: '会员与营销', perm: 'member.stored.view', render: storedPage });
  AdminShell.register({ key: 'member-rules', title: '积分规则', icon: '⭐', group: '会员与营销', perm: ['member.points.adjust', 'sys.config'], render: rulesPage });
})();
