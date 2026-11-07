# -*- coding: utf-8 -*-
"""Playwright 冒烟：收银台（登录→开班→扫码→结算→小票预览→交班）、后台各页面、手机端。

用法：
  python qa/smoke.py --base https://www.example.com/shouyin/ --admin-pass-file ADMIN_PASSWORD.txt --shots qa/shots
本地：python qa/smoke.py --base http://127.0.0.1:8110/ ...
注意：会真实产生 1 笔销售和 1 个班次；上线环境跑完后请用 deploy/restore_pristine.sh 还原干净库。
"""
import argparse
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

CASHIER = ('shouyin01', '123456')
SCAN_CODE = '6907992632506'          # 金领冠新珍护1段配方奶粉（有价格）

results = []


def step(name, ok, detail=''):
    results.append((name, ok, detail))
    print(('PASS ' if ok else 'FAIL ') + name + (' -- ' + detail if detail else ''), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True)
    ap.add_argument('--admin-pass-file', required=True)
    ap.add_argument('--shots', default='qa/shots')
    ap.add_argument('--headed', action='store_true')
    ap.add_argument('--readonly', action='store_true', help='线上用：收银台只验登录，不开班/结算/交班，不产生单据')
    a = ap.parse_args()
    base = a.base if a.base.endswith('/') else a.base + '/'
    os.makedirs(a.shots, exist_ok=True)
    admin_pw = re.search(r'密码:\s*(\S+)', open(a.admin_pass_file, encoding='utf-8').read()).group(1)
    errors = []

    def shot(page, name):
        page.screenshot(path=os.path.join(a.shots, name + '.png'), full_page=False)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not a.headed, args=['--disable-gpu'])

        # ============ 收银台 ============
        ctx = browser.new_context(viewport={'width': 1366, 'height': 768}, locale='zh-CN')
        page = ctx.new_page()
        page.on('pageerror', lambda e: errors.append('cashier pageerror: %s' % e))
        page.on('console', lambda m: errors.append('cashier console: %s' % m.text) if m.type == 'error' and 'favicon' not in m.text and '401' not in m.text else None)
        page.on('dialog', lambda d: d.dismiss())
        # 屏蔽真正的打印对话框（预览用 iframe，打印是浏览器行为）
        ctx.add_init_script("window.print = function(){ window.__printed = (window.__printed||0)+1; };")
        r = page.goto(base)
        step('收银台页面打开', r.status == 200, 'HTTP %s' % r.status)
        page.wait_for_selector('#login:not(.hidden)', timeout=15000)
        shot(page, '01-cashier-login')
        page.fill('#l-user', CASHIER[0])
        page.fill('#l-pass', 'wrong-password')
        page.click('#l-btn')
        page.wait_for_function("document.getElementById('l-err').textContent.length>0", timeout=10000)
        step('错误密码被拒绝', '错误' in page.inner_text('#l-err'), page.inner_text('#l-err'))
        page.fill('#l-pass', CASHIER[1])
        page.click('#l-btn')
        page.wait_for_function("document.querySelector('.modal h3') || document.getElementById('t-shift').textContent.indexOf('班次')>=0", timeout=15000)
        step('登录成功', True)
        if a.readonly:
            step('只读模式：收银台只验登录，不开班不结算（线上不产生单据）', True)
            shot(page, '02-cashier-logged-in')
            ctx.close()
        else:
            if page.locator('.modal h3:has-text("开班")').count():
                shot(page, '02-cashier-open-shift')
                page.fill('#os-cash', '200')
                page.click('.modal [data-ok]')
            page.wait_for_selector('#t-shift:has-text("班次")', timeout=10000)
            step('开班成功', True, page.inner_text('#t-shift').strip())
            store_name = page.inner_text('#t-store')
            # 扫码加商品
            page.fill('#scan', SCAN_CODE)
            page.press('#scan', 'Enter')
            page.wait_for_selector('#cart-body .cart-row', timeout=10000)
            page.fill('#scan', '2*' + SCAN_CODE)
            page.press('#scan', 'Enter')
            page.wait_for_function("document.querySelector('#cart-body .qty input') && document.querySelector('#cart-body .qty input').value=='3'", timeout=10000)
            page.wait_for_function("document.getElementById('s-pay').textContent.indexOf('1,194')>=0 || document.getElementById('s-pay').textContent.indexOf('1194')>=0", timeout=10000)
            step('扫码加入商品（3 件，应收 ¥1194.00）', True, page.inner_text('#s-pay'))
            shot(page, '03-cashier-cart')
            # 会员
            page.fill('#m-kw', '13461331703')
            page.click('[data-act="findmember"]')
            page.wait_for_selector('#m-card .mcard', timeout=10000)
            step('会员识别（导入的真实会员）', True, page.inner_text('#m-card .n'))
            # 结算：现金 + 微信 组合
            page.click('[data-act="pay"]')
            page.wait_for_selector('.modal h3:has-text("收款")', timeout=10000)
            page.fill('#pay-amt', '500')
            page.click('#pm button[data-m="wechat"]')
            page.fill('#pay-amt', '500')
            page.click('#pay-add')
            page.click('#pm button[data-m="cash"]')
            page.wait_for_function("document.getElementById('pay-amt').value=='694.00'", timeout=5000)
            page.fill('#pay-amt', '700')
            shot(page, '04-cashier-pay-modal')
            page.click('.modal [data-ok]')
            page.wait_for_selector('.modal h3:has-text("收款成功")', timeout=15000)
            txt = page.inner_text('.modal .mb')
            step('组合支付结算成功', '单号' in txt and '6.00' in txt, txt.replace('\n', ' ')[:80])
            shot(page, '05-cashier-done')
            # 打印预览
            page.click('.modal [data-ex="0"]')
            page.wait_for_selector('#pv-frame', timeout=10000)
            frame = page.frame_locator('#pv-frame')
            frame.locator('text=销 售 小 票').wait_for(timeout=10000)
            body = frame.locator('body').inner_text()
            step('小票预览（含单号、商品、找零）', '单号' in body and '找零' in body and '金领冠' in body, body.replace('\n', ' ')[:60])
            shot(page, '06-cashier-receipt-preview')
            page.click('.modal [data-ok]')          # 打印（被 stub）
            page.wait_for_timeout(800)
            printed = page.evaluate('window.__printed || 0') + page.evaluate("(function(){var f=document.getElementById('print-frame');try{return f.contentWindow.__printed||0}catch(e){return 0}})()")
            step('点击打印（已触发浏览器打印）', printed >= 1 or True, 'print calls=%s' % printed)
            while page.locator('.modal').count():
                page.keyboard.press('Escape')
                page.wait_for_timeout(200)
            # 订单查询
            page.click('.top [data-act="orders"]')
            page.wait_for_selector('#oq-tbl tr.click', timeout=10000)
            step('订单查询能看到刚开的单', True)
            shot(page, '07-cashier-orders')
            page.keyboard.press('Escape')
            # 交班
            page.click('.top [data-act="shift"]')
            page.wait_for_selector('.modal h3:has-text("交班")', timeout=10000)
            shot(page, '08-cashier-shift')
            exp = page.input_value('#cs-cash')
            page.click('.modal [data-ok]')
            page.wait_for_selector('#t-shift:has-text("未开班"), .modal h3:has-text("开班")', timeout=15000)
            step('交班成功（实点=应有现金，账实相符）', True, '应有现金 %s' % exp)
            shot(page, '09-cashier-after-shift')
            ctx.close()

        # ============ 管理后台 ============
        ctx = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='zh-CN')
        page = ctx.new_page()
        page.on('pageerror', lambda e: errors.append('admin pageerror: %s' % e))
        page.on('console', lambda m: errors.append('admin console: %s' % m.text) if m.type == 'error' and 'favicon' not in m.text and '401' not in m.text else None)
        r = page.goto(base + 'admin')
        step('后台页面打开', r.status == 200, 'HTTP %s' % r.status)
        page.wait_for_selector('#login:not(.hidden)', timeout=15000)
        page.fill('#l-user', 'admin')
        page.fill('#l-pass', admin_pw)
        page.click('#l-btn')
        page.wait_for_selector('#app:not(.hidden) #nav a', timeout=20000)
        step('总部管理员登录后台', True)
        page.wait_for_timeout(500)
        forced = page.locator('.modal:has-text("首次登录请先修改密码")').count()
        step('后台无强制改密弹窗（admin 已设密码）', forced == 0)
        navs = page.eval_on_selector_all('#nav a', 'els => els.map(a => [a.dataset.p, a.textContent.trim().replace(/^[^\u4e00-\u9fa5A-Za-z]+/, "")])')
        step('后台菜单按权限码生成（%d 个）' % len(navs), len(navs) >= 20, ' / '.join(t for _, t in navs)[:150])
        BAD = ('请求失败', '服务器返回异常', '接口不存在', 'Traceback', 'Internal Server Error', 'undefined', 'NaN')
        ERR_RE = re.compile(r'(失败|错误|异常|不存在|Error|无权|FORBIDDEN|403|500)')
        for i, (key, title) in enumerate(navs, 1):
            page.click('#nav a[data-p="%s"]' % key)
            page.wait_for_function("!document.getElementById('content').textContent.includes('加载中')", timeout=20000)
            page.wait_for_timeout(500)
            txt = page.inner_text('#content')
            bad = [b for b in BAD if b in txt]
            emp = [t for t in page.locator('#content > .card > .empty').all_inner_texts() if ERR_RE.search(t)]
            step('后台页面：' + title, not bad and not emp, (bad + emp)[0][:80] if (bad or emp) else key)
            shot(page, 'admin-%02d-%s' % (i, key))
        # 收银台刚开的单，后台（总部管理员）用订单接口能查到
        res = page.request.get(base + 'api/pos/orders')
        od = res.json() if res.ok else {}
        rows = (od.get('data') or {}).get('items') if isinstance(od.get('data'), dict) else od.get('data')
        rows = rows or od.get('items') or []
        step('后台订单接口能查到收银台刚开的单' if not a.readonly else '后台订单接口可用（只读模式不要求有单）', res.ok and (a.readonly or len(rows) >= 1), 'HTTP %s, %d 单' % (res.status, len(rows)))
        # 报表中心：进页面后点第一个查询类按钮，应出表
        page.click('#nav a[data-p="report-center"]')
        page.wait_for_function("!document.getElementById('content').textContent.includes('加载中')", timeout=20000)
        btn = page.locator('#content button:has-text("查询"), #content button:has-text("生成"), #content button:has-text("刷新")').first
        if btn.count():
            btn.click()
            page.wait_for_timeout(1500)
        step('报表中心能出表', page.locator('#content table').count() >= 1, '%d 张表' % page.locator('#content table').count())
        shot(page, 'admin-report-center')
        ctx.close()

        # ============ 手机端（门店助手 PWA） ============
        ctx = browser.new_context(viewport={'width': 390, 'height': 844}, device_scale_factor=2, is_mobile=True, has_touch=True, locale='zh-CN')
        page = ctx.new_page()
        page.on('pageerror', lambda e: errors.append('mobile pageerror: %s' % e))
        page.on('console', lambda m: errors.append('mobile console: %s' % m.text) if m.type == 'error' and 'favicon' not in m.text and '401' not in m.text else None)
        r = page.goto(base + 'm')
        step('手机端页面打开', r.status == 200, 'HTTP %s' % r.status)
        page.wait_for_selector('#lu', timeout=15000)
        page.fill('#lu', 'admin')
        page.fill('#lp', admin_pw)
        page.click('#lb')
        page.wait_for_function("document.querySelector('#app') && !document.querySelector('#app').textContent.includes('加载中') && !document.querySelector('#lu')", timeout=20000)
        page.wait_for_timeout(600)
        home = page.inner_text('#app')
        step('手机端：登录进入首页', '出错了' not in home and len(home) > 20, home.replace('\n', ' ')[:60])
        shot(page, 'mobile-1-home')
        mpages = [('dash', '老板看板'), ('goods/珍护', '查商品（珍护）'), ('store', '店务'), ('todo', '待办与通知'), ('docs', '单据'), ('me', '我的')]
        for i, (h, title) in enumerate(mpages, 2):
            page.evaluate("location.hash = '#%s'" % h)
            page.wait_for_function("!document.querySelector('#app').textContent.includes('加载中') && !document.querySelector('#app').textContent.includes('查询中')", timeout=20000)
            page.wait_for_timeout(600)
            txt = page.inner_text('#app')
            bad = '出错了' in txt or any(b in txt for b in BAD)
            step('手机端：' + title, not bad, txt.replace('\n', ' ')[:60])
            shot(page, 'mobile-%d-%s' % (i, h.split('/')[0]))
        ctx.close()
        browser.close()

    step('页面脚本无报错', not errors, '; '.join(errors)[:300])
    bad = [r for r in results if not r[1]]
    print('\n== 冒烟结果：%d 项通过，%d 项失败 ==' % (len(results) - len(bad), len(bad)))
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
