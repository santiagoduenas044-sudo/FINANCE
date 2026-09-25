"""Mega-cap movers card: quote parsing and the NQ-points estimate."""
import json

from signal_engine.live.movers import MoversService, load_components, parse_quote


def _payload(last, prev):
    return json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": last,
                                                      "previousClose": prev}}]}}).encode()


def test_movers_estimate_nq_points_and_survive_a_bad_quote():
    quotes = {"NVDA": _payload(102.0, 100.0), "AAPL": _payload(99.0, 100.0), "MSFT": b"junk"}
    svc = MoversService(fetch=lambda s, t: quotes[s], components=[
        {"symbol": "NVDA", "name": "Nvidia", "weight": 10.0},
        {"symbol": "AAPL", "name": "Apple", "weight": 8.0},
        {"symbol": "MSFT", "name": "Microsoft", "weight": 8.0}])
    snap = svc.snapshot(nq_price=30000.0)
    nvda, aapl = snap["items"]                       # MSFT dropped, biggest pull first
    assert nvda["symbol"] == "NVDA" and round(nvda["chg_pct"], 6) == 2.0
    assert round(nvda["nq_points"], 6) == 30000 * 0.10 * 0.02      # +60 pts
    assert round(aapl["nq_points"], 6) == -30000 * 0.08 * 0.01     # -24 pts
    assert (snap["up"], snap["down"]) == (1, 1)
    assert parse_quote(b"{}") is None


def test_shipped_component_list_loads():
    comps = load_components()
    assert {"AAPL", "NVDA", "META"} <= {c["symbol"] for c in comps}
