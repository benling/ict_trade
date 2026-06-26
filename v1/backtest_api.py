#!/usr/bin/env python3
"""
MES/ES ICT 量化回测 Web API
============================
轻量级HTTP API：接收前端参数 → 执行回测 → 返回JSON结果

启动: python3 backtest_api.py
前端: http://localhost:8888
"""

import json
import os
import sys
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import Counter, OrderedDict
from typing import Optional

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ict_signals import Candle, detect_fvg, detect_order_blocks, detect_liquidity_zones, _find_swing_points

# ============================================================
# 数据加载
# ============================================================
DATA_DIR = os.path.expanduser("~/mes_ict/tws_data")


def load_data(dataset="3M") -> list[dict]:
    """加载数据文件"""
    fname = f"MES_202609_5mins_{dataset}.json"
    fpath = os.path.join(DATA_DIR, fname)
    if not os.path.exists(fpath):
        raise FileNotFoundError(f"数据文件不存在: {fpath}")
    with open(fpath) as f:
        bars = json.load(f)

    # 去重
    seen = set()
    unique = []
    for b in bars:
        t = b['time']
        if t not in seen:
            seen.add(t)
            unique.append(b)
    unique.sort(key=lambda b: b['time'])
    return unique


def bars_to_candles(bars: list[dict]) -> list[Candle]:
    """将dict bars转为Candle对象"""
    candles = []
    for b in bars:
        ts = b['time'].split('.')[0]
        if ts.endswith('-05:00'):
            ts = ts[:-6]
        elif ts.endswith('-04:00'):
            ts = ts[:-6]
        try:
            t = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        candles.append(Candle(
            time=t, open=b['open'], high=b['high'],
            low=b['low'], close=b['close'], volume=b.get('volume', 0)
        ))
    return candles


def utc_to_et(dt: datetime) -> datetime:
    """UTC → 美东时间 (UTC-4 EDT, UTC-5 EST)"""
    return dt - timedelta(hours=4)  # 简单处理，夏令时默认UTC-4


# ============================================================
# 回测引擎（前端交互版）
# ============================================================

