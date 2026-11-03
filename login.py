#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网易云登录（青龙任务脚本：扫码 / 手机验证码）。

用法（task login.py 后面可以带参数，原样透传）：
    task login.py --type=qrcode                          # 扫码登录
    task login.py --type=code --account=<手机号>          # 手机验证码登录
    task login.py --type=code --account=<手机号> --legacy # 服务端没合 PR #294 时用旧接口
    task login.py --api http://127.0.0.1:3000            # 临时覆盖 API 地址

登录态写入数据目录的 ncm_cookie.json；验证码账号列表在数据目录的
account.json（青龙也可以配置环境变量 account.json）。
真正的逻辑在 login/ 模块（login/flow.py、login/cli.py），
这个文件只负责把参数交给 login.cli.cli，方便青龙 / cron 直接调用。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from login.cli import cli

if __name__ == '__main__':
    sys.exit(cli(sys.argv))
