# -*- coding: utf-8 -*-
"""自检：不联网，用假数据验证配额 / 断点 / 多账号 / 登录态解析 / 每日一次 / 锁 / 时间预算。

运行方式：python run.py --selftest（或 python -m run --selftest）
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date

from shared import api, common
from login import accounts
from run import main as app
from run import player, state


class FakeClient(object):
    """替身：只实现 player / main 用到的那几个方法。"""

    def __init__(self, user_id='u1', tracks=None, nickname=None,
                 can_refresh=False, fail_network=False, auth_fail=False,
                 calls=None):
        self.user_id = user_id
        self.nickname = nickname or ('nick-' + user_id)
        self.tracks = tracks or {}
        self.played = []
        self.calls = calls if calls is not None else []
        self.can_refresh = can_refresh
        self.fail_network = fail_network
        self.auth_fail = auth_fail
        self.refreshed = False
        self.cookies = {'MUSIC_U': 'fake-' + user_id}
        self.session = type('S', (), {'close': lambda self: None})()

    def user_account(self):
        if self.auth_fail:
            return None
        return {'userId': self.user_id, 'nickname': self.nickname}

    def refresh_cookie(self):
        self.refreshed = True
        return self.can_refresh

    def user_detail(self, uid=None):
        return (5, 12345)

    def daily_check(self):
        self.calls.append('signin')
        return {'code': 200}

    def yunbei_sign(self):
        self.calls.append('yunbei')
        return {'code': 200}

    def fetch_playlist_name(self, pid):
        return 'PL-' + str(pid)

    def fetch_tracks(self, pid):
        if self.fail_network:
            raise common.NetworkError('模拟断网')
        return self.tracks.get(str(pid), [])

    def scrobble(self, song, seconds):
        self.played.append(int(song['id']))
        return {'code': 200}


def _tracks(prefix, count):
    return [{'id': int(prefix) * 1000 + i, 'name': '{}{}'.format(prefix, i),
             'al': {'id': 1}, 'dt': 240000} for i in range(count)]


def run():
    common.set_quiet(True)
    tmp = tempfile.mkdtemp()
    common.STATE_FILE = os.path.join(tmp, 'ncm_state.json')
    common.COOKIE_FILE = os.path.join(tmp, 'ncm_cookie.json')
    common.LOCK_FILE = os.path.join(tmp, 'run.lock')
    common.QR_FILE = os.path.join(tmp, 'code.jpg')
    accounts.path = lambda: os.path.join(tmp, 'account.json')

    cfg = common.CFG
    cfg.sleep_range = (0, 0)
    cfg.sleep_long_pause_prob = 0
    cfg.scrobble_jitter_seconds = 0
    cfg.catch_up = False
    cfg.user_sl_id = ['A', 'B']

    client = FakeClient('u1', {'A': _tracks('1', 100), 'B': _tracks('2', 200)})

    def seed(uid='u1', **kw):
        st = state.load_state(uid)
        st.update(kw)
        if 'date' not in kw:
            st['date'] = date.today().isoformat()
        state.save_state(st)
        return st

    today = date.today().isoformat()
    goal = state.daily_goal_for(today)

    # 1) 两轮跑完，全天合计必须等于当日目标（330±jitter）
    client.played.clear()
    st = state.load_state('u1')
    n1 = player.run_play(client, st)
    st = state.load_state('u1')
    assert n1 > 0, n1
    assert st['played_today'] == n1, (st['played_today'], n1)
    n2 = player.run_play(client, st)
    st = state.load_state('u1')
    assert st['played_today'] == goal, (st['played_today'], goal)
    assert n1 + n2 == goal, (n1, n2, goal)
    assert cfg.min_songs_per_run <= n1 <= cfg.max_songs_per_run, n1
    print('[selftest] 1 两轮跑满：{}+{}={} 首（当日目标 {}）'.format(n1, n2, n1 + n2, goal))

    # 2) 已达标再跑 -> 不播放
    client.played.clear()
    n = player.run_play(client, state.load_state('u1'))
    assert n == 0 and not client.played, (n, client.played)
    print('[selftest] 2 今日已达标：播放 0，直接跳过')

    # 3) 本轮中断只播一部分，再跑只补本轮剩余
    seed(target_today=goal, slot_index=0, slot_target=120, slot_played=60,
         played_today=60, playlist_index=0, last_song_id=1059)
    client.played.clear()
    n = player.run_play(client, state.load_state('u1'))
    assert n == 60, n
    assert len(client.played) == 60 and client.played[0] == 1060, (len(client.played), client.played[:2])
    st = state.load_state('u1')
    assert st['slot_played'] == 120 and st['played_today'] == 120
    print('[selftest] 3 本轮断点：只补 60 首（1060 起），今日 120/{}'.format(goal))

    # 4) 第二轮自动补齐当日剩余
    client.played.clear()
    n = player.run_play(client, state.load_state('u1'))
    st = state.load_state('u1')
    assert st['played_today'] == goal, (st['played_today'], goal)
    assert n == goal - 120, (n, goal)
    print('[selftest] 4 第二轮自动补齐：{} 首，今日 {}/{}'.format(n, goal, goal))

    # 5) 每轮数量不固定：多次模拟必须出现不同取值
    sizes = set()
    for _ in range(30):
        sizes.add(state.pick_run_target(goal, 2))
    assert len(sizes) >= 5, sorted(sizes)
    assert min(sizes) >= 1 and max(sizes) <= cfg.max_songs_per_run, sorted(sizes)
    print('[selftest] 5 每轮数量随机：30 次抽取出现 {} 种不同数量（{}..{}）'.format(
        len(sizes), min(sizes), max(sizes)))

    # 6) 每日目标按日期确定性变化，但同一天恒定
    g_a = state.daily_goal_for('2024-01-01')
    g_b = state.daily_goal_for('2024-01-01')
    g_c = state.daily_goal_for('2024-06-15')
    assert g_a == g_b, (g_a, g_b)
    assert abs(g_a - cfg.daily_target_base) <= cfg.daily_target_jitter
    assert g_a != g_c or cfg.daily_target_jitter == 0
    print('[selftest] 6 每日目标：同日恒定 {}，不同日不同 {}（基准 {}±{}）'.format(
        g_a, g_c, cfg.daily_target_base, cfg.daily_target_jitter))

    # 7) catch_up=True：一次跑完当天剩余
    cfg.catch_up = True
    seed(target_today=goal, slot_index=0, slot_target=100, slot_played=40,
         played_today=40, playlist_index=0, last_song_id=1059)
    client.played.clear()
    n = player.run_play(client, state.load_state('u1'))
    st = state.load_state('u1')
    assert st['played_today'] == goal, (st['played_today'], goal)
    assert n == goal - 40, (n, goal)
    print('[selftest] 7 catch_up=True：一次跑完剩余 {} 首，今日 {}/{}'.format(n, goal, goal))
    cfg.catch_up = False

    # 8) 跨天自动重置
    seed(date='2000-01-01', target_today=goal, slot_index=1, slot_target=170,
         slot_played=170, played_today=goal, playlist_index=0, last_song_id=1059)
    client.played.clear()
    n = player.run_play(client, state.load_state('u1'))
    st = state.load_state('u1')
    assert st['date'] == today and st['slot_index'] == 0
    assert st['played_today'] == n and st['target_today'] == goal
    print('[selftest] 8 跨天重置：配额归零，歌单断点保留，播放 {} 首'.format(n))

    # 9) 断网识别
    broken = FakeClient('u1', fail_network=True)
    try:
        player.run_play(broken, state.load_state('u1'))
        raise AssertionError('应当抛出 NetworkError')
    except common.NetworkError:
        print('[selftest] 9 断网识别 OK：立即抛出 NetworkError，已播放进度已落盘')

    # 10) 时间预算：到点必须停止播放并返回
    seed(target_today=goal, slot_index=0, slot_target=150, slot_played=0,
         played_today=0, playlist_index=0, last_song_id=0)
    client.played.clear()
    common.sched['deadline'] = time.time()
    common.sched['reason'] = 'selftest'
    n = player.run_play(client, state.load_state('u1'))
    assert n == 0, n
    common.sched['deadline'] = None
    print('[selftest] 10 时间预算耗尽：立即停止播放并正常返回（不会挂住）')

    # 11) 预算充足时正常跑完本轮随机目标
    seed(target_today=goal, slot_index=0, slot_target=150, slot_played=0,
         played_today=0, playlist_index=0, last_song_id=0)
    client.played.clear()
    common.sched['deadline'] = time.time() + 3600
    n = player.run_play(client, state.load_state('u1'))
    assert n == 150 and state.load_state('u1')['played_today'] == 150, n
    common.sched['deadline'] = None
    print('[selftest] 11 预算充足：正常跑完本轮目标 150 首')

    # 12) 单实例锁
    assert app.acquire_run_lock() is True, '首次应拿到锁'
    app.release_run_lock()

    other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        with open(common.LOCK_FILE, 'w', encoding='utf-8') as f:
            f.write(str(other.pid))
        assert app.acquire_run_lock() is False, '存活进程持锁时应拒绝启动第二个实例'
    finally:
        other.kill()
        other.wait()

    with open(common.LOCK_FILE, 'w', encoding='utf-8') as f:
        f.write('999999999')
    assert app.acquire_run_lock() is True, '失效 pid 不应阻塞'
    app.release_run_lock()
    print('[selftest] 12 单实例锁：存活实例拒绝重复启动，残留锁可接管')

    # 13) 全天总量守恒
    for trial in range(20):
        seed(target_today=goal, slot_index=0, slot_target=0, slot_played=0,
             played_today=0, playlist_index=0, last_song_id=0)
        done = 0
        rounds = 0
        while True:
            st2 = state.load_state('u1')
            t = state.resolve_target(st2)
            if t <= 0:
                state.save_state(st2)
                break
            done += t
            rounds += 1
            assert rounds <= cfg.slots_per_day, '轮次超过配置上限'
            assert cfg.min_songs_per_run <= t <= cfg.max_songs_per_run, (rounds, t)
            st2['slot_played'] = st2['slot_target']
            st2['played_today'] = done
            state.save_state(st2)
        assert done == goal, (trial, done, goal)
    print('[selftest] 13 全天守恒：20 组随机组合均恰好跑满 {} 首'.format(goal))

    # 14) state 是数组：多个账号互不覆盖
    state.save_states([])
    seed('u1', played_today=11, target_today=100)
    seed('u2', played_today=22, target_today=200)
    records = state.load_states()
    assert len(records) == 2, records
    assert {r['userId'] for r in records} == {'u1', 'u2'}
    assert state.load_state('u1')['played_today'] == 11
    assert state.load_state('u2')['played_today'] == 22
    print('[selftest] 14 ncm_state.json 为数组：2 个账号各自独立（userId 字段存在）')

    # 15) cookie 解析：/login/refresh 返回的是 Set-Cookie 风格大串
    blob = ('MUSIC_R_T=1; Max-Age=2147483647; Expires=Sun, 21 Nov 2094 06:24:04 GMT; Path=/weapi/clientlog;;'
            'MUSIC_U=ccc; Max-Age=15552000; Expires=Sun, 02 May 2027 03:09:57 GMT; Path=/;;'
            '__csrf=ff28; Max-Age=1296010; Expires=Wed, 18 Nov 2026 03:10:07 GMT; Path=/;;'
            'MUSIC_SNS=; Max-Age=0; Expires=Tue, 03 Nov 2026 03:09:57 GMT; Path=/;')
    parsed = api.parse_cookie_blob(blob)
    assert parsed.get('MUSIC_U') == 'ccc' and parsed.get('__csrf') == 'ff28', parsed
    assert 'Path' not in parsed and 'Max-Age' not in parsed and 'MUSIC_SNS' not in parsed, parsed
    print('[selftest] 15 cookie 解析：只留真正的 cookie（{} 个），过滤掉 Path/Expires/空值'.format(len(parsed)))

    # 16) 登录态文件：数组结构 + 旧格式兼容
    api.save_cookies([])
    api.upsert_cookie({'userId': '1', 'nickname': 'a', 'type': 'code', 'cookie': {'MUSIC_U': 'x'}})
    api.upsert_cookie({'userId': '2', 'nickname': 'b', 'type': 'qrcode', 'cookie': {'MUSIC_U': 'y'}})
    entries = api.load_cookies()
    assert len(entries) == 2 and entries[0]['userId'] == '1'
    api.upsert_cookie({'userId': '1', 'nickname': 'a2', 'type': 'code', 'cookie': {'MUSIC_U': 'z'}})
    entries = api.load_cookies()
    assert len(entries) == 2 and entries[0]['nickname'] == 'a2'
    # enable 字段：新账号默认 true；重新登录不能把 false 改回来
    assert entries[0]['enable'] is True, entries
    api.upsert_cookie({'userId': '1', 'enable': False, 'cookie': {'MUSIC_U': 'z'}})
    assert api.load_cookies()[0]['enable'] is False
    api.upsert_cookie({'userId': '1', 'nickname': 'a4', 'type': 'code',
                       'cookie': {'MUSIC_U': 'zz'}})          # 不带 enable
    assert api.load_cookies()[0]['enable'] is False, api.load_cookies()[0]
    common.write_json(common.COOKIE_FILE, {'MUSIC_U': 'legacy', '__csrf': 'c'})
    legacy = api.load_cookies()
    assert len(legacy) == 1 and legacy[0]['cookie']['MUSIC_U'] == 'legacy', legacy
    print('[selftest] 16 登录态文件：数组增改、按 userId 覆盖、enable 保留、旧 dict 可迁移')

    # 17) 每天只做一次：签到标记
    st = state.load_state('u3')
    st['signin_date'] = ''
    assert state.mark_once(st, 'signin_date') is True
    assert state.mark_once(st, 'signin_date') is False
    assert state.done_today(st, 'signin_date') is True
    print('[selftest] 17 每日一次：签到/云贝标记当天只放行一次')

    # 18) 打卡参数：/scrobble/v1 的必选 + 可选字段都齐
    song = {'id': 518066366, 'name': '晴天', 'dt': 269000, 'fee': 1,
            'ar': [{'name': 'A'}, {'name': 'B'}], 'al': {'id': 36780169},
            'h': {'br': 320000}, 'm': {'br': 192000}, 'l': {'br': 96000}}
    params = api.scrobble_params(song, 291)
    assert params['id'] == 518066366 and params['time'] == 291, params
    assert params['sourceid'] == 36780169 and params['total'] == 269, params
    assert params['name'] == '晴天' and params['artist'] == 'A/B', params
    assert params['bitrate'] == 320 and params['level'] == 'exhigh', params
    assert params['source'] == 'list' and params['vip'] == 'true', params
    low = api.scrobble_params({'id': 1, 'dt': 100000, 'l': {'br': 96000}}, 50)
    assert low['bitrate'] == 96 and low['level'] == 'standard' and 'vip' not in low, low
    print('[selftest] 18 打卡参数：id/time/sourceid/total/name/artist/bitrate/level/source/vip 齐全')

    # 19) 验证码账号列表：环境变量导入 -> 落盘 -> 填码/清空
    saved_env = {k: os.environ.pop(k) for k in accounts.ENV_KEYS if k in os.environ}
    try:
        if os.path.exists(accounts.path()):
            os.remove(accounts.path())
        os.environ['account.json'] = json.dumps([{'phone': '13800000000', 'code': ''}])
        items = accounts.load_accounts()
        assert items and items[0]['phone'] == '13800000000', items
        assert os.path.exists(accounts.path()), '环境变量应落盘到 account.json'
        accounts.set_code('13800000000', '654321')
        assert accounts.get_code('13800000000') == '654321'
        accounts.clear_code('13800000000')
        assert accounts.get_code('13800000000') == ''
    finally:
        os.environ.pop('account.json', None)
        os.environ.update(saved_env)
    print('[selftest] 19 验证码账号：环境变量导入落盘、填码、登录后清空 code')

    # 20) 端到端：两个账号并发跑 main()，验证登录->签到->播放->通知->返回码
    _smoke(app, api, cfg)

    # 21) 数据目录：旧文件自动移入 data/，已存在不覆盖，写入 0600
    saved = (common.BASE_DIR, common.DATA_DIR, common.CONFIG_FILE, common.COOKIE_FILE,
             common.STATE_FILE, common.LOCK_FILE, common.QR_FILE)
    root = tempfile.mkdtemp()
    data_dir = os.path.join(root, 'data')
    common.BASE_DIR = root
    common.DATA_DIR = data_dir
    common.CONFIG_FILE = os.path.join(data_dir, 'config.json')
    common.COOKIE_FILE = os.path.join(data_dir, 'ncm_cookie.json')
    common.STATE_FILE = os.path.join(data_dir, 'ncm_state.json')
    common.LOCK_FILE = os.path.join(data_dir, 'ncm_run.lock')
    common.QR_FILE = os.path.join(data_dir, 'code.jpg')
    try:
        with open(os.path.join(root, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump({'api': 'http://legacy.local', 'user_sl_id': ['L1']}, f)
        with open(os.path.join(root, 'ncm_cookie.json'), 'w', encoding='utf-8') as f:
            json.dump([{'userId': 'u9', 'type': 'code', 'enable': True,
                        'cookie': {'MUSIC_U': 'm'}}], f)

        moved = common.ensure_data_dir()
        assert set(moved) == {'config.json', 'ncm_cookie.json'}, moved
        assert os.path.isdir(data_dir)
        assert os.path.exists(common.CONFIG_FILE)
        assert not os.path.exists(os.path.join(root, 'config.json')), '旧文件应被移走'
        assert common.read_json(common.CONFIG_FILE, {}).get('api') == 'http://legacy.local'
        assert api.load_cookies()[0]['userId'] == 'u9'

        # 目标已存在时不再移动 / 不覆盖
        assert common.ensure_data_dir() == []

        # JSON 写入原子替换 + 0600
        common.write_json(common.STATE_FILE, [{'userId': 'u9'}])
        assert common.read_json(common.STATE_FILE, [])[0]['userId'] == 'u9'
        assert os.stat(common.STATE_FILE).st_mode & 0o777 == 0o600
    finally:
        common.BASE_DIR, common.DATA_DIR, common.CONFIG_FILE, common.COOKIE_FILE, \
            common.STATE_FILE, common.LOCK_FILE, common.QR_FILE = saved
    print('[selftest] 21 数据目录：旧文件自动移入 data/，不覆盖已有文件，写入 0600')

    print('[selftest] 全部通过')
    return 0


def _smoke(app, api_mod, cfg):
    """端到端冒烟：多账号并发、每日签到只做一次、扫码账号失败要通知。"""
    state.save_states([])
    cfg.api = 'http://smoke.local'
    cfg.user_sl_id = ['SMOKE']
    cfg.slots_per_day = 1
    cfg.daily_target_base = 3
    cfg.daily_target_jitter = 0
    cfg.min_songs_per_run = 3
    cfg.max_songs_per_run = 3
    cfg.sleep_range = (0, 0)
    cfg.sleep_long_pause_prob = 0
    cfg.scrobble_jitter_seconds = 0
    cfg.catch_up = False
    cfg.max_run_minutes = 0
    cfg.max_workers = 2

    tracks = _tracks('9', 3)
    calls = []
    clients = {}

    def make_client(**kwargs):
        uid = kwargs.get('user_id') or 'u1'
        c = FakeClient(uid, {'SMOKE': tracks}, calls=calls,
                       auth_fail=(uid == 'broken'))
        clients[uid] = c
        return c

    saved = {'load_cookies': api_mod.load_cookies, 'Client': api_mod.Client,
             'start_watchdog': common.start_watchdog, 'notify': app.notify}
    notified = []
    try:
        api_mod.load_cookies = lambda: [
            {'userId': 'u1', 'nickname': 'one', 'type': 'code',
             'enable': True, 'cookie': {'MUSIC_U': 'x'}},
            {'userId': 'broken', 'nickname': 'two', 'type': 'qrcode',
             'enable': True, 'cookie': {'MUSIC_U': 'y'}},
            {'userId': 'off', 'nickname': 'disabled', 'type': 'code',
             'enable': False, 'cookie': {'MUSIC_U': 'z'}},
        ]
        api_mod.Client = make_client
        common.start_watchdog = lambda seconds, reason: None
        app.notify = lambda title, content: notified.append((title, content))

        rc = app.main()
        st1 = state.load_state('u1')
        assert 'off' not in clients, 'enable=false 的账号不该被执行'
        assert rc == 1, rc                              # broken 账号失败
        assert len(clients['u1'].played) == 3, clients['u1'].played
        assert st1['played_today'] == 3, st1['played_today']
        assert clients['u1'].calls == ['signin', 'yunbei'], clients['u1'].calls
        assert any('失败' in title for title, _ in notified), notified

        # 再跑一次：今天已经签到过，不能再请求
        clients['u1'].calls.clear()
        state.save_states([])                            # 只重置断点，保留 signin_date 试试
        seed_signin = state.load_state('u1')
        seed_signin['signin_date'] = date.today().isoformat()
        seed_signin['yunbei_date'] = date.today().isoformat()
        state.save_state(seed_signin)
        app.main()
        assert clients['u1'].calls == [], clients['u1'].calls
    finally:
        api_mod.load_cookies = saved['load_cookies']
        api_mod.Client = saved['Client']
        common.start_watchdog = saved['start_watchdog']
        app.notify = saved['notify']
    print('[selftest] 20 端到端：两账号并发、签到只做一次、扫码账号失效会通知')
