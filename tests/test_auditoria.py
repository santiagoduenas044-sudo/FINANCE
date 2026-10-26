"""scripts/auditoria.py: the summary math and the file carries no secrets."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("auditoria", Path(__file__).resolve().parents[1] / "scripts" / "auditoria.py")
aud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aud)


def test_stats_and_sessions():
    rows = [{"net_usd": v} for v in (300, -100, -100, 500, -200)]
    s = aud.stats(rows)
    assert (s["n"], s["net"], s["won_pct"], s["pf"], s["max_dd"]) == (5, 400, 40.0, 2.0, -200)
    assert aud.session_of("2026-10-20T04:10:00-04:00") == "Europa"
    assert aud.session_of("2026-10-20T10:00:00-04:00") == "Mañana NY"
    assert aud.session_of("2026-10-19T21:00:00-04:00") == "Asia"
    assert aud.stats([])["n"] == 0


def test_the_file_has_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(aud, "ROOT", tmp_path)
    aud.main()
    text = next(tmp_path.glob("auditoria-*.json")).read_text()
    for word in ("webhook", "token", "password", "contraseña"):
        assert word not in text.lower()
