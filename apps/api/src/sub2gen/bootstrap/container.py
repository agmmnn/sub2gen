"""Explicit construction of sub2gen application dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sub2gen_worker_protocol import ArtifactGrantStore, WorkerCoordinator

from ..core.api_key_manager import ApiKeyManager
from ..core.config import config
from ..core.config import get_runtime_data_dir
from ..core.database import Database, create_database
from ..generation.catalog import ModelRegistry
from ..generation.routing import GenerationRouter
from ..generation.signals import RuntimeSignalRegistry
from ..persistence.repositories import Repositories
from ..services.concurrency_manager import ConcurrencyManager
from ..services.failed_payload_store import FailedPayloadManager, failed_payload_manager
from ..services.flow_client import FlowClient
from ..services.geminigen_service import GeminiGenService
from ..services.generation_handler import MODEL_CONFIG, GenerationHandler
from ..services.generation_audit import GenerationAuditService
from ..services.generation_routing import PersistentGenerationRouter
from ..services.google_drive_backup import GoogleDriveBackupService
from ..services.load_balancer import LoadBalancer
from ..services.proxy_manager import ProxyManager
from ..services.redis_runtime import RedisRuntime, redis_runtime
from ..services.runway_service import RunwayService
from ..services.token_manager import TokenManager
from ..services.unified_images import UnifiedImageService
from ..services.worker_protocol import PersistentDevicePairing
from ..services.worker_artifacts import WorkerArtifactInbox
from ..services.worker_runtime import WorkerRuntime
from .tasks import TaskRegistry


@dataclass(slots=True)
class AppContainer:
    """Dependencies owned by one FastAPI application instance.

    The container is deliberately passive: it does not resolve dependencies at
    runtime and is never stored as a process-wide singleton. FastAPI owns it via
    ``app.state.container`` and request dependencies retrieve it from there.
    """

    db: Database
    repositories: Repositories
    proxy_manager: ProxyManager
    flow_client: FlowClient
    token_manager: TokenManager
    concurrency_manager: ConcurrencyManager
    load_balancer: LoadBalancer
    generation_handler: GenerationHandler
    generation_audit: GenerationAuditService
    model_registry: ModelRegistry
    generation_router: GenerationRouter
    routing_signals: RuntimeSignalRegistry
    persistent_generation_router: PersistentGenerationRouter
    runway_service: RunwayService
    geminigen_service: GeminiGenService
    google_drive_backup_service: GoogleDriveBackupService
    api_key_manager: ApiKeyManager
    redis_runtime: RedisRuntime
    failed_payload_manager: FailedPayloadManager
    worker_pairing: PersistentDevicePairing
    worker_coordinator: WorkerCoordinator
    worker_artifact_grants: ArtifactGrantStore
    worker_artifact_inbox: WorkerArtifactInbox
    worker_runtime: WorkerRuntime
    unified_images: UnifiedImageService = field(init=False)
    tasks: TaskRegistry = field(default_factory=TaskRegistry)
    database_restore_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.unified_images = UnifiedImageService(self)


def build_container(*, database: Database | None = None) -> AppContainer:
    """Build a complete, isolated application dependency graph."""

    db = database or create_database()
    repositories = Repositories.from_database(db)
    proxy_manager = ProxyManager(db)
    flow_client = FlowClient(proxy_manager, db)
    token_manager = TokenManager(
        db,
        flow_client,
        accounts=repositories.accounts,
        projects=repositories.projects,
    )
    concurrency_manager = ConcurrencyManager(redis_runtime=redis_runtime)
    load_balancer = LoadBalancer(token_manager, concurrency_manager)
    generation_handler = GenerationHandler(
        flow_client,
        token_manager,
        load_balancer,
        db,
        concurrency_manager,
        proxy_manager,
        cache_repository=repositories.cache,
        request_log_repository=repositories.request_logs,
    )
    runway_service = RunwayService(db, generation_handler.file_cache, proxy_manager)
    geminigen_service = GeminiGenService(db, generation_handler.file_cache, proxy_manager)
    google_drive_backup_service = GoogleDriveBackupService(db, app_version="1.0.0")
    api_key_manager = ApiKeyManager(
        db,
        legacy_api_key_provider=lambda: config.api_key,
        redis_runtime=redis_runtime,
        repository=repositories.api_keys,
    )
    worker_pairing = PersistentDevicePairing(repositories.workers.devices)
    worker_artifact_grants = ArtifactGrantStore()
    worker_coordinator = WorkerCoordinator()
    worker_runtime = WorkerRuntime(worker_coordinator, worker_artifact_grants)
    model_registry = ModelRegistry.for_platform(MODEL_CONFIG)
    generation_router = GenerationRouter(model_registry)
    routing_signals = RuntimeSignalRegistry()
    return AppContainer(
        db=db,
        repositories=repositories,
        proxy_manager=proxy_manager,
        flow_client=flow_client,
        token_manager=token_manager,
        concurrency_manager=concurrency_manager,
        load_balancer=load_balancer,
        generation_handler=generation_handler,
        generation_audit=GenerationAuditService(
            repositories.generation_jobs,
            repositories.generation_attempts,
        ),
        model_registry=model_registry,
        generation_router=generation_router,
        routing_signals=routing_signals,
        persistent_generation_router=PersistentGenerationRouter(
            model_registry,
            generation_router,
            repositories,
            routing_signals,
            worker_runtime,
        ),
        runway_service=runway_service,
        geminigen_service=geminigen_service,
        google_drive_backup_service=google_drive_backup_service,
        api_key_manager=api_key_manager,
        redis_runtime=redis_runtime,
        failed_payload_manager=failed_payload_manager,
        worker_pairing=worker_pairing,
        worker_coordinator=worker_coordinator,
        worker_artifact_grants=worker_artifact_grants,
        worker_artifact_inbox=WorkerArtifactInbox(
            worker_artifact_grants,
            get_runtime_data_dir() / "worker-artifacts",
        ),
        worker_runtime=worker_runtime,
    )
