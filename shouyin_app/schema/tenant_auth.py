# -*- coding: utf-8 -*-
"""tenant_auth：组织 / 角色权限 / 数据范围 / 会话 / 数据字典 / 打印模板 / 导出任务。Owner：模块④（多租户+权限+报表+系统配置）。

旧表 users / stores / settings / op_logs 也归模块④（v2 形态见 base.REBUILD）。
"""
MODULE = 'tenant_auth'
VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS org_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, parent_id INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('hq','region')),
    code TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT '启用', sort INTEGER DEFAULT 0, created_at TEXT,
    UNIQUE (merchant_id, code));

CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL,
    code TEXT NOT NULL, name TEXT NOT NULL,
    data_scope TEXT NOT NULL DEFAULT 'store' CHECK (data_scope IN ('platform','merchant','region','store','self')),
    legacy_role TEXT NOT NULL DEFAULT 'staff',
    is_system INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT '启用', note TEXT DEFAULT '',
    created_at TEXT, updated_at TEXT,
    UNIQUE (merchant_id, code));
CREATE TABLE IF NOT EXISTS role_perms (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE, perm TEXT NOT NULL,
    PRIMARY KEY (role_id, perm));
CREATE TABLE IF NOT EXISTS role_limits (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE, limit_key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (role_id, limit_key));

CREATE TABLE IF NOT EXISTS user_scopes (
    user_id INTEGER NOT NULL, scope_type TEXT NOT NULL CHECK (scope_type IN ('store','region')), ref_code TEXT NOT NULL,
    PRIMARY KEY (user_id, scope_type, ref_code));

CREATE TABLE IF NOT EXISTS user_sessions (
    sid TEXT PRIMARY KEY, merchant_id INTEGER NOT NULL, user_id INTEGER NOT NULL, client TEXT NOT NULL DEFAULT 'web',
    device TEXT DEFAULT '', ip TEXT DEFAULT '', created_at TEXT NOT NULL, last_seen TEXT, revoked_at TEXT, revoke_reason TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id, revoked_at);

CREATE TABLE IF NOT EXISTS dict_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL,
    dict_type TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL, sort INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT '启用',
    UNIQUE (merchant_id, dict_type, code));

CREATE TABLE IF NOT EXISTS print_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, store_code TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'sale' CHECK (kind IN ('sale','refund','shift','label')),
    width INTEGER NOT NULL DEFAULT 58, copies INTEGER NOT NULL DEFAULT 1, content TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '启用', updated_at TEXT,
    UNIQUE (merchant_id, store_code, kind));

CREATE TABLE IF NOT EXISTS export_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    kind TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','expired')),
    row_count INTEGER DEFAULT 0, file_path TEXT DEFAULT '', error TEXT DEFAULT '', created_at TEXT NOT NULL, finished_at TEXT, expire_at TEXT);
CREATE INDEX IF NOT EXISTS idx_export_user ON export_jobs(merchant_id, user_id, created_at);
"""

DEFAULT_DICTS = {
    'refund_reason': ['质量问题', '顾客不想要', '买错规格', '临期/过期', '其他'],
    'loss_reason': ['破损', '过期', '丢失', '召回', '其他'],
    'cash_reason': ['零钱兑换', '门店杂费', '交款', '其他'],
    'unit': ['罐', '听', '包', '片', '盒', '箱', '提', '件', '个', '瓶', '袋'],
}


def migrate(db, from_version):
    from ..perms import ensure_default_roles
    mids = [0] + [r[0] for r in db.execute('SELECT id FROM merchants ORDER BY id')]
    for mid in mids:
        ensure_default_roles(db, mid)
    # 旧粗角色 -> 默认角色
    legacy_map = {'admin': 'merchant_admin', 'manager': 'store_manager', 'cashier': 'cashier'}
    for legacy, code in legacy_map.items():
        db.execute('UPDATE users SET role_id=(SELECT r.id FROM roles r WHERE r.merchant_id=users.merchant_id AND r.code=?) '
                   'WHERE role_id IS NULL AND role=?', (code, legacy))
    for mid in mids[1:]:
        for t, names in DEFAULT_DICTS.items():
            for i, n in enumerate(names):
                db.execute('INSERT OR IGNORE INTO dict_items(merchant_id,dict_type,code,name,sort) VALUES(?,?,?,?,?)',
                           (mid, t, 'D%02d' % (i + 1), n, i))
