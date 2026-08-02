"""Backing key-value storage: a small flat-file store.

Storage.get/put persist to a single delimited text file, one "key,value"
pair per line.
"""

from __future__ import annotations

from pathlib import Path


class StorageError(Exception):
    """Raised when the backing file can't be read or written."""


class Storage:
    def __init__(self, path: Path) -> None:
        self._path = path

    def get(self, key: str) -> str | None:
        try:
            if not self._path.exists():
                return None
            for line in self._path.read_text().splitlines():
                stored_key, _, stored_value = line.partition(",")
                if stored_key == key:
                    return stored_value
            return None
        except OSError as exc:
            raise StorageError(f"failed to read {self._path}: {exc}") from exc

    def put(self, key: str, value: str) -> None:
        try:
            existing: dict[str, str] = {}
            if self._path.exists():
                for line in self._path.read_text().splitlines():
                    stored_key, _, stored_value = line.partition(",")
                    existing[stored_key] = stored_value
            existing[key] = value
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                "\n".join(f"{k},{v}" for k, v in existing.items()) + "\n"
            )
        except OSError as exc:
            raise StorageError(f"failed to write {self._path}: {exc}") from exc
