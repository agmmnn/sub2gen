from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import websockets
from sub2gen_provider_chatgpt import ChatGPTImagegenProcessBackend, ChatGPTWebProvider, ChromeUseProcessAdapter
from sub2gen_provider_sdk import CancellationToken, GenerationRequest, ProviderExecutionContext, ReferenceInput, ResolvedExecution
from sub2gen_worker_protocol.codec import decode_envelope, encode_envelope, make_envelope
from sub2gen_worker_protocol.generated import (
    JobCancelPayload,
    JobDecisionPayload,
    JobErrorPayload,
    JobOfferPayload,
    JobResultPayload,
    MessageType,
    StructuredError,
    WorkerHeartbeatPayload,
    WorkerChallengePayload,
    WorkerHelloPayload,
    WorkerRegisterPayload,
    WorkerRegisteredPayload,
)

from .config import WorkerConfig
from .identity import DeviceIdentity


class ImageWorkerClient:
    def __init__(self, config: WorkerConfig, identity: DeviceIdentity) -> None:
        self.config = config
        self.identity = identity
        backend = ChatGPTImagegenProcessBackend(
            config.imagegen_executable,
            chrome_use=ChromeUseProcessAdapter(config.chrome_use_executable),
            profile=config.profile_ref.removeprefix("chrome:"),
        )
        self.provider = ChatGPTWebProvider(backend)
        self._cancellations: dict[str, CancellationToken] = {}

    async def pair(self, pairing_code: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.config.server_url, timeout=30) as client:
            response = await client.post(
                "/api/workers/pair",
                json={
                    "pairing_code": pairing_code,
                    "worker_id": self.config.worker_id,
                    "kind": "image-worker",
                    "label": self.config.label,
                    "public_key": self.identity.public_key_base64,
                    "capabilities": self.config.capabilities,
                },
            )
            response.raise_for_status()
            return response.json()

    async def run(self) -> None:
        async with websockets.connect(self.config.websocket_url, max_size=2**20) as socket:
            hello = make_envelope(
                message_type=MessageType.WORKER_HELLO,
                worker_id=self.config.worker_id,
                payload=WorkerHelloPayload(
                    supported_versions=("1.0",), worker_kind="image-worker", instance_id=uuid4().hex
                ),
            )
            await socket.send(encode_envelope(hello))
            challenge, challenge_payload = decode_envelope(await socket.recv())
            if not isinstance(challenge_payload, WorkerChallengePayload):
                raise RuntimeError("server did not return a worker challenge")
            nonce = base64.b64decode(challenge_payload.nonce)
            signed = challenge_payload.challenge_id.encode() + b"." + nonce
            register = make_envelope(
                message_type=MessageType.WORKER_REGISTER,
                worker_id=self.config.worker_id,
                correlation_id=challenge.message_id,
                payload=WorkerRegisterPayload(
                    selected_version="1.0",
                    challenge_id=challenge_payload.challenge_id,
                    signature=self.identity.sign(signed),
                    capabilities=self.config.capabilities,
                    worker_session_id=uuid4().hex,
                ),
            )
            await socket.send(encode_envelope(register))
            _registered, registered_payload = decode_envelope(await socket.recv())
            if not isinstance(registered_payload, WorkerRegisteredPayload):
                raise RuntimeError("server did not complete worker registration")
            session_id = registered_payload.worker_session_id
            heartbeat = asyncio.create_task(self._heartbeat(socket, session_id))
            try:
                async for frame in socket:
                    envelope, payload = decode_envelope(frame)
                    if envelope.message_type is MessageType.JOB_OFFER and isinstance(payload, JobOfferPayload):
                        asyncio.create_task(self._execute(socket, envelope, payload))
                    elif envelope.message_type is MessageType.JOB_CANCEL and isinstance(payload, JobCancelPayload):
                        token = self._cancellations.get(envelope.job_id or "")
                        if token:
                            token.cancel()
            finally:
                heartbeat.cancel()

    async def _heartbeat(self, socket, session_id: str) -> None:
        while True:
            await asyncio.sleep(10)
            await socket.send(
                encode_envelope(
                    make_envelope(
                        message_type=MessageType.WORKER_HEARTBEAT,
                        worker_id=self.config.worker_id,
                        payload=WorkerHeartbeatPayload(
                            worker_session_id=session_id,
                            active_leases=(),
                            available_slots=0 if self._cancellations else 1,
                        ),
                    )
                )
            )

    async def _execute(self, socket, envelope, offer: JobOfferPayload) -> None:
        job_id = envelope.job_id or ""
        accept = make_envelope(
            message_type=MessageType.JOB_ACCEPT,
            worker_id=self.config.worker_id,
            job_id=job_id,
            job_kind=envelope.job_kind,
            correlation_id=envelope.message_id,
            payload=JobDecisionPayload(attempt=offer.attempt, lease_id=offer.lease_id),
        )
        await socket.send(encode_envelope(accept))
        cancellation = CancellationToken()
        self._cancellations[job_id] = cancellation
        try:
            references = tuple(
                ReferenceInput(item["media_type"], data=base64.b64decode(item["data_base64"]))
                for item in offer.input.get("references", [])
            )
            request = GenerationRequest(
                request_id=job_id,
                prompt=str(offer.input["prompt"]),
                model=str(offer.input["model"]),
                references=references,
                provider_options={
                    "project": (
                        offer.input.get("provider_options", {}).get("project")
                        if isinstance(offer.input.get("provider_options"), dict)
                        else None
                    )
                    or self.config.project,
                },
            )
            context = ProviderExecutionContext(
                resolved=ResolvedExecution(
                    requested_model=request.model,
                    resolved_model=request.model,
                    provider_id="chatgpt-web",
                    provider_account_id=self.config.account_ref,
                    worker_id=self.config.worker_id,
                    billing_pool="chatgpt:web-subscription",
                ),
                cancellation=cancellation,
                timeout_seconds=max(1, (offer.deadline - datetime.now(timezone.utc)).total_seconds()),
                attempt=offer.attempt,
            )
            result = await self.provider.generate(request, context)
            artifacts = []
            for artifact, grant in zip(result.artifacts, offer.artifact_upload_grants, strict=True):
                body = artifact.read_bytes()
                await self._upload(grant, job_id, artifact.media_type, body)
                artifacts.append({"grant_id": grant["grant_id"], "sha256": hashlib.sha256(body).hexdigest()})
            payload = JobResultPayload(attempt=offer.attempt, lease_id=offer.lease_id, output={"artifacts": artifacts})
            message_type = MessageType.JOB_RESULT
        except Exception as exc:
            payload = JobErrorPayload(
                attempt=offer.attempt,
                lease_id=offer.lease_id,
                error=StructuredError(code=type(exc).__name__, message=str(exc)[:500], retryable=False),
            )
            message_type = MessageType.JOB_ERROR
        finally:
            self._cancellations.pop(job_id, None)
        await socket.send(
            encode_envelope(
                make_envelope(
                    message_type=message_type,
                    worker_id=self.config.worker_id,
                    job_id=job_id,
                    job_kind=envelope.job_kind,
                    correlation_id=envelope.message_id,
                    payload=payload,
                )
            )
        )

    async def _upload(self, grant: dict[str, Any], job_id: str, media_type: str, body: bytes) -> None:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.put(
                grant["upload_url"],
                params={"worker_id": self.config.worker_id, "job_id": job_id},
                headers={"content-type": media_type},
                content=body,
            )
            response.raise_for_status()
