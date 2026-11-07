/* 管理后台 · 组织与权限（模块④）：商户资料/连锁政策、区域与门店、员工、角色权限、操作审计。
   接口 api/org/*，全部相对路径；按权限码显示菜单（AdminShell.register 的 perm）。 */
'use strict';
(function () {
  const G = '组织与权限';
  const tag = (ok, a, b) => ok ? `<span class="tag green">${esc(a)}</span>` : `<span class="tag red">${esc(b)}</span>`;
  const pg = d => ({ total: d.total, page: d.page, pageSize: d.size, pages: Math.max(1, Math.ceil(d.total / d.size)) });

  /* ---------- 商户资料 + 连锁政策（ERP-001 / ERP-006） ---------- */
  AdminShell.register({
    key: 'org-merchant', title: '商户资料', icon: '🏢', group: G, perm: 'org.view', render: async box => {
      const m = (await api('api/org/merchant')).data;
      const can = AdminShell.can('org.edit');
      const pol = Object.keys(m.policy_options).map(k => fld(esc(m.policy_options[k].label),
        sel('pol-' + k, m.policy_options[k].choices.map(c => [c, { all: '全部门店', region: '同区域', none: '不允许', hq_only: '只能总部定价', store_allowed: '门店可自主定价' }[c] || c]), m.policy[k]))).join('');
      box.innerHTML = `<div class="card"><h3>商户资料</h3><div class="frow">${fld('商户名称', inp('m-name', m.name))}${fld('联系人', inp('m-contact', m.contact))}</div>
        <div class="frow">${fld('电话', inp('m-phone', m.phone))}${fld('地址', inp('m-addr', m.address))}</div>${fld('经营信息', `<textarea id="m-biz" rows="2">${esc(m.biz_info)}</textarea>`)}
        <div class="hint" style="color:var(--mut)">商户编码 ${esc(m.code)} · 状态 ${esc(m.status)} · 门店 ${m.stores_used}/${m.max_stores || '不限'} · 启用账号 ${m.users_used}/${m.max_users || '不限'}（额度由平台设置）</div></div>
        <div class="card"><h3>连锁政策</h3>${pol}${fld('变更原因（写入审计）', inp('pol-reason', ''))}</div>
        <div class="err" id="m-err"></div>${can ? '<button class="btn pri" id="m-save">保存</button>' : ''}`;
      if (!can) return;
      $('m-save').onclick = async () => {
        const policy = {}; Object.keys(m.policy_options).forEach(k => policy[k] = v('pol-' + k));
        try {
          await api('api/org/merchant', { body: { name: v('m-name'), contact: v('m-contact'), phone: v('m-phone'), address: v('m-addr'), biz_info: $('m-biz').value, policy, reason: v('pol-reason') } });
          $('m-err').textContent = ''; toast('已保存', 'ok');
        } catch (e) { $('m-err').textContent = e.message; }
      };
    }
  });

  /* ---------- 区域与门店（ERP-002 / ERP-003） ---------- */
  AdminShell.register({
    key: 'org-stores', title: '区域与门店', icon: '🏬', group: G, perm: 'org.view', render: async box => {
      const [units, stores] = await Promise.all([api('api/org/units'), api('api/org/stores')]);
      const can = AdminShell.can('org.edit');
      const regions = units.data.filter(u => u.kind === 'region');
      box.innerHTML = `<div class="card"><div class="bar"><h3 style="margin:0">区域</h3><div class="sp"></div>${can ? '<button class="btn" id="u-new">新增区域</button>' : ''}</div>
        <table class="tbl"><tr><th>编码</th><th>名称</th><th>类型</th><th>门店</th><th>状态</th><th></th></tr>${units.data.map(u => `<tr><td>${esc(u.code)}</td><td>${esc(u.name)}</td><td>${u.kind === 'hq' ? '总部' : '区域'}</td><td>${esc(u.stores.join('、') || '-')}</td><td>${tag(u.status === '启用', '启用', '停用')}</td><td>${can ? `<button class="btn sm" data-u="${esc(u.code)}">编辑</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">还没有区域</td></tr>'}</table></div>
        <div class="card"><div class="bar"><h3 style="margin:0">门店</h3><div class="sp"></div>${can ? '<button class="btn pri" id="s-new">新增门店</button>' : ''}</div><div class="tbl-wrap">
        <table class="tbl"><tr><th>编码</th><th>名称</th><th>区域</th><th>类型</th><th>负责人</th><th>营业时间</th><th>电话</th><th>在职账号</th><th>状态</th><th></th></tr>${stores.data.map(s => `<tr><td>${esc(s.code)}</td><td>${esc(s.name)}</td><td>${esc(s.region_name || '-')}</td><td>${esc(s.store_type || '')}</td><td>${esc(s.manager_name || '')}</td><td>${esc(s.open_hours || '')}</td><td>${esc(s.phone || '')}</td><td class="r">${s.users}</td><td>${tag(s.status === '营业', '营业', '停业')}</td><td>${can ? `<button class="btn sm" data-s="${esc(s.code)}">编辑</button>` : ''}</td></tr>`).join('')}</table></div>
        <div class="hint" style="color:var(--mut)">新建门店会自动建门店仓；仓库、收银台在各自菜单维护。批量建店可用「导入模板」里的门店模板。</div></div>`;
      if (!can) return;
      const unitModal = u => {
        u = u || { kind: 'region', code: '', name: '', status: '启用' };
        modal(u.code ? '编辑区域 ' + u.code : '新增区域', `<div class="frow">${fld('编码*', inp('ou-code', u.code, u.code ? 'readonly' : ''))}${fld('名称*', inp('ou-name', u.name))}</div>
          <div class="frow">${fld('类型', sel('ou-kind', [['region', '区域'], ['hq', '总部']], u.kind))}${fld('状态', sel('ou-st', ['启用', '停用'], u.status))}</div>${fld('调整原因', inp('ou-reason', ''))}${errBox}`, async mm => {
          await api('api/org/units', { body: { code: v('ou-code'), name: v('ou-name'), kind: v('ou-kind'), status: v('ou-st'), reason: v('ou-reason') } }); mm.close(); toast('已保存', 'ok'); go('org-stores');
        });
      };
      const storeModal = s => {
        s = s || { code: '', name: '', status: '营业', store_type: '直营', region_code: '' };
        modal(s.id ? '编辑门店 ' + s.code : '新增门店', `<div class="frow">${fld('门店编码*', inp('st-code', s.code, s.id ? 'readonly' : ''), '2-12 位字母数字')}${fld('门店名称*', inp('st-name', s.name))}</div>
          <div class="frow">${fld('区域', sel('st-region', [['', '未分区']].concat(regions.map(r => [r.code, r.name])), s.region_code))}${fld('类型', sel('st-type', ['直营', '加盟'], s.store_type))}</div>
          <div class="frow">${fld('负责人', inp('st-mgr', s.manager_name))}${fld('营业时间', inp('st-hours', s.open_hours, 'placeholder="09:00-21:30"'))}</div>
          <div class="frow">${fld('电话', inp('st-phone', s.phone))}${fld('状态', sel('st-st', ['营业', '停业'], s.status))}</div>${fld('地址', inp('st-addr', s.address))}${errBox}`, async mm => {
          await api('api/org/stores', { body: { code: v('st-code'), name: v('st-name'), region_code: v('st-region'), store_type: v('st-type'), manager_name: v('st-mgr'), open_hours: v('st-hours'), phone: v('st-phone'), status: v('st-st'), address: v('st-addr') } });
          mm.close(); toast('已保存', 'ok'); go('org-stores');
        });
      };
      $('u-new').onclick = () => unitModal(null);
      $('s-new').onclick = () => storeModal(null);
      box.querySelectorAll('[data-u]').forEach(b => b.onclick = () => unitModal(units.data.find(x => x.code === b.dataset.u)));
      box.querySelectorAll('[data-s]').forEach(b => b.onclick = () => storeModal(stores.data.find(x => x.code === b.dataset.s)));
    }
  });

  /* ---------- 导购（收银台选导购、会员归属导购） ---------- */
  AdminShell.register({
    key: 'org-guides', title: '导购', icon: '🙋', group: G, perm: 'org.view', render: async box => {
      const can = AdminShell.can('org.edit');
      const stores = S.stores || [];
      box.innerHTML = `<div class="card"><div class="bar"><select id="gd-store"><option value="">全部门店</option>${stores.map(s => `<option value="${esc(s.code)}">${esc(s.name)}</option>`).join('')}</select><button class="btn pri" id="gd-go">查询</button><div class="sp"></div>${can ? '<button class="btn pri" id="gd-new">新增导购</button>' : ''}</div><div class="tbl-wrap" id="gd-tbl"></div>
        <div class="hint" style="color:var(--mut)">离职导购不再出现在收银台的导购列表里，历史订单和会员归属不受影响。</div></div>`;
      const guideModal = g => {
        g = g || { code: '', name: '', store_code: v('gd-store') || ((stores[0] || {}).code || ''), status: '在职' };
        modal(g.id ? '编辑导购 ' + g.code : '新增导购', `<div class="frow">${fld('导购编号*', inp('gd-code', g.code, g.id ? 'readonly' : ''), '商户内唯一')}${fld('姓名*', inp('gd-name', g.name))}</div>
          <div class="frow">${fld('所属门店*', sel('gd-st', stores.map(s => [s.code, s.name]), g.store_code))}${fld('状态', sel('gd-status', [['在职', '在职'], ['离职', '离职（停用）']], g.status))}</div>${fld('变更原因（写入审计）', inp('gd-reason', ''))}${errBox}`, async mm => {
          await api('api/org/guides', { body: { code: v('gd-code'), name: v('gd-name'), store_code: v('gd-st'), status: v('gd-status'), reason: v('gd-reason') } });
          mm.close(); toast('已保存', 'ok'); load();
        });
      };
      let rows = [];
      const load = async () => {
        rows = (await api('api/org/guides?' + qs({ store: v('gd-store') }))).data;
        $('gd-tbl').innerHTML = `<table class="tbl"><tr><th>编号</th><th>姓名</th><th>门店</th><th>状态</th><th></th></tr>${rows.map(g => `<tr><td>${esc(g.code)}</td><td>${esc(g.name)}</td><td>${esc(g.store_name || g.store_code)}</td><td>${tag(g.status === '在职', '在职', '离职')}</td><td>${can ? `<button class="btn sm" data-g="${esc(g.code)}">编辑</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">还没有导购</td></tr>'}</table>`;
        $('gd-tbl').querySelectorAll('[data-g]').forEach(b => b.onclick = () => guideModal(rows.find(x => x.code === b.dataset.g)));
      };
      $('gd-go').onclick = () => load().catch(e => toast(e.message, 'err'));
      if (can) $('gd-new').onclick = () => guideModal(null);
      await load();
    }
  });

  /* ---------- 员工（ERP-701 / ERP-704） ---------- */
  AdminShell.register({
    key: 'org-users', title: '员工与会话', icon: '🧑‍💼', group: G, perm: 'user.view', render: async box => {
      const f = { kw: '', status: '', page: 1 };
      const can = AdminShell.can('user.edit');
      const [roles, stores] = await Promise.all([can ? api('api/org/roles') : Promise.resolve({ data: [] }), api('api/org/stores')]);
      box.innerHTML = `<div class="card"><div class="bar"><input id="ou-kw" placeholder="账号 / 姓名 / 员工号"><select id="ou-st"><option value="">全部状态</option><option>启用</option><option>停用</option></select><button class="btn pri" id="ou-go">查询</button><div class="sp"></div>${can ? '<button class="btn pri" id="ou-new">新增员工</button>' : ''}</div><div class="tbl-wrap" id="ou-tbl"></div>
        <div class="hint" style="color:var(--mut)">调店、离职（停用）、改角色、改密码/PIN、改授权范围后，该员工所有终端立即下线，重新登录后按新权限生效。</div></div>`;
      const userModal = u => {
        u = u || { username: '', name: '', role_id: (roles.data.find(r => r.code === 'cashier') || {}).id, store_code: '', status: '启用', scopes: [], emp_no: '', phone: '' };
        const scopeTxt = (u.scopes || []).map(s => s.type + ':' + s.code).join(',');
        modal(u.id ? '编辑员工 ' + u.username : '新增员工', `<div class="frow">${fld('登录账号*', inp('e-user', u.username, u.id ? 'readonly' : ''), '全平台唯一，3-20 位字母数字')}${fld('姓名', inp('e-name', u.name))}</div>
          <div class="frow">${fld('角色*', sel('e-role', roles.data.filter(r => r.status === '启用').map(r => [r.id, r.name + '（' + r.data_scope_name + '）']), u.role_id))}${fld('主门店', sel('e-store', [['', '无（总部）']].concat(stores.data.map(s => [s.code, s.name])), u.store_code))}</div>
          <div class="frow">${fld('员工编号', inp('e-emp', u.emp_no))}${fld('收银 PIN（4-8 位数字，不改留空）', inp('e-pin', '', 'type="password" autocomplete="new-password"'))}</div>
          <div class="frow">${fld(u.id ? '新密码（不改留空，至少 8 位）' : '初始密码*（至少 8 位，首次登录须改）', inp('e-pw', '', 'type="password" autocomplete="new-password"'))}${fld('状态', sel('e-st', [['启用', '在职（启用）'], ['停用', '离职/停用']], u.status))}</div>
          <div class="frow">${fld('手机号', inp('e-phone', u.phone))}${fld('额外授权范围', inp('e-scopes', scopeTxt, 'placeholder="store:B001,region:R1"'), '跨店/区域权限，逗号分隔')}</div>${fld('变更原因（写入审计）', inp('e-reason', ''))}${errBox}`, async mm => {
          const scopes = v('e-scopes').split(/[,，]/).map(x => x.trim()).filter(Boolean).map(x => { const p = x.split(':'); return { type: p[0], code: p[1] || '' }; });
          await api('api/org/users', { body: { id: u.id, username: v('e-user'), name: v('e-name'), role_id: +v('e-role'), store_code: v('e-store'), emp_no: v('e-emp'), pin: $('e-pin').value, password: $('e-pw').value, status: v('e-st'), phone: v('e-phone'), scopes, reason: v('e-reason') } });
          mm.close(); toast('已保存', 'ok'); load();
        });
      };
      const sessModal = async u => {
        const d = (await api('api/org/users/' + u.id + '/sessions')).data;
        modal('登录设备 · ' + u.username, `<table class="tbl"><tr><th>终端</th><th>设备</th><th>IP</th><th>登录时间</th><th>状态</th></tr>${d.map(s => `<tr><td>${esc(s.client)}</td><td style="max-width:260px;overflow:hidden;text-overflow:ellipsis">${esc(s.device)}</td><td>${esc(s.ip)}</td><td>${esc(s.created_at)}</td><td>${s.active ? '<span class="tag green">在线</span>' : esc(s.revoke_reason || '已失效')}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">没有会话</td></tr>'}</table>`, null,
          { wide: 'wide', extra: [{ text: '全部踢下线', fn: async mm => { const r = await api('api/org/users/' + u.id + '/sessions', { method: 'DELETE' }); mm.close(); toast('已下线 ' + r.data.revoked + ' 个终端', 'ok'); load(); } }] });
      };
      const load = async () => {
        const d = (await api('api/org/users?' + qs({ kw: v('ou-kw'), status: v('ou-st'), page: f.page, size: 30 }))).data;
        $('ou-tbl').innerHTML = `<table class="tbl"><tr><th>账号</th><th>姓名</th><th>员工号</th><th>角色</th><th>主门店</th><th>授权范围</th><th>手机</th><th>状态</th><th>在线</th><th>最后登录</th><th></th></tr>${d.items.map(u => `<tr><td>${esc(u.username)}</td><td>${esc(u.name)}</td><td>${esc(u.emp_no || '')}${u.has_pin ? ' 🔢' : ''}</td><td>${esc(u.role_name)}</td><td>${esc(u.store_name || '总部')}</td><td>${esc(u.scopes.map(s => s.code).join(',') || '-')}</td><td>${esc(u.phone || '')}</td><td>${tag(u.status === '启用', '在职', '离职/停用')}</td><td class="r">${u.online}</td><td>${esc(u.last_login || '-')}</td><td>${can ? `<button class="btn sm" data-e="${u.id}">编辑</button> <button class="btn sm" data-ss="${u.id}">设备</button>` : ''}</td></tr>`).join('')}</table>` + pager(pg(d));
        bindPager($('ou-tbl'), p => { f.page = p; load(); });
        $('ou-tbl').querySelectorAll('[data-e]').forEach(b => b.onclick = () => userModal(d.items.find(x => x.id == b.dataset.e)));
        $('ou-tbl').querySelectorAll('[data-ss]').forEach(b => b.onclick = () => sessModal(d.items.find(x => x.id == b.dataset.ss)).catch(e => toast(e.message, 'err')));
      };
      $('ou-go').onclick = () => { f.page = 1; load(); };
      if (can) $('ou-new').onclick = () => userModal(null);
      await load();
    }
  });

  /* ---------- 角色权限（ERP-702 / 3.3） ---------- */
  AdminShell.register({
    key: 'org-roles', title: '角色权限', icon: '🔐', group: G, perm: 'role.edit', render: async box => {
      const [roles, reg] = await Promise.all([api('api/org/roles'), api('api/org/perms')]);
      const R = reg.data;
      box.innerHTML = `<div class="card"><div class="bar"><span class="hint" style="color:var(--mut)">系统角色的权限码由系统维护（只能改名称和操作额度）；要调整权限请「复制」后自定义。所有变更写审计。</span><div class="sp"></div><button class="btn pri" id="r-new">新建角色</button></div>
        <table class="tbl"><tr><th>编码</th><th>名称</th><th>数据范围</th><th>权限数</th><th>在用账号</th><th>类型</th><th>状态</th><th></th></tr>${roles.data.map(r => `<tr><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td>${esc(r.data_scope_name)}</td><td class="r">${r.perms.length}</td><td class="r">${r.users}</td><td>${r.is_system ? '系统' : '自定义'}</td><td>${tag(r.status === '启用', '启用', '停用')}</td><td><button class="btn sm" data-r="${r.id}">编辑</button> <button class="btn sm" data-c="${r.id}">复制</button></td></tr>`).join('')}</table></div>`;
      const roleModal = (r, copyFrom) => {
        const src = r || copyFrom || { perms: [], limits: {}, data_scope: 'store' };
        const sys = r && r.is_system;
        const permHtml = R.groups.map(g => `<div style="margin:6px 0"><b>${esc(g.name)}</b><div style="display:flex;flex-wrap:wrap;gap:4px 14px">${g.perms.map(p => `<label style="font-weight:normal"><input type="checkbox" class="pp" value="${esc(p.code)}" ${src.perms.indexOf(p.code) >= 0 ? 'checked' : ''} ${sys ? 'disabled' : ''}> ${esc(p.name)}</label>`).join('')}</div></div>`).join('');
        const limHtml = R.limits.map(l => fld(esc(l.name), inp('lim-' + l.key, (src.limits || {})[l.key] == null ? '' : src.limits[l.key]))).join('');
        modal(r ? '编辑角色 ' + r.name : (copyFrom ? '复制角色 ' + copyFrom.name : '新建角色'), `<div class="frow">${fld('编码*', inp('ro-code', r ? r.code : '', r ? 'readonly' : 'placeholder="小写字母开头"'))}${fld('名称*', inp('ro-name', r ? r.name : (copyFrom ? copyFrom.name + '（副本）' : '')))}</div>
          <div class="frow">${fld('数据范围', sel('ro-scope', R.data_scopes.map(x => [x.code, x.name]), src.data_scope))}${fld('状态', sel('ro-st', ['启用', '停用'], r ? r.status : '启用'))}</div>
          <h4>功能 / 字段权限</h4><div style="max-height:300px;overflow:auto;border:1px solid #eee;padding:6px 10px">${permHtml}</div>
          <h4>操作额度</h4><div class="frow" style="flex-wrap:wrap">${limHtml}</div>${fld('变更原因（写入审计）', inp('ro-reason', ''))}${errBox}`, async mm => {
          const limits = {}; R.limits.forEach(l => { const x = v('lim-' + l.key); if (x !== '') limits[l.key] = x; });
          const body = { id: r ? r.id : undefined, copy_from: copyFrom ? copyFrom.id : undefined, code: v('ro-code'), name: v('ro-name'), data_scope: v('ro-scope'), status: v('ro-st'), limits, reason: v('ro-reason') };
          if (!sys) body.perms = Array.from(mm.root.querySelectorAll('.pp:checked')).map(x => x.value);
          if (sys) delete body.data_scope;
          await api('api/org/roles', { body }); mm.close(); toast('已保存', 'ok'); go('org-roles');
        }, { wide: 'wide' });
      };
      $('r-new').onclick = () => roleModal(null, null);
      box.querySelectorAll('[data-r]').forEach(b => b.onclick = () => roleModal(roles.data.find(x => x.id == b.dataset.r), null));
      box.querySelectorAll('[data-c]').forEach(b => b.onclick = () => roleModal(null, roles.data.find(x => x.id == b.dataset.c)));
    }
  });

  /* ---------- 操作审计（ERP-705） ---------- */
  AdminShell.register({
    key: 'org-audit', title: '操作审计', icon: '🛡️', group: G, perm: 'audit.view', render: async box => {
      const f = { page: 1 };
      box.innerHTML = `<div class="card"><div class="bar"><input id="au-user" placeholder="账号" style="width:110px"><input id="au-ref" placeholder="单号/对象" style="width:160px"><input id="au-act" placeholder="操作" style="width:110px">
        <select id="au-lv"><option value="">全部级别</option><option value="info">普通</option><option value="high">重要</option><option value="security">安全</option></select>
        <input type="date" id="au-s" value="${addDays(-6)}"> 至 <input type="date" id="au-e" value="${localDate()}"><button class="btn pri" id="au-go">查询</button></div><div class="tbl-wrap" id="au-tbl"></div></div>`;
      const short = s => { s = s || ''; return s.length > 120 ? s.slice(0, 120) + '…' : s; };
      const load = async () => {
        const d = (await api('api/org/audit?' + qs({ user: v('au-user'), ref_no: v('au-ref'), action: v('au-act'), level: v('au-lv'), start: v('au-s'), end: v('au-e'), page: f.page, size: 30 }))).data;
        $('au-tbl').innerHTML = `<table class="tbl"><tr><th>时间</th><th>账号</th><th>门店</th><th>模块</th><th>操作</th><th>对象</th><th>变更前</th><th>变更后</th><th>原因</th><th>级别</th><th>IP</th></tr>${d.items.map(x => `<tr><td>${esc(x.created_at)}</td><td>${esc(x.username)}</td><td>${esc(x.store_code)}</td><td>${esc(x.module)}</td><td>${esc(x.action)}</td><td>${esc(x.ref_no)}</td><td title="${esc(x.before_json)}">${esc(short(x.before_json))}</td><td title="${esc(x.after_json)}">${esc(short(x.after_json))}</td><td>${esc(x.reason)}</td><td>${x.level === 'security' ? '<span class="tag red">安全</span>' : x.level === 'high' ? '<span class="tag">重要</span>' : '普通'}</td><td>${esc(x.ip)}</td></tr>`).join('') || '<tr><td colspan="11" class="empty">没有记录</td></tr>'}</table>` + pager(pg(d));
        bindPager($('au-tbl'), p => { f.page = p; load(); });
      };
      $('au-go').onclick = () => { f.page = 1; load(); };
      await load();
    }
  });
})();
