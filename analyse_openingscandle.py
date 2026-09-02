from data_module import get_historical_candles
from atr_module import calculate_atr
from collections import defaultdict

for symbol in ['AAPL', 'MSFT', 'NVDA', 'JPM', 'XOM']:
    candles_15min = get_historical_candles(symbol, duration='20d', bar_size='15min')
    if not candles_15min:
        print(f'{symbol}: geen data')
        continue

    per_dag = defaultdict(list)
    for c in candles_15min:
        per_dag[c.timestamp.date()].append(c)

    opening_ranges = []
    for datum, dag_candles in sorted(per_dag.items()):
        dag_candles.sort(key=lambda c: c.timestamp)
        eerste = dag_candles[0]
        opening_ranges.append(eerste.range)

    if not opening_ranges:
        print(f'{symbol}: geen openingscandles gevonden')
        continue

    gemiddelde_opening_range = sum(opening_ranges) / len(opening_ranges)

    candles_daily = get_historical_candles(symbol, duration='20d', bar_size='1d')
    atr = calculate_atr(candles_daily)
    huidige_drempel = 0.25 * atr

    verhouding = gemiddelde_opening_range / huidige_drempel if huidige_drempel > 0 else 0

    print(f'{symbol}:')
    print(f'  Gemiddelde openingscandle-grootte (laatste {len(opening_ranges)} dagen): {gemiddelde_opening_range:.3f}')
    print(f'  Huidige drempel (0.25 x dag-ATR14={atr:.3f}): {huidige_drempel:.3f}')
    print(f'  Verhouding: {verhouding:.2f}x (>1.0 betekent: een GEMIDDELDE dag haalt de drempel al)')
    print()
