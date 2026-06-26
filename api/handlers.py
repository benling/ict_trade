"""
API 路由处理器
============
职责：HTTP路由分发、请求参数解析、JSON响应
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# 确保项目路径
PROJECT_DIR = os.path.join(os.path.dirname(__file__), '..')
MES_DIR = os.path.expanduser('~/mes_ict')
for p in [PROJECT_DIR, MES_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core import data_loader, indicators
from ict_signals import Candle, detect_fvg, detect_order_blocks, detect_liquidity_zones, _find_swing_points
from datetime import datetime

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'frontend')

CONTENT_TYPES = {
    '.html': 'text/html',
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.json': 'application/json',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
}


class BacktestHandler(BaseHTTPRequestHandler):
    """HTTP请求处理器 — 路由分发"""

    # ── 路由表 ──────────────────────────────────────────────
    ROUTES = {
        '/': 'index.html',
    }

    API_ROUTES = {
        '/api/datasets': 'handle_datasets',
        '/api/data-preview': 'handle_data_preview',
        '/api/bars': 'handle_bars',
        '/api/indicators': 'handle_indicators',
        '/api/filter-info': 'handle_filter_info',
        '/api/backtest': 'handle_backtest',
    }

    API_BACKTEST_ROUTES = {
        '/api/backtest': 'handle_backtest',
    }

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query

        # API 路由
        if path in self.API_ROUTES:
            handler_name = self.API_ROUTES[path]
            handler = getattr(self, handler_name, None)
            if handler:
                handler(query)
            else:
                self._send_error(f'未实现的API: {path}', 501)
            return

        # 静态文件路由
        if path == '/':
            self._serve_frontend('index.html')
        elif path.startswith('/api/'):
            self._send_error(f'未知API: {path}', 404)
        else:
            self._serve_frontend(path.lstrip('/'))

    # ── API Handlers ────────────────────────────────────────

    def handle_datasets(self, query_str):
        """GET /api/datasets — 列出可用数据集"""
        datasets = data_loader.list_datasets()
        self._send_json({'datasets': datasets})

    def handle_data_preview(self, query_str):
        """GET /api/data-preview — 数据预览（均匀采样）"""
        params = self._parse_query(query_str)
        dataset = params.get('dataset', '3M')
        limit = int(params.get('limit', '500'))

        try:
            bars = data_loader.load_raw_bars(dataset)
        except FileNotFoundError as e:
            self._send_error(str(e))
            return

        sampled = data_loader.sample_bars(bars, limit)
        start, end = data_loader.get_date_range(bars)
        start_str = start.isoformat() if start else ''
        end_str = end.isoformat() if end else ''

        self._send_json({
            'total': len(bars),
            'returned': len(sampled),
            'bars': sampled,
            'start': start_str,
            'end': end_str,
        })

    def handle_bars(self, query_str):
        """GET /api/bars — 获取K线数据（支持筛选）
        
        参数:
            dataset: 数据集ID (default: '3M')
            mode: 'all' | 'year-month' | 'day'
            year: 年 (当mode='year-month'时)
            month: 月 (当mode='year-month'时)
            date: 日期 YYYY-MM-DD (当mode='day'时)
            start_date: 开始日期 YYYY-MM-DD (当mode='all'时可选)
            end_date: 结束日期 YYYY-MM-DD (当mode='all'时可选)
        """
        params = self._parse_query(query_str)
        dataset = params.get('dataset', '3M')
        mode = params.get('mode', 'all')

        try:
            bars = data_loader.load_raw_bars(dataset)
        except FileNotFoundError as e:
            self._send_error(str(e))
            return

        total_bars = len(bars)

        # 根据模式筛选
        if mode == 'year-month':
            year = int(params.get('year', 0))
            month = int(params.get('month', 0))
            if year and month:
                bars = data_loader.filter_bars_by_year_month(bars, year, month)
        elif mode == 'day':
            date_str = params.get('date', '')
            if date_str:
                bars = data_loader.filter_bars_by_day(bars, date_str)
        elif mode == 'all':
            start_date = params.get('start_date', '')
            end_date = params.get('end_date', '')
            if start_date or end_date:
                bars = data_loader.filter_bars_by_date(bars, start_date, end_date)

        chart_bars = data_loader.format_bars_for_chart(bars)

        self._send_json({
            'total': total_bars,
            'returned': len(chart_bars),
            'bars': chart_bars,
            'start': chart_bars[0]['time'] if chart_bars else '',
            'end': chart_bars[-1]['time'] if chart_bars else '',
        })

    def handle_indicators(self, query_str):
        """GET /api/indicators — 计算技术指标
        
        参数:
            dataset: 数据集ID
            vwap: 'true' | 'false' (default: 'true')
            ema: 逗号分隔的周期列表 (default: '9,20,50,200')
            atr_period: ATR周期 (default: '')
            mode/year/month/date: 筛选模式（同/api/bars）
        """
        params = self._parse_query(query_str)
        dataset = params.get('dataset', '3M')

        try:
            bars = data_loader.load_raw_bars(dataset)
        except FileNotFoundError as e:
            self._send_error(str(e))
            return

        # 应用筛选
        mode = params.get('mode', 'all')
        if mode == 'year-month':
            year = int(params.get('year', 0))
            month = int(params.get('month', 0))
            if year and month:
                bars = data_loader.filter_bars_by_year_month(bars, year, month)
        elif mode == 'day':
            date_str = params.get('date', '')
            if date_str:
                bars = data_loader.filter_bars_by_day(bars, date_str)
        elif mode == 'all':
            start_date = params.get('start_date', '')
            end_date = params.get('end_date', '')
            if start_date or end_date:
                bars = data_loader.filter_bars_by_date(bars, start_date, end_date)

        result = {}

        # VWAP
        if params.get('vwap', 'true') == 'true':
            # VWAP 需要完整日数据，所以我们用原始（未筛选）数据计算
            # 但只在返回时trim到筛选范围
            result['vwap'] = self._trim_indicator_time(
                indicators.compute_vwap_with_bands(bars),
                bars
            )

        # EMA
        ema_periods_str = params.get('ema', '9,20,50,200')
        if ema_periods_str:
            periods = [int(p.strip()) for p in ema_periods_str.split(',') if p.strip()]
            result['emas'] = indicators.compute_multiple_emas(bars, periods)

        # ATR
        atr_period_str = params.get('atr_period', '')
        if atr_period_str:
            atr_period = int(atr_period_str)
            result['atr'] = indicators.compute_atr(bars, atr_period)

        self._send_json(result)

    def handle_filter_info(self, query_str):
        """GET /api/filter-info — 获取数据集的年月/日期层级信息
        
        参数:
            dataset: 数据集ID
        """
        params = self._parse_query(query_str)
        dataset = params.get('dataset', '3M')

        try:
            bars = data_loader.load_raw_bars(dataset)
        except FileNotFoundError as e:
            self._send_error(str(e))
            return

        years_months = data_loader.get_available_years_months(bars)
        days = data_loader.get_available_days(bars)
        start, end = data_loader.get_date_range(bars)

        self._send_json({
            'years_months': years_months,
            'days': days[:500],  # 最多500天
            'range': {
                'start': start.isoformat() if start else '',
                'end': end.isoformat() if end else '',
            },
        })

    def handle_backtest(self, query_str):
        """GET /api/backtest — 运行回测
        
        复用现有的 BacktestEngine
        """
        from api.backtest_api import run_backtest
        params = self._parse_query(query_str)
        try:
            result = run_backtest(params)
            self._send_json(result)
        except Exception as e:
            self._send_error(str(e))

    # ── 工具方法 ────────────────────────────────────────────

    def _trim_indicator_time(self, indicator_data: dict, bars: list[dict]) -> dict:
        """将指标数据裁剪到与bars相同的时间范围
        
        VWAP是在全部数据上计算的，但前端只显示了筛选后的bars，
        这会导致指标线超出显示范围。此函数不做裁剪，因为
        LightweightCharts会自动处理时间对齐。
        """
        return indicator_data

    @staticmethod
    def _parse_query(query_str: str) -> dict:
        """解析查询字符串为扁平字典"""
        parsed = parse_qs(query_str)
        return {k: v[0] for k, v in parsed.items()}

    def _serve_frontend(self, filename):
        if not filename:
            filename = 'index.html'
        filepath = os.path.join(FRONTEND_DIR, filename)
        if not os.path.exists(filepath):
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not Found')
            return

        ext = os.path.splitext(filename)[1]
        content_type = CONTENT_TYPES.get(ext, 'application/octet-stream')

        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        with open(filepath, 'rb') as f:
            self.wfile.write(f.read())

    def _send_json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _send_error(self, msg, status=400):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps({'error': msg}).encode())

    def log_message(self, fmt, *args):
        print(f'[HTTP] {args[0]} {args[1]} {args[2]}')
