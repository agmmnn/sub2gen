"""Persistence boundaries for database backends and migrations."""
from .domain import (
    CredentialBindingRecord,
    CredentialBindingView,
    CredentialStorageKind,
    GenerationAttemptRecord,
    GenerationAttemptStatus,
    GenerationArtifactRecord,
    GenerationJobRecord,
    GenerationJobStatus,
    ProviderAccountRecord,
    WorkerDeviceRecord,
    WorkerDeviceView,
)
from .credential_resolver import CredentialResolutionError, CredentialResolver, ResolvedCredential
from .legacy_accounts import LegacyAccountCatalog
from .repositories import Repositories

__all__ = [
    "CredentialBindingRecord",
    "CredentialBindingView",
    "CredentialStorageKind",
    "CredentialResolutionError",
    "CredentialResolver",
    "GenerationAttemptRecord",
    "GenerationAttemptStatus",
    "GenerationArtifactRecord",
    "GenerationJobRecord",
    "GenerationJobStatus",
    "ProviderAccountRecord",
    "ResolvedCredential",
    "Repositories",
    "WorkerDeviceRecord",
    "WorkerDeviceView",
    "LegacyAccountCatalog",
]
