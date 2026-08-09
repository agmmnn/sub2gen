from __future__ import annotations

from sub2gen.api import admin, routes
from sub2gen.transport import adobe, auth, cache, extensions, gemini, models, openai, projects, runway, websocket
from sub2gen.transport.admin import api_keys, auth as admin_auth, cache as admin_cache, logs
from sub2gen.transport.admin import geminigen as admin_geminigen
from sub2gen.transport.admin import projects as admin_projects
from sub2gen.transport.admin import runway as admin_runway
from sub2gen.transport.admin import settings, system, tokens, workers


PUBLIC_ROUTERS = (
    adobe.router,
    auth.router,
    cache.router,
    extensions.router,
    gemini.router,
    models.router,
    openai.router,
    projects.router,
    runway.router,
    websocket.router,
)

ADMIN_ROUTERS = (
    admin_projects.router,
    workers.router,
    api_keys.router,
    admin_runway.router,
    admin_geminigen.router,
    tokens.router,
    admin_cache.router,
    logs.router,
    admin_auth.router,
    settings.router,
    system.router,
)


def route_paths(router) -> list[str]:
    return [str(getattr(route, "path", "")) for route in router.routes]


def test_public_routes_are_owned_by_transport_modules() -> None:
    owned_route_ids = [id(route) for router in PUBLIC_ROUTERS for route in router.routes]
    aggregate_route_ids = [id(route) for route in routes.router.routes]

    assert len(owned_route_ids) == len(set(owned_route_ids))
    assert len(aggregate_route_ids) == len(owned_route_ids)
    assert set(route_paths(routes.router)) == {path for router in PUBLIC_ROUTERS for path in route_paths(router)}


def test_public_routes_do_not_expose_mutable_service_globals() -> None:
    forbidden = {
        "generation_handler",
        "runway_service",
        "geminigen_service",
        "set_generation_handler",
        "set_runway_service",
        "set_geminigen_service",
    }

    assert forbidden.isdisjoint(vars(routes))


def test_admin_routes_are_partitioned_once_by_feature() -> None:
    feature_paths = [path for router in ADMIN_ROUTERS for path in route_paths(router)]
    aggregate_paths = route_paths(admin.router)

    assert len(feature_paths) == len(aggregate_paths)
    assert sorted(feature_paths) == sorted(aggregate_paths)
    assert "/api/tokens" in route_paths(tokens.router)
    assert "/api/admin/managed-apikeys" in route_paths(api_keys.router)
    assert "/api/admin/extension/workers" in route_paths(workers.router)
    assert "/api/admin/runway/config" in route_paths(admin_runway.router)
    assert "/api/admin/geminigen/config" in route_paths(admin_geminigen.router)


def test_admin_routes_do_not_expose_mutable_service_globals() -> None:
    forbidden = {
        "api_key_manager",
        "concurrency_manager",
        "db",
        "generation_handler",
        "geminigen_service",
        "google_drive_backup_service",
        "proxy_manager",
        "runway_service",
        "set_dependencies",
        "token_manager",
    }

    assert forbidden.isdisjoint(vars(admin))
