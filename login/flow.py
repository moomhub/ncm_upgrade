#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录流程：扫码（qrcode）或手机验证码（code）。登录态写入数据目录的 ncm_cookie.json。

命令行参数解析在 login/cli.py，入口（青龙里就是 task login.py）：
    python login.py --type=qrcode
    python login.py --type=code --account=13800000000
    python login.py                      # 默认 qrcode
    python login.py --type=code --account=xx --api http://127.0.0.1:3000

流程：
  qrcode  生成二维码 -> 每秒轮询 /login/qr/check -> 803 成功后合并 cookie
  code    从数据目录的 account.json（或青龙环境变量）读 phone -> 发码 -> 每秒读 code -> 登录 -> 清空 code

两者完成后都会带 cookie 调一次 /user/account，取出 profile.userId / profile.nickname，
再写进数据目录的 ncm_cookie.json。
"""
import time
import uuid

from shared import api, common
from shared.common import CFG, ApiError, ConfigError, LoginTimeout, log
from login import accounts

try:                      # 可选依赖
    import qrcode
except ImportError:
    qrcode = None


# ---------------------------- 通用 ----------------------------
def _sleep(seconds):
    time.sleep(max(0.2, float(seconds or 1)))


def render_qr(qrurl):
    log('二维码链接（可直接复制到浏览器/手机打开）：{}'.format(qrurl))
    if qrcode is None:
        log('未安装 qrcode/pillow，跳过图片与终端二维码（uv sync --extra qr）')
        return
    try:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=8, border=2)
        qr.add_data(qrurl)
        qr.make(fit=True)
        qr.make_image().save(common.QR_FILE)
        qr.print_ascii(invert=True)
        log('二维码已保存：{}'.format(common.QR_FILE))
    except Exception as e:
        log('生成二维码图片失败（{}），请直接打开上面的链接'.format(e))


def save_login(client, profile, login_type):
    """把登录态写进数据目录的 ncm_cookie.json（按 userId 覆盖）。"""
    entry = {
        'userId': str(profile['userId']),
        'nickname': profile.get('nickname') or '',
        'type': login_type,
        'enable': True,           # 新账号默认启用；重新登录会保留原来的开关
        'cookie': dict(client.cookies),
    }
    api.upsert_cookie(entry)
    log('登录成功：{}（userId={}，cookie {} 个字段）已写入 {}'.format(
        profile.get('nickname') or '-', profile['userId'],
        len(entry['cookie']), common.COOKIE_FILE))
    return entry


def fetch_profile(client):
    """带 cookie 调 /user/account，取 userId / nickname。"""
    account = client.user_account()
    if not account:
        raise ApiError('/user/account 返回 account=null，登录态可能没生效')
    return account


# ---------------------------- 二维码 ----------------------------
def qrcode_login(client, timeout=None):
    """扫码登录；成功后返回 profile。"""
    timeout = timeout or CFG.qr_timeout
    poll = max(0.2, float(CFG.qr_poll_interval))
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        key_js = client.http('GET', '/login/qr/key')
        key = ((key_js or {}).get('data') or {}).get('unikey')
        if not key:
            raise ApiError('获取二维码 key 失败：{}'.format(str(key_js)[:200]))

        qr_js = client.http('GET', '/login/qr/create', params={'key': key, 'qrimg': 'true'})
        qrurl = ((qr_js or {}).get('data') or {}).get('qrurl')
        if not qrurl:
            raise ApiError('获取二维码内容失败：{}'.format(str(qr_js)[:200]))

        render_qr(qrurl)
        log('第 {} 次生成二维码，请在 {} 秒内用网易云 App 扫码'.format(
            attempt, int(max(1, deadline - time.time()))))

        regenerate = False
        scanned = False
        while time.time() < deadline:
            res = client.http('GET', '/login/qr/check', params={'key': key})
            code = (res or {}).get('code')
            if code == 803:                       # 授权登录成功
                client.merge_cookie_string((res or {}).get('cookie'))
                log('扫码登录成功')
                return fetch_profile(client)
            if code == 802:                       # 已扫码待确认
                if not scanned:
                    log('已扫码，等待手机端确认…')
                    scanned = True
                _sleep(poll)
            elif code == 801:                     # 等待扫码
                _sleep(poll)                      # 每秒一次
            elif code == 800:                     # 二维码过期
                log('二维码已过期，重新生成')
                regenerate = True
                break
            else:
                raise ApiError('二维码登录返回异常：{}'.format(str(res)[:200]))
        if not regenerate:
            break
    raise LoginTimeout('二维码登录超时：{} 秒内未完成扫码'.format(timeout))


# ---------------------------- 验证码 ----------------------------
def send_code(client, phone, ctcode='86', use_yidun=True):
    """发验证码：默认走 PR #294 的 /api/yidun/send（服务端维持会话 jar）。"""
    sid = 'login-' + uuid.uuid4().hex
    if use_yidun:
        js = client.http('POST', '/api/yidun/send',
                         json_body={'phone': phone, 'sid': sid, 'ctcode': ctcode})
        return sid, js
    js = client.http('GET', '/captcha/sent/v1', params={'phone': phone, 'ctcode': ctcode})
    if js.get('code') != 200:
        js2 = client.http('GET', '/captcha/sent', params={'phone': phone, 'ctcode': ctcode})
        if js2.get('code') == 200:
            return sid, js2
    return sid, js


