"""Focused repositories for provider-neutral accounts, workers, and generation jobs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .domain import (
    CredentialBindingRecord,
    CredentialBindingView,
    CredentialStorageKind,
    GenerationAttemptRecord,
    GenerationAttemptStatus,
    GenerationJobRecord,
    GenerationJobStatus,
    ProviderAccountRecord,
    WorkerDeviceRecord,
    WorkerDeviceView,
)


def _json_dump(value: Any) -> str:
    if isinstance(value, Mapping):
        value = dict(value)
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _json_load(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else fallback
    except (TypeError, ValueError):
        return fallback


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


@dataclass(slots=True)
class ProviderAccountRepository:
    database: Any

    async def create(self, account: ProviderAccountRecord) -> ProviderAccountRecord:
        async with self.database._connect(write=True) as connection:
            await connection.execute(
                """
                INSERT INTO provider_accounts (
                    id, provider_key, label, external_account_id, enabled,
                    metadata_json, legacy_source, legacy_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account.id,
                    account.provider_key,
                    account.label,
                    account.external_account_id,
                    account.enabled,
                    _json_dump(account.metadata),
                    account.legacy_source,
                    account.legacy_id,
                ),
            )
            await connection.commit()
        return (await self.get(account.id)) or account

    async def get(self, account_id: str) -> ProviderAccountRecord | None:
        async with self.database._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, provider_key, label, external_account_id, enabled,
                       metadata_json, legacy_source, legacy_id, created_at, updated_at
                FROM provider_accounts WHERE id = ?
                """,
                (account_id,),
            )
            row = await cursor.fetchone()
        return self._record(row) if row else None

    async def list(self, *, provider_key: str | None = None, enabled_only: bool = False) -> list[ProviderAccountRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if provider_key:
            clauses.append("provider_key = ?")
            params.append(provider_key)
        if enabled_only:
            clauses.append("enabled = ?")
            params.append(True)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.database._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, provider_key, label, external_account_id, enabled,
                       metadata_json, legacy_source, legacy_id, created_at, updated_at
                FROM provider_accounts
                """ + where + " ORDER BY provider_key, label, id",
                tuple(params),
            )
            rows = await cursor.fetchall()
        return [self._record(row) for row in rows]

    async def set_enabled(self, account_id: str, enabled: bool) -> bool:
        async with self.database._connect(write=True) as connection:
            cursor = await connection.execute(
                "UPDATE provider_accounts SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (enabled, account_id),
            )
            await connection.commit()
            return int(cursor.rowcount or 0) == 1

    @staticmethod
    def _record(row: Any) -> ProviderAccountRecord:
        return ProviderAccountRecord(
            id=str(row[0]),
            provider_key=str(row[1]),
            label=str(row[2]),
            external_account_id=str(row[3]) if row[3] is not None else None,
            enabled=bool(row[4]),
            metadata=_json_load(row[5], {}),
            legacy_source=str(row[6]) if row[6] is not None else None,
            legacy_id=str(row[7]) if row[7] is not None else None,
            created_at=_timestamp(row[8]),
            updated_at=_timestamp(row[9]),
        )


