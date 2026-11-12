import json
import os
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _default_targets() -> List[Dict[str, Any]]:
    return [
        {"name": "Submit (深色)", "path": "targets/submit_dark.png", "enabled": True},
        {"name": "Submit (淺色)", "path": "targets/submit_light.png", "enabled": True},
    ]


@dataclass
class AppConfig:
    scan_interval: float = 1.0
    threshold: float = 0.85
    multi_scale: bool = False
    show_region_borders: bool = True
    cooldown_sec: float = 0.8
    regions: List[Dict[str, Any]] = field(default_factory=list)
    targets: List[Dict[str, Any]] = field(default_factory=_default_targets)

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "AppConfig":
        if not os.path.exists(path):
            cfg = cls()
            cfg.save(path)
            return cfg
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(
                scan_interval=float(data.get("scan_interval", 1.0)),
                threshold=float(data.get("threshold", 0.85)),
                multi_scale=bool(data.get("multi_scale", False)),
                show_region_borders=bool(data.get("show_region_borders", True)),
                cooldown_sec=float(data.get("cooldown_sec", 0.8)),
                regions=list(data.get("regions", [])),
                targets=list(data.get("targets", _default_targets())),
            )
        except Exception:
            return cls()

    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

