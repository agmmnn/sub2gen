"""sub2gen command-line entry point."""
import copy
import sys

import uvicorn

from sub2gen.core.logger import SensitiveAccessLogFilter
from sub2gen.main import app


def build_uvicorn_log_config():
    """Build Uvicorn logging config with secret redaction on HTTP and WebSocket logs."""
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    filter_name = "sensitive_query"
    log_config.setdefault("filters", {})[filter_name] = {
        "()": SensitiveAccessLogFilter,
    }

    for handler_name in ("access", "default"):
        handler = log_config.setdefault("handlers", {}).setdefault(handler_name, {})
        filters = list(handler.get("filters", []))
        if filter_name not in filters:
            filters.append(filter_name)
        handler["filters"] = filters

    return log_config


def main() -> None:
    """Start the sub2gen Uvicorn server."""
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        from sub2gen.diagnostics import run_doctor

        raise SystemExit(run_doctor(json_output="--json" in sys.argv[2:]))
    if len(sys.argv) > 1:
        raise SystemExit("Usage: sub2gen [doctor [--json]]")
    from sub2gen.core.config import config

    uvicorn.run(
        "sub2gen.main:app",
        host=config.server_host,
        port=config.server_port,
        reload=False,
        log_config=build_uvicorn_log_config(),
    )


if __name__ == "__main__":
    main()
