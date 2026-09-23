"""Stock scanner: indicators, factors, ranking, trade plan, providers, backtest."""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from stock_scanner import StockScanner
from stock_scanner.backtest import run_backtest
from stock_scanner.config import ScannerConfig
from stock_scanner.data import CsvDailyProvider, SyntheticDailyProvider, YahooDailyProvider
from stock_scanner.factors import compute_factors, timing_score, trend_state
from stock_scanner.indicators import atr, pct_return, rsi
from stock_scanner.report import to_console, to_json, to_markdown
from stock_scanner.scanner import AVOID, BUY, build_plan, rank_as_of


def _frame(close: np.ndarray, volume: float = 5e6, start: str = "2024-01-01") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(close))
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": np.full(len(close), volume)}, index=idx)


def _trend(n: int, daily: float, start: float = 100.0, noise: float = 0.004, seed: int = 0):
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(daily, noise, n)))


@pytest.fixture
def cfg() -> ScannerConfig:
    return ScannerConfig(universe=["UP", "FLAT", "DOWN"], benchmark="IDX")


@pytest.fixture
def histories() -> dict[str, pd.DataFrame]:
    n = 300
    return {
        "UP": _frame(_trend(n, 0.003, seed=1)),
        "FLAT": _frame(_trend(n, 0.0, seed=2)),
        "DOWN": _frame(_trend(n, -0.003, seed=3)),
        "IDX": _frame(_trend(n, 0.0, seed=4)),
    }


# -- indicators ----------------------------------------------------------------
def test_indicators_are_causal():
    df = _frame(_trend(120, 0.001, seed=9))
    full_rsi, full_atr = rsi(df["close"]), atr(df)
    cut = df.iloc[:80]
    pd.testing.assert_series_equal(rsi(cut["close"]), full_rsi.iloc[:80])
    pd.testing.assert_series_equal(atr(cut), full_atr.iloc[:80])


def test_rsi_extremes_and_pct_return():
    up = pd.Series(np.arange(1, 50, dtype=float))
    assert rsi(up).iloc[-1] == 100.0
    close = pd.Series([100.0] * 10 + [110.0])
    assert pct_return(close, 10) == pytest.approx(0.10)
    assert math.isnan(pct_return(close, 50))


# -- factors -------------------------------------------------------------------
def test_trend_state_and_timing():
    assert trend_state(110, 105, 100, 104) == 1.0
    assert trend_state(90, 95, 100, 96) == 0.0
    assert timing_score(0.5, 55) == 1.0
    assert timing_score(5.0, 85) < 0.2
    assert math.isnan(timing_score(float("nan"), 50))


def test_factors_uptrend(histories):
    f = compute_factors("UP", histories["UP"], histories["IDX"]["close"])
    assert f.trend == 1.0
    assert f.ret_6m > 0 and f.rel_strength_6m > 0
    assert f.close > f.sma50 > f.sma200


# -- ranking -------------------------------------------------------------------
def test_scan_ranks_uptrend_first_and_downtrend_last(cfg, histories):
    result = StockScanner(cfg).scan(histories)
    order = [o.ticker for o in result.opportunities]
    assert order[0] == "UP" and order[-1] == "DOWN"
    assert "IDX" not in order
    down = result.opportunities[-1]
    assert down.signal == AVOID and down.plan is None
    assert down.setup == "Tendencia bajista"
    assert 0 <= down.score <= result.opportunities[0].score <= 100


def test_filters_reject_illiquid_and_short_history(cfg, histories):
    histories = dict(histories)
    histories["FLAT"] = _frame(_trend(300, 0.0, seed=2), volume=10)   # ~$1k/day
    histories["DOWN"] = histories["DOWN"].iloc[-50:]
    result = StockScanner(cfg).scan(histories)
    assert set(result.rejected) == {"FLAT", "DOWN"}
    assert "liquidez" in result.rejected["FLAT"]
    assert "historial" in result.rejected["DOWN"]


def test_missing_ticker_is_rejected_not_crash(cfg, histories):
    del histories["FLAT"]
    result = StockScanner(cfg).scan(histories)
    assert result.rejected["FLAT"] == "sin datos"


def test_overbought_leader_is_not_a_buy(cfg, histories):
    n = 300
    parabolic = np.concatenate([_trend(n - 20, 0.002, seed=1), np.zeros(20)])
    parabolic[-20:] = parabolic[-21] * np.cumprod(np.full(20, 1.02))
    histories["UP"] = _frame(parabolic)
    top = StockScanner(cfg).scan(histories).opportunities[0]
    assert top.ticker == "UP"
    assert top.factors.rsi14 >= 75
    assert top.signal != BUY and top.setup == "Sobreextendida"


# -- trade plan ----------------------------------------------------------------
def test_plan_sizing_respects_risk_and_position_cap(cfg, histories):
    f = compute_factors("UP", histories["UP"], histories["IDX"]["close"])
    plan = build_plan(f, cfg.risk)
    assert plan.stop == pytest.approx(f.close - 2 * f.atr14)
    assert plan.target == pytest.approx(f.close + 2 * (f.close - plan.stop))
    assert plan.capital_at_risk <= cfg.risk.capital * cfg.risk.risk_per_trade_pct / 100 + 1e-9
    assert plan.position_value <= cfg.risk.capital * cfg.risk.max_position_pct / 100 + 1e-9
    assert plan.shares >= 1


