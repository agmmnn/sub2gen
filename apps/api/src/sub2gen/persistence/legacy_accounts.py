"""Non-destructive descriptors for existing provider-specific account tables."""

from __future__ import annotations

from .domain import ProviderAccountRecord


class LegacyAccountCatalog:
    def __init__(self, database) -> None:
        self._database = database

    async def describe_google_flow(self) -> list[ProviderAccountRecord]:
        records: list[ProviderAccountRecord] = []
        for token in await self._database.get_all_tokens():
            if token.id is None:
                continue
            records.append(
                ProviderAccountRecord(
                    id=f"legacy_google_flow_{token.id}",
                    provider_key="google-flow",
                    label=token.email or token.name or f"Flow account {token.id}",
                    external_account_id=token.email or None,
                    enabled=bool(token.is_active),
                    legacy_source="tokens",
                    legacy_id=str(token.id),
                    metadata={"tier": token.user_paygate_tier or "", "auth_mode": token.auth_mode},
                )
            )
        return records

    async def describe_runway(self) -> list[ProviderAccountRecord]:
        return [
            ProviderAccountRecord(
                id=f"legacy_runway_{account.id}",
                provider_key="runway",
                label=account.label or f"Runway account {account.id}",
                external_account_id=account.workspace_id,
                enabled=bool(account.is_active),
                legacy_source="runway_accounts",
                legacy_id=str(account.id),
                metadata={"team_id": account.team_id, "concurrency_limit": account.concurrency_limit},
            )
            for account in await self._database.list_runway_accounts()
            if account.id is not None
        ]

    async def describe_geminigen(self) -> list[ProviderAccountRecord]:
        return [
            ProviderAccountRecord(
                id=f"legacy_geminigen_{account.id}",
                provider_key="geminigen",
                label=account.label or account.profile_email or f"GeminiGen account {account.id}",
                external_account_id=account.profile_uuid or account.profile_email,
                enabled=bool(account.is_active),
                legacy_source="geminigen_accounts",
                legacy_id=str(account.id),
                metadata={"plan_name": account.plan_name, "available_credit": account.available_credit},
            )
            for account in await self._database.list_geminigen_accounts()
            if account.id is not None
        ]
