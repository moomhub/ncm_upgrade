# -*- coding: utf-8 -*-
"""验证码登录用的账号列表（青龙环境变量 -> 数据目录的 account.json）。

格式（data/account.json）：

    [
      {"phone": "<手机号>", "code": ""},
      {"phone": "<手机号>", "code": ""}
    ]

约定：
  * 优先读数据目录的 account.json；文件不存在时从环境变量
    `account.json` / `ACCOUNT_JSON` / `NCM_ACCOUNT` 导入一次（青龙里配环境变量即可），
    导入后写进 account.json —— 因为登录成功后要把 code 清掉，
    环境变量是只读的，只能靠数据文件记住「已经用掉」。
  * 拿不到 code 时返回空串，login 模块的 flow.wait_code 会每秒重试一次。
"""
import json
import os

from shared import common
from shared.common import log

ENV_KEYS = ('account.json', 'ACCOUNT_JSON', 'NCM_ACCOUNT')
DEFAULT_FIELDS = ('phone', 'code')


def path():
    """账号文件路径：数据目录下的 account.json。"""
    return os.path.join(common.DATA_DIR, 'account.json')


def _normalize(items):
    out = []
    for item in items or []:
        if isinstance(item, dict) and str(item.get('phone') or '').strip():
            entry = dict(item)
            entry['phone'] = str(entry.get('phone')).strip()
            entry['code'] = str(entry.get('code') or '').strip()
            out.append(entry)
    return out


def load_accounts():
    """读账号列表：本地 account.json 优先，没有就从环境变量导入一次。"""
    file_path = path()
    if os.path.exists(file_path):
        return _normalize(common.read_json(file_path, []))

    for key in ENV_KEYS:
        raw = os.environ.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            log('环境变量 {} 不是合法 JSON，已跳过'.format(key))
            continue
        items = _normalize(data)
        if items:
            save_accounts(items)
            log('已从环境变量 {} 导入 {} 个账号到 {}'.format(key, len(items), file_path))
            return items
    return []


def save_accounts(items):
    common.write_json(path(), _normalize(items))


def find_account(phone):
    phone = str(phone or '').strip()
    for item in load_accounts():
        if item['phone'] == phone:
            return item
    return None


def get_code(phone):
    item = find_account(phone)
    return (item or {}).get('code') or ''


def set_code(phone, code):
    items = load_accounts()
    for item in items:
        if item['phone'] == str(phone or '').strip():
            item['code'] = str(code or '')
            save_accounts(items)
            return True
    return False


def clear_code(phone):
    """登录成功后把 code 清掉，避免被重复使用。"""
    return set_code(phone, '')


def phones():
    return [item['phone'] for item in load_accounts()]
