from __future__ import annotations

import os
import uuid
from pathlib import Path


def data_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "data"))


def load_or_create_host_id() -> str:
    path = data_dir() / "secrets" / "siwc_host_id"
    if path.exists():
        return path.read_text().strip()
    host_id = f"urn:uuid:{uuid.uuid4()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(host_id)
    os.chmod(path, 0o600)
    os.chmod(path.parent, 0o700)
    return host_id
