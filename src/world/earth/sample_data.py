"""Loader and deterministic chunk projection for sample Earth big-data."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Final


SAMPLE_DATA_PATH: Final[Path] = Path(__file__).with_name("sample_bigdata.json")
SAMPLE_SCHEMA_VERSION: Final[str] = "earth-bigdata-sample.v1"


@dataclass(frozen=True, slots=True)
class EarthSamplePaletteEntry:
    block_type_id: str
    label: str
    solid: bool
    placeable: bool
    breakable: bool
    color: str
    terrain_subtype: str | None


@dataclass(frozen=True, slots=True)
class EarthSampleWaterBody:
    minimum_x: int
    maximum_x: int
    minimum_z: int
    maximum_z: int
    bed_surface_y: int
    water_surface_y: int

    def contains(self, x: int, z: int) -> bool:
        return (
            self.minimum_x <= x <= self.maximum_x
            and self.minimum_z <= z <= self.maximum_z
        )


@dataclass(frozen=True, slots=True)
class EarthSampleData:
    source_path: str
    source_fingerprint: str
    query_coordinate: tuple[float, float, float]
    spawn_local_m: tuple[float, float, float]
    coverage: tuple[int, int, int, int]
    palette: tuple[EarthSamplePaletteEntry, ...]
    land_surface_y: int
    humus_depth: int
    soil_depth: int
    minimum_terrain_y: int
    water_bodies: tuple[EarthSampleWaterBody, ...]

    @property
    def palette_ids(self) -> tuple[str, ...]:
        return tuple(entry.block_type_id for entry in self.palette)

    def block_type_at(self, x: int, y: int, z: int) -> str | None:
        minimum_x, maximum_x, minimum_z, maximum_z = self.coverage
        if not (
            minimum_x <= x <= maximum_x
            and minimum_z <= z <= maximum_z
        ):
            return None

        water = next(
            (body for body in self.water_bodies if body.contains(x, z)),
            None,
        )
        surface_y = (
            water.bed_surface_y
            if water is not None
            else self.land_surface_y
        )

        if (
            water is not None
            and surface_y < y <= water.water_surface_y
        ):
            return "system_water"
        if y > surface_y or y < self.minimum_terrain_y:
            return None
        if y > surface_y - self.humus_depth:
            return "system_terrain_humus"
        if y > surface_y - self.humus_depth - self.soil_depth:
            return "system_terrain_soil"
        return "system_terrain_rock"

    def generate_chunk(
        self,
        *,
        chunk_size: int,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
    ) -> tuple[tuple[int, ...], tuple[str, ...], str]:
        palette = self.palette_ids
        palette_values = {
            block_type_id: index + 1
            for index, block_type_id in enumerate(palette)
        }
        cells = [0] * (chunk_size ** 3)
        origin_x = chunk_x * chunk_size
        origin_y = chunk_y * chunk_size
        origin_z = chunk_z * chunk_size

        for local_z in range(chunk_size):
            world_z = origin_z + local_z
            for local_y in range(chunk_size):
                world_y = origin_y + local_y
                row_offset = chunk_size * (
                    local_y + chunk_size * local_z
                )
                for local_x in range(chunk_size):
                    block_type_id = self.block_type_at(
                        origin_x + local_x,
                        world_y,
                        world_z,
                    )
                    if block_type_id is not None:
                        cells[row_offset + local_x] = palette_values[
                            block_type_id
                        ]

        fingerprint_payload = {
            "sampleFingerprint": self.source_fingerprint,
            "chunk": [chunk_x, chunk_y, chunk_z],
            "chunkSize": chunk_size,
            "palette": palette,
            "cellsSha256": sha256(bytes(cells)).hexdigest(),
        }
        fingerprint = sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        return tuple(cells), palette, fingerprint


def _required_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object.")
    return value


@lru_cache(maxsize=1)
def get_earth_sample_data() -> EarthSampleData:
    raw_bytes = SAMPLE_DATA_PATH.read_bytes()
    raw = json.loads(raw_bytes.decode("utf-8"))
    if raw.get("schemaVersion") != SAMPLE_SCHEMA_VERSION:
        raise ValueError("Unsupported Earth sample-data schemaVersion.")

    query = _required_mapping(raw.get("query"), "query")
    coordinate = _required_mapping(query.get("coordinate"), "query.coordinate")
    response = _required_mapping(raw.get("response"), "response")
    spawn = _required_mapping(
        response.get("spawnLocalM"),
        "response.spawnLocalM",
    )
    coverage = _required_mapping(
        response.get("coverageLocalBlocks"),
        "response.coverageLocalBlocks",
    )
    terrain = _required_mapping(response.get("terrain"), "response.terrain")

    palette = tuple(
        EarthSamplePaletteEntry(
            block_type_id=str(entry["blockTypeId"]),
            label=str(entry["label"]),
            solid=bool(entry["solid"]),
            placeable=bool(entry["placeable"]),
            breakable=bool(entry["breakable"]),
            color=str(entry["color"]),
            terrain_subtype=(
                str(entry["terrainSubtype"])
                if entry.get("terrainSubtype") is not None
                else None
            ),
        )
        for entry in response.get("palette", ())
        if isinstance(entry, dict)
    )
    if len(palette) != len({entry.block_type_id for entry in palette}):
        raise ValueError("Earth sample palette IDs must be unique.")

    water_bodies = tuple(
        EarthSampleWaterBody(
            minimum_x=int(entry["minimumX"]),
            maximum_x=int(entry["maximumX"]),
            minimum_z=int(entry["minimumZ"]),
            maximum_z=int(entry["maximumZ"]),
            bed_surface_y=int(entry["bedSurfaceY"]),
            water_surface_y=int(entry["waterSurfaceY"]),
        )
        for entry in response.get("waterBodies", ())
        if isinstance(entry, dict)
    )

    return EarthSampleData(
        source_path=str(SAMPLE_DATA_PATH),
        source_fingerprint=sha256(raw_bytes).hexdigest(),
        query_coordinate=(
            float(coordinate["longitude"]),
            float(coordinate["latitude"]),
            float(coordinate["heightM"]),
        ),
        spawn_local_m=(
            float(spawn["x"]),
            float(spawn["y"]),
            float(spawn["z"]),
        ),
        coverage=(
            int(coverage["minimumX"]),
            int(coverage["maximumX"]),
            int(coverage["minimumZ"]),
            int(coverage["maximumZ"]),
        ),
        palette=palette,
        land_surface_y=int(terrain["landSurfaceY"]),
        humus_depth=int(terrain["humusDepth"]),
        soil_depth=int(terrain["soilDepth"]),
        minimum_terrain_y=int(terrain["minimumTerrainY"]),
        water_bodies=water_bodies,
    )


__all__ = [
    "EarthSampleData",
    "EarthSamplePaletteEntry",
    "EarthSampleWaterBody",
    "SAMPLE_DATA_PATH",
    "SAMPLE_SCHEMA_VERSION",
    "get_earth_sample_data",
]
