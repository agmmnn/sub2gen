"""Capability-oriented repositories over the active database backend.

These adapters establish persistence boundaries without duplicating the mature
SQLite/PostgreSQL query implementations. Query bodies can move behind each
repository incrementally while callers depend on the smaller capability API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.models import Project, RequestLog, Token


@dataclass(slots=True)
class AccountRepository:
    database: Any

    async def add_token(self, token: Token) -> int:
        return await self.database.add_token(token)

    async def get_token(self, token_id: int):
        return await self.database.get_token(token_id)

    async def get_token_by_st(self, session_token: str):
        return await self.database.get_token_by_st(session_token)

    async def get_token_by_email(self, email: str):
        return await self.database.get_token_by_email(email)

    async def get_all_tokens(self):
        return await self.database.get_all_tokens()

    async def get_active_tokens(self):
        return await self.database.get_active_tokens()

    async def update_token(self, token_id: int, **changes: Any) -> None:
        await self.database.update_token(token_id, **changes)

    async def delete_token(self, token_id: int) -> None:
        await self.database.delete_token(token_id)

    async def increment_token_stats(self, token_id: int, stat_type: str) -> None:
        await self.database.increment_token_stats(token_id, stat_type)

    async def get_token_stats(self, token_id: int):
        return await self.database.get_token_stats(token_id)

    async def reset_error_count(self, token_id: int) -> None:
        await self.database.reset_error_count(token_id)


@dataclass(slots=True)
class ProjectRepository:
    database: Any

    async def add_project(self, project: Project) -> int:
        return await self.database.add_project(project)

    async def deactivate_projects_for_token_scope(self, token_id: int, api_key_id: int | None = None) -> int:
        return await self.database.deactivate_projects_for_token_scope(token_id, api_key_id=api_key_id)

    async def get_project_by_id(self, project_id: str, api_key_id: int | None = None):
        return await self.database.get_project_by_id(project_id, api_key_id=api_key_id)

    async def get_projects_by_token(self, token_id: int, api_key_id: int | None = None):
        return await self.database.get_projects_by_token(token_id, api_key_id=api_key_id)

    async def count_projects_by_api_key(self, api_key_id: int) -> int:
        return await self.database.count_projects_by_api_key(api_key_id)

    async def count_projects_for_api_key_account(self, api_key_id: int, token_id: int) -> int:
        return await self.database.count_projects_for_api_key_account(api_key_id, token_id)

    async def list_projects_for_api_key_account(self, api_key_id: int, token_id: int, limit: int = 10, offset: int = 0):
        return await self.database.list_projects_for_api_key_account(api_key_id, token_id, limit=limit, offset=offset)

    async def list_projects_by_api_key(self, api_key_id: int, limit: int = 10, offset: int = 0):
        return await self.database.list_projects_by_api_key(api_key_id, limit=limit, offset=offset)

    async def delete_project(self, project_id: str) -> None:
        await self.database.delete_project(project_id)


@dataclass(slots=True)
class ApiKeyRepository:
    database: Any

    async def create_client_api_key(self, **values: Any) -> int:
        return await self.database.create_client_api_key(**values)

    async def get_client_api_key_by_hash(self, key_hash: str):
        return await self.database.get_client_api_key_by_hash(key_hash)

    async def get_api_key_account_ids(self, key_id: int, existing_only: bool = False):
        if existing_only:
            return await self.database.get_api_key_account_ids(key_id, existing_only=True)
        return await self.database.get_api_key_account_ids(key_id)

    async def get_api_key_rate_limits(self, key_id: int, endpoint: str):
        return await self.database.get_api_key_rate_limits(key_id, endpoint)

    async def touch_api_key_usage(self, key_id: int) -> None:
        await self.database.touch_api_key_usage(key_id)

    async def touch_api_key_presence(self, key_id: int) -> None:
        await self.database.touch_api_key_presence(key_id)

    async def list_api_keys(self):
        return await self.database.list_api_keys()

    async def get_api_key_detail(self, key_id: int, include_plaintext: bool = False):
        return await self.database.get_api_key_detail(key_id, include_plaintext=include_plaintext)

    async def update_api_key(self, key_id: int, **changes: Any) -> None:
        await self.database.update_api_key(key_id, **changes)

    async def delete_api_key(self, key_id: int):
        return await self.database.delete_api_key(key_id)

    async def insert_api_key_audit_log(self, **values: Any) -> None:
        await self.database.insert_api_key_audit_log(**values)

    async def list_api_key_audit_logs(self, **filters: Any):
        return await self.database.list_api_key_audit_logs(**filters)


@dataclass(slots=True)
class CacheRepository:
    database: Any

    async def upsert_cache_file(self, **values: Any) -> None:
        await self.database.upsert_cache_file(**values)

    async def delete_all_cache_file_metadata(self) -> int:
        return await self.database.delete_all_cache_file_metadata()

    async def get_cache_file(self, filename: str):
        return await self.database.get_cache_file(filename)

    async def get_cache_file_for_api_key(self, filename: str, api_key_id: int):
        return await self.database.get_cache_file_for_api_key(filename, api_key_id)

    async def list_cache_files_for_api_key(self, api_key_id: int, **pagination: Any):
        return await self.database.list_cache_files_for_api_key(api_key_id, **pagination)

    async def list_cache_files_for_api_key_cleanup(self, api_key_id: int):
        return await self.database.list_cache_files_for_api_key_cleanup(api_key_id)

    async def list_cache_files_for_api_key_project(self, api_key_id: int, flow_project_id: str, **pagination: Any):
        return await self.database.list_cache_files_for_api_key_project(api_key_id, flow_project_id, **pagination)


@dataclass(slots=True)
class RequestLogRepository:
    database: Any

    async def add_request_log(self, log: RequestLog) -> int:
        return await self.database.add_request_log(log)

    async def update_request_log(self, log_id: int, **changes: Any) -> None:
        await self.database.update_request_log(log_id, **changes)

    async def count_request_logs(self, **filters: Any) -> int:
        return await self.database.count_request_logs(**filters)

    async def get_logs(self, **filters: Any):
        return await self.database.get_logs(**filters)

    async def get_log_detail(self, log_id: int, api_key_id: int | None = None):
        return await self.database.get_log_detail(log_id, api_key_id=api_key_id)

    async def clear_all_logs(self) -> None:
        await self.database.clear_all_logs()

    async def delete_request_logs_older_than(self, days: int = 7, batch_size: int = 500) -> int:
        return await self.database.delete_request_logs_older_than(days, batch_size)


@dataclass(slots=True)
class WorkerRepository:
    database: Any

    async def get_extension_worker_binding_for_route_key(self, route_key: str):
        return await self.database.get_extension_worker_binding_for_route_key(route_key)

    async def upsert_extension_worker_binding(self, route_key: str, api_key_id: int) -> None:
        await self.database.upsert_extension_worker_binding(route_key, api_key_id)

    async def delete_extension_worker_binding(self, route_key: str) -> None:
        await self.database.delete_extension_worker_binding(route_key)

    async def list_extension_worker_bindings(self):
        return await self.database.list_extension_worker_bindings()

    async def get_captcha_worker_key_by_hash(self, key_hash: str):
        return await self.database.get_captcha_worker_key_by_hash(key_hash)

    async def get_captcha_worker_key(self, key_id: int):
        return await self.database.get_captcha_worker_key(key_id)

    async def create_captcha_worker_key(self, **values: Any) -> int:
        return await self.database.create_captcha_worker_key(**values)

    async def update_captcha_worker_key(self, key_id: int, **changes: Any) -> None:
        await self.database.update_captcha_worker_key(key_id, **changes)

    async def delete_captcha_worker_key(self, key_id: int) -> None:
        await self.database.delete_captcha_worker_key(key_id)

    async def list_captcha_worker_keys(self):
        return await self.database.list_captcha_worker_keys()


@dataclass(slots=True)
class Repositories:
    accounts: AccountRepository
    projects: ProjectRepository
    api_keys: ApiKeyRepository
    cache: CacheRepository
    request_logs: RequestLogRepository
    workers: WorkerRepository

    @classmethod
    def from_database(cls, database: Any) -> "Repositories":
        return cls(
            accounts=AccountRepository(database),
            projects=ProjectRepository(database),
            api_keys=ApiKeyRepository(database),
            cache=CacheRepository(database),
            request_logs=RequestLogRepository(database),
            workers=WorkerRepository(database),
        )
