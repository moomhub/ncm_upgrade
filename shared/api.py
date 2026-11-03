# -*- coding: utf-8 -*-
"""HTTP 层 + 登录态文件 + 网易云业务接口。

一个账号一个 Client（各自独立的 cookie 与 requests.Session），
所以多账号可以并行跑，互不干扰。

约定：
  * 业务接口统一走 Client.call()：非 200 先尝试 /login/refresh 刷新一次；
    扫码登录的 cookie 不支持刷新，直接抛 AuthError 让上层通知用户重新扫码。
  * 登录态存在数据目录的 ncm_cookie.json（数组）：
    [{"userId","nickname","type","enable","cookie":{...}}]
"""
import json
import random
import threading

import requests

from shared import common
from shared.common import CFG, ApiError, AuthError, NetworkError, log

try:                      # 可选依赖：没有也能跑，只是不生成图片二维码
    import qrcode
except ImportError:
    qrcode = None

UA = 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'

# Set-Cookie 里这些是属性，不是 cookie
_COOKIE_ATTRS = {
    'path', 'domain', 'expires', 'max-age', 'httponly', 'secure',
    'samesite', 'version', 'comment', 'priority',
}


# ---------------------------- cookie 解析 ----------------------------
def parse_cookie_blob(text):
    """把 'a=1; b=2' 或 Set-Cookie 风格（带 Path/Expires/;;）的串解析成 dict。"""
    out = {}
    for chunk in str(text or '').replace(';;', ';').split(';'):
        chunk = chunk.strip()
        if not chunk or '=' not in chunk:
            continue
        key, value = chunk.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"')
        if not key or key.lower() in _COOKIE_ATTRS:
            continue
        if not value:
            continue
        out[key] = value
    return out


def header_of(cookies):
    return '; '.join('{}={}'.format(k, v) for k, v in (cookies or {}).items())


# ---------------------------- 登录态文件（数组） ----------------------------
def _normalize_entry(item):
    if not isinstance(item, dict):
        return None
    cookie = item.get('cookie')
    if not isinstance(cookie, dict):
        # 兼容旧格式：整个文件就是一个 cookie dict
        cookie = {k: v for k, v in item.items()
                  if k not in ('userId', 'nickname', 'type')}
    enable = item.get('enable', True)
    if isinstance(enable, str):
        enable = enable.strip().lower() not in ('0', 'false', 'no', 'off', '')
    return {
        'userId': str(item.get('userId') or ''),
        'nickname': str(item.get('nickname') or ''),
        'type': str(item.get('type') or 'qrcode'),
        'enable': bool(enable),
        'cookie': dict(cookie),
    }


_cookie_lock = threading.Lock()


def _normalized(entries):
    """批量归一化，顺带丢掉无效项。"""
    out = []
    for item in entries or []:
        entry = _normalize_entry(item)
        if entry:
            out.append(entry)
    return out


def load_cookies():
    """读数据目录的 ncm_cookie.json；兼容旧版「单个 cookie dict」格式。"""
    raw = common.read_json(common.COOKIE_FILE, None) or []
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    for item in _normalized(raw):
        if item['cookie']:
            out.append(item)
    return out


def save_cookies(entries):
    common.write_json(common.COOKIE_FILE, _normalized(entries))


def upsert_cookie(entry):
    """按 userId 覆盖或追加一条登录态；重新登录时保留用户设置的 enable。"""
    raw = dict(entry or {})
    entry = _normalize_entry(raw)
    if not entry:
        return
    with _cookie_lock:
        items = load_cookies()
        for index, item in enumerate(items):
            if item['userId'] and item['userId'] == entry['userId']:
                if 'enable' not in raw:      # 重新登录不该把 enable=false 改回 true
                    entry['enable'] = item.get('enable', True)
                items[index] = entry
                break
        else:
            items.append(entry)
        save_cookies(items)


def remove_cookie(user_id):
    with _cookie_lock:
        save_cookies([e for e in load_cookies() if e['userId'] != str(user_id)])


def get_cookie_entry(user_id):
    for item in load_cookies():
        if item['userId'] == str(user_id):
            return item
    return None


