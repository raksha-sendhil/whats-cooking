"""Paths and YAML config loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    root: Path
    db_path: Path
    sources: dict                                   # config/sources.yaml
    grouping: dict                                  # config/grouping.yaml
    scoring: dict = field(default_factory=dict)     # config/scoring.yaml
    budget: dict = field(default_factory=dict)      # config/budget.yaml
    brief: dict = field(default_factory=dict)       # config/brief.yaml
    challenges: list = field(default_factory=list)  # config/challenges.yaml ("the long game")
    profile: str = ""                               # config/profile.md

    def source_cfg(self, name: str) -> dict:
        return self.sources.get(name) or {}

    @property
    def briefs_dir(self) -> Path:
        return Path(os.environ.get("WC_BRIEFS", self.root / "briefs"))


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(root: Path | None = None) -> Settings:
    root = Path(os.environ.get("WC_ROOT", root or ROOT))
    db_path = Path(os.environ.get("WC_DB", root / "data" / "whats_cooking.db"))
    cfg = root / "config"
    profile = cfg / "profile.md"
    return Settings(
        root=root,
        db_path=db_path,
        sources=_load_yaml(cfg / "sources.yaml"),
        grouping=_load_yaml(cfg / "grouping.yaml"),
        scoring=_load_yaml(cfg / "scoring.yaml"),
        budget=_load_yaml(cfg / "budget.yaml"),
        brief=_load_yaml(cfg / "brief.yaml"),
        challenges=_load_yaml(cfg / "challenges.yaml").get("challenges") or [],
        profile=profile.read_text(encoding="utf-8") if profile.exists() else "",
    )
