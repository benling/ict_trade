"""
数据加载模块
===========
职责：加载数据文件、去重、排序、日期范围筛选
"""

import json
import os
from datetime import datetime, timedelta
from typing import Optional

DATA_DIR = os.path.expanduser("~/mes_ict/tws_data")

# 时区偏移量（美东夏令时 UTC-4，美东标准时 UTC-5）
# 数据中标注的是 -05:00，实际是 CDST（Central Daylight Saving Time）
# 这里统一用 -5 偏移来解析
TZ_OFFSET = -5


def parse_time(time_str: str) -> Optional[datetime]:
    """解析带时区偏移的时间字符串"""
    ts = time_str.strip()
    # 去掉时区偏移部分: '2026-03-25 08:30:00-05:00' -> '2026-03-25 08:30:00'
    # 支持 -04:00 / -05:00 两种
    for offset in ['-05:00', '-04:00', '-06:00', '-07:00']:
        if ts.endswith(offset):
            ts = ts[:-len(offset)]
            break
    try:
        return datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def format_time(dt: datetime) -> str:
    """将datetime格式化为前端用的ISO格式"""
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


def list_datasets(data_dir: str = None) -> list[dict]:
    """列出可用的数据集"""
    if data_dir is None:
        data_dir = DATA_DIR
    files = sorted([f for f in os.listdir(data_dir) if f.endswith('.json') and '5mins' in f])
    datasets = []
    for f in files:
        label = f.replace('MES_202609_5mins_', '').replace('.json', '')
        fpath = os.path.join(data_dir, f)
        size_kb = os.path.getsize(fpath) // 1024
        datasets.append({'id': label, 'name': f'MES 5min {label}', 'size_kb': size_kb})
    return datasets


def load_raw_bars(dataset: str, data_dir: str = None) -> list[dict]:
    """从文件加载原始K线数据，去重排序后返回"""
    if data_dir is None:
        data_dir = DATA_DIR
    fname = f'MES_202609_5mins_{dataset}.json'
    fpath = os.path.join(data_dir, fname)
    if not os.path.exists(fpath):
        raise FileNotFoundError(f'数据文件不存在: {fpath}')

    with open(fpath) as f:
        bars = json.load(f)

    # 去重（按 time 字段）
    seen = set()
    unique = []
    for b in bars:
        t = b['time']
        if t not in seen:
            seen.add(t)
            unique.append(b)
    unique.sort(key=lambda b: b['time'])
    return unique


def get_date_range(bars: list[dict]) -> tuple[Optional[datetime], Optional[datetime]]:
    """获取K线数据的日期范围"""
    if not bars:
        return None, None
    start = parse_time(bars[0]['time'])
    end = parse_time(bars[-1]['time'])
    return start, end


def filter_bars_by_date(
    bars: list[dict],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """按日期范围筛选K线数据"""
    if not start_date and not end_date:
        return bars

    start_dt = None
    end_dt = None
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59)
        except ValueError:
            pass

    if not start_dt and not end_dt:
        return bars

    result = []
    for b in bars:
        t = parse_time(b['time'])
        if t is None:
            continue
        if start_dt and t < start_dt:
            continue
        if end_dt and t > end_dt:
            continue
        result.append(b)
    return result


def get_available_years_months(bars: list[dict]) -> list[dict]:
    """获取数据中可用的年月列表"""
    periods = {}
    for b in bars:
        t = parse_time(b['time'])
        if t is None:
            continue
        key = (t.year, t.month)
        if key not in periods:
            periods[key] = {'year': t.year, 'month': t.month, 'count': 0}
        periods[key]['count'] += 1
    result = sorted(periods.values(), key=lambda x: (x['year'], x['month']))
    return result


def get_available_days(bars: list[dict]) -> list[dict]:
    """获取数据中可用的日期列表"""
    days = {}
    for b in bars:
        t = parse_time(b['time'])
        if t is None:
            continue
        day_str = t.strftime('%Y-%m-%d')
        if day_str not in days:
            days[day_str] = {'date': day_str, 'count': 0}
        days[day_str]['count'] += 1
    result = sorted(days.values(), key=lambda x: x['date'])
    return result


def filter_bars_by_year_month(bars: list[dict], year: int, month: int) -> list[dict]:
    """按年月筛选K线"""
    result = []
    for b in bars:
        t = parse_time(b['time'])
        if t and t.year == year and t.month == month:
            result.append(b)
    return result


def filter_bars_by_day(bars: list[dict], date_str: str) -> list[dict]:
    """按具体日期筛选K线"""
    result = []
    for b in bars:
        t = parse_time(b['time'])
        if t and t.strftime('%Y-%m-%d') == date_str:
            result.append(b)
    return result


def sample_bars(bars: list[dict], limit: int = 500) -> list[dict]:
    """均匀采样K线（用于图表预览）"""
    if len(bars) <= limit:
        return [{
            'time': b['time'],
            'o': b['open'],
            'h': b['high'],
            'l': b['low'],
            'c': b['close'],
            'v': b.get('volume', 0),
        } for b in bars]
    step = max(1, len(bars) // limit)
    sampled = []
    for idx, b in enumerate(bars):
        if idx % step == 0:
            sampled.append({
                'time': b['time'],
                'o': b['open'],
                'h': b['high'],
                'l': b['low'],
                'c': b['close'],
                'v': b.get('volume', 0),
            })
    return sampled


def format_bars_for_chart(bars: list[dict]) -> list[dict]:
    """为前端LightweightCharts格式化K线数据
    
    LightweightCharts要求: {time: '2026-03-25', open, high, low, close}
    日线数据用 'YYYY-MM-DD'，分钟数据用 'YYYY-MM-DD HH:mm'
    """
    result = []
    for b in bars:
        dt = parse_time(b['time'])
        if dt is None:
            continue
        # LightweightCharts v5 支持 'YYYY-MM-DD HH:mm' 格式做分钟图
        # 注意：只有完整日期会显示为日线，带时间的会被当作 intraday
        time_str = dt.strftime('%Y-%m-%d %H:%M')
        result.append({
            'time': time_str,
            'open': b['open'],
            'high': b['high'],
            'low': b['low'],
            'close': b['close'],
            'volume': b.get('volume', 0),
        })
    return result
