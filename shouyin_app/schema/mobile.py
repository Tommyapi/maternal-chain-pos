# -*- coding: utf-8 -*-
"""mobile：公告 / 已读确认 / 提醒规则 / H5 版本。Owner：模块⑤。
消息与待办表 notifications 在 base（所有模块都会写），读/处理接口归⑤（shouyin_app/notify.py）。
"""
MODULE = 'mobile'
VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'normal' CHECK (level IN ('normal','important')), need_confirm INTEGER NOT NULL DEFAULT 0,
    target_stores TEXT NOT NULL DEFAULT '', target_roles TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'published' CHECK (status IN ('draft','published','withdrawn')),
    created_by TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS announcement_reads (
    announcement_id INTEGER NOT NULL, user_id INTEGER NOT NULL, read_at TEXT NOT NULL, confirmed_at TEXT,
    PRIMARY KEY (announcement_id, user_id));

-- 提醒规则（APP-302）：按角色/门店/类别/级别决定谁收
CREATE TABLE IF NOT EXISTS notify_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, category TEXT NOT NULL,
    role_code TEXT NOT NULL DEFAULT '', store_code TEXT NOT NULL DEFAULT '', min_level TEXT NOT NULL DEFAULT 'normal',
    enabled INTEGER NOT NULL DEFAULT 1, UNIQUE (merchant_id, category, role_code, store_code));

CREATE TABLE IF NOT EXISTS app_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT NOT NULL CHECK (client IN ('m','pos','admin')), version TEXT NOT NULL,
    min_version TEXT NOT NULL DEFAULT '', force INTEGER NOT NULL DEFAULT 0, notes TEXT DEFAULT '', created_at TEXT NOT NULL,
    UNIQUE (client, version));
"""


def migrate(db, from_version):
    return None
