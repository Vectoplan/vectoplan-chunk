# services/vectoplan-chunk/routes/earth_debug.py
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from src.georeferencing.contracts import (
    GlobalCoordinate,
    GlobalReferencePoint,
)
from src.georeferencing.crs import (
    canonical_geographic_crs,
    configure_proj_network,
)
from src.world.earth.provider import get_earth_world_provider
from src.world.earth.validator import load_earth_world_definition


earth_debug_bp = Blueprint(
    "earth_debug",
    __name__,
    url_prefix="/debug/earth",
)


def _query_decimal(name: str, default: str) -> str:
    value = request.args.get(name, default)
    return str(value).strip()


@earth_debug_bp.get("")
def earth_debug():
    if not current_app.debug:
        return jsonify(
            {
                "ok": False,
                "error": "Earth debug route is only available in debug mode.",
            }
        ), 404

    try:
        configure_proj_network(enabled=False)

        definition = load_earth_world_definition()
        grid_definition = definition.to_earth_grid_definition()
        geographic_crs = canonical_geographic_crs()

        reference_coordinate = GlobalCoordinate.from_values(
            _query_decimal("refLon", "11.576123456"),
            _query_decimal("refLat", "48.137654321"),
            _query_decimal("refHeight", "560.237"),
        )

        target_coordinate = GlobalCoordinate.from_values(
            _query_decimal("lon", "11.577"),
            _query_decimal("lat", "48.138"),
            _query_decimal("height", "561"),
        )

        reference = GlobalReferencePoint(
            coordinate=reference_coordinate,
            crs=geographic_crs,
            grid=grid_definition.grid,
            reference_version=1,
            source="earth-debug-route",
        )

        provider = get_earth_world_provider(
            "world_spawn",
            reference,
            definition=definition,
        )

        generated_chunk = provider.generate_chunk((0, 0, 0))

        local_result = provider.global_to_local(
            target_coordinate,
            geographic_crs,
        )

        roundtrip_result = provider.local_to_global(
            local_result.local_position,
            target_crs=geographic_crs,
        )

        spawn = provider.resolve_spawn_from_global(
            target_coordinate,
            geographic_crs,
        )

        return jsonify(
            {
                "ok": True,
                "provider": {
                    "worldId": provider.world_id,
                    "providerId": provider.provider_id,
                    "templateId": provider.template_id,
                    "providerWorldId": provider.provider_world_id,
                    "providerCacheKey": provider.provider_cache_key,
                },
                "reference": reference.to_dict(),
                "storageOrigin": provider.frame.storage_origin.to_dict(),
                "referenceLocalPosition": (
                    provider.frame.reference_local_position.to_dict()
                ),
                "target": target_coordinate.to_dict(),
                "local": local_result.local_position.to_dict(),
                "roundtrip": (
                    roundtrip_result.target_coordinate.to_dict()
                ),
                "spawn": spawn.to_dict(),
                "chunk": generated_chunk.to_dict(
                    include_cells=False
                ),
            }
        )

    except Exception as error:
        return jsonify(
            {
                "ok": False,
                "error": {
                    "type": type(error).__name__,
                    "code": str(getattr(error, "code", "")) or None,
                    "message": str(error),
                },
            }
        ), 500