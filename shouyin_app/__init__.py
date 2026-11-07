# -*- coding: utf-8 -*-
"""萌贝母婴连锁 · 独立收银系统（收银台 / 管理后台 / 手机端）"""
import os
import secrets
from datetime import timedelta

from flask import Flask, jsonify, request, session, send_from_directory, redirect

from .db import init_db
from .core import (close_db, get_db, current_actor, api, verify_user, log_op, client_ip, ROLE_NAME, store_row, start_session,
                   merchant_row, scope_stores, revoke_user_sessions, tx)
from .util import BizError

__version__ = '2.0.0-dev'

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, 'static')


def _secret_key(db_path):
    env = os.environ.get('SHOUYIN_SECRET')
    if env:
        return env
    path = os.path.join(os.path.dirname(os.path.abspath(db_path)), 'secret.key')
    if os.path.exists(path):
        with open(path, 'r') as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return key


def create_app(config=None):
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='/static')
    db_path = (config or {}).get('DB_PATH') or os.environ.get('SHOUYIN_DB') or os.path.join(os.getcwd(), 'data', 'shouyin.db')
    app.config.update(
        DB_PATH=db_path,
        SECRET_KEY=(config or {}).get('SECRET_KEY') or _secret_key(db_path),
        SESSION_COOKIE_NAME='shouyin_sid',          # 与同域名下其他系统的 cookie 区分开
        SESSION_COOKIE_PATH=os.environ.get('SHOUYIN_COOKIE_PATH', '/'),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('SHOUYIN_COOKIE_SECURE', '') == '1',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
        JSON_AS_ASCII=False,
    )
    if config:
        app.config.update(config)
    app.json.ensure_ascii = False
    init_db(app.config['DB_PATH'])
    app.teardown_appcontext(close_db)

    # 蓝图：每个文件归属一个模块 agent（docs/ARCH.md §2 文件归属表）
    from . import (views_pos, views_m, views_goods, views_stock, views_purchase, views_member, views_promo,
                   views_tenant, views_platform, views_report, views_sys, approval, notify)
    for mod in (views_pos, views_m, views_goods, views_stock, views_purchase, views_member, views_promo,
                views_tenant, views_platform, views_report, views_sys, approval, notify):
        app.register_blueprint(mod.bp)
    # 模块在 import 时可能 register_perm() 了新权限码：补进各商户的系统角色（幂等）
    from .db import connect
    from .perms import ensure_default_roles
    _db = connect(app.config['DB_PATH'])
    try:
        _db.execute('BEGIN IMMEDIATE')
        for (mid,) in _db.execute('SELECT 0 UNION SELECT id FROM merchants').fetchall():
            ensure_default_roles(_db, mid)
        _db.execute('COMMIT')
    finally:
        _db.close()

    # ---------- 页面（经 nginx 反代 /shouyin/ 后，前端一律用相对路径） ----------
    def page(name):
        resp = send_from_directory(STATIC_DIR, name)
        resp.headers['Cache-Control'] = 'no-cache'
        return resp

    @app.route('/')
    def cashier_page():
        return page('cashier.html')

    @app.route('/admin')
    def admin_page():
        return page('admin.html')

    @app.route('/m')
    def mobile_page():
        return page('m.html')

    @app.route('/admin/')
    def admin_slash():
        return redirect('../admin')

    @app.route('/m/')
    def mobile_slash():
        return redirect('../m')

    @app.route('/platform')
    def platform_page():
        return page('platform.html')

    @app.route('/healthz')
    def healthz():
        get_db().execute('SELECT 1').fetchone()
        return jsonify({'ok': True, 'version': __version__})

    # ---------- 登录 ----------
    @app.route('/api/login', methods=['POST'])
    def login():
        db = get_db()
        data = request.get_json(silent=True) or {}
        try:
            u = verify_user(db, data.get('username'), data.get('password'), client_ip())
        except BizError as e:
            return jsonify({'ok': False, 'code': e.code, 'msg': e.msg, 'error': e.msg}), e.status
        if int(u['merchant_id']) != 0:
            m = merchant_row(db, u['merchant_id'])
            if m is None or m['status'] == 'disabled':
                return jsonify({'ok': False, 'code': 'MERCHANT_DISABLED', 'msg': '商户已停用，请联系平台', 'error': '商户已停用，请联系平台'}), 403
        start_session(db, u, (data.get('client') or 'web'), request.headers.get('User-Agent', ''))
        db.execute('UPDATE users SET last_login=? WHERE id=?', (__import__('time').strftime('%Y-%m-%d %H:%M:%S'), u['id']))
        g_actor = current_actor(db)
        log_op('登录', '账号', u['username'], user=u['username'])
        return jsonify({'ok': True, 'user': u['username'], 'name': u['name'], 'role': g_actor['role'],
                        'data': {'username': u['username'], 'name': u['name'], 'merchant_id': g_actor['merchant_id'],
                                 'role_code': g_actor['role_code'], 'is_platform': g_actor['is_platform'],
                                 'must_change_pw': bool(u['must_change_pw'] if 'must_change_pw' in u.keys() else 0)}})

    @app.route('/api/logout', methods=['POST'])
    def logout():
        sid = session.get('sid')
        if sid:
            db = get_db()
            db.execute("UPDATE user_sessions SET revoked_at=datetime('now','localtime'), revoke_reason='退出' WHERE sid=? AND revoked_at IS NULL", (sid,))
        session.clear()
        return jsonify({'ok': True})

    @app.route('/api/me')
    def me():
        db = get_db()
        a = current_actor(db)
        if a is None:
            return jsonify({'ok': False, 'code': 'UNAUTHORIZED', 'msg': '未登录或会话已过期', 'error': '未登录或会话已过期'}), 401
        st = store_row(db, a['store_code'], a['merchant_id']) if a['store_code'] else None
        m = merchant_row(db, a['merchant_id']) if not a['is_platform'] else None
        info = {'username': a['username'], 'name': a['name'], 'role': a['role'], 'role_name': ROLE_NAME.get(a['role'], a['role_name']),
                'role_code': a['role_code'], 'merchant_id': a['merchant_id'], 'merchant_name': m['name'] if m else '平台运营',
                'merchant_status': m['status'] if m else '', 'is_platform': a['is_platform'], 'data_scope': a['data_scope'],
                'perms': sorted(a['perms']), 'stores': [] if a['is_platform'] else scope_stores(db, a),
                'store_code': a['store_code'], 'store_name': st['name'] if st else ''}
        body = dict(info)
        body.update({'ok': True, 'data': info})
        return jsonify(body)

    @app.route('/api/password', methods=['POST'])
    @api()
    def change_password():
        from werkzeug.security import generate_password_hash, check_password_hash
        db = get_db()
        a = current_actor()
        data = request.get_json(silent=True) or {}
        u = db.execute('SELECT * FROM users WHERE id=?', (a['id'],)).fetchone()
        if not check_password_hash(u['password_hash'], data.get('old') or ''):
            raise BizError('原密码不正确')
        new = data.get('new') or ''
        if len(new) < 8:
            raise BizError('新密码至少 8 位')
        db.execute('UPDATE users SET password_hash=?, must_change_pw=0 WHERE id=?', (generate_password_hash(new), a['id']))
        revoke_user_sessions(db, a['id'], '修改密码')         # 其他终端全部下线
        start_session(db, db.execute('SELECT * FROM users WHERE id=?', (a['id'],)).fetchone(), 'web')
        log_op('修改密码', '账号', a['username'])
        return jsonify({'ok': True})

    @app.after_request
    def headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        resp.headers.setdefault('Referrer-Policy', 'same-origin')
        if request.path.startswith('/api/'):
            resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app.errorhandler(404)
    def e404(e):
        if request.path.startswith('/api/'):
            return jsonify({'ok': False, 'code': 'NOT_FOUND', 'msg': '接口不存在', 'error': '接口不存在'}), 404
        return '页面不存在', 404

    @app.errorhandler(405)
    def e405(e):
        return jsonify({'ok': False, 'code': 'METHOD_NOT_ALLOWED', 'msg': '请求方式不对', 'error': '请求方式不对'}), 405

    @app.errorhandler(413)
    def e413(e):
        return jsonify({'ok': False, 'code': 'TOO_LARGE', 'msg': '上传文件太大（最大 20MB）', 'error': '上传文件太大（最大 20MB）'}), 413

    @app.errorhandler(Exception)
    def e500(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        app.logger.exception('unhandled error')
        try:
            db = get_db()
            if db.in_transaction:
                db.execute('ROLLBACK')
        except Exception:
            pass
        return jsonify({'ok': False, 'code': 'SERVER_ERROR', 'msg': '系统繁忙，请稍后重试', 'error': '系统繁忙，请稍后重试'}), 500

    return app