@dataclass(slots=True)
class CredentialBindingRepository:
    database: Any

    async def create(self, binding: CredentialBindingRecord) -> CredentialBindingRecord:
        async with self.database._connect(write=True) as connection:
            await connection.execute(
                """
                INSERT INTO credential_bindings (
                    id, provider_account_id, worker_id, binding_key, credential_type,
                    storage_kind, secret_ref, enabled, expires_at, last_validated_at,
                    last_error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.id,
                    binding.provider_account_id,
                    binding.worker_id,
                    binding.binding_key,
                    binding.credential_type,
                    binding.storage_kind.value,
                    binding.secret_ref,
                    binding.enabled,
                    binding.expires_at,
                    binding.last_validated_at,
                    binding.last_error,
                    _json_dump(binding.metadata),
                ),
            )
            await connection.commit()
        return (await self.get_for_resolution(binding.id)) or binding

    async def get_for_resolution(self, binding_id: str) -> CredentialBindingRecord | None:
        async with self.database._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, provider_account_id, worker_id, binding_key, credential_type,
                       storage_kind, secret_ref, enabled, expires_at, last_validated_at,
                       last_error, metadata_json, created_at, updated_at
                FROM credential_bindings WHERE id = ?
                """,
                (binding_id,),
            )
            row = await cursor.fetchone()
        return self._record(row) if row else None

    async def list_metadata(self, provider_account_id: str | None = None) -> list[CredentialBindingView]:
        where = " WHERE provider_account_id = ?" if provider_account_id else ""
        params = (provider_account_id,) if provider_account_id else ()
        async with self.database._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, provider_account_id, binding_key, credential_type, storage_kind,
                       worker_id, enabled, expires_at, last_validated_at, last_error, metadata_json
                FROM credential_bindings
                """ + where + " ORDER BY provider_account_id, binding_key, id",
                params,
            )
            rows = await cursor.fetchall()
        return [
            CredentialBindingView(
                id=str(row[0]),
                provider_account_id=str(row[1]),
                binding_key=str(row[2]),
                credential_type=str(row[3]),
                storage_kind=CredentialStorageKind(str(row[4])),
                worker_id=str(row[5]) if row[5] is not None else None,
                enabled=bool(row[6]),
                expires_at=_timestamp(row[7]),
                last_validated_at=_timestamp(row[8]),
                last_error=str(row[9]) if row[9] is not None else None,
                metadata=_json_load(row[10], {}),
            )
            for row in rows
        ]

    async def record_validation(self, binding_id: str, *, error: str | None = None) -> bool:
        async with self.database._connect(write=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE credential_bindings
                SET last_validated_at = CURRENT_TIMESTAMP, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error, binding_id),
            )
            await connection.commit()
            return int(cursor.rowcount or 0) == 1

    @staticmethod
    def _record(row: Any) -> CredentialBindingRecord:
        return CredentialBindingRecord(
            id=str(row[0]),
            provider_account_id=str(row[1]),
            worker_id=str(row[2]) if row[2] is not None else None,
            binding_key=str(row[3]),
            credential_type=str(row[4]),
            storage_kind=CredentialStorageKind(str(row[5])),
            secret_ref=str(row[6]),
            enabled=bool(row[7]),
            expires_at=_timestamp(row[8]),
            last_validated_at=_timestamp(row[9]),
            last_error=str(row[10]) if row[10] is not None else None,
            metadata=_json_load(row[11], {}),
            created_at=_timestamp(row[12]),
            updated_at=_timestamp(row[13]),
        )


@dataclass(slots=True)
class WorkerDeviceRepository:
    database: Any

    async def register(self, worker: WorkerDeviceRecord) -> WorkerDeviceRecord:
        async with self.database._connect(write=True) as connection:
            await connection.execute(
                """
                INSERT INTO worker_devices (
                    id, kind, label, enabled, approved_capabilities_json, auth_key_hash,
                    public_key, credential_expires_at, revoked_at, last_seen_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    worker.id,
                    worker.kind,
                    worker.label,
                    worker.enabled,
                    _json_dump(worker.approved_capabilities),
                    worker.auth_key_hash,
                    worker.public_key,
                    worker.credential_expires_at,
                    worker.revoked_at,
                    worker.last_seen_at,
                    _json_dump(worker.metadata),
                ),
            )
            await connection.commit()
        return (await self.get_for_auth(worker.id)) or worker

    async def get_for_auth(self, worker_id: str) -> WorkerDeviceRecord | None:
        async with self.database._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, kind, label, enabled, approved_capabilities_json, auth_key_hash,
                       public_key, credential_expires_at, revoked_at, last_seen_at,
                       metadata_json, created_at, updated_at
                FROM worker_devices WHERE id = ?
                """,
                (worker_id,),
            )
            row = await cursor.fetchone()
        return self._record(row) if row else None

    async def get_by_auth_hash(self, auth_key_hash: str) -> WorkerDeviceRecord | None:
        async with self.database._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, kind, label, enabled, approved_capabilities_json, auth_key_hash,
                       public_key, credential_expires_at, revoked_at, last_seen_at,
                       metadata_json, created_at, updated_at
                FROM worker_devices WHERE auth_key_hash = ?
                """,
                (auth_key_hash,),
            )
            row = await cursor.fetchone()
        return self._record(row) if row else None

    async def list_metadata(self) -> list[WorkerDeviceView]:
        async with self.database._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, kind, label, approved_capabilities_json, enabled,
                       credential_expires_at, revoked_at, last_seen_at, metadata_json
                FROM worker_devices ORDER BY label, id
                """
            )
            rows = await cursor.fetchall()
        return [
            WorkerDeviceView(
                id=str(row[0]),
                kind=str(row[1]),
                label=str(row[2]),
                approved_capabilities=tuple(_json_load(row[3], [])),
                enabled=bool(row[4]),
                credential_expires_at=_timestamp(row[5]),
                revoked_at=_timestamp(row[6]),
                last_seen_at=_timestamp(row[7]),
                metadata=_json_load(row[8], {}),
            )
            for row in rows
        ]

    async def heartbeat(self, worker_id: str) -> bool:
        async with self.database._connect(write=True) as connection:
            cursor = await connection.execute(
                "UPDATE worker_devices SET last_seen_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (worker_id,),
            )
            await connection.commit()
            return int(cursor.rowcount or 0) == 1

    async def set_enabled(self, worker_id: str, enabled: bool) -> bool:
        async with self.database._connect(write=True) as connection:
            cursor = await connection.execute(
                "UPDATE worker_devices SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (enabled, worker_id),
            )
            await connection.commit()
            return int(cursor.rowcount or 0) == 1

    async def set_capabilities(self, worker_id: str, capabilities: tuple[str, ...]) -> bool:
        normalized = tuple(sorted(set(capabilities)))
        async with self.database._connect(write=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE worker_devices
                SET approved_capabilities_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (_json_dump(normalized), worker_id),
            )
            await connection.commit()
            return int(cursor.rowcount or 0) == 1

    @staticmethod
    def _record(row: Any) -> WorkerDeviceRecord:
        return WorkerDeviceRecord(
            id=str(row[0]),
            kind=str(row[1]),
            label=str(row[2]),
            enabled=bool(row[3]),
            approved_capabilities=tuple(_json_load(row[4], [])),
            auth_key_hash=str(row[5]) if row[5] is not None else None,
            public_key=str(row[6]) if row[6] is not None else None,
            credential_expires_at=_timestamp(row[7]),
            revoked_at=_timestamp(row[8]),
            last_seen_at=_timestamp(row[9]),
            metadata=_json_load(row[10], {}),
            created_at=_timestamp(row[11]),
            updated_at=_timestamp(row[12]),
        )


@dataclass(slots=True)
class GenerationJobRepository:
    database: Any

    async def create(self, job: GenerationJobRecord) -> GenerationJobRecord:
        async with self.database._connect(write=True) as connection:
            await connection.execute(
                """
                INSERT INTO generation_jobs (
                    id, idempotency_key, api_key_id, request_id, job_kind, requested_model,
                    status, provider_account_id, worker_id, resolved_execution_json,
                    deadline_at, terminal_at, error_code, error_detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.idempotency_key,
                    job.api_key_id,
                    job.request_id,
                    job.job_kind,
                    job.requested_model,
                    job.status.value,
                    job.provider_account_id,
                    job.worker_id,
                    _json_dump(job.resolved_execution) if job.resolved_execution is not None else None,
                    job.deadline_at,
                    job.terminal_at,
                    job.error_code,
                    job.error_detail,
                ),
            )
            await connection.commit()
        return (await self.get(job.id)) or job

    async def get(self, job_id: str) -> GenerationJobRecord | None:
        return await self._get("id", job_id)

    async def get_by_idempotency_key(self, idempotency_key: str) -> GenerationJobRecord | None:
        return await self._get("idempotency_key", idempotency_key)

    async def _get(self, column: str, value: str) -> GenerationJobRecord | None:
        if column not in {"id", "idempotency_key"}:
            raise ValueError("unsupported job lookup column")
        async with self.database._connect() as connection:
            cursor = await connection.execute(
                f"""
                SELECT id, idempotency_key, api_key_id, request_id, job_kind, requested_model,
                       status, provider_account_id, worker_id, resolved_execution_json,
                       deadline_at, terminal_at, error_code, error_detail, created_at, updated_at
                FROM generation_jobs WHERE {column} = ?
                """,
                (value,),
            )
            row = await cursor.fetchone()
        return self._record(row) if row else None

    async def transition(
        self,
        job_id: str,
        *,
        expected: tuple[GenerationJobStatus, ...],
        status: GenerationJobStatus,
        provider_account_id: str | None = None,
        worker_id: str | None = None,
        resolved_execution: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        terminal: bool = False,
    ) -> bool:
        if not expected:
            raise ValueError("expected statuses must not be empty")
        placeholders = ",".join("?" for _ in expected)
        terminal_sql = ", terminal_at = CURRENT_TIMESTAMP" if terminal else ""
        params: list[Any] = [
            status.value,
            provider_account_id,
            worker_id,
            _json_dump(resolved_execution) if resolved_execution is not None else None,
            error_code,
            error_detail,
            job_id,
            *(item.value for item in expected),
        ]
        async with self.database._connect(write=True) as connection:
            cursor = await connection.execute(
                f"""
                UPDATE generation_jobs
                SET status = ?, provider_account_id = COALESCE(?, provider_account_id),
                    worker_id = COALESCE(?, worker_id),
                    resolved_execution_json = COALESCE(?, resolved_execution_json),
                    error_code = ?, error_detail = ?, updated_at = CURRENT_TIMESTAMP
                    {terminal_sql}
                WHERE id = ? AND status IN ({placeholders})
                """,
                tuple(params),
            )
            await connection.commit()
            return int(cursor.rowcount or 0) == 1

    @staticmethod
    def _record(row: Any) -> GenerationJobRecord:
        return GenerationJobRecord(
            id=str(row[0]),
            idempotency_key=str(row[1]) if row[1] is not None else None,
            api_key_id=int(row[2]) if row[2] is not None else None,
            request_id=str(row[3]),
            job_kind=str(row[4]),
            requested_model=str(row[5]),
            status=GenerationJobStatus(str(row[6])),
            provider_account_id=str(row[7]) if row[7] is not None else None,
            worker_id=str(row[8]) if row[8] is not None else None,
            resolved_execution=_json_load(row[9], None),
            deadline_at=_timestamp(row[10]),
            terminal_at=_timestamp(row[11]),
            error_code=str(row[12]) if row[12] is not None else None,
            error_detail=str(row[13]) if row[13] is not None else None,
            created_at=_timestamp(row[14]),
            updated_at=_timestamp(row[15]),
        )


