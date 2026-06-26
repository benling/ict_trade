#!/usr/bin/env python3
"""
MES ICT 回测系统 — 主入口
==========================
启动命令: python3 main.py

模块结构:
  core/
    __init__.py       # 核心模块初始化
    data_loader.py    # 数据加载、筛选、格式化
    indicators.py     # 技术指标计算（VWAP, EMA, ATR）
  api/
    __init__.py       # API模块初始化
    handlers.py       # HTTP路由分发、请求处理
    backtest_api.py   # ICT策略回测引擎
  frontend/
    index.html        # 前端可视化界面
"""

import os
import sys
from http.server import HTTPServer

# 确保项目路径
sys.path.insert(0, os.path.dirname(__file__))

from api.handlers import BacktestHandler

PORT = int(os.environ.get('BACKTEST_PORT', 8888))


def main():
    print(f'''
╔═══════════════════════════════════════════╗
║   MES ICT 回测系统 v2                      ║
║   Backtest Web Dashboard                   ║
║                                            ║
║   访问: http://localhost:{PORT}                   ║
║   数据: ~/mes_ict/tws_data/                ║
║                                            ║
║   核心功能:                                ║
║   • K线图 (LightweightCharts)              ║
║   • VWAP / EMA (9,20,50,200)               ║
║   • 时间筛选 (年/月/日)                    ║
║   • ICT 策略回测 (FVG/OB/流动性)            ║
╚═══════════════════════════════════════════╝
    ''')

    server = HTTPServer(('0.0.0.0', PORT), BacktestHandler)
    print(f'→ 服务已启动: http://localhost:{PORT}')
    print(f'→ 按 Ctrl+C 停止')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n服务已停止')
        server.server_close()


if __name__ == '__main__':
    main()
