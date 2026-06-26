"""调试：直接调用回测引擎"""
import sys, json, time
sys.path.insert(0, '.')
sys.path.insert(0, '/home/benling/mes_ict')

from api.backtest_api import run_backtest

params = {
    'dataset': '2D',
    'capital': 2000,
    'risk_pct': 1,
    'min_fvg_gap': 0.5,
    'max_fvg_gap': 5,
    'sl_atr_mult': 1.5,
    'tp_rr': 2,
    'min_sl': 3,
    'max_sl': 15,
    'max_position': 5,
    'only_killzone': True,
    'trend_filter': 'none',
    'start_date': '',
    'end_date': '',
    'max_days': 0,
    'excluded_kz': '',
    'excluded_hours': '',
}

print(f"Starting backtest on dataset={params['dataset']}...")
t0 = time.time()
try:
    result = run_backtest(params)
    elapsed = time.time() - t0
    print(f'Success in {elapsed:.1f}s!')
    print(f'trades: {result.get("total_trades")}')
    print(f'pnl: ${result.get("total_pnl", 0):.2f}')
    if result.get('trades'):
        print(f'first trade: {json.dumps(result["trades"][0])}')
except Exception as e:
    elapsed = time.time() - t0
    print(f'Error after {elapsed:.1f}s: {e}')
    import traceback
    traceback.print_exc()