# ---------------------------- 客户端 ----------------------------
class Client(object):
    """一个账号的 HTTP 客户端：独立 cookie + 独立 session。"""

    def __init__(self, api_url=None, cookie=None, user_id='', login_type='', timeout=None):
        self.api = str(api_url or CFG.api).rstrip('/')
        self.cookies = dict(cookie or {})
        self.user_id = str(user_id or '')
        self.login_type = str(login_type or 'qrcode')
        self.timeout = timeout or CFG.request_timeout
        self.session = requests.Session()
        self.refreshed = False

    # ---------- 基础 ----------
    def _url(self, path):
        return self.api + path

    def cookie_header(self):
        return header_of(self.cookies)

    def merge_cookie_string(self, text):
        parsed = parse_cookie_blob(text)
        if parsed:
            self.cookies.update(parsed)
        return parsed

    def merge_cookies(self, cookies):
        self.cookies.update({k: v for k, v in (cookies or {}).items() if v})

    def http(self, method, path, params=None, data=None, json_body=None, timeout=None):
        """发一次请求并返回 JSON（不做业务码判断）。

        data      → application/x-www-form-urlencoded
        json_body → application/json（PR 的 /api/yidun/* 要求 JSON）
        """
        url = self._url(path)
        headers = {'User-Agent': UA, 'Referer': 'https://music.163.com/'}
        if self.cookies:
            headers['Cookie'] = self.cookie_header()
        payload = data
        if json_body is not None:
            headers['Content-Type'] = 'application/json'
            payload = json.dumps(json_body)
        elif data is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            resp = self.session.request(method, url, params=params, data=payload,
                                        headers=headers, timeout=timeout or self.timeout)
        except requests.exceptions.RequestException as e:
            raise NetworkError('{} {} 失败: {}'.format(method, url, e))
        for cookie in resp.cookies:
            if cookie.value:
                self.cookies[cookie.name] = cookie.value
        try:
            return resp.json()
        except ValueError:
            raise ApiError('{} 返回非 JSON（HTTP {}）：{}'.format(
                path, resp.status_code, (resp.text or '')[:200]))

    # ---------- 刷新 ----------
    def refresh_cookie(self):
        """用现有 cookie 调 /login/refresh；成功则合并并回写数据目录的 ncm_cookie.json。

        扫码登录的 cookie 不支持刷新（官方限制），直接返回 False。
        """
        if self.login_type == 'qrcode':
            log('[{}] 扫码登录的 cookie 不支持刷新，需要重新扫码'.format(self.user_id or '-'))
            return False
        js = self.http('GET', '/login/refresh', params={'cookie': self.cookie_header()})
        if js.get('code') == 200 and js.get('cookie'):
            self.merge_cookie_string(js['cookie'])
            self.refreshed = True
            entry = get_cookie_entry(self.user_id) or {"userId": self.user_id,
                                                       "nickname": "", "type": self.login_type}
            entry['cookie'] = dict(self.cookies)
            upsert_cookie(entry)
            log('[{}] 已刷新登录态并写回 {}'.format(self.user_id or '-', common.COOKIE_FILE))
            return True
        log('[{}] 刷新登录态失败：code={} {}'.format(
            self.user_id or '-', js.get('code'), js.get('msg') or js.get('message') or ''))
        return False

    def call(self, method, path, params=None, data=None, auth_retry=True):
        """业务接口：code 必须是 200；登录态失效时按登录方式处理。"""
        js = self.http(method, path, params=params, data=data)
        code = js.get('code')
        if code == 200 or code is None:
            return js
        msg = js.get('msg') or js.get('message') or ''
        if code in (301, 401, 403, 8801):
            if auth_retry and self.refresh_cookie():
                return self.call(method, path, params=params, data=data, auth_retry=False)
            raise AuthError('[{}] 登录态失效（code={} {}）'.format(self.user_id or '-', code, msg))
        raise ApiError('{} 返回 code={} {}'.format(path, code, msg))

    # ---------- 登录信息 ----------
    def user_account(self):
        """GET /user/account；未登录返回 None。"""
        js = self.http('GET', '/user/account')
        if js.get('code') not in (200, None):
            raise ApiError('/user/account 返回 code={}'.format(js.get('code')))
        account = js.get('account')
        if not account:
            return None
        profile = js.get('profile') or {}
        return {
            'userId': str(profile.get('userId') or account.get('id') or ''),
            'nickname': profile.get('nickname') or '',
            'raw': js,
        }

    def has_yidun(self):
        """服务是否打了 PR #294（有 /api/yidun/*）。"""
        try:
            js = self.http('GET', '/api/yidun/state', params={'sid': 'probe'})
            return js.get('code') == 200
        except (ApiError, NetworkError):
            return False

    # ---------- 业务接口 ----------
    def daily_check(self):
        js = self.call('POST', '/daily_signin', data={'r': random.random()})
        log('[{}] 每日签到：成功'.format(self.user_id or '-'))
        return js

    def yunbei_sign(self):
        js = self.call('POST', '/yunbei/sign', data={'r': random.random()})
        log('[{}] 云贝签到：成功'.format(self.user_id or '-'))
        return js

    def user_detail(self, uid=None):
        js = self.call('GET', '/user/detail', params={'uid': uid or self.user_id})
        level = js.get('level')
        listen = js.get('listenSongs')
        if level is None or listen is None:
            raise ApiError('/user/detail 返回缺少 level/listenSongs：{}'.format(str(js)[:200]))
        return int(level), int(listen)

    def fetch_playlist_name(self, pid):
        """歌单名只用于日志，取不到不影响播放；断网必须上抛。"""
        try:
            js = self.http('GET', '/playlist/detail', params={'id': pid})
            return ((js.get('playlist') or {}).get('name') or '')
        except NetworkError:
            raise
        except Exception as e:
            log('歌单 {} 名称获取失败（{}），继续播放'.format(pid, e))
            return ''

    def fetch_tracks(self, pid):
        """分页取歌单全部歌曲，避免只取到前 300 首。"""
        songs, offset, limit = [], 0, 1000
        while True:
            js = self.call('GET', '/playlist/track/all',
                           params={'id': pid, 'limit': limit, 'offset': offset})
            batch = js.get('songs') or []
            songs.extend(batch)
            if len(batch) < limit or offset >= 5000:
                break
            offset += limit
        return songs

    def scrobble(self, song, seconds):
        """听歌打卡：按 /scrobble/v1 文档把能填的参数都填上。"""
        return self.call('GET', CFG.scrobble_path,
                         params=scrobble_params(song, seconds))


