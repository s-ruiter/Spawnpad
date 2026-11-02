import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "proxlaber.db"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"


def get_settings() -> Settings:
    return Settings(data_dir=Path(os.environ.get("PROXLABER_DATA", "./data")).resolve())
