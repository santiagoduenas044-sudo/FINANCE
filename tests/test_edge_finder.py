"""The out-of-sample filter search finds a planted edge and rejects noise."""
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def _csv(tmp_path, planted: bool) -> Path:
    rng = np.random.default_rng(1)
    n = 3000
    dates = pd.date_range("2022-01-03", "2025-12-30", periods=n).strftime("%Y-%m-%d")
    mom = rng.normal(0, 1, n)
    r = rng.normal(-0.05, 1.0, n)
    if planted:
        r = np.where(mom > 0.5, r + 0.6, r)          # trend into the entry pays
    df = pd.DataFrame({"date": dates, "risk_pts": 20.0, "pnl_pts": r * 20 + 1.25,
                       "pnl_2r_pts": r * 20 + 1.25, "mom_60": mom,
                       "minute": rng.integers(570, 960, n), "direction": "LONG",
                       "approved": True, "cost_pts": 1.25})
    p = tmp_path / "x.shadow.csv"
    df.to_csv(p, index=False)
    return p


def _run(p):
    return subprocess.run([sys.executable, "scripts/edge_finder.py", str(p)], cwd=ROOT,
                          capture_output=True, text=True, timeout=120).stdout


def test_planted_edge_holds_out_of_sample(tmp_path):
    out = _run(_csv(tmp_path, planted=True))
    assert "✔ tendencia 1 h a favor (R) ≥" in out


def test_noise_does_not_pass(tmp_path):
    out = _run(_csv(tmp_path, planted=False))
    assert "✔ tendencia 1 h a favor (R) ≥" not in out


def test_old_csv_is_explained(tmp_path):
    p = tmp_path / "old.shadow.csv"
    pd.DataFrame({"risk_pts": [20.0], "pnl_pts": [5.0], "hour": [10]}).to_csv(p, index=False)
    assert "backtest viejo" in _run(p)
