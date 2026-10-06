"""Usuarios de la página web: entrar con usuario y contraseña.

Para publicar el dashboard en internet (Tailscale Funnel) hace falta algo más
que el token de un link: cada persona entra con su usuario y contraseña.

  * owner    — todo: iniciar/parar el bot, actualizar, crear usuarios.
  * viewer   — solo mirar (todo en modo lectura) y la práctica.
  * practica — solo la pantalla de Práctica (y su cuenta de práctica guardada).
    Es lo que recibe quien se registra solo desde el link (/practica).

Los usuarios viven en data_store/users.json con la contraseña cifrada
(PBKDF2-SHA256, 390,000 vueltas, sal propia). La sesión es una cookie firmada
con una clave que se genera sola (data_store/session_secret); borrar ese
archivo cierra todas las sesiones. Nada de esto entra al repositorio.

    python -m signal_engine.api.accounts add NOMBRE [owner|viewer]   (pide la contraseña)
    python -m signal_engine.api.accounts list
    python -m signal_engine.api.accounts remove NOMBRE
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

ROLES = ("owner", "viewer", "practica")
ITERATIONS = 390_000
SESSION_DAYS = 30
COOKIE = "ghost_session"
MIN_PASSWORD = 8
_NAME = re.compile(r"^[A-Za-z0-9._-]{2,32}$")


class AccountError(ValueError):
    """A request the dashboard shows back to the person (in Spanish)."""


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${_b64(salt)}${_b64(dk)}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(it))
        return hmac.compare_digest(got, _unb64(dk))
    except (ValueError, TypeError):
        return False


class Accounts:
    def __init__(self, root: Path):
        self.dir = Path(root) / "data_store"
        self.file = self.dir / "users.json"
        self.secret_file = self.dir / "session_secret"

    # -- users -------------------------------------------------------------- #
    def users(self) -> dict:
        try:
            data = json.loads(self.file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def has_users(self) -> bool:
        return bool(self.users())

    def _write(self, users: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(users, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(self.file)

    def add(self, name: str, password: str, role: str = "viewer") -> None:
        """Create a user, or change the password/role of an existing one."""
        name = (name or "").strip()
        if not _NAME.match(name):
            raise AccountError("El usuario debe tener 2 a 32 letras, números, punto, guion o guion bajo.")
        if role not in ROLES:
            raise AccountError("El rol debe ser owner, viewer o practica.")
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"La contraseña debe tener al menos {MIN_PASSWORD} caracteres.")
        users = self.users()
        users[name] = {"hash": hash_password(password), "role": role,
                       "created": users.get(name, {}).get("created") or time.strftime("%Y-%m-%d")}
        self._write(users)

    def set_role(self, name: str, role: str) -> None:
        users = self.users()
        if name not in users:
            raise AccountError("Ese usuario no existe.")
        if role not in ROLES:
            raise AccountError("El rol debe ser owner, viewer o practica.")
        others = [u for u, d in users.items() if u != name and d.get("role") == "owner"]
        if users[name].get("role") == "owner" and role != "owner" and not others:
            raise AccountError("Es el único owner: nombra a otro owner primero.")
        users[name]["role"] = role
        self._write(users)

    # -- registro abierto (cualquiera con el link crea su usuario de práctica) #
    def signup_open(self) -> bool:
        try:
            return bool(json.loads((self.dir / "signup.json").read_text()).get("open", True))
        except (OSError, ValueError):
            return True

    def set_signup(self, on: bool) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "signup.json").write_text(json.dumps({"open": bool(on)}))

    def signup(self, name: str, password: str) -> None:
        """A new practice-only user. Never replaces an existing one."""
        if not self.has_users():
            raise AccountError("La página todavía no tiene dueño: el registro aún no está abierto.")
        if not self.signup_open():
            raise AccountError("El registro está cerrado. Pídele una cuenta al dueño.")
        if (name or "").strip().lower() in {u.lower() for u in self.users()}:
            raise AccountError("Ese usuario ya existe: elige otro nombre.")
        self.add(name, password, "practica")

    def remove(self, name: str) -> bool:
        users = self.users()
        if name not in users:
            return False
        others = [u for u, d in users.items() if u != name and d.get("role") == "owner"]
        if users[name].get("role") == "owner" and not others:
            raise AccountError("Es el único owner: crea otro owner antes de borrarlo.")
        del users[name]
        self._write(users)
        return True

    def check(self, name: str, password: str) -> str | None:
        """The user's role when the password is right, else None."""
        u = self.users().get((name or "").strip())
        if not u:
            check_password(password or "", hash_password("x"))     # same time either way
            return None
        return u.get("role") if check_password(password or "", u.get("hash", "")) else None

    def listing(self) -> list[dict]:
        return [{"user": n, "role": d.get("role"), "created": d.get("created")}
                for n, d in sorted(self.users().items())]

    # -- sessions ----------------------------------------------------------- #
    def _secret(self) -> bytes:
        try:
            return self.secret_file.read_bytes()
        except OSError:
            self.dir.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            self.secret_file.write_bytes(key)
            os.chmod(self.secret_file, 0o600)
            return key

    def issue(self, name: str, days: int = SESSION_DAYS) -> str:
        body = _b64(json.dumps({"u": name, "exp": int(time.time()) + days * 86400}).encode())
        sig = _b64(hmac.new(self._secret(), body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def verify(self, cookie: str | None) -> tuple[str, str] | None:
        """(user, role) for a valid, unexpired session of a user that still
        exists (its CURRENT role: a demoted user is demoted right away)."""
        if not cookie or "." not in cookie:
            return None
        body, sig = cookie.rsplit(".", 1)
        want = _b64(hmac.new(self._secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            return None
        try:
            data = json.loads(_unb64(body))
        except ValueError:
            return None
        if int(data.get("exp", 0)) < time.time():
            return None
        u = self.users().get(data.get("u"))
        return (data["u"], u.get("role", "viewer")) if u else None


class Throttle:
    """At most `limit` failed logins per `window` seconds from one address."""

    def __init__(self, limit: int = 5, window: float = 900.0):
        self.limit, self.window = limit, window
        self.fails: dict[str, list[float]] = {}

    def blocked(self, key: str) -> bool:
        now = time.time()
        recent = [t for t in self.fails.get(key, []) if now - t < self.window]
        self.fails[key] = recent
        return len(recent) >= self.limit

    def fail(self, key: str) -> None:
        self.fails.setdefault(key, []).append(time.time())

    def ok(self, key: str) -> None:
        self.fails.pop(key, None)


def main(argv: list[str]) -> int:
    import getpass
    root = Path(__file__).resolve().parents[2]
    acc = Accounts(root)
    if len(argv) < 1 or argv[0] not in ("add", "list", "remove"):
        print(__doc__.split("\n\n")[-1])
        return 2
    cmd = argv[0]
    try:
        if cmd == "list":
            rows = acc.listing()
            print("\n".join(f"{r['user']:<20} {r['role']}" for r in rows) or "(sin usuarios)")
        elif cmd == "remove":
            print("borrado" if acc.remove(argv[1]) else "no existe")
        else:
            name = argv[1]
            role = argv[2] if len(argv) > 2 else ("owner" if not acc.has_users() else "viewer")
            p1 = getpass.getpass(f"Contraseña para {name}: ")
            p2 = getpass.getpass("Repítela: ")
            if p1 != p2:
                print("No coinciden; no se guardó nada.")
                return 1
            acc.add(name, p1, role)
            print(f"Listo: {name} ({role}).")
    except (AccountError, IndexError) as exc:
        print(f"Error: {exc}" if isinstance(exc, AccountError) else "Falta el nombre del usuario.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
