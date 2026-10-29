# -*- coding: utf-8 -*-
"""入口：python run.py（在哪个目录运行都行）"""
import os
import sys

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PKG_DIR)
for _p in (ROOT_DIR, PKG_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from pricewatch.app import main
except ImportError:                     # 直接在包目录里运行的情况
    from app import main

if __name__ == "__main__":
    main()