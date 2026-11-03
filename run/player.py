# -*- coding: utf-8 -*-
"""播放主循环：把某个账号的本轮配额变成一串打卡请求，每首之后落盘断点。

多账号并行时，每个账号一个 Client + 一条 state 记录，进度也按 userId 分开记。
"""
import random
import time

from shared import common
from shared.common import CFG, log
from run import state

# userId -> 进度快照（供通知/日志用）
progress_map = {}


def progress_of(user_id=''):
    return progress_map.setdefault(str(user_id or ''), {
        'played': 0, 'target': 0, 'playlist': '', 'song': 0,
        'slot_index': 0, 'slot_target': 0, 'slot_played': 0,
        'played_today': 0, 'target_today': 0,
    })


def _tag(user_id):
    return '[{}] '.format(user_id or '-')


def play_batch(client, st, target):
    """播放 target 首；断网时直接上抛 NetworkError（进度已逐首落盘）。"""
    cfg = CFG
    uid = st.get('userId') or client.user_id
    tag = _tag(uid)
    progress = progress_of(uid)
    playlists = cfg.user_sl_id
    idx = st['playlist_index'] % len(playlists)
    last_id = st['last_song_id']
    remaining = target
    played = 0
    guard = 0

    started = time.time()
    avg_wait = (cfg.sleep_range[0] + cfg.sleep_range[1]) / 2.0

    while remaining > 0 and guard <= len(playlists):
        if common.budget_exhausted():
            log('{}已到达本次运行的时间上限，停止播放（已播 {} 首，进度已落盘，下次继续）'.format(tag, played))
            break
        pid = playlists[idx]
        tracks = client.fetch_tracks(pid)
        if not tracks:
            log('{}歌单 {} 没有取到歌曲，切换到下一个'.format(tag, pid))
            idx = (idx + 1) % len(playlists)
            last_id = 0
            guard += 1
            continue

        pl_name = client.fetch_playlist_name(pid) or '未命名歌单'
        start = 0
        if last_id:
            ids = [int(t.get('id') or 0) for t in tracks]
            if last_id in ids:
                start = ids.index(last_id) + 1
                log('{}歌单 {}《{}》上次播到 id={}，本次从第 {}/{} 首继续'.format(
                    tag, pid, pl_name, last_id, start + 1, len(tracks)))
            else:
                log('{}歌单 {}《{}》中找不到上次的 id={}，从第一首开始'.format(tag, pid, pl_name, last_id))
                start = 0

        if start >= len(tracks):
            log('{}歌单 {}《{}》已播完，切换到下一个歌单'.format(tag, pid, pl_name))
            idx = (idx + 1) % len(playlists)
            last_id = 0
            guard += 1
            continue

        for i in range(start, len(tracks)):
            if remaining <= 0:
                break
            if common.budget_exhausted():
                log('{}已到达本次运行的时间上限，停止播放（已播 {} 首，进度已落盘，下次继续）'.format(tag, played))
                remaining = 0
                break
            song = tracks[i]
            sid = int(song.get('id') or 0)
            seconds = cfg.scrobble_seconds or max(1, int((song.get('dt') or 70000) / 1000))
            if cfg.scrobble_jitter_seconds and seconds > cfg.scrobble_jitter_seconds + 1:
                seconds += random.randint(-cfg.scrobble_jitter_seconds, cfg.scrobble_jitter_seconds)
                seconds = max(1, seconds)
            try:
                client.scrobble(song, seconds)
            except common.NetworkError:
                raise
            except common.ApiError as e:
                log('{}歌曲 id={} 打卡失败：{}（跳过该首）'.format(tag, sid, e))

            remaining -= 1
            played += 1
            last_id = sid
            st['playlist_index'] = idx
            st['last_song_id'] = sid
            st['slot_played'] += 1
            st['played_today'] += 1
            st['total_played'] += 1
            state.save_state(st)

            progress.update(played=played, target=target,
                            playlist='{}《{}》'.format(pid, pl_name), song=sid,
                            slot_index=st['slot_index'], slot_target=st['slot_target'],
                            slot_played=st['slot_played'],
                            played_today=st['played_today'],
                            target_today=st['target_today'])
            log('{}{}[歌单 {}《{}》 第 {}/{} 首] 已打卡 id={} 《{}》 上报{}s | '
                '本轮 {}/{}，今日 {}/{}，累计 {}'.format(
                    tag, '', pid, pl_name, i + 1, len(tracks), sid, song.get('name') or '', seconds,
                    st['slot_played'], st['slot_target'] or target,
                    st['played_today'], st['target_today'], st['total_played']))
            if played % 10 == 0 and remaining > 0:
                used = (time.time() - started) / 60.0
                eta = used / max(1, played) * remaining if played else avg_wait * remaining / 60.0
                log('{}进度提示：本次已播 {}/{} 首，用时 {:.0f} 分钟，预计还需 {:.0f} 分钟'.format(
                    tag, played, target, used, eta))
            if remaining > 0:
                wait = random.uniform(cfg.sleep_range[0], cfg.sleep_range[1])
                if cfg.sleep_long_pause_prob and random.random() < cfg.sleep_long_pause_prob:
                    wait = random.uniform(cfg.sleep_long_pause_range[0], cfg.sleep_long_pause_range[1])
                    log('{}插入一次长停顿 {:.0f} 秒（模拟真人中途离开）'.format(tag, wait))
                if not common.interruptible_sleep(wait):
                    log('{}等待期间到达运行时间上限，提前收尾（已播 {} 首）'.format(tag, played))
                    break

        guard = 0
        if remaining > 0:
            log('{}歌单 {}《{}》已播完，还差 {} 首，切换到下一个歌单'.format(tag, pid, pl_name, remaining))
            idx = (idx + 1) % len(playlists)
            last_id = 0

    st['playlist_index'] = idx
    st['last_song_id'] = last_id
    state.save_state(st)
    return played


