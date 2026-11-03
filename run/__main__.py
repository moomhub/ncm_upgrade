# -*- coding: utf-8 -*-
"""python -m run 入口。"""
import sys

from run.main import cli

if __name__ == '__main__':
    sys.exit(cli(sys.argv))
