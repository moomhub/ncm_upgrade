# -*- coding: utf-8 -*-
"""每个账号的每日目标、单轮配额、断点与「每天只做一次」的标记。

data/ncm_state.json 结构（数组，一个账号一条）：

    [
      {
        "userId": "343525065",
        "date": "2026-11-03",
        "target_today": 336, "slot_index": 0, "slot_target": 180,
        "slot_played": 0, "played_today": 0,
        "playlist_index": 0, "last_song_id": 0, "total_played": 0, "runs": 0,
        "signin_date": "2026-11-03",     # 每日签到（当天做过就不再请求）
        "yunbei_date": "2026-11-03",     # 云贝签到
        "updated_at": "2026-11-03 10:00:00"
      }
    ]

模型：每日总量固定（330±jitter），但每次运行的数量随机：
  - 当日目标按日期确定性随机生成，同一天恒定、每天不同
  - 每轮在剩余额度内随机抽取本轮数量
  - 全天各轮合计始终跑满当日目标
"""
import random
import threading
from datetime import date, datetime

from shared import common
from shared.common import CFG, log

STATE_VERSION = 4

STATE_DEFAULTS = {
    'userId': '',
    'date': '',
    'target_today': 0,      # 当日目标总量（按日期确定性随机生成，当天固定）
    'slot_index': 0,        # 已开始的轮次下标 0..slots_per_day-1
    'slot_target': 0,       # 本轮随机抽到的目标首数
    'slot_played': 0,       # 本轮已播数量
    'played_today': 0,      # 今天累计播放
    'playlist_index': 0,    # 当前歌单在 user_sl_id 中的下标
    'last_song_id': 0,      # 最后播放的歌曲 ID（下次从下一首开始）
    'total_played': 0,      # 历史累计
    'runs': 0,              # 运行次数
    'signin_date': '',      # 每日签到完成日期（YYYY-MM-DD）
    'yunbei_date': '',      # 云贝签到完成日期
    'updated_at': '',
}

_lock = threading.RLock()


def today_str():
    return date.today().isoformat()


# ---------------------------- 文件读写（data/ncm_state.json） ----------------------------
def _read_all():
    """读整个文件，并兼容旧的「单个 dict」格式。"""
    raw = common.read_json(common.STATE_FILE, [])
    if isinstance(raw, dict):
        raw = [raw]
    return [r for r in raw if isinstance(r, dict)]


def _write_all(records):
    common.write_json(common.STATE_FILE, records)


def load_states():
    return _read_all()


# ---------------------------- 单账号 ----------------------------
def load_state(user_id=''):
    """取某个账号的状态记录（不存在则创建）；跨天自动归零当日计数。

    注意：返回的是内存副本，改完要调 save_state(rec) 落盘。
    """
    uid = str(user_id or '')
    with _lock:
        raw = {}
        for item in _read_all():
            if str(item.get('userId') or '') == uid:
                raw = item
                break

    st = dict(STATE_DEFAULTS)
    st['userId'] = uid
    for key, value in raw.items():
        if key in STATE_DEFAULTS:
            st[key] = value

    today = today_str()
    if st['date'] != today:
        if st['date']:
            log('[{}] 检测到新的一天（{} -> {}），当日配额计数归零，歌单断点保留'.format(
                uid or '-', st['date'], today))
        st.update(date=today, target_today=0, slot_index=0, slot_target=0,
                  slot_played=0, played_today=0)
    if not st.get('target_today'):
        st['target_today'] = daily_goal_for(st['date'])
    return st


def save_state(st):
    """把一条账号状态写回数组文件（按 userId 覆盖/追加）。"""
    with _lock:
        records = _read_all()
        st = dict(st)
        st['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        uid = str(st.get('userId') or '')
        st['userId'] = uid
        for index, item in enumerate(records):
            if str(item.get('userId') or '') == uid:
                records[index] = st
                break
        else:
            records.append(st)
        _write_all(records)


def save_states(records):
    with _lock:
        _write_all([dict(r) for r in records])


def remove_state(user_id):
    with _lock:
        records = [r for r in _read_all() if str(r.get('userId') or '') != str(user_id)]
        _write_all(records)


# ---------------------------- 每日一次 ----------------------------
def mark_once(st, key, day=None):
    """把「今天已经做过 X」记到状态里；返回是否需要在本次执行。"""
    day = day or today_str()
    if st.get(key) == day:
        return False
    st[key] = day
    return True


def done_today(st, key, day=None):
    return st.get(key) == (day or today_str())


# ---------------------------- 配额推导 ----------------------------
def daily_goal_for(day_str):
    """按日期确定性生成当天目标总量（base±jitter）。
    用日期做种子，所以同一天无论跑几次、哪个进程算，结果都一样，
    但每天的目标数值都不同 —— 不会出现「天天固定 330」的规律。
    """
    base = int(CFG.daily_target_base)
    jit = max(0, int(CFG.daily_target_jitter))
    if jit == 0:
        return base
    rnd = random.Random('ncm-goal-{}'.format(day_str))
    return base + rnd.randint(-jit, jit)


def pick_run_target(day_left, slots_left):
    """随机决定本轮播放多少首。
    规则：
      - 必须给后续轮次留足额度（min_songs_per_run × 剩余轮次）
      - 在 [剩余量 × run_ratio_min, 剩余量 × run_ratio_max] 内随机
      - 受 max_songs_per_run 上限约束
    """
    cfg = CFG
    if day_left <= 0:
        return 0
    if slots_left <= 1:
        return day_left

    reserve = cfg.min_songs_per_run * (slots_left - 1)
    usable = day_left - reserve
    if usable <= 0:
        return max(1, min(day_left, cfg.min_songs_per_run))

    lo = max(cfg.min_songs_per_run, int(round(usable * cfg.run_ratio_min)))
    hi = min(cfg.max_songs_per_run, int(round(usable * cfg.run_ratio_max)))
    hi = max(lo, hi)
    n = random.randint(lo, hi)
    return max(1, min(n, day_left))


def resolve_target(st):
    """算出本次应该播放多少首（并把本轮目标写回 st）。

    返回 0 表示今天不用再跑。否则返回本次的随机目标首数。
    断点语义：同一轮内若中断，下次只补「本轮剩余」，不会重新抽目标。
    """
    slots_left = CFG.slots_per_day - st['slot_index']
    if slots_left <= 0:
        return 0

    day_left = max(0, st['target_today'] - st['played_today'])
    if day_left <= 0:
        return 0

    if st['slot_target'] > 0:
        remain = st['slot_target'] - st['slot_played']
        if remain > 0:
            return min(remain, day_left)
        st['slot_index'] += 1
        st['slot_target'] = 0
        st['slot_played'] = 0
        return resolve_target(st)

    n = pick_run_target(day_left, slots_left)
    st['slot_target'] = n
    st['slot_played'] = 0
    return n