class BacktestEngine:
    """回测引擎 — 支持参数化配置和Kill Zone排除"""

    def __init__(self, params: dict):
        self.params = params
        self.trades = []
        self.equity_curve = []

    def run(self, bars: list[dict]) -> dict:
        """运行回测"""
        candles = bars_to_candles(bars)
        if not candles:
            return {"error": "没有可用的K线数据"}

        # 解析参数
        p = self.params
        capital = float(p.get("capital", 2000))
        risk_pct = float(p.get("risk_pct", 1.0))
        min_fvg_gap = float(p.get("min_fvg_gap", 0.5))
        max_fvg_gap = float(p.get("max_fvg_gap", 5.0))
        sl_atr_mult = float(p.get("sl_atr_mult", 1.5))
        tp_rr = float(p.get("tp_rr", 2.0))
        min_sl = float(p.get("min_sl", 3.0))
        max_sl = float(p.get("max_sl", 15.0))
        max_pos = int(p.get("max_position", 5))
        only_killzone = p.get("only_killzone", "true") == "true"
        trend_filter = p.get("trend_filter", "none")  # none, follow, reverse

        # 时间过滤
        excluded_hours_raw = p.get("excluded_hours", "")
        excluded_hours = []
        for segment in excluded_hours_raw.split(","):
            segment = segment.strip()
            if not segment:
                continue
            parts = segment.split("-")
            if len(parts) == 2:
                try:
                    start = int(parts[0].strip().split(":")[0])
                    end = int(parts[1].strip().split(":")[0])
                    excluded_hours.append((start, end))
                except (ValueError, IndexError):
                    pass

        # 日期范围过滤
        start_date_str = p.get("start_date", "")
        end_date_str = p.get("end_date", "")
        start_date = None
        end_date = None
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            except ValueError:
                pass
        if end_date_str:
            try:
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
            except ValueError:
                pass

        # Kill Zone 定义（美东时间）
        # Asia: 2:00-5:00 ET, London: 7:00-10:00 ET, NY: 13:00-16:00 ET
        kill_zones = [
            (2, 0, 5, 0),    # Asia Kill Zone (London overlap)
            (7, 0, 10, 0),   # London Open / NY Pre-market
            (13, 0, 16, 0),  # NY Power Hour (NY open + first 3h)
            (19, 0, 23, 0),  # London Close / NY afternoon
        ]

        # Kill Zone 排除
        excluded_kz = p.get("excluded_kz", "")
        excluded_kz_list = [x.strip() for x in excluded_kz.split(",") if x.strip()]

        # 天数限制
        max_days_str = p.get("max_days", "")
        max_days = int(max_days_str) if max_days_str else None

        # 阶段1：检测日线趋势
        trend_detector = DayTrendDetector(float(p.get("day_trend_threshold", 0.25)))
        trend_detector.build(candles)

        # 阶段2：回测
        self.trades = []
        self.equity_curve = []
        equity = capital
        active_trade = None

        # 限制天数
        first_day = candles[0].time.strftime("%Y-%m-%d")
        cutoff_day = None
        if max_days:
            from datetime import timedelta
            first_dt = datetime.strptime(first_day, "%Y-%m-%d")
            cutoff_dt = first_dt + timedelta(days=max_days)
            cutoff_day = cutoff_dt.strftime("%Y-%m-%d")

        for i in range(60, len(candles)):
            c = candles[i]
            et_time = utc_to_et(c.time)
            et_hour = et_time.hour
            et_min = et_time.minute
            total_min = et_hour * 60 + et_min
            price = c.close
            current_date = c.time.strftime("%Y-%m-%d")

            # 日期范围过滤
            if start_date and c.time < start_date:
                continue
            if end_date and c.time > end_date.replace(hour=23, minute=59):
                continue

            # 天数限制
            if cutoff_day and current_date > cutoff_day:
                break

            # 小时段排除
            skip = False
            for sh, eh in excluded_hours:
                if sh <= et_hour < eh:
                    skip = True
                    break
            if skip:
                if active_trade:
                    self._manage_active(active_trade, c, i)
                continue

            # 判定是否在Kill Zone
            in_kz = False
            kz_names = ["asia", "london", "ny_power", "london_close"]
            active_kz = []
            for idx, (sh, sm, eh, em) in enumerate(kill_zones):
                start_m = sh * 60 + sm
                end_m = eh * 60 + em
                if start_m <= total_min <= end_m:
                    in_kz = True
                    active_kz.append(kz_names[idx])

            # 排除Kill Zone
            if excluded_kz_list and any(kz in excluded_kz_list for kz in active_kz):
                if active_trade:
                    self._manage_active(active_trade, c, i)
                continue

            # 管理持仓
            if active_trade:
                hit, exit_price, exit_reason = self._check_exit(active_trade, c)
                if hit:
                    pnl_pts = (exit_price - active_trade["entry_price"]) if active_trade["direction"] == "long" \
                        else (active_trade["entry_price"] - exit_price)
                    pnl_dol = pnl_pts * 5 * active_trade["quantity"]
                    active_trade["exit_price"] = exit_price
                    active_trade["exit_time"] = c.time.isoformat()
                    active_trade["exit_reason"] = exit_reason
                    active_trade["pnl_points"] = round(pnl_pts, 2)
                    active_trade["pnl_dollars"] = round(pnl_dol, 2)
                    equity += pnl_dol
                    self.trades.append(active_trade)
                    self.equity_curve.append({"time": c.time.isoformat(), "equity": round(equity, 2)})
                    active_trade = None
                continue

            # Kill Zone 过滤
            if only_killzone and not in_kz:
                continue

            # 构建分析
            window = candles[max(0, i - 60):i + 1]
            sh, sl = _find_swing_points(window, 3, 3, 0.10)
            fvgs = detect_fvg(window, min_fvg_gap)
            obs = detect_order_blocks(window)
            liq = detect_liquidity_zones(window, sh, sl, price)

            # 信号评分
            fvg_list = [{"type": f.direction, "top": f.top, "bottom": f.bottom, "gap": f.gap_size}
                       for f in fvgs if not f.filled and min_fvg_gap <= f.gap_size <= max_fvg_gap]

            if not fvg_list:
                continue

            # 日线趋势
            trend = trend_detector.get_trend(c.time)

            # 趋势过滤
            if trend_filter == "follow":
                if trend == "uptrend":
                    # 只接受 bullish FVG
                    fvg_list = [f for f in fvg_list if f["type"] == "bullish"]
                elif trend == "downtrend":
                    fvg_list = [f for f in fvg_list if f["type"] == "bearish"]
            elif trend_filter == "reverse":
                if trend == "uptrend":
                    fvg_list = [f for f in fvg_list if f["type"] == "bearish"]
                elif trend == "downtrend":
                    fvg_list = [f for f in fvg_list if f["type"] == "bullish"]

            if not fvg_list:
                continue

            # 计算ATR
            atr = self._calc_atr(candles, i)
            sl_pts = max(min_sl, min(atr * sl_atr_mult, max_sl))

            # 按信号质量排序找最佳FVG
            best_signal = None
            best_score = -999

            for f in fvg_list:
                sig = self._evaluate_fvg(f, price, sl_pts, tp_rr, min_sl)
                if sig:
                    # 评分：置信度 * 趋势一致性
                    trend_bonus = 1.0
                    if trend == "uptrend" and sig["direction"] == "long":
                        trend_bonus = 1.3
                    elif trend == "downtrend" and sig["direction"] == "short":
                        trend_bonus = 1.3
                    elif trend == "sideways":
                        trend_bonus = 1.0
                    else:
                        trend_bonus = 0.7

                    rr = sig["rr"]
                    score = rr * trend_bonus * 10

                    if score > best_score:
                        best_score = score
                        best_signal = sig
                        best_signal["trend"] = trend
                        best_signal["trend_bonus"] = trend_bonus

            if best_signal is None:
                continue

            # 计算仓位
            risk_dollar = risk_pct / 100.0 * equity
            risk_per_contract = abs(best_signal["entry_price"] - best_signal["sl"]) * 5
            qty = max(1, int(risk_dollar / risk_per_contract)) if risk_per_contract > 0 else 1
            qty = min(qty, max_pos)

            active_trade = {
                "entry_time": c.time.isoformat(),
                "entry_price": best_signal["entry_price"],
                "direction": best_signal["direction"],
                "sl": best_signal["sl"],
                "tp": best_signal["tp"],
                "quantity": qty,
                "reason": best_signal["reason"],
                "fvg_top": best_signal.get("fvg_top"),
                "fvg_bottom": best_signal.get("fvg_bottom"),
                "trend": trend,
            }
            self.equity_curve.append({"time": c.time.isoformat(), "equity": round(equity, 2)})

        # 收盘平仓
        if active_trade:
            last = candles[-1]
            pnl_pts = (last.close - active_trade["entry_price"]) if active_trade["direction"] == "long" \
                else (active_trade["entry_price"] - last.close)
            pnl_dol = pnl_pts * 5 * active_trade["quantity"]
            active_trade["exit_price"] = last.close
            active_trade["exit_time"] = last.time.isoformat()
            active_trade["exit_reason"] = "close"
            active_trade["pnl_points"] = round(pnl_pts, 2)
            active_trade["pnl_dollars"] = round(pnl_dol, 2)
            equity += pnl_dol
            self.trades.append(active_trade)
            self.equity_curve.append({"time": last.time.isoformat(), "equity": round(equity, 2)})

        return self._compute_metrics(capital, equity, candles)

    def _evaluate_fvg(self, f: dict, price: float, sl_pts: float,
                       tp_rr: float, min_sl: float) -> Optional[dict]:
        """评估单个FVG的交易机会"""
        top, bottom = f["top"], f["bottom"]
        ftype = f["type"]

        if ftype == "bullish":
            # 长: 价格回落到FVG区域内或下方
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
                "direction": "long",
                "entry_price": price,
                "sl": sl,
                "tp": tp,
                "rr": round(rr, 2),
                "reason": f"FVG多头回踩 {bottom:.1f}-{top:.1f}",
                "fvg_top": top,
                "fvg_bottom": bottom,
            }

        elif ftype == "bearish":
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
                "direction": "short",
                "entry_price": price,
                "sl": sl,
                "tp": tp,
                "rr": round(rr, 2),
                "reason": f"FVG空头反弹 {bottom:.1f}-{top:.1f}",
                "fvg_top": top,
                "fvg_bottom": bottom,
            }

        return None

    def _manage_active(self, trade: dict, candle: Candle, bar_idx: int):
        """管理活跃持仓（当不解进场新单时的持仓检查）"""
        hit, exit_price, exit_reason = self._check_exit(trade, candle)
        if hit:
            pnl_pts = (exit_price - trade["entry_price"]) if trade["direction"] == "long" \
                else (trade["entry_price"] - exit_price)
            pnl_dol = pnl_pts * 5 * trade["quantity"]
            trade["exit_price"] = exit_price
            trade["exit_time"] = candle.time.isoformat()
            trade["exit_reason"] = exit_reason
            trade["pnl_points"] = round(pnl_pts, 2)
            trade["pnl_dollars"] = round(pnl_dol, 2)
            if self.equity_curve:
                last_eq = self.equity_curve[-1]["equity"]
            else:
                last_eq = float(self.params.get("capital", 2000))
            self.equity_curve.append({"time": candle.time.isoformat(), "equity": round(last_eq + pnl_dol, 2)})
            self.trades.append(trade)

    @staticmethod
    def _check_exit(trade: dict, candle: Candle):
        """检查止损止盈"""
        sl, tp = trade["sl"], trade["tp"]
        if trade["direction"] == "long":
            if candle.low <= sl:
                return True, sl, "stop_loss"
            if candle.high >= tp:
                return True, tp, "take_profit"
        else:
            if candle.high >= sl:
                return True, sl, "stop_loss"
            if candle.low <= tp:
                return True, tp, "take_profit"
        return False, 0, ""

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
        """计算回测指标"""
        trades = [t for t in self.trades if "pnl_dollars" in t]
        total = len(trades)
        if total == 0:
            return {
                "total_trades": 0,
                "message": "没有产生任何交易，请调整参数",
                "capital": capital,
                "final_equity": capital,
                "total_pnl": 0,
                "return_pct": 0,
                "candles_count": len(candles),
                "equity_curve": [],
            }

        wins = [t for t in trades if t["pnl_dollars"] > 0]
        losses = [t for t in trades if t["pnl_dollars"] < 0]
        win_rate = len(wins) / total * 100

        total_pnl = sum(t["pnl_dollars"] for t in trades)
        avg_win = sum(t["pnl_dollars"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_dollars"] for t in losses) / len(losses) if losses else 0

        gross_win = sum(t["pnl_dollars"] for t in wins)
        gross_loss = abs(sum(t["pnl_dollars"] for t in losses)) if losses else 0
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else float('inf')

        # 最大回撤
        max_dd = 0
        peak = capital
        if self.equity_curve:
            for point in self.equity_curve:
                eq = point["equity"]
                if eq > peak:
                    peak = eq
                dd = (peak - eq) / peak * 100
                if dd > max_dd:
                    max_dd = dd

        # 胜败序列
        pnl_sequence = [t["pnl_dollars"] for t in trades]

        # 按方向统计
        by_direction = {}
        for t in trades:
            d = t["direction"]
            if d not in by_direction:
                by_direction[d] = {"trades": 0, "wins": 0, "pnl": 0}
            by_direction[d]["trades"] += 1
            by_direction[d]["pnl"] += t["pnl_dollars"]
            if t["pnl_dollars"] > 0:
                by_direction[d]["wins"] += 1

        # 按策略分析
        by_reason = {}
        for t in trades:
            key = "FVG多头" if "多头" in t.get("reason", "") else "FVG空头"
            if key not in by_reason:
                by_reason[key] = {"trades": 0, "wins": 0, "pnl": 0}
            by_reason[key]["trades"] += 1
            by_reason[key]["pnl"] += t["pnl_dollars"]
            if t["pnl_dollars"] > 0:
                by_reason[key]["wins"] += 1

        # 按时间分组（月/周）
        monthly_pnl = {}
        for t in trades:
            if "exit_time" in t and t["exit_time"]:
                month = t["exit_time"][:7]
                monthly_pnl.setdefault(month, 0)
                monthly_pnl[month] += t["pnl_dollars"]

        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": profit_factor,
            "max_drawdown_pct": round(max_dd, 2),
            "capital": capital,
            "final_equity": round(final_equity, 2),
            "return_pct": round((final_equity - capital) / capital * 100, 2),
            "by_direction": by_direction,
            "by_reason": by_reason,
            "monthly_pnl": monthly_pnl,
            "trades": trades[:200],  # 最多200笔交易
            "equity_curve": self.equity_curve,
            "candles_count": len(candles),
            "start_date": candles[0].time.isoformat() if candles else "",
            "end_date": candles[-1].time.isoformat() if candles else "",
        }


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


# ============================================================
# HTTP 服务
# ============================================================

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), 'frontend')
BACKTEST_PORT = int(os.environ.get("BACKTEST_PORT", 8888))


