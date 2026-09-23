"""Stock opportunity scanner.

Daily-bar equity scanner that ranks a universe of stocks by a multi-factor
score (trend, momentum, relative strength, entry timing, risk, volume) and
builds a concrete trade plan (entry, ATR stop, target, position size) for the
best candidates. Independent from the ES/NQ intraday `signal_engine`.

Analysis tool only: no broker connection, no orders, not financial advice.
"""
from .scanner import ScanResult, StockScanner

__all__ = ["ScanResult", "StockScanner"]
