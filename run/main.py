# -*- coding: utf-8 -*-
"""入口：单实例锁、多账号并发、每日一次签到、通知与命令行。

用法（青龙里就是 task run.py）：
    python run.py                  # 跑数据目录 ncm_cookie.json 里的全部账号
    python run.py --user 123456    # 只跑指定 userId
    python run.py --status         # 打印各账号断点
    python run.py --selftest       # 本地自检，不联网
    python run.py --reset          # 清除登录态与断点（保留 config / account）
    python run.py --config X       # 指定独立配置文件（进阶）
    python run.py --migrate        # 把根目录旧数据文件移入数据目录 data/

登录不在这里做：
    python login.py --type=qrcode
    python login.py --type=code --account=<手机号>
"""
import atexit
import json
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

# 支持直接运行 run/main.py：此时 sys.path[0] 是 run/，
# 需要把项目根目录补进来，才能 import shared 包和 run 包。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests

from shared import api, common
from shared.common import (CFG, ApiError, AuthError, ConfigError, LoginTimeout,
                           NetworkError, SlotTimeout, log)
from run import player, state


# ---------------------------- 单实例锁 ----------------------------
def acquire_run_lock():
    """单实例锁：防止青龙定时任务重叠，导致多个进程同时跑、都卡在「执行中」。"""
    lock_file = common.LOCK_FILE
    try:
        if os.path.exists(lock_file):
            pid = 0
            try:
                with open(lock_file, 'r', encoding='utf-8') as f:
                    pid = int((f.read() or '0').strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                log('检测到已有实例在运行（pid={}），本次直接退出，避免重复占用配额'.format(pid))
                return False
        with open(lock_file, 'w', encoding='utf-8') as f:
            f.write(str(os.getpid()))
        return True
    except OSError as e:
        log('创建运行锁失败（{}），继续执行'.format(e))
        return True


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def release_run_lock():
    lock_file = common.LOCK_FILE
    try:
        if os.path.exists(lock_file):
            with open(lock_file, 'r', encoding='utf-8') as f:
                pid = int((f.read() or '0').strip() or 0)
            if pid == os.getpid():
                os.remove(lock_file)
    except (OSError, ValueError):
        pass


# ---------------------------- 通知 ----------------------------
def notify(title, content):
    """青龙内置 notify.py 优先，其次 Server酱；任何失败都不影响主流程。"""
    log('【通知】{} | {}'.format(title, content.replace('\n', ' / ')))
    sent = False
    for path in ('/ql/data/scripts', '/ql/scripts'):
        if path not in sys.path and os.path.isdir(path):
            sys.path.insert(0, path)

    def _ql_push():
        try:
            from notify import send as ql_send
            ql_send(title, content)
        except Exception as e:          # 青龙里没有 notify.py 时不刷 traceback
            log('青龙 notify 不可用：{}'.format(e))

    try:
        t = threading.Thread(target=_ql_push, daemon=True)
        t.start()
        t.join(CFG.notify_timeout)
        if t.is_alive():
            log('青龙 notify 超过 {} 秒未返回，跳过（不影响任务结束）'.format(CFG.notify_timeout))
        else:
            sent = True
    except Exception as e:
        log('青龙 notify 不可用：{}'.format(e))

    if CFG.sckey:
        try:
            requests.post('https://sctapi.ftqq.com/{}.send'.format(CFG.sckey),
                          data={'title': title, 'desp': content}, timeout=CFG.notify_timeout)
            sent = True
        except Exception as e:
            log('Server酱 推送失败：{}'.format(e))
    if not sent:
        log('未配置任何可用通知渠道（notify.py / sckey）')
    return sent


# ---------------------------- 单账号任务 ----------------------------
def run_account(entry):
    """跑一个账号：校验登录态 -> 每日签到（当天首次）-> 播放。异常不外抛。"""
    uid = str(entry.get('userId') or '')
    nickname = entry.get('nickname') or ''
    result = {'userId': uid, 'nickname': nickname, 'played': 0, 'level': None,
              'listen': None, 'ok': False, 'error': '', 'refreshed': False}
    tag = '[{}] '.format(uid or '-')

    client = api.Client(cookie=entry.get('cookie'), user_id=uid,
                        login_type=entry.get('type') or 'qrcode')
    try:
        account = client.user_account()
        if not account:
            log('{}本地 cookie 已失效，尝试刷新…'.format(tag))
            if client.refresh_cookie():
                result['refreshed'] = True
                account = client.user_account()
        if not account:
            if (entry.get('type') or 'qrcode') == 'qrcode':
                raise AuthError('扫码登录的 cookie 已失效，且不支持刷新，请重新扫码：python login.py --type=qrcode')
            raise AuthError('登录态失效且刷新失败，请重新登录：python login.py --type=code --account=<手机号>')

        uid = account['userId'] or uid
        result['userId'] = uid
        result['nickname'] = account.get('nickname') or nickname
        client.user_id = uid
        tag = '[{}] '.format(uid)
        log('{}{} 登录态有效'.format(tag, result['nickname'] or '-'))

        st = state.load_state(uid)
        st['userId'] = uid

        # ---- 每天只做一次：每日签到 / 云贝签到 ----
        if state.mark_once(st, 'signin_date'):
            state.save_state(st)
            api.safe_call('每日签到', client.daily_check, prefix=tag)
        else:
            log('{}每日签到今天已完成，跳过（避免重复请求）'.format(tag))
        if state.mark_once(st, 'yunbei_date'):
            state.save_state(st)
            api.safe_call('云贝签到', client.yunbei_sign, prefix=tag)
        else:
            log('{}云贝签到今天已完成，跳过'.format(tag))

        level, listen = client.user_detail(uid)
        result['level'], result['listen'] = level, listen
        log('{}当前等级 {}，累计听歌 {} 首'.format(tag, level, listen))

        st = state.load_state(uid)
        target = state.resolve_target(st)
        state.save_state(st)
        if target <= 0:
            log('{}今日目标已完成（{}/{}），无需播放'.format(
                tag, st['played_today'], st['target_today']))
        else:
            result['played'] = player.run_play(client, st)
            log('{}本次播放完成：{} 首（今日 {}/{}）'.format(
                tag, result['played'], st['played_today'], st['target_today']))
        result['ok'] = True
        return result

    except NetworkError as e:
        result['error'] = '网络断开：{}'.format(e)
        raise
    except (AuthError, ApiError, SlotTimeout) as e:
        result['error'] = str(e)
        log('{}任务失败：{}'.format(tag, e))
        return result
    except Exception as e:
        result['error'] = '{}: {}'.format(type(e).__name__, e)
        log('{}未预期异常：{}'.format(tag, e))
        log(traceback.format_exc())
        return result
    finally:
        client.session.close()


def build_summary(results, total):
    lines = []
    for item in results:
        if item.get('ok'):
            lines.append('[{}] {}：本次 {} 首，今日 {} 首，{} 级 / 累计 {} 首{}'.format(
                item['userId'], item['nickname'] or '-', item['played'],
                player.progress_of(item['userId'])['played_today'] or '-',
                item['level'] if item['level'] is not None else '-',
                item['listen'] if item['listen'] is not None else '-',
                '（已刷新登录态）' if item.get('refreshed') else ''))
        else:
            lines.append('[{}] {}：失败 —— {}'.format(
                item['userId'], item['nickname'] or '-', item['error'] or '未知错误'))
    lines.append('合计本次播放：{} 首 / {} 个账号'.format(total, len(results)))
    return '\n'.join(lines)


# ---------------------------- 主流程 ----------------------------
def main(only_users=None):
    if not CFG.api:
        raise ApiError('未配置 api 地址')
    if not CFG.user_sl_id:
        raise ApiError('未配置 user_sl_id（歌单 ID）')

    all_entries = api.load_cookies()
    disabled = [e for e in all_entries if not e.get('enable', True)]
    if disabled:
        log('已跳过 {} 个 disable 的账号：{}'.format(
            len(disabled), ', '.join('{} {}'.format(e['userId'], e.get('nickname') or '') for e in disabled)))
    entries = [e for e in all_entries if e.get('enable', True)]
    if only_users:
        entries = [e for e in entries if e['userId'] in set(only_users)]
    if not entries:
        raise ApiError('没有可运行的账号：请先登录（python login.py --type=qrcode / '
                       '--type=code --account=<手机号>），并确认 {} 里对应账号的 '
                       'enable 不是 false'.format(common.COOKIE_FILE))

    log('================ 网易云听歌任务开始 ================')
    playlists = CFG.user_sl_id
    shown = ', '.join(playlists[:3]) + (' …共 {} 个'.format(len(playlists)) if len(playlists) > 3 else '')
    log('账号：{} 个（{}）｜歌单：{}'.format(
        len(entries), ', '.join('{} {}'.format(e['userId'], e['nickname']) for e in entries), shown))
    log('每日目标：{} 首（每天 {} 轮，每轮随机 {}~{} 首）| 上报秒数：{} | 补位模式：{}'.format(
        CFG.daily_target_base, CFG.slots_per_day,
        CFG.min_songs_per_run, CFG.max_songs_per_run,
        CFG.scrobble_seconds or '按真实时长', '开' if CFG.catch_up else '关'))

    # 预先算出每个账号本次要播多少，用最长那个定全局看门狗
    budgets = []
    for entry in entries:
        st = state.load_state(entry['userId'])
        target = state.resolve_target(st)
        state.save_state(st)
        if target > 0:
            budgets.append(common.compute_budget(target))
    if budgets:
        hard = max(budgets) + max(1, CFG.hard_exit_buffer_minutes) * 60
        common.start_watchdog(hard, '{} 个账号并发播放'.format(len(entries)))

    workers = CFG.max_workers or len(entries)
    workers = max(1, min(workers, len(entries)))
    results = []
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix='ncm') as pool:
        futures = {pool.submit(run_account, entry): entry for entry in entries}
        for future in as_completed(futures):
            entry = futures[future]
            try:
                results.append(future.result())
            except NetworkError:
                raise
            except Exception as e:            # 兜底，别让一个账号拖垮全部
                results.append({'userId': entry.get('userId'), 'nickname': entry.get('nickname'),
                                'played': 0, 'ok': False, 'error': str(e)})

    results.sort(key=lambda r: str(r.get('userId')))
    total = sum(r.get('played') or 0 for r in results)
    summary = build_summary(results, total)
    failed = [r for r in results if not r.get('ok')]
    notify('网易云听歌任务结束' if not failed else '网易云任务有账号失败', summary)
    log(summary)
    log('================ 任务结束 ================')
    return 0 if not failed else 1


