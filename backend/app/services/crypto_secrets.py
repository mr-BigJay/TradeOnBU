from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def secrets_key_path() -> Path:
    data = Path(os.getenv("DATA_DIR", "data"))
    return data / "secrets" / "master.key"


def load_or_create_fernet() -> Fernet:
    path = secrets_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raw = path.read_bytes().strip()
    else:
        raw = Fernet.generate_key()
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        os.chmod(path.parent, 0o700)
    return Fernet(raw)


def encrypt_value(plain: str) -> str:
    return load_or_create_fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_value(token: str) -> str:
    try:
        return load_or_create_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("invalid encrypted secret") from exc
