#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网易云听歌（青龙任务脚本）。

cron: 30 9,17 * * *
new Env('网易云听歌')

用法（task run.py 后面可以带参数，原样透传）：
    task run.py                    # 跑数据目录 ncm_cookie.json 里的全部账号
    task run.py --user 123456      # 只跑指定 userId（逗号分隔）
    task run.py --status           # 打印每个账号的断点与今日进度
    task run.py --selftest         # 21 个自检用例，不联网
    task run.py --reset            # 清除登录态与断点（保留 config / account）
    task run.py --migrate          # 把项目根目录的旧数据文件移入 data/ 目录
    task run.py --config X         # 指定独立配置文件（进阶）

真正的逻辑在 run/ 模块（run/main.py、run/player.py、run/state.py），
这个文件只负责把参数交给 run.main.cli，方便青龙 / cron 直接调用。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from run.main import cli

if __name__ == '__main__':
    sys.exit(cli(sys.argv))
