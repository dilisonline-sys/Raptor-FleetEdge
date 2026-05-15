import json
import logging
import os
import sys
from datetime import datetime, timezone

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
_log = logging.getLogger("dipu")
_agent_name = os.environ.get("AGENT_NAME", "dipu")


def log(module: str, action: str, **kwargs) -> None:
    record = {
        "ts":     datetime.now(timezone.utc).isoformat(),
        "agent":  _agent_name,
        "module": module,
        "action": action,
        **kwargs,
    }
    _log.info(json.dumps(record))