def _quality_of(song):
    """从歌曲的 h/m/l/sq/hr 里推 bitrate(kbps) 与 level，供打卡接口上报。"""
    for key, level in (('sq', 'lossless'), ('hr', 'lossless'),
                       ('h', 'exhigh'), ('m', 'higher'), ('l', 'standard')):
        info = song.get(key)
        if isinstance(info, dict) and info.get('br'):
            return level, max(1, int(round(float(info['br']) / 1000.0)))
    return 'exhigh', 320


def scrobble_params(song, seconds):
    """组装 /scrobble/v1 的参数。

    必选：id、time；可选：sourceid / source / name / artist / bitrate / level / total / vip。
    文档：https://neteasecloudmusicapienhanced.vercel.app/#/docs?id=听歌打卡-v2-ncbl-加密版
    """
    artists = song.get('ar') or song.get('artists') or []
    names = [str(a.get('name') or '').strip() for a in artists
             if isinstance(a, dict) and a.get('name')]
    level, bitrate = _quality_of(song)
    total = int(round(float(song.get('dt') or 0) / 1000.0)) or int(seconds)
    params = {
        'id': song.get('id'),
        'sourceid': (song.get('al') or {}).get('id') or '',
        'time': int(seconds),
        'total': total,
        'name': song.get('name') or '',
        'artist': '/'.join(names),
        'bitrate': bitrate,
        'level': level,
        'source': 'list',
    }
    if str(song.get('fee') if song.get('fee') is not None else '0') == '1':
        params['vip'] = 'true'
    return params


def safe_call(name, fn, prefix=''):
    try:
        return fn()
    except NetworkError:
        raise
    except ApiError as e:
        log('{}【{}】失败：{}（继续执行）'.format(prefix, name, e))
        return None