def test_weights_are_normalized_and_validated():
    c = ScannerConfig(weights={"momentum": 2, "trend": 2})
    assert sum(c.weights.values()) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        ScannerConfig(weights={"astrology": 1})


def test_default_yaml_loads():
    c = ScannerConfig.load()
    assert len(c.universe) > 50 and c.benchmark == "SPY"
    assert "BRK.B" in c.universe


# -- providers -----------------------------------------------------------------
def _yahoo_payload(n: int = 3) -> bytes:
    ts = [1_700_000_000 + 86_400 * i for i in range(n)]
    close = [100.0, 102.0, None][:n]
    return json.dumps({"chart": {"error": None, "result": [{
        "timestamp": ts,
        "indicators": {
            "quote": [{"open": [99.0, 101.0, None], "high": [101.0, 103.0, None],
                       "low": [98.0, 100.0, None], "close": close, "volume": [1e6, 2e6, None]}],
            "adjclose": [{"adjclose": [50.0, 51.0, None]}],
        }}]}}).encode()


def test_yahoo_provider_parses_and_adjusts():
    seen = []
    prov = YahooDailyProvider(transport=lambda url, t: seen.append(url) or _yahoo_payload())
    df = prov.history("BRK.B")
    assert "BRK-B" in seen[0] and "interval=1d" in seen[0]
    assert len(df) == 2                                   # null bar dropped
    assert df["close"].tolist() == pytest.approx([50.0, 51.0])
    assert df["high"].iloc[0] == pytest.approx(101.0 * 0.5)
    assert df["volume"].iloc[1] == 2e6


def test_yahoo_provider_errors_become_lookup_errors():
    def boom(url, t):
        raise OSError("network down")
    with pytest.raises(LookupError):
        YahooDailyProvider(transport=boom).history("AAPL")
    err = json.dumps({"chart": {"error": {"code": "Not Found"}, "result": None}}).encode()
    with pytest.raises(LookupError):
        YahooDailyProvider(transport=lambda u, t: err).history("ZZZZ")
    assert YahooDailyProvider(transport=boom).history_many(["AAPL", "MSFT"]) == {}


def test_csv_provider(tmp_path):
    df = _frame(_trend(5, 0.0))
    df.index.name = "Date"
    df.to_csv(tmp_path / "ABC.csv")
    out = CsvDailyProvider(tmp_path).history("abc")
    assert len(out) == 5 and list(out.columns) == ["open", "high", "low", "close", "volume"]
    with pytest.raises(LookupError):
        CsvDailyProvider(tmp_path).history("NOPE")


def test_synthetic_provider_is_deterministic():
    a = SyntheticDailyProvider(days=100, end="2026-01-02").history("AAPL")
    b = SyntheticDailyProvider(days=100, end="2026-01-02").history("AAPL")
    pd.testing.assert_frame_equal(a, b)
    assert (a["high"] >= a[["open", "close"]].max(axis=1)).all()
    assert (a["low"] <= a[["open", "close"]].min(axis=1)).all()


# -- backtest ------------------------------------------------------------------
def test_point_in_time_scan_ignores_future_bars(cfg, histories):
    scanner = StockScanner(cfg)
    date = histories["IDX"].index[260]
    before = [(o.ticker, o.score) for o in rank_as_of(histories, date, scanner).opportunities]
    mutated = {t: df.copy() for t, df in histories.items()}
    for df in mutated.values():
        df.loc[df.index > date, ["open", "high", "low", "close"]] *= 5   # rewrite the future
    after = [(o.ticker, o.score) for o in rank_as_of(mutated, date, scanner).opportunities]
    assert before == after


def test_backtest_runs_and_reports(cfg, histories):
    cfg.universe = ["UP", "FLAT", "DOWN"]
    bt = run_backtest(histories, StockScanner(cfg), top_n=1, rebalance_days=10, warmup_bars=220)
    assert len(bt.periods) >= 7
    assert set(bt.metrics) == {"strategy", "universe", "benchmark"}
    assert (bt.periods["picks"].isin(["UP", ""])).all()
    assert bt.metrics["strategy"]["total_return"] > bt.metrics["universe"]["total_return"]
    with pytest.raises(ValueError):
        run_backtest({t: df.iloc[:100] for t, df in histories.items()}, StockScanner(cfg))


# -- reports / CLI -------------------------------------------------------------
def test_reports_render(cfg, histories):
    result = StockScanner(cfg).scan(histories)
    assert "UP" in to_console(result) and "| **UP** |" in to_markdown(result)
    payload = json.loads(to_json(result))                  # strict JSON, NaN -> null
    assert payload["opportunities"][0]["ticker"] == "UP"


def test_cli_synthetic_end_to_end(tmp_path, capsys):
    from scripts.scan_stocks import main
    rc = main(["--provider", "synthetic", "--tickers", "AAPL,MSFT,NVDA,XOM,KO",
               "--backtest", "--bt-top", "2", "--out", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SINTÉTICOS" in out and "BACKTEST" in out
    assert (tmp_path / "scan.json").exists() and (tmp_path / "scan.md").exists()
