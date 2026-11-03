# -*- coding: utf-8 -*-
"""run 模块：主程序（自动听歌涨等级）。

负责单实例锁、多账号并发、每日签到（每天只做一次）、播放主循环、通知与自检；
登录不在这里做（见 login 模块），主程序只按数据目录的 ncm_cookie.json 干活。
共享底座见项目根目录 shared/（common.py / api.py）。

入口（青龙里就是 task run.py）：
    python run.py
    python -m run
"""

__all__ = ['main', 'player', 'selftest', 'state']
