import json
import logging
import os
import sys
from datetime import datetime, timezone

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
_log = logging.getLogger("raptor")
_agent_name = os.environ.get("AGENT_NAME", "raptor")


class _SafeEncoder(json.JSONEncoder):
    """Handle numpy scalar types (bool_, int_, float_) that json can't serialize."""
    def default(self, obj):
        try:
            import numpy as np
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
        except ImportError:
            pass
        return super().default(obj)


def log(module: str, action: str, **kwargs) -> None:
    record = {
        "ts":     datetime.now(timezone.utc).isoformat(),
        "agent":  _agent_name,
        "module": module,
        "action": action,
        **kwargs,
    }
    _log.info(json.dumps(record, cls=_SafeEncoder))
