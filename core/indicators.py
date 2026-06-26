"""
技术指标计算模块
=============
职责：VWAP、EMA、ATR 等指标的计算
"""

from datetime import datetime, timedelta
from typing import Optional


def parse_bar_time(bar: dict) -> Optional[datetime]:
    """从bar dict解析时间"""
    ts = bar.get('time', bar.get('t', ''))
    if isinstance(ts, datetime):
        return ts
    for offset in ['-05:00', '-04:00', '-06:00', '-07:00']:
        if ts.endswith(offset):
            ts = ts[:-len(offset)]
            break
    try:
        return datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def get_bar_date(bar: dict) -> Optional[str]:
    """获取bar的日期字符串"""
    dt = parse_bar_time(bar)
    return dt.strftime('%Y-%m-%d') if dt else None


# ── VWAP ──────────────────────────────────────────────────────

def compute_vwap(bars: list[dict]) -> list[dict]:
    """计算每日VWAP
    
    返回: [{'time': datetime, 'value': float}, ...]
    每天从第一个bar开始重新计算VWAP
    """
    if not bars:
        return []

    # 按日分组
    daily_bars = {}  # date_str -> [(price*vol, vol)]
    for b in bars:
        date_str = get_bar_date(b)
        if not date_str:
            continue
        if date_str not in daily_bars:
            daily_bars[date_str] = []
        typical_price = (b['high'] + b['low'] + b['close']) / 3
        vol = b.get('volume', 0)
        daily_bars[date_str].append({
            'time': parse_bar_time(b),
            'tp': typical_price,
            'vol': vol,
        })

    # 逐bar计算累积VWAP
    vwap_values = []
    for date_str, day_bars in sorted(daily_bars.items()):
        cum_pv = 0.0
        cum_vol = 0.0
        for db in day_bars:
            cum_pv += db['tp'] * db['vol']
            cum_vol += db['vol']
            if cum_vol > 0:
                vwap_values.append({
                    'time': db['time'],
                    'value': round(cum_pv / cum_vol, 2),
                })

    # 如果volume都为0，用简单平均
    if not vwap_values:
        for b in bars:
            dt = parse_bar_time(b)
            if dt:
                vwap_values.append({
                    'time': dt,
                    'value': round((b['high'] + b['low'] + b['close']) / 3, 2),
                })

    return vwap_values


def compute_vwap_with_bands(bars: list[dict], std_mult: float = 2.0) -> dict:
    """计算VWAP + 上下带
    
    返回: {'vwap': [...], 'upper': [...], 'lower': [...]}
    """
    vwap_data = compute_vwap(bars)

    if not vwap_data:
        return {'vwap': [], 'upper': [], 'lower': []}

    # 按日计算标准差
    daily_stats = {}
    for b in bars:
        dt = parse_bar_time(b)
        date_str = dt.strftime('%Y-%m-%d') if dt else None
        if not date_str:
            continue
        if date_str not in daily_stats:
            daily_stats[date_str] = {'prices': [], 'tps': []}
        daily_stats[date_str]['prices'].append(b['close'])
        daily_stats[date_str]['tps'].append(
            (b['high'] + b['low'] + b['close']) / 3
        )

    # 计算每日标准差
    import math
    bands = {'vwap': vwap_data, 'upper': [], 'lower': []}

    current_date = None
    current_vwap = None
    for v in vwap_data:
        dt = v['time']
        date_str = dt.strftime('%Y-%m-%d') if isinstance(dt, datetime) else str(dt)
        if date_str != current_date:
            current_date = date_str
            # 用当日的VWAP值
            current_vwap = v['value']
            stats = daily_stats.get(date_str, None)
            if stats and len(stats['tps']) > 0:
                tps = stats['tps']
                mean = sum(tps) / len(tps)
                variance = sum((tp - mean) ** 2 for tp in tps) / len(tps)
                std = math.sqrt(variance)
            else:
                std = 0.5  # 兜底值

        bands['upper'].append({
            'time': dt,
            'value': round(current_vwap + std_mult * std, 2),
        })
        bands['lower'].append({
            'time': dt,
            'value': round(current_vwap - std_mult * std, 2),
        })

    return bands


# ── EMA ───────────────────────────────────────────────────────

def compute_ema(values: list[float], period: int) -> list[Optional[float]]:
    """计算EMA
    
    参数:
        values: 价格序列（按时间顺序）
        period: 周期
    返回:
        与输入等长的列表，前 period-1 个为 None
    """
    if not values or period <= 0:
        return [None] * len(values)

    multiplier = 2 / (period + 1)
    ema = [None] * len(values)

    # 第一个EMA用SMA初始化
    if len(values) >= period:
        ema[period - 1] = sum(values[:period]) / period
    else:
        return ema

    for i in range(period, len(values)):
        ema[i] = (values[i] - ema[i - 1]) * multiplier + ema[i - 1]

    return ema


def compute_ema_on_bars(bars: list[dict], period: int, field: str = 'close') -> list[dict]:
    """在K线上计算EMA
    
    返回: [{'time': datetime, 'value': float}, ...]
    """
    if not bars:
        return []

    prices = [b[field] for b in bars]
    ema_values = compute_ema(prices, period)

    result = []
    for i, b in enumerate(bars):
        if ema_values[i] is not None:
            dt = parse_bar_time(b)
            if dt:
                result.append({
                    'time': dt,
                    'value': round(ema_values[i], 2),
                })
    return result


def compute_multiple_emas(bars: list[dict], periods: list[int] = None) -> dict:
    """计算多条EMA线
    
    返回: {'ema_{period}': [{'time': ..., 'value': ...}, ...]}
    """
    if periods is None:
        periods = [9, 20, 50, 200]

    result = {}
    for period in periods:
        result[f'ema_{period}'] = compute_ema_on_bars(bars, period)
    return result


# ── ATR ───────────────────────────────────────────────────────

def compute_atr(bars: list[dict], period: int = 14) -> list[dict]:
    """计算ATR
    
    返回: [{'time': datetime, 'value': float}, ...]
    """
    if not bars or len(bars) < period + 1:
        return []

    result = []
    for i in range(period, len(bars)):
        tr_sum = 0.0
        for j in range(i - period + 1, i + 1):
            prev_close = bars[j - 1]['close']
            h = bars[j]['high']
            l = bars[j]['low']
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            tr_sum += tr
        dt = parse_bar_time(bars[i])
        if dt:
            result.append({
                'time': dt,
                'value': round(tr_sum / period, 2),
            })
    return result


# ── 其他辅助 ──────────────────────────────────────────────────

def compute_volume_profile(bars: list[dict]) -> list[dict]:
    """成交量剖面（按价格的成交量分布）
    
    返回: [{'price_zone': str, 'volume': float}, ...]
    """
    if not bars:
        return []

    # 按价格区间（每5点）聚合成交量
    from collections import defaultdict
    zones = defaultdict(float)
    step = 5.0

    for b in bars:
        low_zone = int(b['low'] / step) * step
        high_zone = int(b['high'] / step) * step
        vol_per_point = b.get('volume', 0) / max(b['high'] - b['low'], 0.01)
        # 将成交量分配到每个价格区
        p = low_zone
        while p <= high_zone:
            zones[p] += vol_per_point * step
            p += step

    sorted_zones = sorted(zones.items(), key=lambda x: -x[1])
    return [{'price_zone': f'{p:.0f}-{p+step:.0f}', 'volume': round(v, 0)}
            for p, v in sorted_zones[:30]]