def wait_code(phone, timeout=None):
    """每秒读一次 account.json，等用户把 code 填进去。"""
    timeout = timeout or CFG.code_timeout
    poll = max(0.2, float(CFG.code_poll_interval))
    deadline = time.time() + timeout
    log('已发送验证码，等待 {} 填入 {}（最多 {} 秒）'.format(phone, accounts.path(), timeout))
    last_hint = 0
    while time.time() < deadline:
        code = accounts.get_code(phone)
        if code:
            return code
        if time.time() - last_hint >= 30:
            last_hint = time.time()
            log('仍在等待验证码…（剩余 {} 秒）'.format(int(deadline - time.time())))
        _sleep(poll)
    raise LoginTimeout('等待验证码超时：{} 秒内 {} 的 code 一直是空的'.format(timeout, phone))


def code_login(client, phone, timeout=None, sca='', legacy=False):
    """验证码登录；成功后清空该手机号的 code 并返回 profile。

    默认用 PR #294 的 /api/yidun/*（提前就位，未合并的服务会明确报错）；
    --legacy 时退回仓库原有的 /captcha/sent + /login/cellphone。
    """
    phone = str(phone or '').strip()
    if not phone:
        raise ApiError('缺少手机号')

    has_yidun = False if legacy else client.has_yidun()
    if not legacy and not has_yidun:
        raise ConfigError(
            '目标服务没有 /api/yidun/*（PR #294 还没合并/打补丁），验证码登录需要它。\n'
            '  方案一：先把 PR 合进你的 api 服务；\n'
            '  方案二：加 --legacy 退回旧接口（受限：无服务端会话，风控更容易触发）')
    log('验证码登录接口：{}'.format('/api/yidun/*（PR #294）' if has_yidun else '/login/cellphone（legacy）'))

    sid, send_js = send_code(client, phone, use_yidun=has_yidun)
    code = (send_js or {}).get('code')
    if code == 406:
        raise ApiError('发码太频繁（406），等 60 秒再试')
    if code == 416:
        raise ApiError('发码间隔过短（416），稍后再试')
    if code not in (200, None):
        raise ApiError('发码失败：code={} {}'.format(code, (send_js or {}).get('msg') or ''))

    captcha = wait_code(phone, timeout)

    if has_yidun:
        payload = {'phone': phone, 'captcha': captcha, 'sid': sid}
        if sca:
            payload['sca'] = sca
        js = client.http('POST', '/api/yidun/login', json_body=payload)
        client.merge_cookie_string(js.get('cookie') or '')
        client.merge_cookie_string(js.get('loginCookie') or '')
    else:
        params = {'phone': phone, 'captcha': captcha, 'countrycode': '86'}
        if sca:
            params['sca'] = sca
        js = client.http('GET', '/login/cellphone', params=params)
        client.merge_cookie_string(js.get('cookie') or '')

    login_code = (js or {}).get('code')
    if login_code == 200:
        accounts.clear_code(phone)
        log('验证码登录成功，已清空 {} 的 code'.format(phone))
        return fetch_profile(client)

    body = (js or {}).get('body') or {}
    hint = {
        503: '验证码错误或已过期，重新发码',
        8821: '需要行为验证（滑块），去网页版拖一次滑块再试',
        8830: '需要二阶段验证，换扫码登录更快',
        8810: '当前网络环境被风控，换网络或等冷却',
        10004: '设备环境风控，先停手，去官方 App 确认身份',
        502: '账号或密码错误（验证码登录不该出现）',
        509: '错误次数过多被锁，等冷却',
    }.get(login_code, (js or {}).get('msg') or '')
    raise ApiError('登录失败：code={} {}{}'.format(
        login_code, hint,
        '（captchaId={}）'.format(body.get('captchaId')) if body.get('captchaId') else ''))


# ---------------------------- 入口 ----------------------------
def run(login_type=None, account=None, api_url=None, timeout=None, sca='', legacy=False):
    login_type = (login_type or 'qrcode').strip().lower()
    if login_type not in ('qrcode', 'code'):
        raise ConfigError('type 只能是 qrcode 或 code，当前：{}'.format(login_type))

    client = api.Client(api_url=api_url, login_type=login_type)
    log('登录方式：{}｜api：{}'.format('扫码' if login_type == 'qrcode' else '验证码', client.api))

    if login_type == 'qrcode':
        profile = qrcode_login(client, timeout=timeout)
    else:
        phone = str(account or '').strip()
        if not phone:
            if not accounts.load_accounts():
                raise ConfigError('没有可用账号：请先配置环境变量 account.json 或本地 {}'.format(
                    accounts.path()))
            phone = input('手机号: ').strip()
        profile = code_login(client, phone, timeout=timeout, sca=sca, legacy=legacy)

    return save_login(client, profile, login_type)

