"""
回测引擎模块（从原backtest_api.py迁移）
======================================
职责：ICT策略回测运行逻辑
"""

import json
import os
import sys
from datetime import datetime, timedelta
from collections import Counter, OrderedDict
from typing import Optional

# 确保所有路径
PROJECT_DIR = os.path.join(os.path.dirname(__file__), '..')
MES_DIR = os.path.expanduser('~/mes_ict')
for p in [PROJECT_DIR, MES_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from ict_signals import Candle, detect_fvg, detect_order_blocks, detect_liquidity_zones, _find_swing_points
from core.data_loader import load_raw_bars, parse_time


def bars_to_candles(bars: list[dict]) -> list[Candle]:
    """将dict bars转为Candle对象"""
    candles = []
    for b in bars:
        dt = parse_time(b['time'])
        if dt is None:
            continue
        candles.append(Candle(
            time=dt, open=b['open'], high=b['high'],
            low=b['low'], close=b['close'], volume=b.get('volume', 0)
        ))
    return candles


def utc_to_et(dt: datetime) -> datetime:
    """UTC → 美东时间"""
    return dt - timedelta(hours=4)


class DayTrendDetector:
    """日线趋势检测"""

    def __init__(self, threshold=0.25):
        self.threshold = threshold
        self._daily_trend = {}

    def build(self, candles: list[Candle]):
        daily = OrderedDict()
        prev_close = None
        for c in candles:
            day = c.time.strftime('%Y-%m-%d')
            if day not in daily:
                daily[day] = {'open': c.open, 'high': c.high,
                              'low': c.low, 'close': c.close, 'prev_close': prev_close}
                prev_close = c.close
            else:
                d = daily[day]
                d['high'] = max(d['high'], c.high)
                d['low'] = min(d['low'], c.low)
                d['close'] = c.close
            prev_close = c.close

        for day, info in daily.items():
            o, c = info['open'], info['close']
            pc = info.get('prev_close')
            day_chg = (c - o) / o * 100
            prev_chg = ((c - pc) / pc * 100) if pc else 0

            if day_chg > self.threshold and c > o:
                tr = 'uptrend'
            elif day_chg < -self.threshold and c < o:
                tr = 'downtrend'
            elif prev_chg > self.threshold * 1.2:
                tr = 'uptrend'
            elif prev_chg < -self.threshold * 1.2:
                tr = 'downtrend'
            else:
                tr = 'sideways'
            self._daily_trend[day] = tr

    def get_trend(self, dt: datetime) -> str:
        day = dt.strftime('%Y-%m-%d')
        return self._daily_trend.get(day, 'sideways')


class BacktestEngine:
    """回测引擎 — 支持参数化配置和Kill Zone排除"""

    def __init__(self, params: dict):
        self.params = params
        self.trades = []
        self.equity_curve = []

    def run(self, bars: list[dict]) -> dict:
        candles = bars_to_candles(bars)
        if not candles:
            return {'error': '没有可用的K线数据'}

        p = self.params
        capital = float(p.get('capital', 2000))
        risk_pct = float(p.get('risk_pct', 1.0))
        min_fvg_gap = float(p.get('min_fvg_gap', 0.5))
        max_fvg_gap = float(p.get('max_fvg_gap', 5.0))
        sl_atr_mult = float(p.get('sl_atr_mult', 1.5))
        tp_rr = float(p.get('tp_rr', 2.0))
        min_sl = float(p.get('min_sl', 3.0))
        max_sl = float(p.get('max_sl', 15.0))
        max_pos = int(p.get('max_position', 5))
        only_killzone = p.get('only_killzone', 'true') == 'true'
        trend_filter = p.get('trend_filter', 'none')

        excluded_hours_raw = p.get('excluded_hours', '')
        excluded_hours = []
        for segment in excluded_hours_raw.split(','):
            segment = segment.strip()
            if not segment:
                continue
            parts = segment.split('-')
            if len(parts) == 2:
                try:
                    start = int(parts[0].strip().split(':')[0])
                    end = int(parts[1].strip().split(':')[0])
                    excluded_hours.append((start, end))
                except (ValueError, IndexError):
                    pass

        start_date_str = p.get('start_date', '')
        end_date_str = p.get('end_date', '')
        start_date = None
        end_date = None
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            except ValueError:
                pass
        if end_date_str:
            try:
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
            except ValueError:
                pass

        kill_zones = [
            (2, 0, 5, 0),    # Asia Kill Zone
            (7, 0, 10, 0),   # London Open
            (13, 0, 16, 0),  # NY Power Hour
            (19, 0, 23, 0),  # London Close
        ]
        excluded_kz = p.get('excluded_kz', '')
        excluded_kz_list = [x.strip() for x in excluded_kz.split(',') if x.strip()]

        max_days_str = p.get('max_days', '')
        max_days = int(max_days_str) if max_days_str else None

        trend_detector = DayTrendDetector(float(p.get('day_trend_threshold', 0.25)))
        trend_detector.build(candles)

        self.trades = []
        self.equity_curve = []
        equity = capital
        active_trade = None

        first_day = candles[0].time.strftime('%Y-%m-%d')
        cutoff_day = None
        if max_days:
            first_dt = datetime.strptime(first_day, '%Y-%m-%d')
            cutoff_dt = first_dt + timedelta(days=max_days)
            cutoff_day = cutoff_dt.strftime('%Y-%m-%d')

        for i in range(60, len(candles)):
            c = candles[i]
            et_time = utc_to_et(c.time)
            et_hour = et_time.hour
            et_min = et_time.minute
            total_min = et_hour * 60 + et_min
            price = c.close
            current_date = c.time.strftime('%Y-%m-%d')

            if start_date and c.time < start_date:
                continue
            if end_date and c.time > end_date.replace(hour=23, minute=59):
                continue
            if cutoff_day and current_date > cutoff_day:
                break

            skip = False
            for sh, eh in excluded_hours:
                if sh <= et_hour < eh:
                    skip = True
                    break
            if skip:
                if active_trade:
                    self._manage_active(active_trade, c, i)
                continue

            in_kz = False
            kz_names = ['asia', 'london', 'ny_power', 'london_close']
            active_kz = []
            for idx, (sh, sm, eh, em) in enumerate(kill_zones):
                start_m = sh * 60 + sm
                end_m = eh * 60 + em
                if start_m <= total_min <= end_m:
                    in_kz = True
                    active_kz.append(kz_names[idx])

            if excluded_kz_list and any(kz in excluded_kz_list for kz in active_kz):
                if active_trade:
                    self._manage_active(active_trade, c, i)
                continue

            if active_trade:
                hit, exit_price, exit_reason = self._check_exit(active_trade, c)
                if hit:
                    pnl_pts = (exit_price - active_trade['entry_price']) if active_trade['direction'] == 'long' \
                        else (active_trade['entry_price'] - exit_price)
                    pnl_dol = pnl_pts * 5 * active_trade['quantity']
                    active_trade['exit_price'] = exit_price
                    active_trade['exit_time'] = c.time.isoformat()
                    active_trade['exit_reason'] = exit_reason
                    active_trade['pnl_points'] = round(pnl_pts, 2)
                    active_trade['pnl_dollars'] = round(pnl_dol, 2)
                    equity += pnl_dol
                    self.trades.append(active_trade)
                    self.equity_curve.append({'time': c.time.isoformat(), 'equity': round(equity, 2)})
                    active_trade = None
                continue

            if only_killzone and not in_kz:
                continue

            window = candles[max(0, i - 60):i + 1]
            sh, sl = _find_swing_points(window, 3, 3, 0.10)
            fvgs = detect_fvg(window, min_fvg_gap)
            obs = detect_order_blocks(window)
            liq = detect_liquidity_zones(window, sh, sl, price)

            fvg_list = [{'type': f.direction, 'top': f.top, 'bottom': f.bottom, 'gap': f.gap_size}
                        for f in fvgs if not f.filled and min_fvg_gap <= f.gap_size <= max_fvg_gap]

            if not fvg_list:
                continue

            trend = trend_detector.get_trend(c.time)

            if trend_filter == 'follow':
                if trend == 'uptrend':
                    fvg_list = [f for f in fvg_list if f['type'] == 'bullish']
                elif trend == 'downtrend':
                    fvg_list = [f for f in fvg_list if f['type'] == 'bearish']
            elif trend_filter == 'reverse':
                if trend == 'uptrend':
                    fvg_list = [f for f in fvg_list if f['type'] == 'bearish']
                elif trend == 'downtrend':
                    fvg_list = [f for f in fvg_list if f['type'] == 'bullish']

            if not fvg_list:
                continue

            atr = self._calc_atr(candles, i)
            sl_pts = max(min_sl, min(atr * sl_atr_mult, max_sl))

            best_signal = None
            best_score = -999

            for f in fvg_list:
                sig = self._evaluate_fvg(f, price, sl_pts, tp_rr, min_sl)
                if sig:
                    trend_bonus = 1.0
                    if trend == 'uptrend' and sig['direction'] == 'long':
                        trend_bonus = 1.3
                    elif trend == 'downtrend' and sig['direction'] == 'short':
                        trend_bonus = 1.3
                    elif trend == 'sideways':
                        trend_bonus = 1.0
                    else:
                        trend_bonus = 0.7

                    rr = sig['rr']
                    score = rr * trend_bonus * 10

                    if score > best_score:
                        best_score = score
                        best_signal = sig
                        best_signal['trend'] = trend
                        best_signal['trend_bonus'] = trend_bonus

            if best_signal is None:
                continue

            risk_dollar = risk_pct / 100.0 * equity
            risk_per_contract = abs(best_signal['entry_price'] - best_signal['sl']) * 5
            qty = max(1, int(risk_dollar / risk_per_contract)) if risk_per_contract > 0 else 1
            qty = min(qty, max_pos)

            active_trade = {
                'entry_time': c.time.isoformat(),
                'entry_price': best_signal['entry_price'],
                'direction': best_signal['direction'],
                'sl': best_signal['sl'],
                'tp': best_signal['tp'],
                'quantity': qty,
                'reason': best_signal['reason'],
                'fvg_top': best_signal.get('fvg_top'),
                'fvg_bottom': best_signal.get('fvg_bottom'),
                'trend': trend,
            }
            self.equity_curve.append({'time': c.time.isoformat(), 'equity': round(equity, 2)})

        if active_trade:
            last = candles[-1]
            pnl_pts = (last.close - active_trade['entry_price']) if active_trade['direction'] == 'long' \
                else (active_trade['entry_price'] - last.close)
            pnl_dol = pnl_pts * 5 * active_trade['quantity']
            active_trade['exit_price'] = last.close
            active_trade['exit_time'] = last.time.isoformat()
            active_trade['exit_reason'] = 'close'
            active_trade['pnl_points'] = round(pnl_pts, 2)
            active_trade['pnl_dollars'] = round(pnl_dol, 2)
            equity += pnl_dol
            self.trades.append(active_trade)
            self.equity_curve.append({'time': last.time.isoformat(), 'equity': round(equity, 2)})

        return self._compute_metrics(capital, equity, candles)

    def _evaluate_fvg(self, f: dict, price: float, sl_pts: float,
                       tp_rr: float, min_sl: float) -> Optional[dict]:
        top, bottom = f['top'], f['bottom']
        ftype = f['type']

        if ftype == 'bullish':
            fvg_mid = (top + bottom) / 2
            if not (bottom - 0.5 <= price <= fvg_mid + 0.3):
                return None
            sl = round(bottom - sl_pts, 2)
            if price - sl < min_sl:
                sl = round(bottom - min_sl, 2)
            tp = round(price + (price - sl) * tp_rr, 2)
            rr = (tp - price) / (price - sl) if price > sl else 0
            if rr < 0.8:
                return None
            return {
                'direction': 'long',
                'entry_price': price,
                'sl': sl,
                'tp': tp,
                'rr': round(rr, 2),
                'reason': f'FVG多头回踩 {bottom:.1f}-{top:.1f}',
                'fvg_top': top,
                'fvg_bottom': bottom,
            }

        elif ftype == 'bearish':
            fvg_mid = (top + bottom) / 2
            if not (fvg_mid - 0.3 <= price <= top + 0.5):
                return None
            sl = round(top + sl_pts, 2)
            if sl - price < min_sl:
                sl = round(top + min_sl, 2)
            tp = round(price - (sl - price) * tp_rr, 2)
            rr = (price - tp) / (sl - price) if sl > price else 0
            if rr < 0.8:
                return None
            return {
                'direction': 'short',
                'entry_price': price,
                'sl': sl,
                'tp': tp,
                'rr': round(rr, 2),
                'reason': f'FVG空头反弹 {bottom:.1f}-{top:.1f}',
                'fvg_top': top,
                'fvg_bottom': bottom,
            }

        return None

    def _manage_active(self, trade: dict, candle: Candle, bar_idx: int):
        hit, exit_price, exit_reason = self._check_exit(trade, candle)
        if hit:
            pnl_pts = (exit_price - trade['entry_price']) if trade['direction'] == 'long' \
                else (trade['entry_price'] - exit_price)
            pnl_dol = pnl_pts * 5 * trade['quantity']
            trade['exit_price'] = exit_price
            trade['exit_time'] = candle.time.isoformat()
            trade['exit_reason'] = exit_reason
            trade['pnl_points'] = round(pnl_pts, 2)
            trade['pnl_dollars'] = round(pnl_dol, 2)
            last_eq = self.equity_curve[-1]['equity'] if self.equity_curve else float(self.params.get('capital', 2000))
            self.equity_curve.append({'time': candle.time.isoformat(), 'equity': round(last_eq + pnl_dol, 2)})
            self.trades.append(trade)

    @staticmethod
    def _check_exit(trade: dict, candle: Candle):
        sl, tp = trade['sl'], trade['tp']
        if trade['direction'] == 'long':
            if candle.low <= sl:
                return True, sl, 'stop_loss'
            if candle.high >= tp:
                return True, tp, 'take_profit'
        else:
            if candle.high >= sl:
                return True, sl, 'stop_loss'
            if candle.low <= tp:
                return True, tp, 'take_profit'
        return False, 0, ''

    @staticmethod
    def _calc_atr(candles: list[Candle], i: int, period: int = 14) -> float:
        if i < period + 1:
            return 2.0
        tr_sum = 0.0
        for j in range(i - period + 1, i + 1):
            prev = candles[j - 1].close
            h = candles[j].high
            l = candles[j].low
            tr = max(h - l, abs(h - prev), abs(l - prev))
            tr_sum += tr
        return round(tr_sum / period, 2)

    def _compute_metrics(self, capital: float, final_equity: float, candles: list) -> dict:
        trades = [t for t in self.trades if 'pnl_dollars' in t]
        total = len(trades)
        if total == 0:
            return {
                'total_trades': 0,
                'message': '没有产生任何交易，请调整参数',
                'capital': capital,
                'final_equity': capital,
                'total_pnl': 0,
                'return_pct': 0,
                'candles_count': len(candles),
                'equity_curve': [],
            }

        wins = [t for t in trades if t['pnl_dollars'] > 0]
        losses = [t for t in trades if t['pnl_dollars'] < 0]
        win_rate = len(wins) / total * 100

        total_pnl = sum(t['pnl_dollars'] for t in trades)
        avg_win = sum(t['pnl_dollars'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['pnl_dollars'] for t in losses) / len(losses) if losses else 0

        gross_win = sum(t['pnl_dollars'] for t in wins)
        gross_loss = abs(sum(t['pnl_dollars'] for t in losses)) if losses else 0
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else float('inf')

        max_dd = 0
        peak = capital
        if self.equity_curve:
            for point in self.equity_curve:
                eq = point['equity']
                if eq > peak:
                    peak = eq
                dd = (peak - eq) / peak * 100
                if dd > max_dd:
                    max_dd = dd

        by_direction = {}
        for t in trades:
            d = t['direction']
            if d not in by_direction:
                by_direction[d] = {'trades': 0, 'wins': 0, 'pnl': 0}
            by_direction[d]['trades'] += 1
            by_direction[d]['pnl'] += t['pnl_dollars']
            if t['pnl_dollars'] > 0:
                by_direction[d]['wins'] += 1

        by_reason = {}
        for t in trades:
            key = 'FVG多头' if '多头' in t.get('reason', '') else 'FVG空头'
            if key not in by_reason:
                by_reason[key] = {'trades': 0, 'wins': 0, 'pnl': 0}
            by_reason[key]['trades'] += 1
            by_reason[key]['pnl'] += t['pnl_dollars']
            if t['pnl_dollars'] > 0:
                by_reason[key]['wins'] += 1

        monthly_pnl = {}
        for t in trades:
            if 'exit_time' in t and t['exit_time']:
                month = t['exit_time'][:7]
                monthly_pnl.setdefault(month, 0)
                monthly_pnl[month] += t['pnl_dollars']

        return {
            'total_trades': total,
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': round(win_rate, 1),
            'total_pnl': round(total_pnl, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': profit_factor,
            'max_drawdown_pct': round(max_dd, 2),
            'capital': capital,
            'final_equity': round(final_equity, 2),
            'return_pct': round((final_equity - capital) / capital * 100, 2),
            'by_direction': by_direction,
            'by_reason': by_reason,
            'monthly_pnl': monthly_pnl,
            'trades': trades[:200],
            'equity_curve': self.equity_curve,
            'candles_count': len(candles),
            'start_date': candles[0].time.isoformat() if candles else '',
            'end_date': candles[-1].time.isoformat() if candles else '',
        }


def run_backtest(params: dict) -> dict:
    """运行回测（API入口）"""
    dataset = params.get('dataset', '3M')
    bars = load_raw_bars(dataset)

    # 应用日期筛选
    start_date = params.get('start_date', '')
    end_date = params.get('end_date', '')
    if start_date or end_date:
        from core.data_loader import filter_bars_by_date
        bars = filter_bars_by_date(bars, start_date, end_date)

    engine = BacktestEngine(params)
    start = datetime.now()
    results = engine.run(bars)
    elapsed = (datetime.now() - start).total_seconds()
    results['elapsed_seconds'] = round(elapsed, 2)
    return results
