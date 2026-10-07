"""The daily copy of data_store/: what goes in, once a day, the newest kept."""
import io
import zipfile

from signal_engine.api import backup


def _store(tmp_path):
    src = tmp_path / "data_store"
    (src / "shadows" / "ghost" / "zonas_1m" / "paper").mkdir(parents=True)
    (src / "shadows" / "ghost" / "zonas_1m" / "paper" / "paper_closed.jsonl").write_text("{}\n")
    (src / "users.json").write_text("{}")
    (src / "backtests").mkdir()
    (src / "backtests" / "big.json").write_text("x" * 1000)          # reproducible: left out
    (src / "orbit.restart").write_text("")
    return src


def test_zip_keeps_the_record_and_leaves_out_what_can_be_rebuilt(tmp_path):
    names = zipfile.ZipFile(io.BytesIO(backup.zip_bytes(_store(tmp_path)))).namelist()
    assert "data_store/users.json" in names
    assert "data_store/shadows/ghost/zonas_1m/paper/paper_closed.jsonl" in names
    assert not any("backtests" in n or n.endswith(".restart") for n in names)


def test_one_copy_a_day_and_only_the_newest_are_kept(tmp_path):
    src, dest = _store(tmp_path), tmp_path / "copias"
    dest.mkdir()
    for d in range(1, 18):
        backup.make_backup(src, dest, day=f"2026-10-{d:02d}", keep=14)
    first = backup.make_backup(src, dest, day="2026-10-17", keep=14)   # again the same day
    zips = sorted(p.name for p in dest.glob("*.zip"))
    assert len(zips) == 14 and zips[-1] == first.name == "ghost-respaldo-2026-10-17.zip"
    assert zips[0] == "ghost-respaldo-2026-10-04.zip"


def test_backup_dir_skips_a_folder_it_cannot_write(tmp_path, monkeypatch):
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    monkeypatch.setenv("BOT_BACKUP_DIR", str(blocked / "x"))
    monkeypatch.setattr(backup, "ICLOUD", tmp_path / "no-icloud")
    monkeypatch.setattr(backup, "ROOT", tmp_path)
    assert backup.backup_dir() == tmp_path / "backups"


def test_backup_is_for_the_owner_only(tmp_path):
    from signal_engine.api.app import _OWNER_ONLY_GET
    assert any("/api/backup.zip".startswith(p) for p in _OWNER_ONLY_GET)


def test_backup_endpoints():
    from fastapi.testclient import TestClient
    from signal_engine.api.app import create_app
    from signal_engine.config.schema import EngineConfig
    from signal_engine.data.engine import DataEngine
    from tests.test_bot_runtime import _runtime
    c = TestClient(create_app(_runtime(DataEngine(EngineConfig.load(strategy="zones")))))
    r = c.get("/api/backup.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.content[:2] == b"PK"
    assert {"dir", "last", "count", "keep"} <= set(c.get("/api/backup").json())
