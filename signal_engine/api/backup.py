"""Daily copy of data_store/: the trades of every set up, users, each
person's practice and the phone subscriptions, as one zip. A dead disk must
not take the record with it.

Where the zips go, the first folder that can be written:
  1. $BOT_BACKUP_DIR, if set;
  2. iCloud Drive → "GHOST respaldos" (on the Mac, so they leave the disk);
  3. backups/ next to the code (the cloud server keeps it in its volume).
The newest KEEP days are kept. The owner can also download one from Sistema.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
KEEP = 14
PREFIX = "ghost-respaldo-"
# reproducible or throwaway: left out so the zip stays small
SKIP_DIRS = {"backtests", "lab", "news"}
SKIP_SUFFIXES = (".restart", ".tmp")
ICLOUD = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
log = logging.getLogger(__name__)


def _candidates() -> list[Path]:
    out = []
    if os.environ.get("BOT_BACKUP_DIR", "").strip():
        out.append(Path(os.environ["BOT_BACKUP_DIR"]).expanduser())
    if ICLOUD.is_dir():
        out.append(ICLOUD / "GHOST respaldos")
    out.append(ROOT / "backups")
    return out


def backup_dir() -> Path | None:
    """The first folder that can really be written (macOS may refuse iCloud
    to a background process)."""
    for d in _candidates():
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".probe"
            probe.write_text("ok")
            probe.unlink()
            return d
        except OSError:
            continue
    return None


def _files(src: Path):
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if not p.is_file() or rel.parts[0] in SKIP_DIRS or p.name.endswith(SKIP_SUFFIXES):
            continue
        yield p, rel


def zip_bytes(src: Path | None = None) -> bytes:
    """data_store/ as a zip, in memory (the download in Sistema)."""
    src = src or ROOT / "data_store"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p, rel in _files(src):
            z.write(p, Path("data_store") / rel)
    return buf.getvalue()


def _today() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def make_backup(src: Path | None = None, dest: Path | None = None, day: str | None = None,
                keep: int = KEEP) -> Path | None:
    """Today's zip (once per day), then only the newest `keep` are kept."""
    src = src or ROOT / "data_store"
    dest = dest or backup_dir()
    if dest is None or not src.is_dir():
        return None
    target = dest / f"{PREFIX}{day or _today()}.zip"
    if not target.exists():
        tmp = target.with_suffix(".zip.tmp")
        tmp.write_bytes(zip_bytes(src))
        tmp.replace(target)
    for old in sorted(dest.glob(f"{PREFIX}*.zip"))[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


def status() -> dict:
    """For Sistema: where the copies go and the newest one."""
    d = backup_dir()
    zips = sorted(d.glob(f"{PREFIX}*.zip")) if d else []
    last = zips[-1] if zips else None
    return {"dir": str(d) if d else None, "icloud": bool(d and ICLOUD in d.parents),
            "count": len(zips), "keep": KEEP,
            "last": last.name[len(PREFIX):-4] if last else None,
            "last_kb": round(last.stat().st_size / 1024) if last else None}


def start_daily(every: float = 3600.0) -> threading.Thread | None:
    """A background thread that makes today's copy if it is missing, checked
    every hour. BOT_RESPALDO=off turns it off."""
    if os.environ.get("BOT_RESPALDO", "").strip().lower() in ("off", "0", "no", "false"):
        return None
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                made = make_backup()
                if made is not None:
                    log.info("respaldo del día: %s", made.name)
            except Exception as exc:  # noqa: BLE001 - a backup never stops the bot
                log.warning("respaldo falló (%s)", type(exc).__name__)
            stop.wait(every)
    t = threading.Thread(target=loop, name="respaldo", daemon=True)
    t.start()
    return t