def run_play(client, st):
    """按配额执行；返回本次实际播放数。一定会返回，不会无限循环。"""
    cfg = CFG
    uid = st.get('userId') or client.user_id
    tag = _tag(uid)
    total = 0
    while True:
        target = state.resolve_target(st)
        state.save_state(st)
        if target <= 0:
            if st['played_today'] >= st['target_today']:
                log('{}今日目标已完成：{}/{} 首，本次不再播放'.format(
                    tag, st['played_today'], st['target_today']))
            else:
                log('{}今日 {} 轮已全部跑完（今日 {}/{}），本次不再播放'.format(
                    tag, cfg.slots_per_day, st['played_today'], st['target_today']))
            break

        if st['slot_played'] > 0:
            log('{}检测到本轮未完成：今日第 {}/{} 轮已播 {}/{} 首，本次只补剩下的 {} 首（今日 {}/{}）'.format(
                tag, st['slot_index'] + 1, cfg.slots_per_day,
                st['slot_played'], st['slot_target'], target,
                st['played_today'], st['target_today']))
        else:
            log('{}开始今日第 {}/{} 轮，本轮随机目标 {} 首（今日目标 {}，已播 {}）'.format(
                tag, st['slot_index'] + 1, cfg.slots_per_day, target,
                st['target_today'], st['played_today']))

        avg_wait = (cfg.sleep_range[0] + cfg.sleep_range[1]) / 2.0
        log('{}预计耗时约 {:.0f} 分钟（{} 首 × 平均 {:.0f} 秒间隔）'.format(
            tag, (target - 1) * avg_wait / 60.0, target, avg_wait))
        played = play_batch(client, st, target)
        total += played
        state.save_state(st)

        if played < target:          # 被中断（断网/时间上限/歌单取不到），交给下一次运行补
            if common.budget_exhausted():
                log('{}本次因到达运行时间上限提前结束，已播 {} 首，剩余 {} 首由下次运行补齐'.format(
                    tag, played, target - played))
            else:
                log('{}本轮未跑完（已播 {}/{}），剩余部分下次运行自动补齐'.format(tag, played, target))
            break
        if not cfg.catch_up:
            break
    st['runs'] += 1
    state.save_state(st)
    return total



def build_progress_text(user_id=''):
    progress = progress_of(user_id)
    return ('[{}] 本次已播放 {}/{} 首\n今日进度：{}/{} 首\n当前轮次：第 {} 轮，已播 {} 首\n'
            '当前歌单：{}\n最后歌曲 id：{}\n'
            '进度已落盘，下次运行会从下一首继续补齐').format(
        user_id or '-', progress['played'], progress['target'] or '-',
        progress['played_today'], progress['target_today'] or '-',
        (progress['slot_index'] or 0) + 1, progress['slot_played'],
        progress['playlist'] or '-', progress['song'] or '-')
