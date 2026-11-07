/* 管理后台 · 报表中心（模块④）：销售/商品毛利/时段/人员/支付/班次/库存/会员/订单明细 + 导出（同步 / 异步任务）。
   接口 api/report/*；成本毛利按 report.profit 由服务端决定是否返回（无权限为 null，列也不出现）。 */
'use strict';
(function () {
  const KINDS = [
    ['sales', '销售汇总', 'report.sales', [['day', '按日'], ['week', '按周'], ['month', '按月'], ['store', '按门店'], ['region', '按区域']]],
    ['goods', '商品分析', 'report.goods', [['product', '按商品'], ['category', '按分类'], ['brand', '按品牌'], ['spec', '按规格']]],
    ['hourly', '时段分析', 'report.sales', []],
    ['staff', '人员/收银台', 'report.sales', [['cashier', '按收银员'], ['terminal', '按收银台'], ['guide', '按导购']]],
    ['pay', '支付与现金收支', 'report.pay', []],
    ['shifts', '班次长短款', 'report.pay', []],
    ['stock', '库存报表', 'report.stock', [['', '门店汇总'], ['detail', '商品明细']]],
    ['members', '会员报表', 'report.member', []],
    ['orders', '订单明细', 'report.sales', [['', '全部'], ['sale', '销售'], ['refund', '退货']]],
  ];
  const cell = x => x == null ? '<span style="color:#bbb">—</span>' : (typeof x === 'number' ? (Number.isInteger(x) ? x : fx(x)) : esc(x));
  const table = (cols, rows, total) => `<table class="tbl"><tr>${cols.map(c => `<th>${esc(c.title)}</th>`).join('')}</tr>${rows.map(r => `<tr>${cols.map(c => `<td>${cell(r[c.key])}</td>`).join('')}</tr>`).join('') || `<tr><td colspan="${cols.length}" class="empty">这个范围没有数据</td></tr>`}${total && rows.length && cols.some((c, i) => i && total[c.key] !== undefined) ? `<tr style="font-weight:700;background:#fafbfc">${cols.map((c, i) => `<td>${i === 0 ? '合计' : cell(total[c.key])}</td>`).join('')}</tr>` : ''}</table>`;

  AdminShell.register({
    key: 'report-center', title: '报表中心', icon: '📈', group: '报表', perm: ['report.sales', 'report.goods', 'report.pay', 'report.stock', 'report.member'], render: async box => {
      const kinds = KINDS.filter(k => AdminShell.can(k[2]));
      const canExport = AdminShell.can('report.export');
      const stores = (S.me.stores || []);
      box.innerHTML = `<div class="card"><div class="bar" style="flex-wrap:wrap"><select id="rc-kind">${kinds.map(k => `<option value="${k[0]}">${k[1]}</option>`).join('')}</select><select id="rc-dim"></select>
        <select id="rc-store"><option value="">${stores.length > 1 ? '全部可见门店' : '本店'}</option>${stores.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('')}</select>
        <input type="date" id="rc-s" value="${addDays(-29)}"> 至 <input type="date" id="rc-e" value="${localDate()}"><button class="btn pri" id="rc-go">查询</button><div class="sp"></div>
        ${canExport ? '<button class="btn" id="rc-x">导出 Excel</button><button class="btn" id="rc-ax">后台导出（大数据量）</button>' : ''}</div>
        <div id="rc-cal" class="hint" style="color:var(--mut);margin:6px 0"></div><div class="tbl-wrap" id="rc-tbl"></div><div id="rc-extra"></div></div>
        ${canExport ? '<div class="card"><div class="bar"><h3 style="margin:0">我的导出任务</h3><div class="sp"></div><button class="btn sm" id="rc-jr">刷新</button></div><div id="rc-jobs"></div><div class="hint" style="color:var(--mut)">后台导出不影响门店收银；文件 24 小时内有效，只能本人下载。</div></div>' : ''}`;
      const setDims = () => { const k = kinds.find(x => x[0] === v('rc-kind')); $('rc-dim').innerHTML = (k[3].length ? k[3] : [['', '—']]).map(d => `<option value="${d[0]}">${d[1]}</option>`).join(''); $('rc-dim').style.display = k[3].length ? '' : 'none'; };
      const params = () => {
        const k = v('rc-kind'), d = v('rc-dim'), p = { start: v('rc-s'), end: v('rc-e'), store: v('rc-store') };
        if (k === 'stock') { if (d === 'detail') p.detail = '1'; } else if (k === 'orders') { if (d) p.type = d; } else if (d) p.dim = d;
        return p;
      };
      const load = async () => {
        const k = v('rc-kind');
        const d = (await api('api/report/' + k + '?' + qs(params()))).data;
        $('rc-cal').innerHTML = (d.caliber || []).map(x => '· ' + esc(x)).join('<br>');
        $('rc-tbl').innerHTML = table(d.columns || [], d.rows || [], d.total);
        let extra = '';
        if (k === 'members' && d.total) extra = `<h4>会员概况</h4><div class="hint">活跃 ${d.total.active_members} · 复购 ${d.total.repeat_members}（复购率 ${fx(d.total.repeat_rate)}%）· 会员贡献 ${fx(d.total.contribution)}% · 积分发放 ${d.total.points_issued} / 使用 ${d.total.points_used} · 券核销 ${d.total.coupons_used} 张 ${money(d.total.coupon_amount)}</div>
          <h4>等级分布</h4>${table([{ key: 'level', title: '等级' }, { key: 'count', title: '人数' }], d.levels || [])}<h4>贡献前 20 会员</h4>${table([{ key: 'member_code', title: '会员号' }, { key: 'name', title: '姓名' }, { key: 'phone', title: '手机' }, { key: 'orders', title: '单数' }, { key: 'sales', title: '净销售额' }], d.top_members || [])}`;
        if (k === 'pay') extra = `<h4>现金收支与支付差异</h4><div class="hint">现金收入 ${money(d.total.cash_in)} · 现金支出 ${money(d.total.cash_out)}</div>${table([{ key: 'order_no', title: '单号' }, { key: 'payable', title: '应收' }, { key: 'paid', title: '支付流水合计' }, { key: 'diff', title: '差异' }, { key: 'created_at', title: '时间' }], d.pay_diffs || [])}`;
        if (k === 'stock' && d.moves) extra = `<h4>区间出入库（基本单位）</h4>${table([{ key: 'type', title: '类型' }, { key: 'in_qty', title: '入库' }, { key: 'out_qty', title: '出库' }, { key: 'count', title: '笔数' }], d.moves)}<div class="hint">滞销 SKU（${d.total.slow_days} 天无销售）：${d.total.slow_moving_sku}</div>`;
        if (k === 'shifts' && d.total) extra = `<div class="hint">班次 ${d.total.shifts} · 长款 ${d.total.over_count} 次 · 短款 ${d.total.short_count} 次 · 长短款合计 ${money(d.total.diff)}</div>`;
        $('rc-extra').innerHTML = extra;
      };
      const loadJobs = async () => {
        if (!$('rc-jobs')) return;
        const d = (await api('api/report/exports')).data;
        const st = { queued: '排队中', running: '生成中', done: '已完成', failed: '失败', expired: '已过期' };
        $('rc-jobs').innerHTML = `<table class="tbl"><tr><th>任务号</th><th>报表</th><th>条件</th><th>状态</th><th>行数</th><th>创建</th><th>有效期至</th><th></th></tr>${d.map(j => `<tr><td>${esc(j.job_no)}</td><td>${esc(j.kind_name)}</td><td>${esc(Object.keys(j.params).map(x => x + '=' + j.params[x]).join(' '))}</td><td>${esc(st[j.status] || j.status)}${j.error ? ' · ' + esc(j.error) : ''}</td><td class="r">${j.row_count || ''}</td><td>${esc(j.created_at)}</td><td>${esc(j.expire_at || '')}</td><td>${j.download ? `<a class="btn sm" href="${esc(j.download)}">下载</a>` : ''}</td></tr>`).join('') || '<tr><td colspan="8" class="empty">还没有导出任务</td></tr>'}</table>`;
        if (d.some(j => j.status === 'queued' || j.status === 'running')) setTimeout(() => loadJobs().catch(() => { }), 2000);
      };
      $('rc-kind').onchange = () => { setDims(); load().catch(e => toast(e.message, 'err')); };
      $('rc-go').onclick = () => load().catch(e => toast(e.message, 'err'));
      if (canExport) {
        $('rc-x').onclick = () => { location.href = 'api/report/' + v('rc-kind') + '/export?' + qs(params()); };
        $('rc-ax').onclick = async () => { try { await api('api/report/exports', { body: { kind: v('rc-kind'), params: params() } }); toast('已提交后台导出', 'ok'); loadJobs(); } catch (e) { toast(e.message, 'err'); } };
        $('rc-jr').onclick = () => loadJobs();
        loadJobs().catch(() => { });
      }
      setDims();
      await load();
    }
  });
})();
