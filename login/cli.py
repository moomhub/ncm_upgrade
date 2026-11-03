# -*- coding: utf-8 -*-
"""login 模块的命令行入口：解析参数 -> 调用 login.flow.run()。

用法（青龙里就是 task login.py）：
    python login.py --type=qrcode
    python login.py --type=code --account=<手机号>
    python -m login --type=code --account=<手机号> --api http://127.0.0.1:3000
"""
import argparse
import os
import sys

# 支持直接运行 login/cli.py：此时 sys.path[0] 是 login/，
# 需要把项目根目录补进来，才能 import shared 包和 login 包。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import common                                                              # noqa: E402
from shared.common import ApiError, ConfigError, LoginTimeout, NetworkError, log         # noqa: E402
from login.flow import run                                                # noqa: E402


def cli(argv=None):
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(description='网易云登录（扫码 / 验证码）')
    parser.add_argument('--type', choices=('qrcode', 'code'), default='qrcode',
                        help='登录方式，默认 qrcode')
    parser.add_argument('--account', help='验证码登录的手机号（对应 account.json 里的 phone）')
    parser.add_argument('--api', help='覆盖 data/config.json 里的 api')
    parser.add_argument('--timeout', type=int, help='登录超时秒数')
    parser.add_argument('--sca', default='', help='易盾滑块 validate（返回 8821 时用）')
    parser.add_argument('--legacy', action='store_true',
                        help='验证码登录退回旧接口（服务端没合 PR #294 时用）')
    parser.add_argument('--config', help='指定独立配置文件路径（进阶）')
    args = parser.parse_args(argv[1:])

    common.ensure_data_dir()
    try:
        common.load_config(args.config)
    except ConfigError as e:
        print('[配置错误] {}'.format(e), file=sys.stderr)
        return 3

    try:
        run(args.type, args.account, args.api, args.timeout, args.sca, args.legacy)
        return 0
    except LoginTimeout as e:
        log('登录超时：{}'.format(e))
        return 1
    except NetworkError as e:
        log('网络错误：{}'.format(e))
        return 1
    except (ApiError, ConfigError) as e:
        log('登录失败：{}'.format(e))
        return 1


if __name__ == '__main__':
    sys.exit(cli(sys.argv))