class BacktestHandler(BaseHTTPRequestHandler):
    """HTTP请求处理器"""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_frontend("index.html")
        elif path == "/api/datasets":
            self._send_json(self._list_datasets())
        elif path.startswith("/api/backtest"):
            self._handle_backtest(parsed.query)
        elif path.startswith("/api/data-preview"):
            self._handle_data_preview(parsed.query)
        else:
            # 尝试提供静态文件
            file_path = path.lstrip("/")
            self._serve_frontend(file_path)

    def _serve_frontend(self, filename):
        if not filename:
            filename = "index.html"
        filepath = os.path.join(FRONTEND_DIR, filename)
        if not os.path.exists(filepath):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        ext = os.path.splitext(filename)[1]
        content_types = {
            ".html": "text/html",
            ".css": "text/css",
            ".js": "application/javascript",
            ".json": "application/json",
            ".png": "image/png",
            ".svg": "image/svg+xml",
        }

        self.send_response(200)
        self.send_header("Content-Type", content_types.get(ext, "application/octet-stream"))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(filepath, "rb") as f:
            self.wfile.write(f.read())

    def _send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _send_error(self, msg, status=400):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode())

    @staticmethod
    def _list_datasets():
        files = sorted([f for f in os.listdir(DATA_DIR) if f.endswith(".json") and "5mins" in f])
        datasets = []
        for f in files:
            label = f.replace("MES_202609_5mins_", "").replace(".json", "")
            fpath = os.path.join(DATA_DIR, f)
            size_kb = os.path.getsize(fpath) // 1024
            datasets.append({"id": label, "name": f"MES 5min {label}", "size_kb": size_kb})
        return {"datasets": datasets}

    def _handle_backtest(self, query_str):
        params = parse_qs(query_str)
        flat = {k: v[0] for k, v in params.items()}

        dataset = flat.get("dataset", "3M")

        try:
            bars = load_data(dataset)
        except FileNotFoundError as e:
            self._send_error(str(e))
            return

        engine = BacktestEngine(flat)
        start = datetime.now()
        results = engine.run(bars)
        elapsed = (datetime.now() - start).total_seconds()
        results["elapsed_seconds"] = round(elapsed, 2)

        self._send_json(results)

    def _handle_data_preview(self, query_str):
        params = parse_qs(query_str)
        flat = {k: v[0] for k, v in params.items()}
        dataset = flat.get("dataset", "3M")
        limit = int(flat.get("limit", 500))

        try:
            bars = load_data(dataset)
        except FileNotFoundError as e:
            self._send_error(str(e))
            return

        # 均匀采样
        step = max(1, len(bars) // limit)
        sampled = [{
            "time": b["time"],
            "o": b["open"],
            "h": b["high"],
            "l": b["low"],
            "c": b["close"],
            "v": b.get("volume", 0),
        } for idx, b in enumerate(bars) if idx % step == 0]

        self._send_json({
            "total": len(bars),
            "returned": len(sampled),
            "bars": sampled,
            "start": bars[0]["time"],
            "end": bars[-1]["time"],
        })

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]} {args[1]} {args[2]}")


def main():
    print(f"""
╔══════════════════════════════════════════╗
║  MES ICT 回测可视化系统                   ║
║  Backtest Web Dashboard                  ║
║                                          ║
║  启动: http://localhost:{BACKTEST_PORT}           ║
║  数据: ~/mes_ict/tws_data/               ║
╚══════════════════════════════════════════╝
    """)

    os.makedirs(FRONTEND_DIR, exist_ok=True)

    server = HTTPServer(("0.0.0.0", BACKTEST_PORT), BacktestHandler)
    print(f"服务已启动: http://localhost:{BACKTEST_PORT}")
    print(f"按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
