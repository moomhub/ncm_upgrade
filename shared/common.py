# -*- coding: utf-8 -*-
"""公共底座：数据目录、配置加载、路径、日志、异常、JSON 读写、时间预算与看门狗。

数据目录：默认 <项目根>/data/，可用 NCM_DATA_DIR 指定。里面放：
    config.json      # 配置（api / user_sl_id / 配额等）
    account.json     # 验证码登录账号列表
    ncm_cookie.json  # 登录态
    ncm_state.json   # 断点与签到标记
    ncm_run.lock     # 单实例锁
    code.jpg         # 二维码图片

配置来源（优先级从高到低）：
  NCM_CONFIG 指向的独立 JSON > data/config.json > 本文件 DEFAULTS。

约定：
  * load_config() 就地更新 CFG 的属性、不替换对象本身，
    所以其它模块 `from shared.common import CFG` 拿到的引用始终有效；
  * 数据文件路径（COOKIE_FILE / STATE_FILE 等）保存在本模块，selftest 会直接替换它们，
    所以其它模块必须用 `common.COOKIE_FILE` 这种属性访问，不要 from-import。
"""
import copy
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime

# 本文件在 shared/ 目录里，项目根目录是它的上一级
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('NCM_DATA_DIR') or os.path.join(BASE_DIR, 'data')

CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
ACCOUNT_FILE = os.path.join(DATA_DIR, 'account.json')
COOKIE_FILE = os.path.join(DATA_DIR, 'ncm_cookie.json')
STATE_FILE = os.path.join(DATA_DIR, 'ncm_state.json')
LOCK_FILE = os.path.join(DATA_DIR, 'ncm_run.lock')
QR_FILE = os.path.join(DATA_DIR, 'code.jpg')

# 旧版放在项目根目录的数据文件：首次运行时自动移进 DATA_DIR
LEGACY_NAMES = ('config.json', 'account.json', 'ncm_cookie.json', 'ncm_state.json')

# config.json 里没写的键，一律用这里的默认值
DEFAULTS = {
    'api': '',
    'sckey': '',
    'user_sl_id': [],
    'daily_target_base': 330,
    'daily_target_jitter': 15,
    'slots_per_day': 2,
    'catch_up': False,
    'run_ratio_min': 0.35,
    'run_ratio_max': 0.65,
    'min_songs_per_run': 100,
    'max_songs_per_run': 240,
    'scrobble_seconds': 70,
    'scrobble_jitter_seconds': 8,
    'sleep_range': [30, 60],
    'sleep_long_pause_prob': 0.12,
    'sleep_long_pause_range': [120, 300],
    'slot_overlap_seconds': 0,
    'max_run_minutes': 0,
    'hard_exit_buffer_minutes': 10,
    'qr_timeout': 180,
    'qr_poll_interval': 1,      # 扫码结果每秒轮询一次
    'code_timeout': 180,        # 等用户填验证码的超时（秒）
    'code_poll_interval': 1,    # 每秒读一次 account.json 里的 code
    'max_workers': 0,           # 多账号并发数，0=按账号数
    'request_timeout': 15,
    'notify_timeout': 15,
    'scrobble_path': '/scrobble/v1',
}


# ---------------------------- 异常类型 ----------------------------
class NetworkError(Exception):
    """断网 / 连接失败 / 超时 —— 与「未登录」严格区分"""


class ApiError(Exception):
    """接口返回异常（HTTP 通，但业务码或格式不对）"""


class AuthError(ApiError):
    """登录态失效，且无法自动恢复（例如扫码登录的 cookie 不支持刷新）"""


class LoginTimeout(Exception):
    """二维码/验证码登录超时"""


class SlotTimeout(Exception):
    """单次运行超出配额块的时间预算 —— 主动收尾，避免青龙一直显示「执行中」"""


class ConfigError(Exception):
    """配置缺失或配置项非法"""


# ---------------------------- 日志 ----------------------------
_quiet = False