@dataclass(slots=True)
class GenerationAttemptRepository:
    database: Any

    async def create(self, attempt: GenerationAttemptRecord) -> GenerationAttemptRecord:
        async with self.database._connect(write=True) as connection:
            await connection.execute(
                """
                INSERT INTO generation_attempts (
                    id, job_id, attempt, status, lease_id, provider_job_id,
                    resolved_execution_json, started_at, finished_at, error_code, error_detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?)
                """,
                (
                    attempt.id,
                    attempt.job_id,
                    attempt.attempt,
                    attempt.status.value,
                    attempt.lease_id,
                    attempt.provider_job_id,
                    _json_dump(attempt.resolved_execution) if attempt.resolved_execution is not None else None,
                    attempt.started_at,
                    attempt.finished_at,
                    attempt.error_code,
                    attempt.error_detail,
                ),
            )
            await connection.commit()
        return (await self.get(attempt.id)) or attempt

    async def get(self, attempt_id: str) -> GenerationAttemptRecord | None:
        async with self.database._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, job_id, attempt, status, lease_id, provider_job_id,
                       resolved_execution_json, started_at, finished_at, error_code, error_detail
                FROM generation_attempts WHERE id = ?
                """,
                (attempt_id,),
            )
            row = await cursor.fetchone()
        return self._record(row) if row else None

    async def list_for_job(self, job_id: str) -> list[GenerationAttemptRecord]:
        async with self.database._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, job_id, attempt, status, lease_id, provider_job_id,
                       resolved_execution_json, started_at, finished_at, error_code, error_detail
                FROM generation_attempts WHERE job_id = ? ORDER BY attempt
                """,
                (job_id,),
            )
            rows = await cursor.fetchall()
        return [self._record(row) for row in rows]

    async def finish(
        self,
        attempt_id: str,
        *,
        expected_lease_id: str,
        status: GenerationAttemptStatus,
        provider_job_id: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        async with self.database._connect(write=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE generation_attempts
                SET status = ?, provider_job_id = COALESCE(?, provider_job_id),
                    finished_at = CURRENT_TIMESTAMP, error_code = ?, error_detail = ?
                WHERE id = ? AND lease_id = ? AND finished_at IS NULL
                """,
                (status.value, provider_job_id, error_code, error_detail, attempt_id, expected_lease_id),
            )
            await connection.commit()
            return int(cursor.rowcount or 0) == 1

    @staticmethod
    def _record(row: Any) -> GenerationAttemptRecord:
        return GenerationAttemptRecord(
            id=str(row[0]),
            job_id=str(row[1]),
            attempt=int(row[2]),
            status=GenerationAttemptStatus(str(row[3])),
            lease_id=str(row[4]) if row[4] is not None else None,
            provider_job_id=str(row[5]) if row[5] is not None else None,
            resolved_execution=_json_load(row[6], None),
            started_at=_timestamp(row[7]),
            finished_at=_timestamp(row[8]),
            error_code=str(row[9]) if row[9] is not None else None,
            error_detail=str(row[10]) if row[10] is not None else None,
        )
