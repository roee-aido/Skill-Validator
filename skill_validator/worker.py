"""Agent Worker process: takes pending tasks from the database and runs them.

    python -m skill_validator.worker

The local network and process guard (D9) is installed before anything else runs.
It is a local protection layer, not infrastructure isolation (see netguard.py).
"""

import argparse
import time

from . import netguard
from .orchestrator import next_task, run_task
from .services import build_services


def main() -> None:
    parser = argparse.ArgumentParser(description="Skill Validator agent worker")
    parser.add_argument("--config", help="agent config path (default: $SV_CONFIG)")
    parser.add_argument("--once", action="store_true", help="run pending tasks, then exit")
    parser.add_argument("--data-root", help="data folder relative to the project (default: config/system.yaml)")
    args = parser.parse_args()

    guard = netguard.install()
    svc = build_services(args.config, args.data_root)
    print(f"worker ready: config {svc.agent_cfg.config_version}, model {svc.agent_cfg.model.model_id}, "
          f"network allowlist {sorted(guard.allowed)}", flush=True)
    while True:
        db = svc.db_factory()
        try:
            task = next_task(db)
            if task is not None:
                outcome = run_task(svc, db, task)
                print(f"task {task.id[:8]} {task.stage}: {outcome}", flush=True)
                continue
        finally:
            db.close()
        if args.once:
            return
        time.sleep(1.0)


if __name__ == "__main__":
    main()
