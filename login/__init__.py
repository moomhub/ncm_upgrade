# -*- coding: utf-8 -*-
"""login 模块：网易云扫码 / 验证码登录。

与主程序（run 模块）解耦：只负责把登录态写进数据目录的 ncm_cookie.json，
不参与听歌、签到等任务；两边共用的底座是项目根目录的 shared/（common.py / api.py）。

入口（青龙里就是 task login.py）：
    python login.py --type=qrcode
    python login.py --type=code --account=<手机号>
    python -m login --type=qrcode              # 包方式，等价
"""

__all__ = ['accounts', 'cli', 'flow']