def set_quiet(value):
    global _quiet
    _quiet = bool(value)


def log(msg):
    if _quiet:
        return
    print('[{}] {}'.format(datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg), flush=True)


# ---------------------------- 数据目录 / JSON 读写 ----------------------------
def ensure_data_dir():
    """确保数据目录存在，并把根目录遗留的旧数据文件移进来。

    返回本次移动的文件名列表；任何一步失败都只记日志，不影响主流程。
    """
    moved = []
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            os.chmod(DATA_DIR, 0o700)      # 目录里有 cookie，只给当前用户
        except OSError:
            pass
    except OSError as e:
        log('无法创建数据目录 {}：{}'.format(DATA_DIR, e))
        return moved

    for name in LEGACY_NAMES:
        legacy = os.path.join(BASE_DIR, name)
        target = os.path.join(DATA_DIR, name)
        if os.path.abspath(legacy) == os.path.abspath(target):
            continue
        if not os.path.exists(legacy) or os.path.exists(target):
            continue
        try:
            shutil.move(legacy, target)
            try:
                os.chmod(target, 0o600)    # 迁移过来的文件收紧权限
            except OSError:
                pass
            moved.append(name)
        except OSError as e:
            log('旧文件 {} 移入数据目录失败：{}'.format(name, e))
    if moved:
        log('已把 {} 移动到数据目录 {}'.format('、'.join(moved), DATA_DIR))
    return moved


