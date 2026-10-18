"""Wiring of configuration, database and storage for the web app and the worker."""

import os

from . import PROJECT_ROOT
from .config import load_agent_config, load_system_config
from .db import make_engine, make_session_factory
from .orchestrator import Services, make_brain
from .storage import Storage


def build_services(config_path: str | None = None, data_root: str | None = None) -> Services:
    path = config_path or os.environ.get("SV_CONFIG")
    if not path:
        raise SystemExit("Set SV_CONFIG (for local testing: config/agent.mock.yaml)")
    agent_cfg = load_agent_config(path)
    system_cfg = load_system_config()
    root = PROJECT_ROOT / (data_root or system_cfg.storage.root)
    engine = make_engine(root / "app.db")
    return Services(
        db_factory=make_session_factory(engine),
        storage=Storage(root),
        agent_cfg=agent_cfg,
        system_cfg=system_cfg,
        brain_factory=make_brain,
    )
