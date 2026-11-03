# -*- coding: utf-8 -*-
"""python -m login 入口。"""
import sys

from login.cli import cli

if __name__ == '__main__':
    sys.exit(cli(sys.argv))