def run(only_users=None):
    atexit.register(release_run_lock)
    if not acquire_run_lock():
        return 0
    try:
        return main(only_users)
    except NetworkError as e:
        notify('网易云任务中断：网络断了', '检测到网络断开，任务已立即停止（这不是未登录）\n\n原因：{}'.format(e))
        log('网络中断，任务立即结束：{}'.format(e))
        return 1
    except LoginTimeout as e:
        notify('网易云登录失败', '请重新登录：\n{}'.format(e))
        log('登录超时，任务结束：{}'.format(e))
        return 1
    except ApiError as e:
        notify('网易云任务异常', '接口异常导致任务停止：\n{}'.format(e))
        log('接口异常，任务结束：{}'.format(e))
        return 1
    except KeyboardInterrupt:
        notify('网易云任务被手动中断', '已手动停止')
        log('手动中断')
        return 130
    except SystemExit:
        raise
    except BaseException as e:
        log('未预期异常：{}'.format(e))
        log(traceback.format_exc())
        try:
            notify('网易云任务异常退出', '{}'.format(e))
        except Exception:
            pass
        return 1
    finally:
        release_run_lock()


# ---------------------------- 命令行 ----------------------------
def cli(argv):
    args = list(argv[1:])
    config_path = None
    if '--config' in args:
        index = args.index('--config')
        try:
            config_path = args[index + 1]
        except IndexError:
            print('--config 需要跟一个文件路径', file=sys.stderr)
            return 2
        del args[index:index + 2]

    only_users = []
    if '--user' in args:
        index = args.index('--user')
        try:
            only_users = [s for s in args[index + 1].replace('，', ',').split(',') if s.strip()]
        except IndexError:
            print('--user 需要跟 userId（多个用逗号分隔）', file=sys.stderr)
            return 2
        del args[index:index + 2]

    if '--migrate' in args:
        moved = common.ensure_data_dir()
        log('数据目录：{}（本次移动：{}）'.format(
            common.DATA_DIR, '、'.join(moved) if moved else '无'))
        return 0

    if '--selftest' in args:
        from run import selftest
        return selftest.run()

    common.ensure_data_dir()

    if '--reset' in args:
        for path in (common.COOKIE_FILE, common.STATE_FILE):
            if os.path.exists(path):
                os.remove(path)
        log('已清除登录态与断点（config / account 保留在 {}）'.format(common.DATA_DIR))
        return 0

    try:
        common.load_config(config_path)
    except ConfigError as e:
        print('[配置错误] {}'.format(e), file=sys.stderr)
        return 3

    if '--status' in args:
        entries = api.load_cookies()
        if not entries:
            print('没有登录态，请先运行 python login.py --type=qrcode')
            return 0
        for entry in entries:
            st = state.load_state(entry['userId'])
            print('\n=== {} {}（{}，{}）==='.format(
                entry['userId'], entry.get('nickname') or '-', entry.get('type') or '-',
                '启用' if entry.get('enable', True) else '已禁用'))
            print(json.dumps(st, ensure_ascii=False, indent=2))
            print('今日目标：{}/{} 首（第 {}/{} 轮，本轮 {}/{} 首）'.format(
                st['played_today'], st['target_today'],
                min(st['slot_index'] + 1, CFG.slots_per_day), CFG.slots_per_day,
                st['slot_played'], st['slot_target'] or 0))
        return 0

    return run(only_users)


if __name__ == '__main__':
    sys.exit(cli(sys.argv))