def read_json(path, default):
    """读 JSON；文件不存在 / 解析失败 / 类型对不上都返回 default（dict / list 都支持）。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    if default is None:                 # 不做类型约束，原样返回
        return data
    if isinstance(default, dict) and isinstance(data, dict):
        return data
    if isinstance(default, list) and isinstance(data, list):
        return data
    return default


def write_json(path, data, mode=0o600):
    """原子写 JSON；数据文件里有 cookie，默认 0600。"""
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


# ---------------------------- 配置 ----------------------------
def normalize_playlists(raw):
    """歌单 ID 归一化：支持单个字符串 / 逗号分隔字符串 / 数组 / 数字，去空去重保序。"""
    if isinstance(raw, str):
        raw = str(raw).replace('，', ',').split(',')
    out = []
    for item in raw or []:
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out


class Config(object):
    """运行参数容器；每个字段的默认值见 DEFAULTS。"""

    def __init__(self, **kw):
        for key in DEFAULTS:
            setattr(self, key, copy.deepcopy(DEFAULTS[key]))
        self.update(kw)

    def update(self, data):
        for key, value in data.items():
            if key in DEFAULTS:
                setattr(self, key, copy.deepcopy(value))


CFG = Config()


def _as_int(key, value, minimum=None):
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise ConfigError('config 里的 {} 必须是整数，当前值：{!r}'.format(key, value))
    if minimum is not None and num < minimum:
        raise ConfigError('config 里的 {} 不能小于 {}，当前值：{}'.format(key, minimum, num))
    return num


def _as_float(key, value, low=None, high=None):
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ConfigError('config 里的 {} 必须是数字，当前值：{!r}'.format(key, value))
    if low is not None and num < low:
        raise ConfigError('config 里的 {} 不能小于 {}，当前值：{}'.format(key, low, num))
    if high is not None and num > high:
        raise ConfigError('config 里的 {} 不能大于 {}，当前值：{}'.format(key, high, num))
    return num


def _as_pair(key, value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError('config 里的 {} 必须是 [最小值, 最大值] 两个数字'.format(key))
    low = _as_float('{}[0]'.format(key), value[0], 0)
    high = _as_float('{}[1]'.format(key), value[1], 0)
    if high < low:
        raise ConfigError('config 里的 {} 第二个值不能小于第一个值'.format(key))
    return (low, high)


def _as_bool(key, value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'on'):
        return True
    if text in ('0', 'false', 'no', 'off', ''):
        return False
    raise ConfigError('config 里的 {} 必须是 true/false'.format(key))


def _build(data, path):
    """把 config 段整理成「只包含已配置项」的 dict，并做合法性校验。"""
    unknown = sorted(k for k in data if k not in DEFAULTS)
    if unknown:
        log('配置文件 {} 中未识别的键已忽略：{}'.format(path, ', '.join(unknown)))

    out = {}

    api_url = str(data.get('api') or '').strip()
    if not api_url:
        raise ConfigError('{} 缺少 api：请填写网易云 API 服务地址，例如 http://127.0.0.1:3000'.format(path))
    if not api_url.lower().startswith(('http://', 'https://')):
        raise ConfigError('{} 的 api 必须以 http:// 或 https:// 开头，当前：{}'.format(path, api_url))
    out['api'] = api_url

    playlists = normalize_playlists(data.get('user_sl_id'))
    if not playlists:
        raise ConfigError('{} 缺少 user_sl_id：歌单 ID，字符串或数组，例如 ["1234567890"]'.format(path))
    for index, pid in enumerate(playlists):
        if '{{' in pid or '}}' in pid:
            raise ConfigError('{} 的 user_sl_id 第 {} 项仍是占位符：{}，请填入真实歌单 ID'.format(
                path, index + 1, pid))
    out['user_sl_id'] = playlists

    if 'sckey' in data:
        out['sckey'] = str(data.get('sckey') or '').strip()
    if 'catch_up' in data:
        out['catch_up'] = _as_bool('catch_up', data['catch_up'])

    int_rules = (
        ('daily_target_base', 0),
        ('daily_target_jitter', 0),
        ('slots_per_day', 1),
        ('min_songs_per_run', 1),
        ('max_songs_per_run', 1),
        ('scrobble_seconds', 0),
        ('scrobble_jitter_seconds', 0),
        ('slot_overlap_seconds', 0),
        ('max_run_minutes', 0),
        ('hard_exit_buffer_minutes', 0),
        ('qr_timeout', 1),
        ('qr_poll_interval', 1),
        ('code_timeout', 1),
        ('code_poll_interval', 1),
        ('max_workers', 0),
        ('request_timeout', 1),
        ('notify_timeout', 1),
    )
    for key, minimum in int_rules:
        if key in data:
            out[key] = _as_int(key, data[key], minimum)

    if 'run_ratio_min' in data:
        out['run_ratio_min'] = _as_float('run_ratio_min', data['run_ratio_min'], 0, 1)
    if 'run_ratio_max' in data:
        out['run_ratio_max'] = _as_float('run_ratio_max', data['run_ratio_max'], 0, 1)
    if 'sleep_long_pause_prob' in data:
        out['sleep_long_pause_prob'] = _as_float(
            'sleep_long_pause_prob', data['sleep_long_pause_prob'], 0, 1)
    if 'sleep_range' in data:
        out['sleep_range'] = _as_pair('sleep_range', data['sleep_range'])
    if 'sleep_long_pause_range' in data:
        out['sleep_long_pause_range'] = _as_pair(
            'sleep_long_pause_range', data['sleep_long_pause_range'])
    if 'scrobble_path' in data:
        scrobble_path = str(data['scrobble_path'] or '').strip()
        if not scrobble_path.startswith('/'):
            raise ConfigError('config 里的 scrobble_path 必须以 / 开头，当前：{}'.format(scrobble_path))
        out['scrobble_path'] = scrobble_path

    merged = dict(DEFAULTS)
    merged.update(out)
    if merged['max_songs_per_run'] < merged['min_songs_per_run']:
        raise ConfigError('max_songs_per_run({}) 不能小于 min_songs_per_run({})'.format(
            merged['max_songs_per_run'], merged['min_songs_per_run']))
    if not (0 < merged['run_ratio_min'] < merged['run_ratio_max'] <= 1):
        raise ConfigError('run_ratio_min / run_ratio_max 需满足 0 < min < max <= 1，当前 {} / {}'.format(
            merged['run_ratio_min'], merged['run_ratio_max']))
    return out


def _read_config_override(path):
    """读取 NCM_CONFIG / --config 指向的独立配置文件（进阶用法）。"""
    if not os.path.exists(path):
        raise ConfigError('未找到配置文件：{}\n请检查 NCM_CONFIG / --config 指向的路径'.format(path))
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError('配置文件读取失败：{}（{}）'.format(path, e))
    if not isinstance(data, dict):
        raise ConfigError('配置文件根节点必须是 JSON 对象：{}'.format(path))
    return data


def load_config(path=None):
    """读取配置并就地更新全局 CFG；失败抛 ConfigError。

    默认读数据目录里的 data/config.json；NCM_CONFIG 或 --config 可临时指向独立文件。
    """
    ensure_data_dir()
    override = path or os.environ.get('NCM_CONFIG')
    if override:
        data = _read_config_override(override)
        CFG.update(_build(data, override))
        return CFG

    if not os.path.exists(CONFIG_FILE):
        raise ConfigError(
            '未找到配置文件：{}\n'
            '请先复制模板：cp data/config.example.json data/config.json，再填写 api 与 user_sl_id'.format(
                CONFIG_FILE))
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError('配置文件读取失败：{}（{}）'.format(CONFIG_FILE, e))
    if not isinstance(data, dict):
        raise ConfigError('配置文件根节点必须是 JSON 对象：{}'.format(CONFIG_FILE))
    CFG.update(_build(data, CONFIG_FILE))
    return CFG


# ---------------------------- 运行时长控制 ----------------------------
sched = {'deadline': None, 'reason': ''}   # 单次运行的时间预算（看门狗）


def compute_budget(target):
    """算出本次运行的时间上限（秒）。target 为本次要补的歌曲数。"""
    cfg = CFG
    if cfg.max_run_minutes and cfg.max_run_minutes > 0:
        return int(cfg.max_run_minutes * 60)
    base_wait = (cfg.sleep_range[0] + cfg.sleep_range[1]) / 2.0
    # 计入偶发长停顿的期望值，避免长停顿把预算打爆
    if cfg.sleep_long_pause_prob:
        long_wait = (cfg.sleep_long_pause_range[0] + cfg.sleep_long_pause_range[1]) / 2.0
        base_wait = (base_wait * (1 - cfg.sleep_long_pause_prob)
                     + long_wait * cfg.sleep_long_pause_prob)
    per_song = base_wait + max(0, cfg.slot_overlap_seconds) + 2
    # 预留：登录/签到/歌单拉取/推送，约 3 分钟
    return int(max(180, target * per_song + 180))


def start_watchdog(seconds, reason):
    """到点强制退出进程 —— 兜底，保证青龙任务一定会结束。
    主流程正常情况下会自己收尾；这里只处理「卡在某个请求/异常分支」的情况。
    """
    sched['deadline'] = time.time() + seconds
    sched['reason'] = reason

    def _kill():
        log('【看门狗】已超过运行上限 {} 分钟（{}），强制结束进程'.format(
            int(seconds / 60.0), reason))
        try:
            # 延迟导入，避免 common <-> run.state 循环依赖；
            # 状态文件在 run 模块里，强制退出前尽量把断点落一次盘。
            from run import state
            state.save_state(state.load_state())
        except Exception:
            pass
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(2)

    t = threading.Timer(max(1, seconds), _kill)
    t.daemon = True
    t.start()
    return t


def time_left():
    d = sched.get('deadline')
    return None if d is None else max(0.0, d - time.time())


def budget_exhausted():
    left = time_left()
    return left is not None and left <= 0


def interruptible_sleep(seconds):
    """可被时间预算打断的 sleep：到点就不再等，直接返回 False 让主流程收尾。"""
    left = time_left()
    if left is None:
        time.sleep(seconds)
        return True
    if left <= 0:
        return False
    time.sleep(min(seconds, left))
    return time_left() > 0
