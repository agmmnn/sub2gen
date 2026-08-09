from __future__ import annotations

import time
from typing import Any, cast

from fastapi import WebSocket

from sub2gen.workers.extension.models import ExtensionConnection
from sub2gen.workers.extension.routing import ExtensionWorkerRouting


def connection(session_id: str) -> ExtensionConnection:
    return ExtensionConnection(
        websocket=cast(WebSocket, cast(Any, object())),
        worker_session_id=session_id,
        refresh_token_id=7,
        allow_captcha=True,
    )


def test_dedicated_routing_round_robins_tied_workers() -> None:
    routing = ExtensionWorkerRouting()
    workers = [connection("worker-a"), connection("worker-b")]
    metadata: dict[str, Any] = {}

    first = routing.pick_dedicated(
        workers,
        7,
        eligible=lambda worker: worker.allow_captcha,
        selection_meta_out=metadata,
    )
    second = routing.pick_dedicated(
        workers,
        7,
        eligible=lambda worker: worker.allow_captcha,
    )

    assert first is workers[0]
    assert second is workers[1]
    assert metadata["dedicated_pool_size"] == 2


def test_dedicated_routing_avoids_worker_in_cooldown() -> None:
    routing = ExtensionWorkerRouting()
    workers = [connection("worker-a"), connection("worker-b")]
    failed = routing.stats("worker-a")
    now = time.time()
    routing.record_failure(failed, now, is_timeout=False)
    routing.record_failure(failed, now + 0.1, is_timeout=False)

    chosen = routing.pick_dedicated(
        workers,
        7,
        eligible=lambda worker: worker.allow_captcha,
    )

    assert chosen is workers[1]
