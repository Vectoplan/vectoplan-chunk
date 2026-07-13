# services/vectoplan-chunk/models/world.py
"""
SQLAlchemy model for concrete VECTOPLAN Chunk world instances.

A WorldInstance is the persistent editable world inside a Universe. It is not
the same thing as a provider/template world.

Current intended hierarchy:

    Project
      -> Universe
          -> WorldInstance
              -> ChunkSnapshot
              -> WorldCommandLog
              -> ChunkEvent

Important design rules:
- `id` is the internal database primary key.
- `project_db_id` references `projects.id`.
- `universe_db_id` references `universes.id`.
- `world_id` is the stable public/API identifier inside one universe.
- `world_id` is only unique per universe, not globally.
- `world_spawn` or generated `chk_wld_...` is a concrete editable world.
- `flat` and `earth` are provider/template worlds and must be stored as
  `template_id` / `provider_world_id`, not as the concrete `world_id`.
- Earth WorldInstances persist exactly one global reference contract; blocks,
  chunks, events, commands, objects, players and spawn remain local.
- This model stores world configuration, not chunk cells.
- Chunk cells are stored in ChunkSnapshot.
- This model does not perform commits.
- Repository/service/route/bootstrap layers own database transactions.
"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Dict, Optional
from uuid import uuid4


try:
    from extensions import db
except Exception as exc:  # pragma: no cover - import failure should be explicit at app startup
    db = None  # type: ignore[assignment]
    _DB_IMPORT_ERROR = exc
else:
    _DB_IMPORT_ERROR = None


if db is None:  # pragma: no cover
    raise RuntimeError(
        "Could not import `db` from `extensions` while loading "
        "models/world.py. Ensure extensions.py exposes a Flask-SQLAlchemy "
        "`db` instance before importing models."
    ) from _DB_IMPORT_ERROR


try:
    from sqlalchemy.dialects.postgresql import JSONB
except Exception:  # pragma: no cover - fallback is useful for tests/non-postgres tooling
    JSONB = None  # type: ignore[assignment]


try:
    JSON_COLUMN_TYPE = (
        JSONB()
        .with_variant(db.JSON(), "sqlite")
        .with_variant(db.JSON(), "mysql")
    ) if JSONB is not None else db.JSON
except Exception:  # pragma: no cover
    JSON_COLUMN_TYPE = db.JSON


try:
    NULLABLE_JSON_COLUMN_TYPE = (
        JSONB(none_as_null=True)
        .with_variant(db.JSON(none_as_null=True), "sqlite")
        .with_variant(db.JSON(none_as_null=True), "mysql")
    ) if JSONB is not None else db.JSON(none_as_null=True)
except Exception:  # pragma: no cover
    NULLABLE_JSON_COLUMN_TYPE = db.JSON(none_as_null=True)


WORLD_INSTANCE_SCHEMA_VERSION = "world-instance.schema.v3"

WORLD_STATUS_ACTIVE = "active"
WORLD_STATUS_ARCHIVED = "archived"
WORLD_STATUS_DELETED = "deleted"

VALID_WORLD_STATUSES = frozenset(
    {
        WORLD_STATUS_ACTIVE,
        WORLD_STATUS_ARCHIVED,
        WORLD_STATUS_DELETED,
    }
)

WORLD_TYPE_RUNTIME = "runtime-world"
WORLD_TYPE_TEMPLATE_INSTANCE = "template-instance"
WORLD_TYPE_IMPORTED = "imported-world"
WORLD_TYPE_SIMULATION = "simulation-world"

VALID_WORLD_TYPES = frozenset(
    {
        WORLD_TYPE_RUNTIME,
        WORLD_TYPE_TEMPLATE_INSTANCE,
        WORLD_TYPE_IMPORTED,
        WORLD_TYPE_SIMULATION,
    }
)

WORLD_SCOPE_PROJECT = "project"

VALID_WORLD_SCOPES = frozenset(
    {
        WORLD_SCOPE_PROJECT,
    }
)

WORLD_ROLE_DEFAULT_SPAWN = "default_spawn"
WORLD_ROLE_DESIGN = "design"
WORLD_ROLE_SITE = "site"
WORLD_ROLE_INTERIOR = "interior"
WORLD_ROLE_IMPORTED = "imported"
WORLD_ROLE_SIMULATION = "simulation"
WORLD_ROLE_SANDBOX = "sandbox"

VALID_WORLD_ROLES = frozenset(
    {
        WORLD_ROLE_DEFAULT_SPAWN,
        WORLD_ROLE_DESIGN,
        WORLD_ROLE_SITE,
        WORLD_ROLE_INTERIOR,
        WORLD_ROLE_IMPORTED,
        WORLD_ROLE_SIMULATION,
        WORLD_ROLE_SANDBOX,
    }
)

DEFAULT_WORLD_ID = "world_spawn"
DEFAULT_WORLD_SLUG = "spawn"
DEFAULT_WORLD_NAME = "Flat Spawn World"

DEFAULT_TEMPLATE_ID = "flat"
DEFAULT_PROVIDER_ID = "flat"
DEFAULT_PROVIDER_WORLD_ID = "flat"

EARTH_TEMPLATE_ID = "earth"
EARTH_PROVIDER_ID = "earth"
EARTH_PROVIDER_WORLD_ID = "earth"
EARTH_WORLD_NAME = "Earth Spawn World"

EARTH_GENERATOR_TYPE = "earth-flat-periodic"
EARTH_GENERATOR_VERSION = "1"
EARTH_PROJECTION_TYPE = "vectoplan-periodic-equirectangular"
EARTH_TOPOLOGY_TYPE = "periodic-x-v1"
EARTH_COORDINATE_SYSTEM = "vectoplan-earth-grid-v1"
EARTH_GRID_ID = "vectoplan-earth-grid"
EARTH_GRID_VERSION = "1"

PROVIDER_LIKE_WORLD_IDS = frozenset(
    {
        DEFAULT_TEMPLATE_ID,
        DEFAULT_PROVIDER_ID,
        DEFAULT_PROVIDER_WORLD_ID,
        EARTH_TEMPLATE_ID,
        EARTH_PROVIDER_ID,
        EARTH_PROVIDER_WORLD_ID,
    }
)

DEFAULT_GENERATOR_TYPE = "flat-world"
DEFAULT_GENERATOR_VERSION = "1"
DEFAULT_PROJECTION_TYPE = "flat-local-v1"
DEFAULT_TOPOLOGY_TYPE = "flat-unbounded-v1"
DEFAULT_COORDINATE_SYSTEM = "vectoplan-world-y-up-v1"

DEFAULT_CHUNK_SIZE = 16
DEFAULT_CELL_SIZE = 1.0
DEFAULT_SURFACE_Y = 0
DEFAULT_MIN_Y = -8
DEFAULT_MAX_Y = 64

DEFAULT_BLOCK_REGISTRY_ID = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION = "1"

DEFAULT_SPAWN_X = 0
DEFAULT_SPAWN_Y = 2
DEFAULT_SPAWN_Z = 0
DEFAULT_SPAWN_YAW = 0.0
DEFAULT_SPAWN_PITCH = 0.0
DEFAULT_SPAWN_COORDINATE_SPACE = "local_block"
EARTH_SPAWN_COORDINATE_SPACE = "local_metric"
VALID_SPAWN_COORDINATE_SPACES = frozenset(
    {
        DEFAULT_SPAWN_COORDINATE_SPACE,
        EARTH_SPAWN_COORDINATE_SPACE,
    }
)

WORLD_ID_MAX_LENGTH = 96
WORLD_SLUG_MAX_LENGTH = 120
WORLD_NAME_MAX_LENGTH = 255
WORLD_DESCRIPTION_MAX_LENGTH = 4096
WORLD_TYPE_MAX_LENGTH = 64
WORLD_ROLE_MAX_LENGTH = 64
WORLD_SCOPE_MAX_LENGTH = 64
WORLD_TEMPLATE_ID_MAX_LENGTH = 96
WORLD_PROVIDER_ID_MAX_LENGTH = 96
WORLD_PROVIDER_WORLD_ID_MAX_LENGTH = 96
WORLD_GENERATOR_TYPE_MAX_LENGTH = 96
WORLD_GENERATOR_VERSION_MAX_LENGTH = 64
WORLD_PROJECTION_TYPE_MAX_LENGTH = 96
WORLD_TOPOLOGY_TYPE_MAX_LENGTH = 96
WORLD_COORDINATE_SYSTEM_MAX_LENGTH = 128
WORLD_SEED_MAX_LENGTH = 128
WORLD_BLOCK_REGISTRY_ID_MAX_LENGTH = 128
WORLD_BLOCK_REGISTRY_VERSION_MAX_LENGTH = 64
WORLD_USER_ID_MAX_LENGTH = 128
WORLD_SOURCE_SERVICE_MAX_LENGTH = 96
WORLD_EXTERNAL_REF_MAX_LENGTH = 128
WORLD_GLOBAL_REFERENCE_FINGERPRINT_LENGTH = 64
WORLD_GLOBAL_REFERENCE_REASON_MAX_LENGTH = 256
WORLD_SPAWN_COORDINATE_SPACE_MAX_LENGTH = 32
WORLD_PRECISE_COORDINATE_PRECISION = 50
WORLD_PRECISE_COORDINATE_SCALE = 20

PUBLIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def datetime_to_iso(value: Optional[datetime]) -> Optional[str]:
    """Serialize datetime values safely for API responses."""
    if value is None:
        return None

    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(value)


def make_json_safe(value: Any) -> Any:
    """
    Convert arbitrary values to JSON-safe structures.

    This is intentionally defensive because metadata can later contain values
    coming from editor, importers, scripts or integration layers.
    """
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, datetime):
        return datetime_to_iso(value)

    if isinstance(value, Mapping):
        safe_dict: Dict[str, Any] = {}
        for key, item in value.items():
            try:
                safe_key = str(key)
            except Exception:
                safe_key = "<unserializable-key>"
            safe_dict[safe_key] = make_json_safe(item)
        return safe_dict

    if isinstance(value, (list, tuple, set, frozenset)):
        return [make_json_safe(item) for item in value]

    try:
        return str(value)
    except Exception:
        return "<unserializable-value>"


def normalize_optional_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> Optional[str]:
    """Normalize optional text values."""
    if value is None:
        return None

    try:
        text = str(value).strip()
    except Exception as exc:
        raise ValueError(f"{field_name} must be text-like.") from exc

    if not text:
        return None

    if len(text) > max_length:
        raise ValueError(
            f"{field_name} must not exceed {max_length} characters."
        )

    return text


def normalize_required_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    """Normalize required text values."""
    text = normalize_optional_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )

    if text is None:
        raise ValueError(f"{field_name} is required.")

    return text


def normalize_public_id(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    """
    Normalize public API identifiers.

    Allowed:
    - letters
    - numbers
    - underscore
    - dash
    - dot
    - colon

    The first character must be alphanumeric.
    """
    text = normalize_required_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )

    if not PUBLIC_ID_PATTERN.match(text):
        raise ValueError(
            f"{field_name} may only contain letters, numbers, underscores, "
            "dashes, dots and colons, and must start with a letter or number."
        )

    return text


def is_provider_like_world_id(value: Any) -> bool:
    """
    Return true if a value looks like a provider/template id rather than a
    concrete editable world id.
    """
    try:
        text = str(value).strip().lower()
    except Exception:
        return False

    if not text:
        return False

    return text in PROVIDER_LIKE_WORLD_IDS


def normalize_world_id(value: Any) -> str:
    """Normalize a public concrete world id."""
    return normalize_public_id(
        value,
        field_name="world_id",
        max_length=WORLD_ID_MAX_LENGTH,
    )


def normalize_concrete_world_id(
    value: Any,
    *,
    default: str = DEFAULT_WORLD_ID,
    field_name: str = "world_id",
    allow_provider_like: bool = False,
) -> str:
    """
    Normalize a concrete editable world id.

    By default this rejects provider/template ids such as `flat`. This protects
    the bootstrap path from accidentally using provider ids as concrete world ids.
    """
    fallback = normalize_world_id(default)
    candidate = normalize_world_id(value or fallback)

    if not allow_provider_like and is_provider_like_world_id(candidate):
        return fallback

    return candidate


def normalize_template_id(value: Any) -> str:
    """Normalize a provider/template id such as `flat`."""
    return normalize_public_id(
        value,
        field_name="template_id",
        max_length=WORLD_TEMPLATE_ID_MAX_LENGTH,
    )


def normalize_provider_id(value: Any) -> str:
    """Normalize a provider id such as `flat`."""
    return normalize_public_id(
        value,
        field_name="provider_id",
        max_length=WORLD_PROVIDER_ID_MAX_LENGTH,
    )


def normalize_provider_world_id(value: Any) -> str:
    """Normalize a provider world id such as `flat`."""
    return normalize_public_id(
        value,
        field_name="provider_world_id",
        max_length=WORLD_PROVIDER_WORLD_ID_MAX_LENGTH,
    )


def normalize_slug(value: Any) -> Optional[str]:
    """Normalize optional world slugs."""
    text = normalize_optional_text(
        value,
        field_name="slug",
        max_length=WORLD_SLUG_MAX_LENGTH,
    )

    if text is None:
        return None

    if not SLUG_PATTERN.match(text):
        raise ValueError(
            "slug may only contain letters, numbers, underscores and dashes, "
            "and must start with a letter or number."
        )

    return text


def normalize_status(value: Any) -> str:
    """Normalize and validate world status."""
    if value is None:
        return WORLD_STATUS_ACTIVE

    try:
        status = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("status must be text-like.") from exc

    if status not in VALID_WORLD_STATUSES:
        allowed = ", ".join(sorted(VALID_WORLD_STATUSES))
        raise ValueError(f"Invalid world status '{value}'. Allowed: {allowed}.")

    return status


def normalize_world_type(value: Any) -> str:
    """Normalize and validate world type."""
    if value is None:
        return WORLD_TYPE_RUNTIME

    try:
        world_type = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("world_type must be text-like.") from exc

    if world_type not in VALID_WORLD_TYPES:
        allowed = ", ".join(sorted(VALID_WORLD_TYPES))
        raise ValueError(f"Invalid world type '{value}'. Allowed: {allowed}.")

    return world_type


def normalize_world_role(value: Any) -> str:
    """Normalize and validate world role."""
    if value is None:
        return WORLD_ROLE_DEFAULT_SPAWN

    try:
        role = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("world_role must be text-like.") from exc

    if role not in VALID_WORLD_ROLES:
        allowed = ", ".join(sorted(VALID_WORLD_ROLES))
        raise ValueError(f"Invalid world role '{value}'. Allowed: {allowed}.")

    return role


def normalize_world_scope(value: Any) -> str:
    """Normalize and validate world scope."""
    if value is None:
        return WORLD_SCOPE_PROJECT

    try:
        scope = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("world_scope must be text-like.") from exc

    if scope not in VALID_WORLD_SCOPES:
        allowed = ", ".join(sorted(VALID_WORLD_SCOPES))
        raise ValueError(f"Invalid world scope '{value}'. Allowed: {allowed}.")

    return scope


def normalize_version_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
    default: str,
) -> str:
    """
    Normalize version strings.

    Version strings are deliberately less strict than public ids because later
    systems may use semver-like values such as `1.0.0`.
    """
    if value is None:
        value = default

    return normalize_required_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )


def normalize_metadata(value: Any) -> Dict[str, Any]:
    """
    Normalize metadata into a JSON-safe object.

    Metadata must be an object at the top level. This keeps database content
    predictable and avoids storing lists/scalars where later services expect
    key/value data.
    """
    if value is None:
        return {}

    if not isinstance(value, Mapping):
        raise ValueError("metadata_json must be a JSON object/dict.")

    return make_json_safe(dict(value))


def normalize_db_id(value: Any, *, field_name: str) -> int:
    """Normalize an internal database id."""
    if value is None:
        raise ValueError(f"{field_name} is required.")

    try:
        db_id = int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc

    if db_id <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")

    return db_id


def normalize_positive_int(
    value: Any,
    *,
    field_name: str,
    default: int,
) -> int:
    """Normalize positive integer configuration values."""
    if value is None:
        value = default

    try:
        integer_value = int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc

    if integer_value <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")

    return integer_value


def normalize_int(
    value: Any,
    *,
    field_name: str,
    default: int,
) -> int:
    """Normalize integer configuration values."""
    if value is None:
        value = default

    try:
        return int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc


def normalize_float(
    value: Any,
    *,
    field_name: str,
    default: float,
) -> float:
    """Normalize finite float configuration values."""
    if value is None:
        value = default

    try:
        float_value = float(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a number.") from exc

    if not math.isfinite(float_value):
        raise ValueError(f"{field_name} must be finite.")

    return float_value


def normalize_positive_float(
    value: Any,
    *,
    field_name: str,
    default: float,
) -> float:
    """Normalize positive float configuration values."""
    float_value = normalize_float(
        value,
        field_name=field_name,
        default=default,
    )

    if float_value <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")

    return float_value



def normalize_spawn_coordinate_space(
    value: Any,
    *,
    default: str = DEFAULT_SPAWN_COORDINATE_SPACE,
) -> str:
    """Normalize the persisted spawn coordinate-space identifier."""
    if value is None:
        value = default

    try:
        coordinate_space = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("spawn_coordinate_space must be text-like.") from exc

    if coordinate_space not in VALID_SPAWN_COORDINATE_SPACES:
        allowed = ", ".join(sorted(VALID_SPAWN_COORDINATE_SPACES))
        raise ValueError(
            f"Invalid spawn_coordinate_space '{value}'. Allowed: {allowed}."
        )

    return coordinate_space


def normalize_decimal_coordinate(
    value: Any,
    *,
    field_name: str,
    required: bool = True,
) -> Optional[Decimal]:
    """Normalize a finite coordinate into a bounded Decimal."""
    if value is None:
        if required:
            raise ValueError(f"{field_name} is required.")
        return None

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite decimal number.")

    try:
        if isinstance(value, Decimal):
            decimal_value = value
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{field_name} must be finite.")
            decimal_value = Decimal(str(value))
        else:
            decimal_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(
            f"{field_name} must be a finite decimal number."
        ) from exc

    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be finite.")

    if len(decimal_value.as_tuple().digits) > WORLD_PRECISE_COORDINATE_PRECISION:
        raise ValueError(
            f"{field_name} must not exceed "
            f"{WORLD_PRECISE_COORDINATE_PRECISION} significant digits."
        )

    exponent = decimal_value.as_tuple().exponent
    if exponent < -WORLD_PRECISE_COORDINATE_SCALE:
        quantum = Decimal(1).scaleb(-WORLD_PRECISE_COORDINATE_SCALE)
        decimal_value = decimal_value.quantize(quantum)

    return decimal_value


def decimal_to_plain_text(value: Optional[Decimal]) -> Optional[str]:
    """Serialize Decimal values without exponent notation."""
    if value is None:
        return None

    decimal_value = normalize_decimal_coordinate(
        value,
        field_name="decimal",
    )
    assert decimal_value is not None

    if decimal_value == 0:
        return "0"

    text = format(decimal_value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def decimal_floor_to_int(value: Decimal, *, field_name: str) -> int:
    """Return the containing integer block for a precise local coordinate."""
    decimal_value = normalize_decimal_coordinate(
        value,
        field_name=field_name,
    )
    assert decimal_value is not None
    return int(decimal_value.to_integral_value(rounding=ROUND_FLOOR))


def normalize_sha256(value: Any, *, field_name: str) -> str:
    """Normalize a lowercase SHA-256 fingerprint."""
    text = normalize_required_text(
        value,
        field_name=field_name,
        max_length=WORLD_GLOBAL_REFERENCE_FINGERPRINT_LENGTH,
    ).lower()

    if len(text) != WORLD_GLOBAL_REFERENCE_FINGERPRINT_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{field_name} must be a SHA-256 fingerprint.")

    return text


def normalize_reference_lock_reasons(value: Any) -> list[str]:
    """Normalize persisted reference-lock reasons."""
    if value is None:
        return []

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value,
        Iterable,
    ):
        raise ValueError(
            "global_reference_lock_reasons_json must be a list of strings."
        )

    normalized: list[str] = []
    seen: set[str] = set()

    for index, item in enumerate(value):
        reason = normalize_required_text(
            item,
            field_name=f"global_reference_lock_reasons[{index}]",
            max_length=WORLD_GLOBAL_REFERENCE_REASON_MAX_LENGTH,
        )
        if reason in seen:
            continue
        seen.add(reason)
        normalized.append(reason)

    return normalized


def _import_first_available(*module_names: str) -> Any:
    """Import the first available module without hiding the final failure."""
    last_error: Optional[BaseException] = None

    for module_name in module_names:
        try:
            return importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError) as exc:
            last_error = exc

    raise RuntimeError(
        f"Could not import any required module: {', '.join(module_names)}."
    ) from last_error


def coerce_global_reference(value: Any) -> Any:
    """Coerce a mapping or contract object into GlobalReferencePoint."""
    contracts = _import_first_available(
        "src.georeferencing.contracts",
        "georeferencing.contracts",
    )
    reference_type = getattr(contracts, "GlobalReferencePoint")

    if isinstance(value, reference_type):
        return value

    if isinstance(value, Mapping):
        return reference_type.from_mapping(value)

    raise ValueError(
        "global_reference must be a GlobalReferencePoint or JSON object."
    )


def normalize_global_reference_storage(
    value: Any,
) -> tuple[Dict[str, Any], str, int]:
    """Return canonical JSON, fingerprint and frame revision."""
    reference = coerce_global_reference(value)
    payload = reference.to_persistence_dict()

    if not isinstance(payload, Mapping):
        raise ValueError(
            "GlobalReferencePoint.to_persistence_dict() must return an object."
        )

    fingerprint = normalize_sha256(
        reference.fingerprint,
        field_name="global_reference_fingerprint",
    )
    revision = normalize_positive_int(
        reference.reference_version,
        field_name="coordinate_frame_revision",
        default=1,
    )

    return (
        normalize_metadata(payload),
        fingerprint,
        revision,
    )


def validate_vertical_bounds(*, min_y: int, surface_y: int, max_y: int) -> None:
    """Validate vertical world bounds."""
    if min_y > surface_y:
        raise ValueError("min_y must be less than or equal to surface_y.")

    if surface_y > max_y:
        raise ValueError("surface_y must be less than or equal to max_y.")


def generate_world_id(prefix: str = "world") -> str:
    """
    Generate a stable public world identifier.

    Example:
        world_2f2f7a1c9d3b4a44a5d9e41910b51e70
    """
    normalized_prefix = normalize_public_id(
        prefix,
        field_name="world_id_prefix",
        max_length=24,
    )
    return f"{normalized_prefix}_{uuid4().hex}"


def _payload_metadata_value(payload: Mapping[str, Any]) -> Any:
    """Read metadata payload from several compatible keys."""
    if "metadataJson" in payload:
        return payload.get("metadataJson")
    if "metadata_json" in payload:
        return payload.get("metadata_json")
    if "metadata" in payload:
        return payload.get("metadata")
    if "worldMetadata" in payload:
        return payload.get("worldMetadata")
    if "world_metadata" in payload:
        return payload.get("world_metadata")
    return None


def _payload_get(payload: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """Read first available payload value."""
    for key in keys:
        if key in payload:
            return payload.get(key)
    return default


def _resolve_project_universe_db_ids(
    *,
    project: Any = None,
    universe: Any = None,
    project_db_id: Any = None,
    universe_db_id: Any = None,
) -> tuple[int, int]:
    """Resolve internal project/universe ids from either objects or explicit ids."""
    resolved_project_db_id = project_db_id
    resolved_universe_db_id = universe_db_id

    if resolved_project_db_id is None and project is not None:
        resolved_project_db_id = getattr(project, "id", None)

    if resolved_universe_db_id is None and universe is not None:
        resolved_universe_db_id = getattr(universe, "id", None)

    if resolved_project_db_id is None and universe is not None:
        resolved_project_db_id = getattr(universe, "project_db_id", None)

    return (
        normalize_db_id(resolved_project_db_id, field_name="project_db_id"),
        normalize_db_id(resolved_universe_db_id, field_name="universe_db_id"),
    )


class WorldInstance(db.Model):
    """
    Persistent concrete VECTOPLAN Chunk world instance.

    The default app-integrated flow creates:

        Project(external_app_project_id="prj_...")
          -> Universe(universe_id="chk_uni_...")
              -> WorldInstance(
                     world_id="chk_wld_..." or "world_spawn",
                     template_id="flat",
                     provider_world_id="flat",
                     generator_type="flat-world"
                 )

    Important:
    - this model stores world configuration and provider mapping,
    - this model does not store generated chunks,
    - unchanged chunks are generated from provider/template config,
    - changed chunks are stored as ChunkSnapshot.
    """

    __tablename__ = "world_instances"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    project_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    universe_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("universes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    world_id = db.Column(
        db.String(WORLD_ID_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    slug = db.Column(
        db.String(WORLD_SLUG_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    name = db.Column(
        db.String(WORLD_NAME_MAX_LENGTH),
        nullable=False,
    )

    description = db.Column(
        db.String(WORLD_DESCRIPTION_MAX_LENGTH),
        nullable=True,
    )

    status = db.Column(
        db.String(32),
        nullable=False,
        default=WORLD_STATUS_ACTIVE,
        index=True,
    )

    schema_version = db.Column(
        db.String(64),
        nullable=False,
        default=WORLD_INSTANCE_SCHEMA_VERSION,
    )

    revision = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )

    world_type = db.Column(
        db.String(WORLD_TYPE_MAX_LENGTH),
        nullable=False,
        default=WORLD_TYPE_RUNTIME,
        index=True,
    )

    world_role = db.Column(
        db.String(WORLD_ROLE_MAX_LENGTH),
        nullable=False,
        default=WORLD_ROLE_DEFAULT_SPAWN,
        index=True,
    )

    world_scope = db.Column(
        db.String(WORLD_SCOPE_MAX_LENGTH),
        nullable=False,
        default=WORLD_SCOPE_PROJECT,
        index=True,
    )

    template_id = db.Column(
        db.String(WORLD_TEMPLATE_ID_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_TEMPLATE_ID,
        index=True,
    )

    provider_id = db.Column(
        db.String(WORLD_PROVIDER_ID_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_PROVIDER_ID,
        index=True,
    )

    provider_world_id = db.Column(
        db.String(WORLD_PROVIDER_WORLD_ID_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_PROVIDER_WORLD_ID,
        index=True,
    )

    generator_type = db.Column(
        db.String(WORLD_GENERATOR_TYPE_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_GENERATOR_TYPE,
        index=True,
    )

    generator_version = db.Column(
        db.String(WORLD_GENERATOR_VERSION_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_GENERATOR_VERSION,
        index=True,
    )

    projection_type = db.Column(
        db.String(WORLD_PROJECTION_TYPE_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_PROJECTION_TYPE,
        index=True,
    )

    topology_type = db.Column(
        db.String(WORLD_TOPOLOGY_TYPE_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_TOPOLOGY_TYPE,
        index=True,
    )

    coordinate_system = db.Column(
        db.String(WORLD_COORDINATE_SYSTEM_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_COORDINATE_SYSTEM,
        index=True,
    )

    chunk_size = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_CHUNK_SIZE,
    )

    cell_size = db.Column(
        db.Float,
        nullable=False,
        default=DEFAULT_CELL_SIZE,
    )

    surface_y = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_SURFACE_Y,
    )

    min_y = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_MIN_Y,
    )

    max_y = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_MAX_Y,
    )

    seed = db.Column(
        db.String(WORLD_SEED_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    block_registry_id = db.Column(
        db.String(WORLD_BLOCK_REGISTRY_ID_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_BLOCK_REGISTRY_ID,
        index=True,
    )

    block_registry_version = db.Column(
        db.String(WORLD_BLOCK_REGISTRY_VERSION_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_BLOCK_REGISTRY_VERSION,
        index=True,
    )

    spawn_x = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_SPAWN_X,
    )

    spawn_y = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_SPAWN_Y,
    )

    spawn_z = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_SPAWN_Z,
    )

    spawn_yaw = db.Column(
        db.Float,
        nullable=False,
        default=DEFAULT_SPAWN_YAW,
    )

    spawn_pitch = db.Column(
        db.Float,
        nullable=False,
        default=DEFAULT_SPAWN_PITCH,
    )

    spawn_coordinate_space = db.Column(
        db.String(WORLD_SPAWN_COORDINATE_SPACE_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_SPAWN_COORDINATE_SPACE,
        index=True,
    )

    spawn_x_precise = db.Column(
        db.Numeric(
            WORLD_PRECISE_COORDINATE_PRECISION,
            WORLD_PRECISE_COORDINATE_SCALE,
            asdecimal=True,
        ),
        nullable=True,
    )

    spawn_y_precise = db.Column(
        db.Numeric(
            WORLD_PRECISE_COORDINATE_PRECISION,
            WORLD_PRECISE_COORDINATE_SCALE,
            asdecimal=True,
        ),
        nullable=True,
    )

    spawn_z_precise = db.Column(
        db.Numeric(
            WORLD_PRECISE_COORDINATE_PRECISION,
            WORLD_PRECISE_COORDINATE_SCALE,
            asdecimal=True,
        ),
        nullable=True,
    )

    source_service = db.Column(
        db.String(WORLD_SOURCE_SERVICE_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    external_ref = db.Column(
        db.String(WORLD_EXTERNAL_REF_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    created_by_user_id = db.Column(
        db.String(WORLD_USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    updated_by_user_id = db.Column(
        db.String(WORLD_USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    metadata_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    coordinate_frame_revision = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    global_reference_json = db.Column(
        NULLABLE_JSON_COLUMN_TYPE,
        nullable=True,
    )

    global_reference_fingerprint = db.Column(
        db.String(WORLD_GLOBAL_REFERENCE_FINGERPRINT_LENGTH),
        nullable=True,
        index=True,
    )

    global_reference_locked_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    global_reference_lock_reasons_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=list,
    )

    global_reference_updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    global_reference_updated_by_user_id = db.Column(
        db.String(WORLD_USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        index=True,
    )

    archived_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    deleted_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    project = db.relationship(
        "Project",
        backref=db.backref(
            "world_instances",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    universe = db.relationship(
        "Universe",
        backref=db.backref(
            "worlds",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    __table_args__ = (
        db.UniqueConstraint(
            "universe_db_id",
            "world_id",
            name="uq_world_instances_universe_world_id",
        ),
        db.UniqueConstraint(
            "universe_db_id",
            "slug",
            name="uq_world_instances_universe_slug",
        ),
        db.CheckConstraint(
            "project_db_id > 0",
            name="ck_world_instances_project_db_id_positive",
        ),
        db.CheckConstraint(
            "universe_db_id > 0",
            name="ck_world_instances_universe_db_id_positive",
        ),
        db.CheckConstraint(
            "world_id <> ''",
            name="ck_world_instances_world_id_not_empty",
        ),
        db.CheckConstraint(
            "name <> ''",
            name="ck_world_instances_name_not_empty",
        ),
        db.CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="ck_world_instances_status_valid",
        ),
        db.CheckConstraint(
            "world_type IN ('runtime-world', 'template-instance', 'imported-world', 'simulation-world')",
            name="ck_world_instances_world_type_valid",
        ),
        db.CheckConstraint(
            "world_role IN ('default_spawn', 'design', 'site', 'interior', 'imported', 'simulation', 'sandbox')",
            name="ck_world_instances_world_role_valid",
        ),
        db.CheckConstraint(
            "world_scope IN ('project')",
            name="ck_world_instances_world_scope_valid",
        ),
        db.CheckConstraint(
            "revision >= 1",
            name="ck_world_instances_revision_positive",
        ),
        db.CheckConstraint(
            "chunk_size > 0",
            name="ck_world_instances_chunk_size_positive",
        ),
        db.CheckConstraint(
            "cell_size > 0",
            name="ck_world_instances_cell_size_positive",
        ),
        db.CheckConstraint(
            "spawn_coordinate_space IN ('local_block', 'local_metric')",
            name="ck_world_instances_spawn_coordinate_space_valid",
        ),
        db.CheckConstraint(
            "("
            "spawn_x_precise IS NULL AND "
            "spawn_y_precise IS NULL AND "
            "spawn_z_precise IS NULL"
            ") OR ("
            "spawn_x_precise IS NOT NULL AND "
            "spawn_y_precise IS NOT NULL AND "
            "spawn_z_precise IS NOT NULL"
            ")",
            name="ck_world_instances_precise_spawn_all_or_none",
        ),
        db.CheckConstraint(
            "coordinate_frame_revision >= 0",
            name="ck_world_instances_coordinate_frame_revision_nonnegative",
        ),
        db.CheckConstraint(
            "("
            "global_reference_json IS NULL AND "
            "global_reference_fingerprint IS NULL AND "
            "coordinate_frame_revision = 0"
            ") OR ("
            "global_reference_json IS NOT NULL AND "
            "global_reference_fingerprint IS NOT NULL AND "
            "coordinate_frame_revision >= 1"
            ")",
            name="ck_world_instances_global_reference_consistent",
        ),
        db.CheckConstraint(
            "global_reference_locked_at IS NULL OR "
            "global_reference_json IS NOT NULL",
            name="ck_world_instances_reference_lock_requires_reference",
        ),
        db.CheckConstraint(
            "provider_id <> 'earth' OR global_reference_json IS NOT NULL",
            name="ck_world_instances_earth_requires_reference",
        ),
        db.CheckConstraint(
            "min_y <= surface_y",
            name="ck_world_instances_min_y_lte_surface_y",
        ),
        db.CheckConstraint(
            "surface_y <= max_y",
            name="ck_world_instances_surface_y_lte_max_y",
        ),
        db.Index(
            "ix_world_instances_project_universe_status",
            "project_db_id",
            "universe_db_id",
            "status",
        ),
        db.Index(
            "ix_world_instances_project_role",
            "project_db_id",
            "world_role",
        ),
        db.Index(
            "ix_world_instances_provider_mapping",
            "provider_id",
            "provider_world_id",
            "template_id",
        ),
        db.Index(
            "ix_world_instances_generator",
            "generator_type",
            "generator_version",
        ),
        db.Index(
            "ix_world_instances_registry",
            "block_registry_id",
            "block_registry_version",
        ),
        db.Index(
            "ix_world_instances_source_external",
            "source_service",
            "external_ref",
        ),
        db.Index(
            "ix_world_instances_reference_state",
            "provider_id",
            "global_reference_fingerprint",
            "coordinate_frame_revision",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<WorldInstance id={self.id!r} project_db_id={self.project_db_id!r} "
            f"universe_db_id={self.universe_db_id!r} world_id={self.world_id!r} "
            f"provider_world_id={self.provider_world_id!r} status={self.status!r}>"
        )

    @classmethod
    def create(
        cls,
        *,
        project_db_id: int,
        universe_db_id: int,
        world_id: Optional[str] = None,
        slug: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: str = WORLD_STATUS_ACTIVE,
        world_type: str = WORLD_TYPE_RUNTIME,
        world_role: str = WORLD_ROLE_DEFAULT_SPAWN,
        world_scope: str = WORLD_SCOPE_PROJECT,
        template_id: str = DEFAULT_TEMPLATE_ID,
        provider_id: str = DEFAULT_PROVIDER_ID,
        provider_world_id: str = DEFAULT_PROVIDER_WORLD_ID,
        generator_type: str = DEFAULT_GENERATOR_TYPE,
        generator_version: str = DEFAULT_GENERATOR_VERSION,
        projection_type: str = DEFAULT_PROJECTION_TYPE,
        topology_type: str = DEFAULT_TOPOLOGY_TYPE,
        coordinate_system: str = DEFAULT_COORDINATE_SYSTEM,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        cell_size: float = DEFAULT_CELL_SIZE,
        surface_y: int = DEFAULT_SURFACE_Y,
        min_y: int = DEFAULT_MIN_Y,
        max_y: int = DEFAULT_MAX_Y,
        seed: Optional[str] = None,
        block_registry_id: str = DEFAULT_BLOCK_REGISTRY_ID,
        block_registry_version: str = DEFAULT_BLOCK_REGISTRY_VERSION,
        spawn_x: int = DEFAULT_SPAWN_X,
        spawn_y: int = DEFAULT_SPAWN_Y,
        spawn_z: int = DEFAULT_SPAWN_Z,
        spawn_x_precise: Any = None,
        spawn_y_precise: Any = None,
        spawn_z_precise: Any = None,
        spawn_coordinate_space: Optional[str] = None,
        spawn_yaw: float = DEFAULT_SPAWN_YAW,
        spawn_pitch: float = DEFAULT_SPAWN_PITCH,
        global_reference: Any = None,
        global_reference_locked_at: Optional[datetime] = None,
        global_reference_lock_reasons: Optional[Iterable[str]] = None,
        source_service: Optional[str] = None,
        external_ref: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        allow_provider_like_world_id: bool = False,
    ) -> "WorldInstance":
        """
        Create a WorldInstance without adding it to a session.

        Earth instances must provide exactly one global reference contract.
        Flat instances remain unchanged and do not carry a global reference.

        Repository/service code is responsible for:
        - checking project/universe existence,
        - checking that universe belongs to project,
        - checking uniqueness inside the universe,
        - adding to db.session,
        - committing or rolling back.
        """
        normalized_project_db_id = normalize_db_id(
            project_db_id,
            field_name="project_db_id",
        )
        normalized_universe_db_id = normalize_db_id(
            universe_db_id,
            field_name="universe_db_id",
        )

        normalized_template_id = normalize_template_id(template_id)
        normalized_provider_id = normalize_provider_id(provider_id)
        normalized_provider_world_id = normalize_provider_world_id(
            provider_world_id
        )

        public_world_id = normalize_concrete_world_id(
            world_id or generate_world_id(),
            allow_provider_like=allow_provider_like_world_id,
        )

        normalized_name = normalize_required_text(
            name or public_world_id,
            field_name="name",
            max_length=WORLD_NAME_MAX_LENGTH,
        )

        normalized_status = normalize_status(status)

        normalized_chunk_size = normalize_positive_int(
            chunk_size,
            field_name="chunk_size",
            default=DEFAULT_CHUNK_SIZE,
        )
        normalized_cell_size = normalize_positive_float(
            cell_size,
            field_name="cell_size",
            default=DEFAULT_CELL_SIZE,
        )
        normalized_surface_y = normalize_int(
            surface_y,
            field_name="surface_y",
            default=DEFAULT_SURFACE_Y,
        )
        normalized_min_y = normalize_int(
            min_y,
            field_name="min_y",
            default=DEFAULT_MIN_Y,
        )
        normalized_max_y = normalize_int(
            max_y,
            field_name="max_y",
            default=DEFAULT_MAX_Y,
        )
        validate_vertical_bounds(
            min_y=normalized_min_y,
            surface_y=normalized_surface_y,
            max_y=normalized_max_y,
        )

        is_earth_provider = normalized_provider_id == EARTH_PROVIDER_ID

        if is_earth_provider:
            if (
                normalized_template_id != EARTH_TEMPLATE_ID
                or normalized_provider_world_id
                != EARTH_PROVIDER_WORLD_ID
            ):
                raise ValueError(
                    "Earth worlds require template_id, provider_id and "
                    "provider_world_id to all be 'earth'."
                )
            if global_reference is None:
                raise ValueError(
                    "Earth worlds require exactly one global_reference."
                )

            earth_contract_values = {
                "generator_type": (
                    normalize_required_text(
                        generator_type,
                        field_name="generator_type",
                        max_length=WORLD_GENERATOR_TYPE_MAX_LENGTH,
                    ),
                    EARTH_GENERATOR_TYPE,
                ),
                "generator_version": (
                    normalize_version_text(
                        generator_version,
                        field_name="generator_version",
                        max_length=WORLD_GENERATOR_VERSION_MAX_LENGTH,
                        default=EARTH_GENERATOR_VERSION,
                    ),
                    EARTH_GENERATOR_VERSION,
                ),
                "projection_type": (
                    normalize_required_text(
                        projection_type,
                        field_name="projection_type",
                        max_length=WORLD_PROJECTION_TYPE_MAX_LENGTH,
                    ),
                    EARTH_PROJECTION_TYPE,
                ),
                "topology_type": (
                    normalize_required_text(
                        topology_type,
                        field_name="topology_type",
                        max_length=WORLD_TOPOLOGY_TYPE_MAX_LENGTH,
                    ),
                    EARTH_TOPOLOGY_TYPE,
                ),
                "coordinate_system": (
                    normalize_required_text(
                        coordinate_system,
                        field_name="coordinate_system",
                        max_length=WORLD_COORDINATE_SYSTEM_MAX_LENGTH,
                    ),
                    EARTH_COORDINATE_SYSTEM,
                ),
                "chunk_size": (
                    normalized_chunk_size,
                    DEFAULT_CHUNK_SIZE,
                ),
                "cell_size": (
                    normalized_cell_size,
                    DEFAULT_CELL_SIZE,
                ),
            }
            mismatches = {
                field_name: {
                    "actual": actual,
                    "expected": expected,
                }
                for field_name, (actual, expected)
                in earth_contract_values.items()
                if actual != expected
            }
            if mismatches:
                raise ValueError(
                    "Earth worlds must use the fixed Earth-v1 provider, "
                    f"geometry and grid contract: {mismatches}."
                )
        elif global_reference is not None:
            raise ValueError(
                "global_reference is only supported for the earth provider."
            )

        default_spawn_space = (
            EARTH_SPAWN_COORDINATE_SPACE
            if is_earth_provider
            else DEFAULT_SPAWN_COORDINATE_SPACE
        )
        normalized_spawn_space = normalize_spawn_coordinate_space(
            spawn_coordinate_space,
            default=default_spawn_space,
        )
        if (
            is_earth_provider
            and normalized_spawn_space
            != EARTH_SPAWN_COORDINATE_SPACE
        ):
            raise ValueError(
                "Earth spawn must be persisted in local_metric coordinates."
            )

        precise_values = (
            spawn_x_precise,
            spawn_y_precise,
            spawn_z_precise,
        )
        any_precise = any(value is not None for value in precise_values)
        all_precise = all(value is not None for value in precise_values)

        if any_precise and not all_precise:
            raise ValueError(
                "spawn_x_precise, spawn_y_precise and spawn_z_precise "
                "must be provided together."
            )

        if is_earth_provider and not any_precise:
            reference = coerce_global_reference(global_reference)
            provider_module = _import_first_available(
                "src.world.earth.provider",
                "world.earth.provider",
            )
            validator_module = _import_first_available(
                "src.world.earth.validator",
                "world.earth.validator",
            )
            definition = validator_module.load_earth_world_definition()
            provider = provider_module.get_earth_world_provider(
                public_world_id,
                reference,
                definition=definition,
            )
            default_spawn = provider.default_spawn_position()
            spawn_x_precise = default_spawn.x
            spawn_y_precise = default_spawn.y
            spawn_z_precise = default_spawn.z
            any_precise = True
            all_precise = True

        normalized_spawn_x = normalize_int(
            spawn_x,
            field_name="spawn_x",
            default=DEFAULT_SPAWN_X,
        )
        normalized_spawn_y = normalize_int(
            spawn_y,
            field_name="spawn_y",
            default=DEFAULT_SPAWN_Y,
        )
        normalized_spawn_z = normalize_int(
            spawn_z,
            field_name="spawn_z",
            default=DEFAULT_SPAWN_Z,
        )

        normalized_spawn_x_precise: Optional[Decimal] = None
        normalized_spawn_y_precise: Optional[Decimal] = None
        normalized_spawn_z_precise: Optional[Decimal] = None

        if all_precise:
            normalized_spawn_x_precise = normalize_decimal_coordinate(
                spawn_x_precise,
                field_name="spawn_x_precise",
            )
            normalized_spawn_y_precise = normalize_decimal_coordinate(
                spawn_y_precise,
                field_name="spawn_y_precise",
            )
            normalized_spawn_z_precise = normalize_decimal_coordinate(
                spawn_z_precise,
                field_name="spawn_z_precise",
            )
            assert normalized_spawn_x_precise is not None
            assert normalized_spawn_y_precise is not None
            assert normalized_spawn_z_precise is not None

            normalized_spawn_x = decimal_floor_to_int(
                normalized_spawn_x_precise,
                field_name="spawn_x_precise",
            )
            normalized_spawn_y = decimal_floor_to_int(
                normalized_spawn_y_precise,
                field_name="spawn_y_precise",
            )
            normalized_spawn_z = decimal_floor_to_int(
                normalized_spawn_z_precise,
                field_name="spawn_z_precise",
            )
        elif normalized_spawn_space == EARTH_SPAWN_COORDINATE_SPACE:
            normalized_spawn_x_precise = Decimal(normalized_spawn_x)
            normalized_spawn_y_precise = Decimal(normalized_spawn_y)
            normalized_spawn_z_precise = Decimal(normalized_spawn_z)

        now = utc_now()

        instance = cls(
            project_db_id=normalized_project_db_id,
            universe_db_id=normalized_universe_db_id,
            world_id=public_world_id,
            slug=normalize_slug(slug),
            name=normalized_name,
            description=normalize_optional_text(
                description,
                field_name="description",
                max_length=WORLD_DESCRIPTION_MAX_LENGTH,
            ),
            status=normalized_status,
            schema_version=WORLD_INSTANCE_SCHEMA_VERSION,
            revision=1,
            world_type=normalize_world_type(world_type),
            world_role=normalize_world_role(world_role),
            world_scope=normalize_world_scope(world_scope),
            template_id=normalized_template_id,
            provider_id=normalized_provider_id,
            provider_world_id=normalized_provider_world_id,
            generator_type=normalize_required_text(
                generator_type,
                field_name="generator_type",
                max_length=WORLD_GENERATOR_TYPE_MAX_LENGTH,
            ),
            generator_version=normalize_version_text(
                generator_version,
                field_name="generator_version",
                max_length=WORLD_GENERATOR_VERSION_MAX_LENGTH,
                default=DEFAULT_GENERATOR_VERSION,
            ),
            projection_type=normalize_required_text(
                projection_type,
                field_name="projection_type",
                max_length=WORLD_PROJECTION_TYPE_MAX_LENGTH,
            ),
            topology_type=normalize_required_text(
                topology_type,
                field_name="topology_type",
                max_length=WORLD_TOPOLOGY_TYPE_MAX_LENGTH,
            ),
            coordinate_system=normalize_required_text(
                coordinate_system,
                field_name="coordinate_system",
                max_length=WORLD_COORDINATE_SYSTEM_MAX_LENGTH,
            ),
            chunk_size=normalized_chunk_size,
            cell_size=normalized_cell_size,
            surface_y=normalized_surface_y,
            min_y=normalized_min_y,
            max_y=normalized_max_y,
            seed=normalize_optional_text(
                seed,
                field_name="seed",
                max_length=WORLD_SEED_MAX_LENGTH,
            ),
            block_registry_id=normalize_required_text(
                block_registry_id,
                field_name="block_registry_id",
                max_length=WORLD_BLOCK_REGISTRY_ID_MAX_LENGTH,
            ),
            block_registry_version=normalize_version_text(
                block_registry_version,
                field_name="block_registry_version",
                max_length=WORLD_BLOCK_REGISTRY_VERSION_MAX_LENGTH,
                default=DEFAULT_BLOCK_REGISTRY_VERSION,
            ),
            spawn_x=normalized_spawn_x,
            spawn_y=normalized_spawn_y,
            spawn_z=normalized_spawn_z,
            spawn_x_precise=normalized_spawn_x_precise,
            spawn_y_precise=normalized_spawn_y_precise,
            spawn_z_precise=normalized_spawn_z_precise,
            spawn_coordinate_space=normalized_spawn_space,
            spawn_yaw=normalize_float(
                spawn_yaw,
                field_name="spawn_yaw",
                default=DEFAULT_SPAWN_YAW,
            ),
            spawn_pitch=normalize_float(
                spawn_pitch,
                field_name="spawn_pitch",
                default=DEFAULT_SPAWN_PITCH,
            ),
            coordinate_frame_revision=0,
            global_reference_json=None,
            global_reference_fingerprint=None,
            global_reference_locked_at=None,
            global_reference_lock_reasons_json=[],
            global_reference_updated_at=None,
            global_reference_updated_by_user_id=None,
            source_service=normalize_optional_text(
                source_service,
                field_name="source_service",
                max_length=WORLD_SOURCE_SERVICE_MAX_LENGTH,
            ),
            external_ref=normalize_optional_text(
                external_ref,
                field_name="external_ref",
                max_length=WORLD_EXTERNAL_REF_MAX_LENGTH,
            ),
            created_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="created_by_user_id",
                max_length=WORLD_USER_ID_MAX_LENGTH,
            ),
            updated_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="updated_by_user_id",
                max_length=WORLD_USER_ID_MAX_LENGTH,
            ),
            metadata_json=normalize_metadata(metadata_json),
            created_at=now,
            updated_at=now,
            archived_at=(
                now
                if normalized_status == WORLD_STATUS_ARCHIVED
                else None
            ),
            deleted_at=(
                now
                if normalized_status == WORLD_STATUS_DELETED
                else None
            ),
        )

        if global_reference is not None:
            instance.set_global_reference(
                global_reference,
                allow_replace=False,
                updated_by_user_id=created_by_user_id,
                touch=False,
            )

        if global_reference_locked_at is not None:
            if not instance.has_global_reference:
                raise ValueError(
                    "global_reference_locked_at requires global_reference."
                )
            instance.global_reference_locked_at = global_reference_locked_at
            instance.global_reference_lock_reasons_json = (
                normalize_reference_lock_reasons(
                    global_reference_lock_reasons
                )
            )

        return instance


    @classmethod
    def create_flat_spawn(
        cls,
        *,
        project: Any = None,
        universe: Any = None,
        project_db_id: Optional[int] = None,
        universe_db_id: Optional[int] = None,
        world_id: str = DEFAULT_WORLD_ID,
        slug: str = DEFAULT_WORLD_SLUG,
        name: str = DEFAULT_WORLD_NAME,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        source_service: Optional[str] = "vectoplan-chunk-bootstrap",
        external_ref: Optional[str] = None,
    ) -> "WorldInstance":
        """
        Create the default project spawn world backed by the `flat` provider.

        This factory accepts either:
        - explicit `project_db_id` and `universe_db_id`, or
        - persisted `project` and `universe` model instances.

        This flexibility is intentional because bootstrap paths and provisioning
        paths do not always call factories with the same shape.
        """
        resolved_project_db_id, resolved_universe_db_id = _resolve_project_universe_db_ids(
            project=project,
            universe=universe,
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
        )

        concrete_world_id = normalize_concrete_world_id(
            world_id or DEFAULT_WORLD_ID,
            default=DEFAULT_WORLD_ID,
        )

        merged_metadata = normalize_metadata(metadata_json)
        merged_metadata.setdefault("schemaVersion", WORLD_INSTANCE_SCHEMA_VERSION)
        merged_metadata.setdefault("seededBy", "WorldInstance.create_flat_spawn")
        merged_metadata.setdefault("chunkWorldId", concrete_world_id)
        merged_metadata.setdefault("templateId", DEFAULT_TEMPLATE_ID)
        merged_metadata.setdefault("providerId", DEFAULT_PROVIDER_ID)
        merged_metadata.setdefault("providerWorldId", DEFAULT_PROVIDER_WORLD_ID)

        return cls.create(
            project_db_id=resolved_project_db_id,
            universe_db_id=resolved_universe_db_id,
            world_id=concrete_world_id,
            slug=slug,
            name=name,
            status=WORLD_STATUS_ACTIVE,
            world_type=WORLD_TYPE_RUNTIME,
            world_role=WORLD_ROLE_DEFAULT_SPAWN,
            world_scope=WORLD_SCOPE_PROJECT,
            template_id=DEFAULT_TEMPLATE_ID,
            provider_id=DEFAULT_PROVIDER_ID,
            provider_world_id=DEFAULT_PROVIDER_WORLD_ID,
            generator_type=DEFAULT_GENERATOR_TYPE,
            generator_version=DEFAULT_GENERATOR_VERSION,
            projection_type=DEFAULT_PROJECTION_TYPE,
            topology_type=DEFAULT_TOPOLOGY_TYPE,
            coordinate_system=DEFAULT_COORDINATE_SYSTEM,
            chunk_size=DEFAULT_CHUNK_SIZE,
            cell_size=DEFAULT_CELL_SIZE,
            surface_y=DEFAULT_SURFACE_Y,
            min_y=DEFAULT_MIN_Y,
            max_y=DEFAULT_MAX_Y,
            block_registry_id=DEFAULT_BLOCK_REGISTRY_ID,
            block_registry_version=DEFAULT_BLOCK_REGISTRY_VERSION,
            spawn_x=DEFAULT_SPAWN_X,
            spawn_y=DEFAULT_SPAWN_Y,
            spawn_z=DEFAULT_SPAWN_Z,
            spawn_yaw=DEFAULT_SPAWN_YAW,
            spawn_pitch=DEFAULT_SPAWN_PITCH,
            source_service=source_service,
            external_ref=external_ref or concrete_world_id,
            created_by_user_id=created_by_user_id,
            metadata_json=merged_metadata,
        )

    @classmethod
    def create_earth_spawn(
        cls,
        *,
        global_reference: Any,
        project: Any = None,
        universe: Any = None,
        project_db_id: Optional[int] = None,
        universe_db_id: Optional[int] = None,
        world_id: str = DEFAULT_WORLD_ID,
        slug: str = DEFAULT_WORLD_SLUG,
        name: str = EARTH_WORLD_NAME,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        source_service: Optional[str] = "vectoplan-chunk-bootstrap",
        external_ref: Optional[str] = None,
        block_registry_id: str = DEFAULT_BLOCK_REGISTRY_ID,
        block_registry_version: str = DEFAULT_BLOCK_REGISTRY_VERSION,
        spawn_yaw: float = DEFAULT_SPAWN_YAW,
        spawn_pitch: float = DEFAULT_SPAWN_PITCH,
    ) -> "WorldInstance":
        """
        Create a concrete Earth spawn world without adding it to a session.

        The global reference is validated through the Earth provider stack.
        The default spawn is then derived from that exact reference and stored
        locally in ``local_metric`` coordinates. No global spawn coordinate is
        persisted.
        """
        resolved_project_db_id, resolved_universe_db_id = (
            _resolve_project_universe_db_ids(
                project=project,
                universe=universe,
                project_db_id=project_db_id,
                universe_db_id=universe_db_id,
            )
        )

        concrete_world_id = normalize_concrete_world_id(
            world_id or DEFAULT_WORLD_ID,
            default=DEFAULT_WORLD_ID,
        )
        reference = coerce_global_reference(global_reference)

        provider_module = _import_first_available(
            "src.world.earth.provider",
            "world.earth.provider",
        )
        validator_module = _import_first_available(
            "src.world.earth.validator",
            "world.earth.validator",
        )

        definition = validator_module.load_earth_world_definition()
        provider = provider_module.get_earth_world_provider(
            concrete_world_id,
            reference,
            definition=definition,
        )
        default_spawn = provider.default_spawn_position()

        merged_metadata = normalize_metadata(metadata_json)
        merged_metadata.setdefault(
            "schemaVersion",
            WORLD_INSTANCE_SCHEMA_VERSION,
        )
        merged_metadata.setdefault(
            "seededBy",
            "WorldInstance.create_earth_spawn",
        )
        merged_metadata.setdefault("chunkWorldId", concrete_world_id)
        merged_metadata.setdefault("templateId", EARTH_TEMPLATE_ID)
        merged_metadata.setdefault("providerId", EARTH_PROVIDER_ID)
        merged_metadata.setdefault(
            "providerWorldId",
            EARTH_PROVIDER_WORLD_ID,
        )
        merged_metadata.setdefault(
            "globalReferenceFingerprint",
            reference.fingerprint,
        )
        merged_metadata.setdefault(
            "coordinateFrameRevision",
            reference.reference_version,
        )
        merged_metadata.setdefault("earthGridId", reference.grid.grid_id)
        merged_metadata.setdefault(
            "earthGridVersion",
            reference.grid.grid_version,
        )
        merged_metadata.setdefault(
            "spawnCoordinateSpace",
            EARTH_SPAWN_COORDINATE_SPACE,
        )

        return cls.create(
            project_db_id=resolved_project_db_id,
            universe_db_id=resolved_universe_db_id,
            world_id=concrete_world_id,
            slug=slug,
            name=name,
            status=WORLD_STATUS_ACTIVE,
            world_type=WORLD_TYPE_RUNTIME,
            world_role=WORLD_ROLE_DEFAULT_SPAWN,
            world_scope=WORLD_SCOPE_PROJECT,
            template_id=EARTH_TEMPLATE_ID,
            provider_id=EARTH_PROVIDER_ID,
            provider_world_id=EARTH_PROVIDER_WORLD_ID,
            generator_type=EARTH_GENERATOR_TYPE,
            generator_version=EARTH_GENERATOR_VERSION,
            projection_type=EARTH_PROJECTION_TYPE,
            topology_type=EARTH_TOPOLOGY_TYPE,
            coordinate_system=EARTH_COORDINATE_SYSTEM,
            chunk_size=definition.chunk.size,
            cell_size=float(
                definition.grid.vertical_meters_per_cell
            ),
            surface_y=DEFAULT_SURFACE_Y,
            min_y=DEFAULT_MIN_Y,
            max_y=DEFAULT_MAX_Y,
            block_registry_id=block_registry_id,
            block_registry_version=block_registry_version,
            spawn_x=decimal_floor_to_int(
                normalize_decimal_coordinate(
                    default_spawn.x,
                    field_name="default_spawn.x",
                ),
                field_name="default_spawn.x",
            ),
            spawn_y=decimal_floor_to_int(
                normalize_decimal_coordinate(
                    default_spawn.y,
                    field_name="default_spawn.y",
                ),
                field_name="default_spawn.y",
            ),
            spawn_z=decimal_floor_to_int(
                normalize_decimal_coordinate(
                    default_spawn.z,
                    field_name="default_spawn.z",
                ),
                field_name="default_spawn.z",
            ),
            spawn_x_precise=default_spawn.x,
            spawn_y_precise=default_spawn.y,
            spawn_z_precise=default_spawn.z,
            spawn_coordinate_space=EARTH_SPAWN_COORDINATE_SPACE,
            spawn_yaw=spawn_yaw,
            spawn_pitch=spawn_pitch,
            global_reference=reference,
            source_service=source_service,
            external_ref=external_ref or concrete_world_id,
            created_by_user_id=created_by_user_id,
            metadata_json=merged_metadata,
        )


    @classmethod
    def create_for_universe(
        cls,
        universe: Any,
        *,
        world_id: Optional[str] = None,
        slug: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        world_role: str = WORLD_ROLE_DEFAULT_SPAWN,
        template_id: str = DEFAULT_TEMPLATE_ID,
        provider_id: str = DEFAULT_PROVIDER_ID,
        provider_world_id: str = DEFAULT_PROVIDER_WORLD_ID,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        **world_config: Any,
    ) -> "WorldInstance":
        """
        Create a world for a Universe model instance.

        The universe instance must already have internal database ids.
        """
        project_db_id = getattr(universe, "project_db_id", None)
        universe_db_id = getattr(universe, "id", None)

        if project_db_id is None:
            raise ValueError(
                "Cannot create world for universe without universe.project_db_id."
            )

        if universe_db_id is None:
            raise ValueError(
                "Cannot create world for universe without persisted universe.id."
            )

        return cls.create(
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
            world_id=world_id,
            slug=slug,
            name=name,
            description=description,
            world_role=world_role,
            template_id=template_id,
            provider_id=provider_id,
            provider_world_id=provider_world_id,
            created_by_user_id=created_by_user_id,
            metadata_json=metadata_json,
            **world_config,
        )

    @classmethod
    def from_create_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        project_db_id: int,
        universe_db_id: int,
        created_by_user_id: Optional[str] = None,
    ) -> "WorldInstance":
        """
        Create a WorldInstance from an API-style payload.

        Supported keys:
        - worldId / world_id / chunkWorldId / chunk_world_id
        - name / worldName
        - slug
        - description
        - worldType / world_type
        - worldRole / world_role
        - templateId / template_id
        - providerId / provider_id
        - providerWorldId / provider_world_id
        - generatorType / generator_type
        - generatorVersion / generator_version
        - projectionType / projection_type
        - topologyType / topology_type
        - coordinateSystem / coordinate_system
        - chunkSize / chunk_size
        - cellSize / cell_size
        - surfaceY / surface_y
        - minY / min_y
        - maxY / max_y
        - seed
        - blockRegistryId / block_registry_id
        - blockRegistryVersion / block_registry_version
        - spawn / spawnX/spawnY/spawnZ/spawnYaw/spawnPitch
        - spawnCoordinateSpace / precise spawn coordinates
        - globalReference / global_reference
        - metadata / metadataJson / metadata_json
        """
        if not isinstance(payload, Mapping):
            raise ValueError("World create payload must be a JSON object.")

        metadata_value = _payload_metadata_value(payload)
        spawn = (
            payload.get("spawn")
            if isinstance(payload.get("spawn"), Mapping)
            else {}
        )
        global_reference = _payload_get(
            payload,
            "globalReference",
            "global_reference",
        )
        spawn_coordinate_space = _payload_get(
            spawn,
            "coordinateSpace",
            "coordinate_space",
            default=_payload_get(
                payload,
                "spawnCoordinateSpace",
                "spawn_coordinate_space",
            ),
        )

        raw_world_id = (
            payload.get("chunkWorldId")
            or payload.get("chunk_world_id")
            or payload.get("worldId")
            or payload.get("world_id")
        )

        requested_provider_id = normalize_provider_id(
            _payload_get(
                payload,
                "providerId",
                "provider_id",
                default=DEFAULT_PROVIDER_ID,
            )
        )
        earth_requested = requested_provider_id == EARTH_PROVIDER_ID

        provider_defaults = {
            "template_id": (
                EARTH_TEMPLATE_ID
                if earth_requested
                else DEFAULT_TEMPLATE_ID
            ),
            "provider_id": requested_provider_id,
            "provider_world_id": (
                EARTH_PROVIDER_WORLD_ID
                if earth_requested
                else DEFAULT_PROVIDER_WORLD_ID
            ),
            "generator_type": (
                EARTH_GENERATOR_TYPE
                if earth_requested
                else DEFAULT_GENERATOR_TYPE
            ),
            "generator_version": (
                EARTH_GENERATOR_VERSION
                if earth_requested
                else DEFAULT_GENERATOR_VERSION
            ),
            "projection_type": (
                EARTH_PROJECTION_TYPE
                if earth_requested
                else DEFAULT_PROJECTION_TYPE
            ),
            "topology_type": (
                EARTH_TOPOLOGY_TYPE
                if earth_requested
                else DEFAULT_TOPOLOGY_TYPE
            ),
            "coordinate_system": (
                EARTH_COORDINATE_SYSTEM
                if earth_requested
                else DEFAULT_COORDINATE_SYSTEM
            ),
            "spawn_coordinate_space": (
                EARTH_SPAWN_COORDINATE_SPACE
                if earth_requested
                else DEFAULT_SPAWN_COORDINATE_SPACE
            ),
        }

        return cls.create(
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
            world_id=raw_world_id,
            slug=payload.get("slug") or payload.get("worldSlug") or payload.get("world_slug"),
            name=payload.get("worldName") or payload.get("world_name") or payload.get("name"),
            description=payload.get("description"),
            world_type=payload.get("worldType") or payload.get("world_type") or WORLD_TYPE_RUNTIME,
            world_role=payload.get("worldRole") or payload.get("world_role") or WORLD_ROLE_DEFAULT_SPAWN,
            world_scope=payload.get("worldScope") or payload.get("world_scope") or WORLD_SCOPE_PROJECT,
            template_id=payload.get("templateId") or payload.get("template_id") or provider_defaults["template_id"],
            provider_id=requested_provider_id,
            provider_world_id=payload.get("providerWorldId") or payload.get("provider_world_id") or provider_defaults["provider_world_id"],
            generator_type=payload.get("generatorType") or payload.get("generator_type") or provider_defaults["generator_type"],
            generator_version=payload.get("generatorVersion") or payload.get("generator_version") or provider_defaults["generator_version"],
            projection_type=payload.get("projectionType") or payload.get("projection_type") or provider_defaults["projection_type"],
            topology_type=payload.get("topologyType") or payload.get("topology_type") or provider_defaults["topology_type"],
            coordinate_system=payload.get("coordinateSystem") or payload.get("coordinate_system") or provider_defaults["coordinate_system"],
            chunk_size=payload.get("chunkSize") or payload.get("chunk_size") or DEFAULT_CHUNK_SIZE,
            cell_size=payload.get("cellSize") or payload.get("cell_size") or DEFAULT_CELL_SIZE,
            surface_y=payload.get("surfaceY") if "surfaceY" in payload else payload.get("surface_y", DEFAULT_SURFACE_Y),
            min_y=payload.get("minY") if "minY" in payload else payload.get("min_y", DEFAULT_MIN_Y),
            max_y=payload.get("maxY") if "maxY" in payload else payload.get("max_y", DEFAULT_MAX_Y),
            seed=payload.get("seed"),
            block_registry_id=payload.get("blockRegistryId") or payload.get("block_registry_id") or DEFAULT_BLOCK_REGISTRY_ID,
            block_registry_version=payload.get("blockRegistryVersion") or payload.get("block_registry_version") or DEFAULT_BLOCK_REGISTRY_VERSION,
            spawn_x=payload.get("spawnX") if "spawnX" in payload else payload.get("spawn_x", spawn.get("x", DEFAULT_SPAWN_X)),
            spawn_y=payload.get("spawnY") if "spawnY" in payload else payload.get("spawn_y", spawn.get("y", DEFAULT_SPAWN_Y)),
            spawn_z=payload.get("spawnZ") if "spawnZ" in payload else payload.get("spawn_z", spawn.get("z", DEFAULT_SPAWN_Z)),
            spawn_x_precise=_payload_get(
                spawn,
                "preciseX",
                "xPrecise",
                default=_payload_get(
                    payload,
                    "spawnXPrecise",
                    "spawn_x_precise",
                ),
            ),
            spawn_y_precise=_payload_get(
                spawn,
                "preciseY",
                "yPrecise",
                default=_payload_get(
                    payload,
                    "spawnYPrecise",
                    "spawn_y_precise",
                ),
            ),
            spawn_z_precise=_payload_get(
                spawn,
                "preciseZ",
                "zPrecise",
                default=_payload_get(
                    payload,
                    "spawnZPrecise",
                    "spawn_z_precise",
                ),
            ),
            spawn_coordinate_space=(
                spawn_coordinate_space
                or provider_defaults["spawn_coordinate_space"]
            ),
            spawn_yaw=payload.get("spawnYaw") if "spawnYaw" in payload else payload.get("spawn_yaw", spawn.get("yaw", DEFAULT_SPAWN_YAW)),
            spawn_pitch=payload.get("spawnPitch") if "spawnPitch" in payload else payload.get("spawn_pitch", spawn.get("pitch", DEFAULT_SPAWN_PITCH)),
            source_service=payload.get("sourceService") or payload.get("source_service"),
            external_ref=payload.get("externalRef") or payload.get("external_ref"),
            global_reference=global_reference,
            created_by_user_id=created_by_user_id,
            metadata_json=metadata_value,
        )

    @property
    def is_active(self) -> bool:
        return self.status == WORLD_STATUS_ACTIVE and self.deleted_at is None

    @property
    def is_archived(self) -> bool:
        return self.status == WORLD_STATUS_ARCHIVED

    @property
    def is_deleted(self) -> bool:
        return self.status == WORLD_STATUS_DELETED or self.deleted_at is not None

    @property
    def is_earth_world(self) -> bool:
        """Return whether this concrete world uses the Earth provider."""
        return (
            str(self.provider_id or "").strip().lower()
            == EARTH_PROVIDER_ID
        )

    @property
    def has_global_reference(self) -> bool:
        """Return whether a complete global reference contract is stored."""
        return bool(
            self.global_reference_json
            and self.global_reference_fingerprint
            and int(self.coordinate_frame_revision or 0) >= 1
        )

    @property
    def is_global_reference_locked(self) -> bool:
        """Return whether normal reference replacement is forbidden."""
        return self.global_reference_locked_at is not None

    @property
    def project_public_id(self) -> Optional[str]:
        """Return the parent project's public id if available."""
        try:
            project = getattr(self, "project", None)
            return getattr(project, "project_id", None)
        except Exception:
            return None

    @property
    def universe_public_id(self) -> Optional[str]:
        """Return the parent universe's public id if available."""
        try:
            universe = getattr(self, "universe", None)
            return getattr(universe, "universe_id", None)
        except Exception:
            return None

    @property
    def chunk_project_id(self) -> Optional[str]:
        """Compatibility alias for parent Project.project_id."""
        return self.project_public_id

    @property
    def chunk_universe_id(self) -> Optional[str]:
        """Compatibility alias for parent Universe.universe_id."""
        return self.universe_public_id

    @property
    def chunk_world_id(self) -> str:
        """Compatibility alias for world_id."""
        return self.world_id

    @property
    def chunk_config(self) -> Dict[str, Any]:
        """Return chunk/grid configuration in API-compatible shape."""
        return {
            "chunkSize": self.chunk_size,
            "cellSize": self.cell_size,
            "coordinateSystem": self.coordinate_system,
            "projectionType": self.projection_type,
            "topologyType": self.topology_type,
            "surfaceY": self.surface_y,
            "minY": self.min_y,
            "maxY": self.max_y,
            "coordinateFrameRevision": int(
                self.coordinate_frame_revision or 0
            ),
            "globalReferenceRequired": self.is_earth_world,
            "globalReferencePresent": self.has_global_reference,
        }

    @property
    def provider_mapping(self) -> Dict[str, Any]:
        """Return provider/template mapping in API-compatible shape."""
        return {
            "templateId": self.template_id,
            "providerId": self.provider_id,
            "providerWorldId": self.provider_world_id,
            "generatorType": self.generator_type,
            "generatorVersion": self.generator_version,
        }

    @property
    def registry_context(self) -> Dict[str, Any]:
        """Return block registry context."""
        return {
            "blockRegistryId": self.block_registry_id,
            "blockRegistryVersion": self.block_registry_version,
        }

    @property
    def spawn_position(self) -> Dict[str, int]:
        """Return the legacy containing-block spawn position."""
        return {
            "x": int(self.spawn_x),
            "y": int(self.spawn_y),
            "z": int(self.spawn_z),
        }

    @property
    def spawn_precise_position(self) -> Dict[str, Optional[str]]:
        """Return the precise local spawn as decimal strings."""
        precise_values = (
            self.spawn_x_precise,
            self.spawn_y_precise,
            self.spawn_z_precise,
        )

        if all(value is None for value in precise_values):
            return {
                "x": str(int(self.spawn_x)),
                "y": str(int(self.spawn_y)),
                "z": str(int(self.spawn_z)),
            }

        if any(value is None for value in precise_values):
            raise ValueError(
                "Precise spawn coordinates must be all null or all populated."
            )

        return {
            "x": decimal_to_plain_text(self.spawn_x_precise),
            "y": decimal_to_plain_text(self.spawn_y_precise),
            "z": decimal_to_plain_text(self.spawn_z_precise),
        }

    @property
    def spawn_metric_position(self) -> Dict[str, float]:
        """Return the precise local spawn as metric floats."""
        position = self.spawn_precise_position
        return {
            "x": float(position["x"]),
            "y": float(position["y"]),
            "z": float(position["z"]),
        }

    @property
    def spawn_rotation(self) -> Dict[str, float]:
        """Return spawn camera/player rotation."""
        return {
            "yaw": float(self.spawn_yaw),
            "pitch": float(self.spawn_pitch),
        }

    @property
    def spawn_context(self) -> Dict[str, Any]:
        """Return combined local spawn context."""
        return {
            "coordinateSpace": normalize_spawn_coordinate_space(
                self.spawn_coordinate_space,
                default=(
                    EARTH_SPAWN_COORDINATE_SPACE
                    if self.is_earth_world
                    else DEFAULT_SPAWN_COORDINATE_SPACE
                ),
            ),
            "position": self.spawn_position,
            "precisePosition": self.spawn_precise_position,
            "metricPosition": self.spawn_metric_position,
            "rotation": self.spawn_rotation,
            "globalReferenceChanged": False,
            "worldReanchored": False,
        }

    def get_global_reference_point(self) -> Any:
        """Return the validated GlobalReferencePoint contract."""
        if not self.has_global_reference:
            return None

        reference = coerce_global_reference(
            normalize_metadata(self.global_reference_json)
        )
        expected_fingerprint = normalize_sha256(
            self.global_reference_fingerprint,
            field_name="global_reference_fingerprint",
        )

        if reference.fingerprint != expected_fingerprint:
            raise ValueError(
                "Stored global reference fingerprint does not match payload."
            )

        if (
            int(reference.reference_version)
            != int(self.coordinate_frame_revision)
        ):
            raise ValueError(
                "Stored global reference revision does not match "
                "coordinate_frame_revision."
            )

        return reference

    def global_reference_context(
        self,
        *,
        include_crs_definition: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Return the safe global reference API context."""
        reference = self.get_global_reference_point()
        if reference is None:
            return None

        payload = reference.to_dict(
            include_crs_definition=include_crs_definition,
            numeric_coordinates=False,
        )
        payload["locked"] = self.is_global_reference_locked
        payload["lockedAt"] = datetime_to_iso(
            self.global_reference_locked_at
        )
        payload["lockReasons"] = normalize_reference_lock_reasons(
            self.global_reference_lock_reasons_json
        )
        payload["updatedAt"] = datetime_to_iso(
            self.global_reference_updated_at
        )
        payload["updatedByUserId"] = (
            self.global_reference_updated_by_user_id
        )
        return payload

    @property
    def coordinate_frame_context(self) -> Dict[str, Any]:
        """Return stable coordinate-frame metadata."""
        reference = self.get_global_reference_point()
        grid_context = None

        if reference is not None:
            grid_context = reference.grid.to_dict()

        return {
            "revision": int(self.coordinate_frame_revision or 0),
            "referencePresent": reference is not None,
            "referenceFingerprint": (
                self.global_reference_fingerprint
            ),
            "referenceLocked": self.is_global_reference_locked,
            "referenceLockedAt": datetime_to_iso(
                self.global_reference_locked_at
            ),
            "referenceLockReasons": normalize_reference_lock_reasons(
                self.global_reference_lock_reasons_json
            ),
            "grid": grid_context,
            "persistedEntityCoordinates": "local",
            "derivedGlobalCoordinatesPersistedPerEntity": False,
        }

    def build_earth_provider(self) -> Any:
        """Build or retrieve the runtime Earth provider for this instance."""
        if not self.is_earth_world:
            raise ValueError(
                "Only earth worlds have an EarthWorldProvider."
            )

        reference = self.get_global_reference_point()
        if reference is None:
            raise ValueError(
                "Earth world is missing its global reference."
            )

        provider_module = _import_first_available(
            "src.world.earth.provider",
            "world.earth.provider",
        )
        validator_module = _import_first_available(
            "src.world.earth.validator",
            "world.earth.validator",
        )
        definition = validator_module.load_earth_world_definition()

        return provider_module.get_earth_world_provider(
            self.world_id,
            reference,
            definition=definition,
        )

    def build_world_context_key(self) -> str:
        """Build a stable debug/context key for logs and traces."""
        return f"{self.project_db_id}:{self.universe_db_id}:{self.world_id}"

    def build_route_hints(self, *, api_prefix: str = "") -> Dict[str, str]:
        """Build project-scoped route hints for this world."""
        prefix = str(api_prefix or "").rstrip("/")
        project_id = self.project_public_id or str(self.project_db_id)
        world_id = self.world_id

        return {
            "projectBootstrap": f"{prefix}/projects/{project_id}/bootstrap",
            "project": f"{prefix}/projects/{project_id}",
            "worlds": f"{prefix}/projects/{project_id}/worlds",
            "world": f"{prefix}/projects/{project_id}/worlds/{world_id}",
            "blocks": f"{prefix}/projects/{project_id}/worlds/{world_id}/blocks",
            "chunk": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks",
            "chunks": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks",
            "chunksBatch": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks/batch",
            "commands": f"{prefix}/projects/{project_id}/worlds/{world_id}/commands",
            "globalReference": (
                f"{prefix}/projects/{project_id}/worlds/{world_id}/global-reference"
            ),
            "coordinateTransforms": (
                f"{prefix}/projects/{project_id}/worlds/{world_id}/coordinates"
            ),
            "spawn": f"{prefix}/projects/{project_id}/worlds/{world_id}/spawn",
        }

    def touch(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Mark the world as updated and increment its optimistic revision."""
        self.updated_at = utc_now()
        self.revision = int(self.revision or 1) + 1

        normalized_user_id = normalize_optional_text(
            updated_by_user_id,
            field_name="updated_by_user_id",
            max_length=WORLD_USER_ID_MAX_LENGTH,
        )

        if normalized_user_id is not None:
            self.updated_by_user_id = normalized_user_id

    def ensure_not_deleted(self) -> None:
        """Raise when a mutation is attempted on a soft-deleted world."""
        if self.is_deleted:
            raise ValueError(
                f"World '{self.world_id}' is deleted and cannot be modified."
            )

    def ensure_global_reference_mutable(self) -> None:
        """Raise when the Earth reference may no longer be replaced."""
        self.ensure_not_deleted()

        if not self.is_earth_world:
            raise ValueError(
                "Global references are only supported for earth worlds."
            )

        if self.is_global_reference_locked:
            reasons = ", ".join(
                normalize_reference_lock_reasons(
                    self.global_reference_lock_reasons_json
                )
            )
            suffix = f" Reasons: {reasons}." if reasons else ""
            raise ValueError(
                f"Global reference for world '{self.world_id}' is locked."
                f"{suffix}"
            )

    def set_global_reference(
        self,
        global_reference: Any,
        *,
        allow_replace: bool = False,
        updated_by_user_id: Optional[str] = None,
        touch: bool = True,
    ) -> bool:
        """
        Persist the one global reference contract of an Earth world.

        Returns ``True`` when state changed and ``False`` for an idempotent
        identical reference.
        """
        self.ensure_not_deleted()

        if not self.is_earth_world:
            raise ValueError(
                "global_reference can only be set for the earth provider."
            )

        reference = coerce_global_reference(global_reference)
        payload, fingerprint, frame_revision = (
            normalize_global_reference_storage(reference)
        )

        if reference.grid.grid_id != EARTH_GRID_ID:
            raise ValueError(
                f"Earth reference grid_id must be '{EARTH_GRID_ID}'."
            )
        if reference.grid.grid_version != EARTH_GRID_VERSION:
            raise ValueError(
                f"Earth reference grid_version must be '{EARTH_GRID_VERSION}'."
            )
        if reference.grid.projection_id != EARTH_PROJECTION_TYPE:
            raise ValueError(
                "Earth reference projection does not match world geometry."
            )
        if reference.grid.topology_type != EARTH_TOPOLOGY_TYPE:
            raise ValueError(
                "Earth reference topology does not match world geometry."
            )

        if self.has_global_reference:
            current_fingerprint = normalize_sha256(
                self.global_reference_fingerprint,
                field_name="global_reference_fingerprint",
            )

            if current_fingerprint == fingerprint:
                # Canonicalize old JSON without changing optimistic revision.
                self.global_reference_json = payload
                return False

            self.ensure_global_reference_mutable()

            if not allow_replace:
                raise ValueError(
                    "A different global reference already exists. "
                    "Use the dedicated pre-materialization replacement path."
                )

            current_revision = int(self.coordinate_frame_revision or 0)
            if frame_revision <= current_revision:
                raise ValueError(
                    "Replacement global reference revision must be greater "
                    "than the current coordinate frame revision."
                )

        self.global_reference_json = payload
        self.global_reference_fingerprint = fingerprint
        self.coordinate_frame_revision = frame_revision
        self.global_reference_updated_at = utc_now()
        self.global_reference_updated_by_user_id = normalize_optional_text(
            updated_by_user_id,
            field_name="global_reference_updated_by_user_id",
            max_length=WORLD_USER_ID_MAX_LENGTH,
        )

        if touch:
            self.touch(updated_by_user_id=updated_by_user_id)

        return True

    def replace_global_reference_before_materialization(
        self,
        global_reference: Any,
        *,
        materialization_lock_reasons: Optional[Iterable[str]] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> bool:
        """Replace an Earth reference only while no materialized state exists."""
        reasons = normalize_reference_lock_reasons(
            materialization_lock_reasons
        )
        if reasons:
            raise ValueError(
                "Global reference cannot be replaced after materialization. "
                f"Lock reasons: {', '.join(reasons)}."
            )

        return self.set_global_reference(
            global_reference,
            allow_replace=True,
            updated_by_user_id=updated_by_user_id,
        )

    def lock_global_reference(
        self,
        reasons: Iterable[str],
        *,
        updated_by_user_id: Optional[str] = None,
        touch: bool = True,
    ) -> None:
        """Lock normal reanchoring after the first materialized state."""
        self.ensure_not_deleted()

        if not self.is_earth_world or not self.has_global_reference:
            raise ValueError(
                "Only a referenced earth world can lock its global reference."
            )

        normalized_reasons = normalize_reference_lock_reasons(reasons)
        if not normalized_reasons:
            raise ValueError(
                "At least one global reference lock reason is required."
            )

        merged = normalize_reference_lock_reasons(
            [
                *normalize_reference_lock_reasons(
                    self.global_reference_lock_reasons_json
                ),
                *normalized_reasons,
            ]
        )
        self.global_reference_lock_reasons_json = merged
        self.global_reference_locked_at = (
            self.global_reference_locked_at or utc_now()
        )
        self.global_reference_updated_at = utc_now()
        self.global_reference_updated_by_user_id = normalize_optional_text(
            updated_by_user_id,
            field_name="global_reference_updated_by_user_id",
            max_length=WORLD_USER_ID_MAX_LENGTH,
        )

        if touch:
            self.touch(updated_by_user_id=updated_by_user_id)

    def clear_global_reference_before_materialization(
        self,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Clear a reference only for an unmaterialized world.

        This is intended for an atomic provider migration. An Earth world
        cannot be flushed while the reference is absent because the database
        constraint requires it.
        """
        self.ensure_global_reference_mutable()
        self.global_reference_json = None
        self.global_reference_fingerprint = None
        self.coordinate_frame_revision = 0
        self.global_reference_updated_at = utc_now()
        self.global_reference_updated_by_user_id = normalize_optional_text(
            updated_by_user_id,
            field_name="global_reference_updated_by_user_id",
            max_length=WORLD_USER_ID_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_spawn_metric_position(
        self,
        *,
        x: Any,
        y: Any,
        z: Any,
        yaw: Optional[float] = None,
        pitch: Optional[float] = None,
        updated_by_user_id: Optional[str] = None,
        touch: bool = True,
    ) -> None:
        """Persist a precise local-metric spawn without changing the reference."""
        self.ensure_not_deleted()

        precise_x = normalize_decimal_coordinate(
            x,
            field_name="spawn_x_precise",
        )
        precise_y = normalize_decimal_coordinate(
            y,
            field_name="spawn_y_precise",
        )
        precise_z = normalize_decimal_coordinate(
            z,
            field_name="spawn_z_precise",
        )
        assert precise_x is not None
        assert precise_y is not None
        assert precise_z is not None

        if self.is_earth_world:
            earth_grid_module = _import_first_available(
                "src.georeferencing.earth_grid",
                "georeferencing.earth_grid",
            )
            local_position = earth_grid_module.LocalEarthPosition(
                x=precise_x,
                y=precise_y,
                z=precise_z,
            )
            normalized = (
                self.build_earth_provider()
                .frame
                .normalize_local_position(local_position)
            )
            precise_x = normalized.x
            assert normalized.y is not None
            precise_y = normalized.y
            precise_z = normalized.z

        self.spawn_coordinate_space = EARTH_SPAWN_COORDINATE_SPACE
        self.spawn_x_precise = precise_x
        self.spawn_y_precise = precise_y
        self.spawn_z_precise = precise_z
        self.spawn_x = decimal_floor_to_int(
            precise_x,
            field_name="spawn_x_precise",
        )
        self.spawn_y = decimal_floor_to_int(
            precise_y,
            field_name="spawn_y_precise",
        )
        self.spawn_z = decimal_floor_to_int(
            precise_z,
            field_name="spawn_z_precise",
        )

        if yaw is not None:
            self.spawn_yaw = normalize_float(
                yaw,
                field_name="spawn_yaw",
                default=DEFAULT_SPAWN_YAW,
            )
        if pitch is not None:
            self.spawn_pitch = normalize_float(
                pitch,
                field_name="spawn_pitch",
                default=DEFAULT_SPAWN_PITCH,
            )

        if touch:
            self.touch(updated_by_user_id=updated_by_user_id)

    def rename(
        self,
        *,
        name: str,
        slug: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Rename the world and optionally update its slug."""
        self.ensure_not_deleted()
        self.name = normalize_required_text(
            name,
            field_name="name",
            max_length=WORLD_NAME_MAX_LENGTH,
        )

        if slug is not None:
            self.slug = normalize_slug(slug)

        self.touch(updated_by_user_id=updated_by_user_id)

    def update_description(
        self,
        description: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Update the world description."""
        self.ensure_not_deleted()
        self.description = normalize_optional_text(
            description,
            field_name="description",
            max_length=WORLD_DESCRIPTION_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_role(
        self,
        world_role: str,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set the world role."""
        self.ensure_not_deleted()
        self.world_role = normalize_world_role(world_role)
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_status(
        self,
        status: str,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Set world status.

        Prefer using archive(), restore() and soft_delete() in application code
        because those methods maintain timestamp fields consistently.
        """
        normalized_status = normalize_status(status)
        now = utc_now()

        if normalized_status == WORLD_STATUS_DELETED:
            self.deleted_at = self.deleted_at or now
        elif normalized_status == WORLD_STATUS_ARCHIVED:
            self.archived_at = self.archived_at or now
            self.deleted_at = None
        elif normalized_status == WORLD_STATUS_ACTIVE:
            self.archived_at = None
            self.deleted_at = None

        self.status = normalized_status
        self.touch(updated_by_user_id=updated_by_user_id)

    def archive(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Archive the world without deleting historical data."""
        self.ensure_not_deleted()
        self.status = WORLD_STATUS_ARCHIVED
        self.archived_at = self.archived_at or utc_now()
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def restore(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Restore an archived or soft-deleted world."""
        self.status = WORLD_STATUS_ACTIVE
        self.archived_at = None
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def soft_delete(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """
        Soft-delete the world.

        This intentionally keeps snapshots, command logs and events available
        for audit/history/AI-training purposes unless a later explicit purge
        process removes them.
        """
        now = utc_now()
        self.status = WORLD_STATUS_DELETED
        self.deleted_at = self.deleted_at or now
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_provider_mapping(
        self,
        *,
        template_id: str,
        provider_id: str,
        provider_world_id: str,
        generator_type: str,
        generator_version: str,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Update provider/template mapping.

        Switching to or from ``earth`` requires a dedicated atomic migration
        because the global-reference and storage-frame invariants must change
        together. Flat-to-flat style mapping updates remain supported.
        """
        self.ensure_not_deleted()

        normalized_template_id = normalize_template_id(template_id)
        normalized_provider_id = normalize_provider_id(provider_id)
        normalized_provider_world_id = normalize_provider_world_id(
            provider_world_id
        )
        normalized_generator_type = normalize_required_text(
            generator_type,
            field_name="generator_type",
            max_length=WORLD_GENERATOR_TYPE_MAX_LENGTH,
        )
        normalized_generator_version = normalize_version_text(
            generator_version,
            field_name="generator_version",
            max_length=WORLD_GENERATOR_VERSION_MAX_LENGTH,
            default=DEFAULT_GENERATOR_VERSION,
        )

        current_mapping = (
            self.template_id,
            self.provider_id,
            self.provider_world_id,
            self.generator_type,
            self.generator_version,
        )
        proposed_mapping = (
            normalized_template_id,
            normalized_provider_id,
            normalized_provider_world_id,
            normalized_generator_type,
            normalized_generator_version,
        )

        if (
            self.provider_id == EARTH_PROVIDER_ID
            or normalized_provider_id == EARTH_PROVIDER_ID
        ):
            expected_earth = (
                EARTH_TEMPLATE_ID,
                EARTH_PROVIDER_ID,
                EARTH_PROVIDER_WORLD_ID,
                EARTH_GENERATOR_TYPE,
            )
            proposed_earth = proposed_mapping[:4]

            if proposed_earth != expected_earth:
                raise ValueError(
                    "Earth provider mapping must use the fixed Earth-v1 "
                    "template, provider world and generator."
                )

            if proposed_mapping != current_mapping:
                raise ValueError(
                    "Switching or changing an Earth provider mapping requires "
                    "a dedicated reference-aware migration."
                )

            return

        self.template_id = normalized_template_id
        self.provider_id = normalized_provider_id
        self.provider_world_id = normalized_provider_world_id
        self.generator_type = normalized_generator_type
        self.generator_version = normalized_generator_version
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_world_geometry(
        self,
        *,
        projection_type: str,
        topology_type: str,
        coordinate_system: str,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Update world geometry metadata.

        Earth geometry is fixed by its versioned grid contract. Changing it
        requires a dedicated reanchor/migration and is not a normal mutation.
        """
        self.ensure_not_deleted()

        normalized_projection = normalize_required_text(
            projection_type,
            field_name="projection_type",
            max_length=WORLD_PROJECTION_TYPE_MAX_LENGTH,
        )
        normalized_topology = normalize_required_text(
            topology_type,
            field_name="topology_type",
            max_length=WORLD_TOPOLOGY_TYPE_MAX_LENGTH,
        )
        normalized_coordinate_system = normalize_required_text(
            coordinate_system,
            field_name="coordinate_system",
            max_length=WORLD_COORDINATE_SYSTEM_MAX_LENGTH,
        )

        if self.is_earth_world:
            expected = (
                EARTH_PROJECTION_TYPE,
                EARTH_TOPOLOGY_TYPE,
                EARTH_COORDINATE_SYSTEM,
            )
            proposed = (
                normalized_projection,
                normalized_topology,
                normalized_coordinate_system,
            )
            if proposed != expected or proposed != (
                self.projection_type,
                self.topology_type,
                self.coordinate_system,
            ):
                raise ValueError(
                    "Earth world geometry is immutable outside a dedicated "
                    "coordinate-frame migration."
                )
            return

        self.projection_type = normalized_projection
        self.topology_type = normalized_topology
        self.coordinate_system = normalized_coordinate_system
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_chunk_grid(
        self,
        *,
        chunk_size: int,
        cell_size: float,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Update chunk grid configuration.

        Earth grid phase, chunk size and cell scale are fixed by ``world.json``.
        Existing Earth worlds require a dedicated data migration.
        """
        self.ensure_not_deleted()

        normalized_chunk_size = normalize_positive_int(
            chunk_size,
            field_name="chunk_size",
            default=DEFAULT_CHUNK_SIZE,
        )
        normalized_cell_size = normalize_positive_float(
            cell_size,
            field_name="cell_size",
            default=DEFAULT_CELL_SIZE,
        )

        if self.is_earth_world:
            if (
                normalized_chunk_size != int(self.chunk_size)
                or normalized_cell_size != float(self.cell_size)
            ):
                raise ValueError(
                    "Earth chunk-grid changes require a dedicated migration."
                )
            return

        self.chunk_size = normalized_chunk_size
        self.cell_size = normalized_cell_size
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_vertical_bounds(
        self,
        *,
        min_y: int,
        surface_y: int,
        max_y: int,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Update vertical generation bounds.

        Existing ChunkSnapshots are not automatically migrated.
        """
        self.ensure_not_deleted()

        normalized_min_y = normalize_int(
            min_y,
            field_name="min_y",
            default=DEFAULT_MIN_Y,
        )
        normalized_surface_y = normalize_int(
            surface_y,
            field_name="surface_y",
            default=DEFAULT_SURFACE_Y,
        )
        normalized_max_y = normalize_int(
            max_y,
            field_name="max_y",
            default=DEFAULT_MAX_Y,
        )

        validate_vertical_bounds(
            min_y=normalized_min_y,
            surface_y=normalized_surface_y,
            max_y=normalized_max_y,
        )

        self.min_y = normalized_min_y
        self.surface_y = normalized_surface_y
        self.max_y = normalized_max_y
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_block_registry(
        self,
        *,
        block_registry_id: str,
        block_registry_version: str,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Update block registry context.

        Existing ChunkSnapshots keep their saved registry metadata.
        """
        self.ensure_not_deleted()
        self.block_registry_id = normalize_required_text(
            block_registry_id,
            field_name="block_registry_id",
            max_length=WORLD_BLOCK_REGISTRY_ID_MAX_LENGTH,
        )
        self.block_registry_version = normalize_version_text(
            block_registry_version,
            field_name="block_registry_version",
            max_length=WORLD_BLOCK_REGISTRY_VERSION_MAX_LENGTH,
            default=DEFAULT_BLOCK_REGISTRY_VERSION,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_seed(
        self,
        seed: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Set generator seed.

        Existing materialized chunks are not automatically regenerated.
        """
        self.ensure_not_deleted()
        self.seed = normalize_optional_text(
            seed,
            field_name="seed",
            max_length=WORLD_SEED_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_spawn_position(
        self,
        *,
        x: int,
        y: int,
        z: int,
        yaw: Optional[float] = None,
        pitch: Optional[float] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set a local spawn without changing the Earth reference."""
        self.ensure_not_deleted()

        normalized_x = normalize_int(
            x,
            field_name="spawn_x",
            default=DEFAULT_SPAWN_X,
        )
        normalized_y = normalize_int(
            y,
            field_name="spawn_y",
            default=DEFAULT_SPAWN_Y,
        )
        normalized_z = normalize_int(
            z,
            field_name="spawn_z",
            default=DEFAULT_SPAWN_Z,
        )

        if self.is_earth_world:
            self.set_spawn_metric_position(
                x=normalized_x,
                y=normalized_y,
                z=normalized_z,
                yaw=yaw,
                pitch=pitch,
                updated_by_user_id=updated_by_user_id,
                touch=True,
            )
            return

        self.spawn_x = normalized_x
        self.spawn_y = normalized_y
        self.spawn_z = normalized_z
        self.spawn_x_precise = Decimal(normalized_x)
        self.spawn_y_precise = Decimal(normalized_y)
        self.spawn_z_precise = Decimal(normalized_z)
        self.spawn_coordinate_space = (
            EARTH_SPAWN_COORDINATE_SPACE
            if self.is_earth_world
            else DEFAULT_SPAWN_COORDINATE_SPACE
        )

        if yaw is not None:
            self.spawn_yaw = normalize_float(
                yaw,
                field_name="spawn_yaw",
                default=DEFAULT_SPAWN_YAW,
            )

        if pitch is not None:
            self.spawn_pitch = normalize_float(
                pitch,
                field_name="spawn_pitch",
                default=DEFAULT_SPAWN_PITCH,
            )

        self.touch(updated_by_user_id=updated_by_user_id)

    def set_source_context(
        self,
        *,
        source_service: Optional[str],
        external_ref: Optional[str],
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set source service/external reference metadata."""
        self.ensure_not_deleted()
        self.source_service = normalize_optional_text(
            source_service,
            field_name="source_service",
            max_length=WORLD_SOURCE_SERVICE_MAX_LENGTH,
        )
        self.external_ref = normalize_optional_text(
            external_ref,
            field_name="external_ref",
            max_length=WORLD_EXTERNAL_REF_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def ensure_bootstrap_defaults(
        self,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Ensure required bootstrap/default-world fields are populated.

        Flat defaults remain unchanged. Earth worlds keep their fixed provider
        and geometry contract and must already possess a global reference.
        """
        self.ensure_not_deleted()

        if self.is_earth_world:
            self.template_id = EARTH_TEMPLATE_ID
            self.provider_id = EARTH_PROVIDER_ID
            self.provider_world_id = EARTH_PROVIDER_WORLD_ID
            self.generator_type = EARTH_GENERATOR_TYPE
            self.generator_version = (
                self.generator_version or EARTH_GENERATOR_VERSION
            )
            self.projection_type = EARTH_PROJECTION_TYPE
            self.topology_type = EARTH_TOPOLOGY_TYPE
            self.coordinate_system = EARTH_COORDINATE_SYSTEM
            self.spawn_coordinate_space = EARTH_SPAWN_COORDINATE_SPACE

            if not self.has_global_reference:
                raise ValueError(
                    "Earth bootstrap defaults require a global reference."
                )
        else:
            if not self.template_id:
                self.template_id = DEFAULT_TEMPLATE_ID
            if not self.provider_id:
                self.provider_id = DEFAULT_PROVIDER_ID
            if not self.provider_world_id:
                self.provider_world_id = DEFAULT_PROVIDER_WORLD_ID
            if not self.generator_type:
                self.generator_type = DEFAULT_GENERATOR_TYPE
            if not self.generator_version:
                self.generator_version = DEFAULT_GENERATOR_VERSION
            if not self.projection_type:
                self.projection_type = DEFAULT_PROJECTION_TYPE
            if not self.topology_type:
                self.topology_type = DEFAULT_TOPOLOGY_TYPE
            if not self.coordinate_system:
                self.coordinate_system = DEFAULT_COORDINATE_SYSTEM
            self.spawn_coordinate_space = normalize_spawn_coordinate_space(
                self.spawn_coordinate_space,
                default=DEFAULT_SPAWN_COORDINATE_SPACE,
            )

        if not self.block_registry_id:
            self.block_registry_id = DEFAULT_BLOCK_REGISTRY_ID
        if not self.block_registry_version:
            self.block_registry_version = DEFAULT_BLOCK_REGISTRY_VERSION
        if self.chunk_size is None or int(self.chunk_size) <= 0:
            self.chunk_size = DEFAULT_CHUNK_SIZE
        if self.cell_size is None or float(self.cell_size) <= 0:
            self.cell_size = DEFAULT_CELL_SIZE
        if self.spawn_y is None:
            self.spawn_y = DEFAULT_SPAWN_Y
        if self.spawn_yaw is None:
            self.spawn_yaw = DEFAULT_SPAWN_YAW
        if self.spawn_pitch is None:
            self.spawn_pitch = DEFAULT_SPAWN_PITCH

        precise_values = (
            self.spawn_x_precise,
            self.spawn_y_precise,
            self.spawn_z_precise,
        )
        if any(value is not None for value in precise_values):
            if not all(value is not None for value in precise_values):
                raise ValueError(
                    "Precise spawn coordinates must be all null or all populated."
                )
        elif self.is_earth_world:
            self.spawn_x_precise = Decimal(int(self.spawn_x or 0))
            self.spawn_y_precise = Decimal(int(self.spawn_y or 0))
            self.spawn_z_precise = Decimal(int(self.spawn_z or 0))

        self.schema_version = WORLD_INSTANCE_SCHEMA_VERSION
        self.touch(updated_by_user_id=updated_by_user_id)

    def replace_metadata(
        self,
        metadata_json: Optional[Mapping[str, Any]],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Replace metadata_json entirely."""
        self.ensure_not_deleted()
        self.metadata_json = normalize_metadata(metadata_json)
        self.touch(updated_by_user_id=updated_by_user_id)

    def update_metadata(
        self,
        values: Mapping[str, Any],
        *,
        remove_keys: Optional[Iterable[str]] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Merge metadata values and optionally remove keys.

        This method does not commit.
        """
        self.ensure_not_deleted()

        if not isinstance(values, Mapping):
            raise ValueError("metadata update values must be a JSON object/dict.")

        current = normalize_metadata(self.metadata_json)

        for key in remove_keys or []:
            try:
                current.pop(str(key), None)
            except Exception:
                continue

        for key, value in values.items():
            current[str(key)] = make_json_safe(value)

        self.metadata_json = current
        self.touch(updated_by_user_id=updated_by_user_id)

    def merge_provisioning_metadata(
        self,
        *,
        chunk_project_id: Optional[str] = None,
        chunk_universe_id: Optional[str] = None,
        external_app_project_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Merge standard provisioning metadata."""
        self.update_metadata(
            {
                "schemaVersion": WORLD_INSTANCE_SCHEMA_VERSION,
                "chunkProjectId": chunk_project_id or self.project_public_id,
                "chunkUniverseId": chunk_universe_id or self.universe_public_id,
                "chunkWorldId": self.world_id,
                "externalAppProjectId": external_app_project_id,
                "templateId": self.template_id,
                "providerWorldId": self.provider_world_id,
                "blockRegistryId": self.block_registry_id,
                "blockRegistryVersion": self.block_registry_version,
                "spawn": self.spawn_context,
                "coordinateFrame": self.coordinate_frame_context,
                "globalReferenceFingerprint": (
                    self.global_reference_fingerprint
                ),
                "provisionedAt": datetime_to_iso(utc_now()),
            },
            updated_by_user_id=updated_by_user_id,
        )

    def get_metadata_value(self, key: str, default: Any = None) -> Any:
        """Read one metadata value safely."""
        try:
            metadata = normalize_metadata(self.metadata_json)
            return metadata.get(key, default)
        except Exception:
            return default

    def apply_patch_payload(
        self,
        payload: Mapping[str, Any],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Apply a PATCH-style API payload.

        Supported mutable keys:
        - name
        - slug
        - description
        - worldRole / world_role
        - templateId / template_id
        - providerId / provider_id
        - providerWorldId / provider_world_id
        - generatorType / generator_type
        - generatorVersion / generator_version
        - projectionType / projection_type
        - topologyType / topology_type
        - coordinateSystem / coordinate_system
        - chunkSize / chunk_size
        - cellSize / cell_size
        - surfaceY / surface_y
        - minY / min_y
        - maxY / max_y
        - seed
        - blockRegistryId / block_registry_id
        - blockRegistryVersion / block_registry_version
        - sourceService / source_service
        - externalRef / external_ref
        - spawn / spawnX/spawnY/spawnZ/spawnYaw/spawnPitch
        - spawnCoordinateSpace / precise spawn coordinates
        - globalReference (idempotent only)
        - globalReferenceLockReasons
        - metadata / metadataJson / metadata_json
        - metadataMerge
        - metadataRemoveKeys
        - status
        """
        if not isinstance(payload, Mapping):
            raise ValueError("World patch payload must be a JSON object.")

        self.ensure_not_deleted()

        earth_immutable_fields = {
            "templateId",
            "template_id",
            "providerId",
            "provider_id",
            "providerWorldId",
            "provider_world_id",
            "generatorType",
            "generator_type",
            "generatorVersion",
            "generator_version",
            "projectionType",
            "projection_type",
            "topologyType",
            "topology_type",
            "coordinateSystem",
            "coordinate_system",
            "chunkSize",
            "chunk_size",
            "cellSize",
            "cell_size",
        }
        if self.is_earth_world and any(
            key in payload for key in earth_immutable_fields
        ):
            raise ValueError(
                "Earth provider, geometry and chunk-grid fields are immutable "
                "outside a dedicated coordinate-frame migration."
            )

        changed = False

        if "globalReference" in payload or "global_reference" in payload:
            reference_value = (
                payload.get("globalReference")
                if "globalReference" in payload
                else payload.get("global_reference")
            )
            changed = self.set_global_reference(
                reference_value,
                allow_replace=False,
                updated_by_user_id=updated_by_user_id,
                touch=False,
            ) or changed

        if (
            "globalReferenceLockReasons" in payload
            or "global_reference_lock_reasons" in payload
        ):
            reasons = (
                payload.get("globalReferenceLockReasons")
                if "globalReferenceLockReasons" in payload
                else payload.get("global_reference_lock_reasons")
            )
            self.lock_global_reference(
                reasons or [],
                updated_by_user_id=updated_by_user_id,
                touch=False,
            )
            changed = True

        if "name" in payload:
            self.name = normalize_required_text(
                payload.get("name"),
                field_name="name",
                max_length=WORLD_NAME_MAX_LENGTH,
            )
            changed = True

        if "slug" in payload:
            self.slug = normalize_slug(payload.get("slug"))
            changed = True

        if "description" in payload:
            self.description = normalize_optional_text(
                payload.get("description"),
                field_name="description",
                max_length=WORLD_DESCRIPTION_MAX_LENGTH,
            )
            changed = True

        if "worldRole" in payload or "world_role" in payload:
            self.world_role = normalize_world_role(
                payload.get("worldRole")
                if "worldRole" in payload
                else payload.get("world_role")
            )
            changed = True

        provider_fields = {
            "templateId",
            "template_id",
            "providerId",
            "provider_id",
            "providerWorldId",
            "provider_world_id",
            "generatorType",
            "generator_type",
            "generatorVersion",
            "generator_version",
        }

        if any(key in payload for key in provider_fields):
            self.template_id = normalize_template_id(
                payload.get("templateId")
                if "templateId" in payload
                else payload.get("template_id", self.template_id)
            )
            self.provider_id = normalize_provider_id(
                payload.get("providerId")
                if "providerId" in payload
                else payload.get("provider_id", self.provider_id)
            )
            self.provider_world_id = normalize_provider_world_id(
                payload.get("providerWorldId")
                if "providerWorldId" in payload
                else payload.get("provider_world_id", self.provider_world_id)
            )
            self.generator_type = normalize_required_text(
                payload.get("generatorType")
                if "generatorType" in payload
                else payload.get("generator_type", self.generator_type),
                field_name="generator_type",
                max_length=WORLD_GENERATOR_TYPE_MAX_LENGTH,
            )
            self.generator_version = normalize_version_text(
                payload.get("generatorVersion")
                if "generatorVersion" in payload
                else payload.get("generator_version", self.generator_version),
                field_name="generator_version",
                max_length=WORLD_GENERATOR_VERSION_MAX_LENGTH,
                default=DEFAULT_GENERATOR_VERSION,
            )
            changed = True

        geometry_fields = {
            "projectionType",
            "projection_type",
            "topologyType",
            "topology_type",
            "coordinateSystem",
            "coordinate_system",
        }

        if any(key in payload for key in geometry_fields):
            self.projection_type = normalize_required_text(
                payload.get("projectionType")
                if "projectionType" in payload
                else payload.get("projection_type", self.projection_type),
                field_name="projection_type",
                max_length=WORLD_PROJECTION_TYPE_MAX_LENGTH,
            )
            self.topology_type = normalize_required_text(
                payload.get("topologyType")
                if "topologyType" in payload
                else payload.get("topology_type", self.topology_type),
                field_name="topology_type",
                max_length=WORLD_TOPOLOGY_TYPE_MAX_LENGTH,
            )
            self.coordinate_system = normalize_required_text(
                payload.get("coordinateSystem")
                if "coordinateSystem" in payload
                else payload.get("coordinate_system", self.coordinate_system),
                field_name="coordinate_system",
                max_length=WORLD_COORDINATE_SYSTEM_MAX_LENGTH,
            )
            changed = True

        if "chunkSize" in payload or "chunk_size" in payload:
            self.chunk_size = normalize_positive_int(
                payload.get("chunkSize")
                if "chunkSize" in payload
                else payload.get("chunk_size"),
                field_name="chunk_size",
                default=DEFAULT_CHUNK_SIZE,
            )
            changed = True

        if "cellSize" in payload or "cell_size" in payload:
            self.cell_size = normalize_positive_float(
                payload.get("cellSize")
                if "cellSize" in payload
                else payload.get("cell_size"),
                field_name="cell_size",
                default=DEFAULT_CELL_SIZE,
            )
            changed = True

        bounds_changed = False
        new_min_y = self.min_y
        new_surface_y = self.surface_y
        new_max_y = self.max_y

        if "minY" in payload or "min_y" in payload:
            new_min_y = normalize_int(
                payload.get("minY") if "minY" in payload else payload.get("min_y"),
                field_name="min_y",
                default=DEFAULT_MIN_Y,
            )
            bounds_changed = True

        if "surfaceY" in payload or "surface_y" in payload:
            new_surface_y = normalize_int(
                payload.get("surfaceY")
                if "surfaceY" in payload
                else payload.get("surface_y"),
                field_name="surface_y",
                default=DEFAULT_SURFACE_Y,
            )
            bounds_changed = True

        if "maxY" in payload or "max_y" in payload:
            new_max_y = normalize_int(
                payload.get("maxY") if "maxY" in payload else payload.get("max_y"),
                field_name="max_y",
                default=DEFAULT_MAX_Y,
            )
            bounds_changed = True

        if bounds_changed:
            validate_vertical_bounds(
                min_y=new_min_y,
                surface_y=new_surface_y,
                max_y=new_max_y,
            )
            self.min_y = new_min_y
            self.surface_y = new_surface_y
            self.max_y = new_max_y
            changed = True

        if "seed" in payload:
            self.seed = normalize_optional_text(
                payload.get("seed"),
                field_name="seed",
                max_length=WORLD_SEED_MAX_LENGTH,
            )
            changed = True

        if "blockRegistryId" in payload or "block_registry_id" in payload:
            self.block_registry_id = normalize_required_text(
                payload.get("blockRegistryId")
                if "blockRegistryId" in payload
                else payload.get("block_registry_id"),
                field_name="block_registry_id",
                max_length=WORLD_BLOCK_REGISTRY_ID_MAX_LENGTH,
            )
            changed = True

        if "blockRegistryVersion" in payload or "block_registry_version" in payload:
            self.block_registry_version = normalize_version_text(
                payload.get("blockRegistryVersion")
                if "blockRegistryVersion" in payload
                else payload.get("block_registry_version"),
                field_name="block_registry_version",
                max_length=WORLD_BLOCK_REGISTRY_VERSION_MAX_LENGTH,
                default=DEFAULT_BLOCK_REGISTRY_VERSION,
            )
            changed = True

        if "sourceService" in payload or "source_service" in payload:
            self.source_service = normalize_optional_text(
                payload.get("sourceService")
                if "sourceService" in payload
                else payload.get("source_service"),
                field_name="source_service",
                max_length=WORLD_SOURCE_SERVICE_MAX_LENGTH,
            )
            changed = True

        if "externalRef" in payload or "external_ref" in payload:
            self.external_ref = normalize_optional_text(
                payload.get("externalRef")
                if "externalRef" in payload
                else payload.get("external_ref"),
                field_name="external_ref",
                max_length=WORLD_EXTERNAL_REF_MAX_LENGTH,
            )
            changed = True

        spawn = (
            payload.get("spawn")
            if isinstance(payload.get("spawn"), Mapping)
            else None
        )
        precise_position = (
            spawn.get("precisePosition")
            if isinstance(spawn, Mapping)
            and isinstance(spawn.get("precisePosition"), Mapping)
            else {}
        )
        spawn_field_names = {
            "spawn",
            "spawnX",
            "spawn_x",
            "spawnY",
            "spawn_y",
            "spawnZ",
            "spawn_z",
            "spawnXPrecise",
            "spawn_x_precise",
            "spawnYPrecise",
            "spawn_y_precise",
            "spawnZPrecise",
            "spawn_z_precise",
            "spawnCoordinateSpace",
            "spawn_coordinate_space",
            "spawnYaw",
            "spawn_yaw",
            "spawnPitch",
            "spawn_pitch",
        }
        spawn_changed = any(
            key in payload for key in spawn_field_names
        )

        if spawn_changed:
            requested_space = _payload_get(
                spawn or {},
                "coordinateSpace",
                "coordinate_space",
                default=_payload_get(
                    payload,
                    "spawnCoordinateSpace",
                    "spawn_coordinate_space",
                    default=(
                        EARTH_SPAWN_COORDINATE_SPACE
                        if self.is_earth_world
                        else self.spawn_coordinate_space
                    ),
                ),
            )
            coordinate_space = normalize_spawn_coordinate_space(
                requested_space,
                default=(
                    EARTH_SPAWN_COORDINATE_SPACE
                    if self.is_earth_world
                    else DEFAULT_SPAWN_COORDINATE_SPACE
                ),
            )

            current_precise = self.spawn_precise_position

            raw_x = _payload_get(
                precise_position,
                "x",
                default=_payload_get(
                    spawn or {},
                    "preciseX",
                    "xPrecise",
                    "x",
                    default=_payload_get(
                        payload,
                        "spawnXPrecise",
                        "spawn_x_precise",
                        "spawnX",
                        "spawn_x",
                        default=current_precise["x"],
                    ),
                ),
            )
            raw_y = _payload_get(
                precise_position,
                "y",
                default=_payload_get(
                    spawn or {},
                    "preciseY",
                    "yPrecise",
                    "y",
                    default=_payload_get(
                        payload,
                        "spawnYPrecise",
                        "spawn_y_precise",
                        "spawnY",
                        "spawn_y",
                        default=current_precise["y"],
                    ),
                ),
            )
            raw_z = _payload_get(
                precise_position,
                "z",
                default=_payload_get(
                    spawn or {},
                    "preciseZ",
                    "zPrecise",
                    "z",
                    default=_payload_get(
                        payload,
                        "spawnZPrecise",
                        "spawn_z_precise",
                        "spawnZ",
                        "spawn_z",
                        default=current_precise["z"],
                    ),
                ),
            )
            raw_yaw = _payload_get(
                spawn or {},
                "yaw",
                default=_payload_get(
                    payload,
                    "spawnYaw",
                    "spawn_yaw",
                    default=self.spawn_yaw,
                ),
            )
            raw_pitch = _payload_get(
                spawn or {},
                "pitch",
                default=_payload_get(
                    payload,
                    "spawnPitch",
                    "spawn_pitch",
                    default=self.spawn_pitch,
                ),
            )

            if (
                coordinate_space == EARTH_SPAWN_COORDINATE_SPACE
                or self.is_earth_world
            ):
                self.set_spawn_metric_position(
                    x=raw_x,
                    y=raw_y,
                    z=raw_z,
                    yaw=raw_yaw,
                    pitch=raw_pitch,
                    updated_by_user_id=updated_by_user_id,
                    touch=False,
                )
            else:
                normalized_x = normalize_int(
                    raw_x,
                    field_name="spawn_x",
                    default=DEFAULT_SPAWN_X,
                )
                normalized_y = normalize_int(
                    raw_y,
                    field_name="spawn_y",
                    default=DEFAULT_SPAWN_Y,
                )
                normalized_z = normalize_int(
                    raw_z,
                    field_name="spawn_z",
                    default=DEFAULT_SPAWN_Z,
                )
                self.spawn_coordinate_space = (
                    DEFAULT_SPAWN_COORDINATE_SPACE
                )
                self.spawn_x = normalized_x
                self.spawn_y = normalized_y
                self.spawn_z = normalized_z
                self.spawn_x_precise = Decimal(normalized_x)
                self.spawn_y_precise = Decimal(normalized_y)
                self.spawn_z_precise = Decimal(normalized_z)
                self.spawn_yaw = normalize_float(
                    raw_yaw,
                    field_name="spawn_yaw",
                    default=DEFAULT_SPAWN_YAW,
                )
                self.spawn_pitch = normalize_float(
                    raw_pitch,
                    field_name="spawn_pitch",
                    default=DEFAULT_SPAWN_PITCH,
                )

            changed = True

        metadata_replace_key = None
        for candidate in ("metadataJson", "metadata_json", "metadata"):
            if candidate in payload:
                metadata_replace_key = candidate
                break

        if metadata_replace_key is not None:
            self.metadata_json = normalize_metadata(payload.get(metadata_replace_key))
            changed = True

        if "metadataMerge" in payload:
            merge_value = payload.get("metadataMerge")
            if not isinstance(merge_value, Mapping):
                raise ValueError("metadataMerge must be a JSON object/dict.")
            current = normalize_metadata(self.metadata_json)
            for key, value in merge_value.items():
                current[str(key)] = make_json_safe(value)
            self.metadata_json = current
            changed = True

        if "metadataRemoveKeys" in payload:
            remove_keys = payload.get("metadataRemoveKeys") or []
            if not isinstance(remove_keys, Iterable) or isinstance(remove_keys, (str, bytes)):
                raise ValueError("metadataRemoveKeys must be a list of keys.")
            current = normalize_metadata(self.metadata_json)
            for key in remove_keys:
                current.pop(str(key), None)
            self.metadata_json = current
            changed = True

        if "status" in payload:
            self.set_status(
                str(payload.get("status")),
                updated_by_user_id=updated_by_user_id,
            )
            return

        if changed:
            self.touch(updated_by_user_id=updated_by_user_id)

    def get_validation_errors(self) -> Dict[str, str]:
        """
        Return validation errors without raising.

        Useful for debug/status endpoints and repository preflight checks.
        """
        errors: Dict[str, str] = {}

        try:
            normalize_db_id(self.project_db_id, field_name="project_db_id")
        except Exception as exc:
            errors["projectDbId"] = str(exc)

        try:
            normalize_db_id(self.universe_db_id, field_name="universe_db_id")
        except Exception as exc:
            errors["universeDbId"] = str(exc)

        try:
            normalize_world_id(self.world_id)
        except Exception as exc:
            errors["worldId"] = str(exc)

        if is_provider_like_world_id(self.world_id):
            errors["worldId"] = (
                "world_id must be a concrete editable world id. "
                "`flat` or `earth` belongs in template_id/provider_world_id."
            )

        try:
            normalize_required_text(
                self.name,
                field_name="name",
                max_length=WORLD_NAME_MAX_LENGTH,
            )
        except Exception as exc:
            errors["name"] = str(exc)

        try:
            normalize_status(self.status)
        except Exception as exc:
            errors["status"] = str(exc)

        try:
            normalize_world_type(self.world_type)
        except Exception as exc:
            errors["worldType"] = str(exc)

        try:
            normalize_world_role(self.world_role)
        except Exception as exc:
            errors["worldRole"] = str(exc)

        try:
            normalize_world_scope(self.world_scope)
        except Exception as exc:
            errors["worldScope"] = str(exc)

        try:
            normalize_template_id(self.template_id)
        except Exception as exc:
            errors["templateId"] = str(exc)

        try:
            normalize_provider_id(self.provider_id)
        except Exception as exc:
            errors["providerId"] = str(exc)

        try:
            normalize_provider_world_id(self.provider_world_id)
        except Exception as exc:
            errors["providerWorldId"] = str(exc)

        try:
            normalize_positive_int(
                self.chunk_size,
                field_name="chunk_size",
                default=DEFAULT_CHUNK_SIZE,
            )
        except Exception as exc:
            errors["chunkSize"] = str(exc)

        try:
            normalize_positive_float(
                self.cell_size,
                field_name="cell_size",
                default=DEFAULT_CELL_SIZE,
            )
        except Exception as exc:
            errors["cellSize"] = str(exc)

        try:
            validate_vertical_bounds(
                min_y=int(self.min_y),
                surface_y=int(self.surface_y),
                max_y=int(self.max_y),
            )
        except Exception as exc:
            errors["verticalBounds"] = str(exc)

        try:
            normalize_float(
                self.spawn_yaw,
                field_name="spawn_yaw",
                default=DEFAULT_SPAWN_YAW,
            )
        except Exception as exc:
            errors["spawnYaw"] = str(exc)

        try:
            normalize_float(
                self.spawn_pitch,
                field_name="spawn_pitch",
                default=DEFAULT_SPAWN_PITCH,
            )
        except Exception as exc:
            errors["spawnPitch"] = str(exc)

        try:
            normalize_metadata(self.metadata_json)
        except Exception as exc:
            errors["metadataJson"] = str(exc)

        if self.slug is not None:
            try:
                normalize_slug(self.slug)
            except Exception as exc:
                errors["slug"] = str(exc)

        if self.source_service is not None:
            try:
                normalize_optional_text(
                    self.source_service,
                    field_name="source_service",
                    max_length=WORLD_SOURCE_SERVICE_MAX_LENGTH,
                )
            except Exception as exc:
                errors["sourceService"] = str(exc)

        if self.external_ref is not None:
            try:
                normalize_optional_text(
                    self.external_ref,
                    field_name="external_ref",
                    max_length=WORLD_EXTERNAL_REF_MAX_LENGTH,
                )
            except Exception as exc:
                errors["externalRef"] = str(exc)

        try:
            normalize_spawn_coordinate_space(
                self.spawn_coordinate_space,
                default=(
                    EARTH_SPAWN_COORDINATE_SPACE
                    if self.is_earth_world
                    else DEFAULT_SPAWN_COORDINATE_SPACE
                ),
            )
        except Exception as exc:
            errors["spawnCoordinateSpace"] = str(exc)

        precise_values = (
            self.spawn_x_precise,
            self.spawn_y_precise,
            self.spawn_z_precise,
        )
        if any(value is not None for value in precise_values):
            if not all(value is not None for value in precise_values):
                errors["spawnPrecisePosition"] = (
                    "Precise spawn coordinates must be all null or all populated."
                )
            else:
                for field_name, value in (
                    ("spawnXPrecise", self.spawn_x_precise),
                    ("spawnYPrecise", self.spawn_y_precise),
                    ("spawnZPrecise", self.spawn_z_precise),
                ):
                    try:
                        normalize_decimal_coordinate(
                            value,
                            field_name=field_name,
                        )
                    except Exception as exc:
                        errors[field_name] = str(exc)

        try:
            normalize_reference_lock_reasons(
                self.global_reference_lock_reasons_json
            )
        except Exception as exc:
            errors["globalReferenceLockReasons"] = str(exc)

        if self.is_earth_world:
            expected_mapping = (
                EARTH_TEMPLATE_ID,
                EARTH_PROVIDER_ID,
                EARTH_PROVIDER_WORLD_ID,
                EARTH_GENERATOR_TYPE,
                EARTH_PROJECTION_TYPE,
                EARTH_TOPOLOGY_TYPE,
                EARTH_COORDINATE_SYSTEM,
            )
            actual_mapping = (
                self.template_id,
                self.provider_id,
                self.provider_world_id,
                self.generator_type,
                self.projection_type,
                self.topology_type,
                self.coordinate_system,
            )
            if actual_mapping != expected_mapping:
                errors["earthProviderContract"] = (
                    "Earth provider mapping and geometry must match Earth v1."
                )

            if not self.has_global_reference:
                errors["globalReference"] = (
                    "Earth worlds require exactly one global reference."
                )
            else:
                try:
                    reference = self.get_global_reference_point()
                    if reference.grid.grid_id != EARTH_GRID_ID:
                        errors["globalReferenceGridId"] = (
                            f"Earth grid id must be '{EARTH_GRID_ID}'."
                        )
                    if reference.grid.grid_version != EARTH_GRID_VERSION:
                        errors["globalReferenceGridVersion"] = (
                            f"Earth grid version must be '{EARTH_GRID_VERSION}'."
                        )
                except Exception as exc:
                    errors["globalReference"] = str(exc)

            if (
                self.spawn_coordinate_space
                != EARTH_SPAWN_COORDINATE_SPACE
            ):
                errors["spawnCoordinateSpace"] = (
                    "Earth spawn must be persisted in local_metric coordinates."
                )
        elif self.has_global_reference:
            errors["globalReference"] = (
                "Only earth worlds may persist a global reference."
            )
        elif int(self.coordinate_frame_revision or 0) != 0:
            errors["coordinateFrameRevision"] = (
                "Worlds without a global reference must use revision 0."
            )

        if (
            self.global_reference_locked_at is not None
            and not self.has_global_reference
        ):
            errors["globalReferenceLockedAt"] = (
                "A reference lock requires a stored global reference."
            )

        if self.revision is None or int(self.revision) < 1:
            errors["revision"] = "revision must be greater than or equal to 1."

        return errors

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
        project_id: Optional[str] = None,
        universe_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Serialize the world instance for API/service responses.

        Internal database IDs are excluded by default.
        """
        resolved_project_id = project_id if project_id is not None else self.project_public_id
        resolved_universe_id = universe_id if universe_id is not None else self.universe_public_id

        result: Dict[str, Any] = {
            "projectId": resolved_project_id,
            "chunkProjectId": resolved_project_id,
            "universeId": resolved_universe_id,
            "chunkUniverseId": resolved_universe_id,
            "worldId": self.world_id,
            "chunkWorldId": self.world_id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "schemaVersion": self.schema_version,
            "revision": self.revision,
            "worldType": self.world_type,
            "worldRole": self.world_role,
            "worldScope": self.world_scope,
            "templateId": self.template_id,
            "providerId": self.provider_id,
            "providerWorldId": self.provider_world_id,
            "generatorType": self.generator_type,
            "generatorVersion": self.generator_version,
            "projectionType": self.projection_type,
            "topologyType": self.topology_type,
            "coordinateSystem": self.coordinate_system,
            "chunkSize": self.chunk_size,
            "cellSize": self.cell_size,
            "surfaceY": self.surface_y,
            "minY": self.min_y,
            "maxY": self.max_y,
            "seed": self.seed,
            "blockRegistryId": self.block_registry_id,
            "blockRegistryVersion": self.block_registry_version,
            "spawn": self.spawn_context,
            "coordinateFrame": self.coordinate_frame_context,
            "globalReference": self.global_reference_context(
                include_crs_definition=False
            ),
            "sourceService": self.source_service,
            "externalRef": self.external_ref,
            "createdByUserId": self.created_by_user_id,
            "updatedByUserId": self.updated_by_user_id,
            "createdAt": datetime_to_iso(self.created_at),
            "updatedAt": datetime_to_iso(self.updated_at),
            "archivedAt": datetime_to_iso(self.archived_at),
            "deletedAt": datetime_to_iso(self.deleted_at),
            "chunkConfig": self.chunk_config,
            "providerMapping": self.provider_mapping,
            "registryContext": self.registry_context,
            "routeHints": self.build_route_hints(),
            "flags": {
                "active": self.is_active,
                "archived": self.is_archived,
                "deleted": self.is_deleted,
                "runtimeWorld": self.world_type == WORLD_TYPE_RUNTIME,
                "defaultSpawn": self.world_role == WORLD_ROLE_DEFAULT_SPAWN,
                "providerLikeWorldId": is_provider_like_world_id(self.world_id),
                "earthWorld": self.is_earth_world,
                "globalReferencePresent": self.has_global_reference,
                "globalReferenceLocked": self.is_global_reference_locked,
            },
        }

        if include_metadata:
            result["metadata"] = normalize_metadata(self.metadata_json)

        if include_internal:
            result["id"] = self.id
            result["projectDbId"] = self.project_db_id
            result["universeDbId"] = self.universe_db_id
            result["worldContextKey"] = self.build_world_context_key()
            result["globalReferencePersistence"] = (
                self.global_reference_context(
                    include_crs_definition=True
                )
            )

        return result

    def to_public_dict(
        self,
        *,
        project_id: Optional[str] = None,
        universe_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Serialize without internal database identifiers."""
        return self.to_dict(
            include_internal=False,
            include_metadata=True,
            project_id=project_id,
            universe_id=universe_id,
        )


__all__ = [
    "DEFAULT_BLOCK_REGISTRY_ID",
    "DEFAULT_BLOCK_REGISTRY_VERSION",
    "DEFAULT_CELL_SIZE",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_COORDINATE_SYSTEM",
    "DEFAULT_GENERATOR_TYPE",
    "DEFAULT_GENERATOR_VERSION",
    "DEFAULT_MAX_Y",
    "DEFAULT_MIN_Y",
    "DEFAULT_PROJECTION_TYPE",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_PROVIDER_WORLD_ID",
    "DEFAULT_SPAWN_COORDINATE_SPACE",
    "DEFAULT_SPAWN_PITCH",
    "DEFAULT_SPAWN_X",
    "DEFAULT_SPAWN_Y",
    "DEFAULT_SPAWN_YAW",
    "DEFAULT_SPAWN_Z",
    "DEFAULT_SURFACE_Y",
    "DEFAULT_TEMPLATE_ID",
    "DEFAULT_TOPOLOGY_TYPE",
    "DEFAULT_WORLD_ID",
    "DEFAULT_WORLD_NAME",
    "DEFAULT_WORLD_SLUG",
    "EARTH_COORDINATE_SYSTEM",
    "EARTH_GENERATOR_TYPE",
    "EARTH_GENERATOR_VERSION",
    "EARTH_GRID_ID",
    "EARTH_GRID_VERSION",
    "EARTH_PROJECTION_TYPE",
    "EARTH_PROVIDER_ID",
    "EARTH_PROVIDER_WORLD_ID",
    "EARTH_SPAWN_COORDINATE_SPACE",
    "EARTH_TEMPLATE_ID",
    "EARTH_TOPOLOGY_TYPE",
    "EARTH_WORLD_NAME",
    "JSON_COLUMN_TYPE",
    "NULLABLE_JSON_COLUMN_TYPE",
    "PROVIDER_LIKE_WORLD_IDS",
    "VALID_SPAWN_COORDINATE_SPACES",
    "VALID_WORLD_ROLES",
    "VALID_WORLD_SCOPES",
    "VALID_WORLD_STATUSES",
    "VALID_WORLD_TYPES",
    "WORLD_INSTANCE_SCHEMA_VERSION",
    "WORLD_ROLE_DEFAULT_SPAWN",
    "WORLD_SCOPE_PROJECT",
    "WORLD_STATUS_ACTIVE",
    "WORLD_STATUS_ARCHIVED",
    "WORLD_STATUS_DELETED",
    "WORLD_TYPE_RUNTIME",
    "WorldInstance",
    "coerce_global_reference",
    "datetime_to_iso",
    "decimal_to_plain_text",
    "generate_world_id",
    "is_provider_like_world_id",
    "make_json_safe",
    "normalize_concrete_world_id",
    "normalize_decimal_coordinate",
    "normalize_global_reference_storage",
    "normalize_spawn_coordinate_space",
    "normalize_world_id",
    "utc_now",
]
