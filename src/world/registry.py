# src/world/registry.py
"""
VECTOPLAN World Registry.

Diese Datei enthält die neutrale Registry für verfügbare World-Provider.

Aufgabe dieser Registry:
- bekannte Welt-Provider registrieren
- Provider über worldId / providerId / Alias auflösen
- Default-Welt kennen
- verfügbare Welten auflisten
- keine konkrete Weltlogik ausführen
- keine world.json lesen
- keine Chunks generieren
- keine Provider-Module beim Registry-Import hart importieren

Wichtig:
Die Registry ist nur ein Verzeichnis.

Sie weiß zum Beispiel:

    "flat" gehört zu "src.world.flat.provider"

Sie weiß aber nicht:

    wie flat/world.json aufgebaut ist
    wie ein Flat-Chunk generiert wird
    welche Layer die flache Welt besitzt

Diese Trennung ist wichtig, damit später weitere Welten ergänzt werden können:

    src/world/flat/
    src/world/realWorld/
    src/world/devTerrain/
    src/world/planetPatch/

Der spätere Loader nutzt diese Registry, um den passenden Provider zu finden.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from importlib.util import find_spec
from threading import RLock
from typing import Any, Final

try:
    from src.world.errors import (
        WorldNotFoundError,
        WorldRegistryError,
        WorldValidationError,
        make_json_safe,
    )
    from src.world.models import WorldListResult, WorldProviderInfo
except Exception as exc:  # pragma: no cover - defensive bootstrap guard
    raise RuntimeError(
        "src.world.registry requires src.world.errors and src.world.models "
        "to be importable before the registry can be used."
    ) from exc


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORLD_REGISTRY_VERSION: Final[str] = "0.1.0"

DEFAULT_WORLD_ID: Final[str] = "flat"

DEFAULT_PROVIDER_MODULE_PREFIX: Final[str] = "src.world"

RESERVED_PROVIDER_IDS: Final[frozenset[str]] = frozenset(
    {
        "",
        ".",
        "..",
        "__pycache__",
        "__init__",
        "models",
        "errors",
        "registry",
        "loader",
        "service",
        "serializer",
    }
)

DEFAULT_WORLD_PROVIDER_DEFINITIONS: Final[tuple[dict[str, Any], ...]] = (
    {
        "providerId": "flat",
        "worldType": "flat",
        "label": "Flat Debug World",
        "providerModule": "src.world.flat.provider",
        "configPath": "src/world/flat/world.json",
        "supportsChunkGeneration": True,
        "supportsWorldMetadata": True,
        "aliases": ("default", "flat-local-v1", "dev-flat"),
        "metadata": {
            "description": "Initial flat deterministic debug world provider.",
            "projectionType": "flat-local-v1",
            "topologyType": "flat-unbounded-v1",
            "stage": "phase-1",
        },
    },
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any, *, default: str = "") -> str:
    """
    Wandelt einen beliebigen Wert robust in einen bereinigten String um.
    """
    if value is None:
        return default

    try:
        text = str(value).strip()
    except Exception:
        return default

    return text if text else default


def _normalize_key(value: Any) -> str:
    """
    Normalisiert einen Registry-Schlüssel.

    Registry-Schlüssel sind bewusst case-sensitiv genug für IDs,
    aber tolerant gegen Leerzeichen.
    """
    return _safe_str(value)


def _normalize_aliases(value: Any) -> tuple[str, ...]:
    """
    Normalisiert Aliase aus Tuple/List/Set/String zu einem stabilen Tuple.
    """
    if value is None:
        return tuple()

    raw_values: list[Any]

    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, Iterable):
        raw_values = list(value)
    else:
        raw_values = [value]

    aliases: list[str] = []
    seen: set[str] = set()

    for item in raw_values:
        alias = _normalize_key(item)

        if not alias:
            continue

        if alias in seen:
            continue

        seen.add(alias)
        aliases.append(alias)

    return tuple(aliases)


def _module_spec_exists(module_path: str) -> bool:
    """
    Prüft defensiv, ob ein Modulpfad grundsätzlich auffindbar ist.

    Wichtig:
    - Das Modul wird dabei nicht importiert.
    - find_spec kann bei defekten Importpfaden selbst Exceptions werfen.
    """
    try:
        return find_spec(module_path) is not None
    except Exception:
        return False


def _validate_provider_id(provider_id: str) -> None:
    """
    Prüft eine providerId auf offensichtliche Strukturfehler.
    """
    normalized = _normalize_key(provider_id)

    if not normalized:
        raise WorldValidationError(
            "World provider id must not be empty.",
            details={"providerId": provider_id},
        )

    if normalized in RESERVED_PROVIDER_IDS:
        raise WorldValidationError(
            f"World provider id '{normalized}' is reserved.",
            details={
                "providerId": normalized,
                "reservedProviderIds": sorted(RESERVED_PROVIDER_IDS),
            },
        )

    if "/" in normalized or "\\" in normalized:
        raise WorldValidationError(
            "World provider id must not contain path separators.",
            details={"providerId": normalized},
        )


def _validate_provider_module(provider_module: str) -> None:
    """
    Prüft den Provider-Modulpfad auf offensichtliche Strukturfehler.

    Hier wird nur strukturell geprüft. Ob das Modul wirklich importierbar ist,
    kann optional über verify_importable=True geprüft werden.
    """
    normalized = _normalize_key(provider_module)

    if not normalized:
        raise WorldValidationError(
            "World provider module must not be empty.",
            details={"providerModule": provider_module},
        )

    if normalized.startswith(".") or normalized.endswith("."):
        raise WorldValidationError(
            "World provider module must not start or end with '.'.",
            details={"providerModule": normalized},
        )

    if ".." in normalized:
        raise WorldValidationError(
            "World provider module must not contain '..'.",
            details={"providerModule": normalized},
        )


def _info_from_definition(definition: Mapping[str, Any]) -> tuple[WorldProviderInfo, tuple[str, ...]]:
    """
    Wandelt eine Registry-Definition in ProviderInfo + Aliase um.
    """
    if not isinstance(definition, Mapping):
        raise WorldRegistryError(
            "World provider definition must be an object.",
            details={"definition": make_json_safe(definition)},
        )

    provider_info = WorldProviderInfo.from_dict(definition)
    aliases = _normalize_aliases(definition.get("aliases", ()))

    return provider_info, aliases


# ---------------------------------------------------------------------------
# Registry entries
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorldProviderRegistration:
    """
    Vollständiger Registry-Eintrag für einen World-Provider.

    WorldProviderInfo beschreibt den Provider für externe Auflistung.
    Die Registration ergänzt interne Registry-Daten wie Aliase und Status.
    """

    info: WorldProviderInfo
    aliases: tuple[str, ...] = field(default_factory=tuple)
    enabled: bool = True
    import_verified: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def provider_id(self) -> str:
        return self.info.provider_id

    @property
    def world_type(self) -> str:
        return self.info.world_type

    @property
    def provider_module(self) -> str:
        return self.info.provider_module

    @property
    def config_path(self) -> str | None:
        return self.info.config_path

    @property
    def all_keys(self) -> tuple[str, ...]:
        """
        Alle Schlüssel, unter denen dieser Provider erreichbar sein soll.
        """
        keys: list[str] = [self.provider_id]

        for alias in self.aliases:
            if alias not in keys:
                keys.append(alias)

        return tuple(keys)

    def to_provider_info(self) -> WorldProviderInfo:
        """
        Gibt die öffentliche ProviderInfo zurück.
        """
        return self.info

    def to_dict(self, *, camel_case: bool = True) -> dict[str, Any]:
        """
        Serialisiert den Registry-Eintrag.
        """
        data = {
            "info": self.info.to_dict(camel_case=camel_case),
            "aliases": list(self.aliases),
            "enabled": self.enabled,
            "importVerified": self.import_verified,
            "metadata": self.metadata,
        }

        if not camel_case:
            data["import_verified"] = data.pop("importVerified")

        return data

    def with_import_verified(self, value: bool) -> "WorldProviderRegistration":
        """
        Gibt eine Kopie mit aktualisiertem import_verified-Status zurück.
        """
        return replace(self, import_verified=bool(value))

    def with_enabled(self, value: bool) -> "WorldProviderRegistration":
        """
        Gibt eine Kopie mit aktualisiertem enabled-Status zurück.
        """
        return replace(self, enabled=bool(value))


# ---------------------------------------------------------------------------
# WorldRegistry
# ---------------------------------------------------------------------------

class WorldRegistry:
    """
    Registry für verfügbare World-Provider.

    Die Registry ist threadsicher genug für normale Flask-/Gunicorn-Nutzung:
    - Mutationen werden mit RLock geschützt.
    - Leseoperationen geben Kopien oder unveränderliche Tupel zurück.

    Die Registry importiert Provider-Module nicht automatisch.
    Der spätere WorldLoader übernimmt den eigentlichen Import.
    """

    def __init__(
        self,
        *,
        default_world_id: str = DEFAULT_WORLD_ID,
        provider_definitions: Iterable[Mapping[str, Any]] | None = None,
        verify_importable: bool = False,
        strict: bool = True,
    ) -> None:
        self._lock = RLock()
        self._registrations_by_provider_id: dict[str, WorldProviderRegistration] = {}
        self._aliases_to_provider_id: dict[str, str] = {}
        self._default_world_id = _normalize_key(default_world_id) or DEFAULT_WORLD_ID
        self._strict = bool(strict)

        if provider_definitions is not None:
            self.register_many(
                provider_definitions,
                verify_importable=verify_importable,
                replace_existing=False,
            )

    @property
    def default_world_id(self) -> str:
        """
        Aktuelle Default-Welt-ID.
        """
        with self._lock:
            return self._default_world_id

    @property
    def strict(self) -> bool:
        """
        Gibt zurück, ob die Registry im Strict-Modus arbeitet.
        """
        return self._strict

    def set_default_world_id(self, world_id: str, *, require_registered: bool = True) -> None:
        """
        Setzt die Default-Welt.

        Wenn require_registered=True ist, muss die Welt bereits registriert sein.
        """
        normalized = _normalize_key(world_id)

        if not normalized:
            raise WorldRegistryError(
                "Default world id must not be empty.",
                details={"worldId": world_id},
            )

        with self._lock:
            if require_registered and not self.has(normalized, include_disabled=True):
                raise WorldNotFoundError(
                    normalized,
                    details={
                        "operation": "set_default_world_id",
                        "availableWorldIds": self.provider_ids(include_disabled=True),
                    },
                )

            self._default_world_id = normalized

    def register(
        self,
        provider: WorldProviderInfo | Mapping[str, Any],
        *,
        aliases: Iterable[str] | None = None,
        enabled: bool = True,
        verify_importable: bool = False,
        replace_existing: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> WorldProviderRegistration:
        """
        Registriert einen World-Provider.

        provider kann sein:
        - WorldProviderInfo
        - Dictionary im Format von DEFAULT_WORLD_PROVIDER_DEFINITIONS

        Wenn replace_existing=False ist, wird ein vorhandener providerId- oder
        Alias-Konflikt abgelehnt.
        """
        if isinstance(provider, WorldProviderInfo):
            info = provider
            provider_aliases = _normalize_aliases(aliases)
        elif isinstance(provider, Mapping):
            info, definition_aliases = _info_from_definition(provider)
            explicit_aliases = _normalize_aliases(aliases)
            provider_aliases = tuple(dict.fromkeys((*definition_aliases, *explicit_aliases)))
        else:
            raise WorldRegistryError(
                "Provider registration requires WorldProviderInfo or mapping.",
                details={"provider": make_json_safe(provider)},
            )

        _validate_provider_id(info.provider_id)
        _validate_provider_module(info.provider_module)

        normalized_provider_id = _normalize_key(info.provider_id)
        normalized_aliases = tuple(
            alias for alias in _normalize_aliases(provider_aliases)
            if alias != normalized_provider_id
        )

        if verify_importable and not _module_spec_exists(info.provider_module):
            raise WorldRegistryError(
                "World provider module is not importable.",
                details={
                    "providerId": normalized_provider_id,
                    "providerModule": info.provider_module,
                },
            )

        registration = WorldProviderRegistration(
            info=info,
            aliases=normalized_aliases,
            enabled=bool(enabled),
            import_verified=bool(verify_importable),
            metadata=dict(metadata or {}),
        )

        with self._lock:
            self._register_locked(
                registration,
                replace_existing=replace_existing,
            )

        return registration

    def register_many(
        self,
        providers: Iterable[WorldProviderInfo | Mapping[str, Any]],
        *,
        verify_importable: bool = False,
        replace_existing: bool = False,
    ) -> tuple[WorldProviderRegistration, ...]:
        """
        Registriert mehrere Provider.

        Bei Fehlern im Strict-Modus bricht die Registrierung ab.
        Im Non-Strict-Modus werden fehlerhafte Einträge übersprungen.
        """
        registrations: list[WorldProviderRegistration] = []
        errors: list[dict[str, Any]] = []

        for index, provider in enumerate(providers):
            try:
                registration = self.register(
                    provider,
                    verify_importable=verify_importable,
                    replace_existing=replace_existing,
                )
                registrations.append(registration)
            except Exception as exc:
                if self._strict:
                    raise

                errors.append(
                    {
                        "index": index,
                        "errorType": type(exc).__name__,
                        "error": str(exc),
                        "provider": make_json_safe(provider),
                    }
                )

        if errors and self._strict:
            raise WorldRegistryError(
                "One or more world providers could not be registered.",
                details={"errors": errors},
            )

        return tuple(registrations)

    def _register_locked(
        self,
        registration: WorldProviderRegistration,
        *,
        replace_existing: bool,
    ) -> None:
        """
        Interne Registrierungslogik.

        Muss unter self._lock aufgerufen werden.
        """
        provider_id = registration.provider_id

        if provider_id in self._registrations_by_provider_id and not replace_existing:
            raise WorldRegistryError(
                f"World provider '{provider_id}' is already registered.",
                details={
                    "providerId": provider_id,
                    "existingProvider": self._registrations_by_provider_id[provider_id].to_dict(),
                },
            )

        conflicting_aliases: list[dict[str, str]] = []

        for key in registration.all_keys:
            existing_provider_id = self._aliases_to_provider_id.get(key)

            if existing_provider_id is None:
                continue

            if replace_existing:
                continue

            if existing_provider_id != provider_id:
                conflicting_aliases.append(
                    {
                        "key": key,
                        "existingProviderId": existing_provider_id,
                        "newProviderId": provider_id,
                    }
                )

        if conflicting_aliases:
            raise WorldRegistryError(
                "World provider aliases conflict with existing registrations.",
                details={"conflicts": conflicting_aliases},
            )

        if replace_existing and provider_id in self._registrations_by_provider_id:
            self._remove_provider_aliases_locked(provider_id)

        self._registrations_by_provider_id[provider_id] = registration

        for key in registration.all_keys:
            self._aliases_to_provider_id[key] = provider_id

    def unregister(self, provider_id_or_alias: str) -> bool:
        """
        Entfernt einen Provider aus der Registry.

        Rückgabe:
            True, wenn etwas entfernt wurde.
            False, wenn kein Provider gefunden wurde.
        """
        key = _normalize_key(provider_id_or_alias)

        if not key:
            return False

        with self._lock:
            provider_id = self._aliases_to_provider_id.get(key, key)

            if provider_id not in self._registrations_by_provider_id:
                return False

            self._remove_provider_aliases_locked(provider_id)
            del self._registrations_by_provider_id[provider_id]

            if self._default_world_id == provider_id:
                self._default_world_id = DEFAULT_WORLD_ID

            return True

    def _remove_provider_aliases_locked(self, provider_id: str) -> None:
        """
        Entfernt alle Alias-Einträge eines Providers.

        Muss unter self._lock aufgerufen werden.
        """
        keys_to_remove = [
            key
            for key, mapped_provider_id in self._aliases_to_provider_id.items()
            if mapped_provider_id == provider_id
        ]

        for key in keys_to_remove:
            self._aliases_to_provider_id.pop(key, None)

    def clear(self) -> None:
        """
        Leert die Registry vollständig.
        """
        with self._lock:
            self._registrations_by_provider_id.clear()
            self._aliases_to_provider_id.clear()

    def has(self, provider_id_or_alias: str, *, include_disabled: bool = False) -> bool:
        """
        Prüft, ob ein Provider oder Alias existiert.
        """
        try:
            self.resolve(provider_id_or_alias, include_disabled=include_disabled)
            return True
        except WorldNotFoundError:
            return False

    def resolve(
        self,
        provider_id_or_alias: str | None = None,
        *,
        include_disabled: bool = False,
    ) -> WorldProviderRegistration:
        """
        Löst providerId oder Alias zu einer Registration auf.

        Wenn provider_id_or_alias leer ist, wird die Default-Welt verwendet.
        """
        key = _normalize_key(provider_id_or_alias) or self.default_world_id

        with self._lock:
            provider_id = self._aliases_to_provider_id.get(key, key)
            registration = self._registrations_by_provider_id.get(provider_id)

            if registration is None:
                raise WorldNotFoundError(
                    key,
                    details={
                        "availableWorldIds": self.provider_ids(include_disabled=True),
                        "availableAliases": self.aliases(),
                        "defaultWorldId": self._default_world_id,
                    },
                )

            if not include_disabled and not registration.enabled:
                raise WorldNotFoundError(
                    key,
                    details={
                        "reason": "world_provider_disabled",
                        "providerId": provider_id,
                        "availableWorldIds": self.provider_ids(include_disabled=False),
                    },
                )

            return registration

    def get_provider_info(
        self,
        provider_id_or_alias: str | None = None,
        *,
        include_disabled: bool = False,
    ) -> WorldProviderInfo:
        """
        Gibt die öffentliche ProviderInfo für eine Welt zurück.
        """
        return self.resolve(
            provider_id_or_alias,
            include_disabled=include_disabled,
        ).to_provider_info()

    def get_provider_module(
        self,
        provider_id_or_alias: str | None = None,
        *,
        include_disabled: bool = False,
    ) -> str:
        """
        Gibt den Modulpfad eines Providers zurück.
        """
        return self.resolve(
            provider_id_or_alias,
            include_disabled=include_disabled,
        ).provider_module

    def provider_ids(self, *, include_disabled: bool = False) -> tuple[str, ...]:
        """
        Gibt registrierte providerIds zurück.
        """
        with self._lock:
            ids = [
                provider_id
                for provider_id, registration in self._registrations_by_provider_id.items()
                if include_disabled or registration.enabled
            ]

        return tuple(sorted(ids))

    def aliases(self) -> tuple[str, ...]:
        """
        Gibt alle registrierten Alias-Schlüssel zurück.
        """
        with self._lock:
            provider_ids = set(self._registrations_by_provider_id.keys())
            aliases = [
                key
                for key in self._aliases_to_provider_id.keys()
                if key not in provider_ids
            ]

        return tuple(sorted(aliases))

    def registrations(
        self,
        *,
        include_disabled: bool = False,
    ) -> tuple[WorldProviderRegistration, ...]:
        """
        Gibt alle Registrierungen zurück.
        """
        with self._lock:
            values = [
                registration
                for registration in self._registrations_by_provider_id.values()
                if include_disabled or registration.enabled
            ]

        return tuple(sorted(values, key=lambda item: item.provider_id))

    def list_provider_info(
        self,
        *,
        include_disabled: bool = False,
    ) -> tuple[WorldProviderInfo, ...]:
        """
        Gibt alle öffentlichen ProviderInfos zurück.
        """
        return tuple(
            registration.to_provider_info()
            for registration in self.registrations(include_disabled=include_disabled)
        )

    def list_worlds(
        self,
        *,
        include_disabled: bool = False,
    ) -> WorldListResult:
        """
        Gibt eine WorldListResult-Struktur zurück.
        """
        return WorldListResult(
            worlds=self.list_provider_info(include_disabled=include_disabled),
            default_world_id=self.default_world_id,
            metadata={
                "registryVersion": WORLD_REGISTRY_VERSION,
                "aliases": self.aliases(),
            },
        )

    def enable(self, provider_id_or_alias: str) -> WorldProviderRegistration:
        """
        Aktiviert einen registrierten Provider.
        """
        return self._set_enabled(provider_id_or_alias, True)

    def disable(self, provider_id_or_alias: str) -> WorldProviderRegistration:
        """
        Deaktiviert einen registrierten Provider.
        """
        return self._set_enabled(provider_id_or_alias, False)

    def _set_enabled(
        self,
        provider_id_or_alias: str,
        value: bool,
    ) -> WorldProviderRegistration:
        """
        Interne Aktivierungs-/Deaktivierungslogik.
        """
        key = _normalize_key(provider_id_or_alias)

        with self._lock:
            registration = self.resolve(key, include_disabled=True)
            updated = registration.with_enabled(value)
            self._registrations_by_provider_id[registration.provider_id] = updated

        return updated

    def verify_provider_importable(
        self,
        provider_id_or_alias: str,
        *,
        update_registration: bool = True,
    ) -> bool:
        """
        Prüft, ob das Provider-Modul auffindbar ist.

        Das Modul wird nicht importiert.
        """
        with self._lock:
            registration = self.resolve(provider_id_or_alias, include_disabled=True)
            exists = _module_spec_exists(registration.provider_module)

            if update_registration:
                updated = registration.with_import_verified(exists)
                self._registrations_by_provider_id[registration.provider_id] = updated

        return exists

    def verify_all_provider_modules(
        self,
        *,
        update_registration: bool = True,
        include_disabled: bool = True,
    ) -> dict[str, bool]:
        """
        Prüft alle registrierten Provider-Module auf Auffindbarkeit.
        """
        results: dict[str, bool] = {}

        for registration in self.registrations(include_disabled=include_disabled):
            results[registration.provider_id] = self.verify_provider_importable(
                registration.provider_id,
                update_registration=update_registration,
            )

        return results

    def validate(
        self,
        *,
        require_default: bool = True,
        verify_importable: bool = False,
    ) -> None:
        """
        Validiert den Registry-Zustand.

        Diese Methode ist für Startup-Checks geeignet.
        """
        errors: list[dict[str, Any]] = []

        with self._lock:
            if require_default and not self.has(self._default_world_id, include_disabled=False):
                errors.append(
                    {
                        "code": "default_world_missing",
                        "defaultWorldId": self._default_world_id,
                        "availableWorldIds": self.provider_ids(include_disabled=False),
                    }
                )

            for provider_id, registration in self._registrations_by_provider_id.items():
                try:
                    _validate_provider_id(provider_id)
                    _validate_provider_module(registration.provider_module)
                except Exception as exc:
                    errors.append(
                        {
                            "code": "invalid_registration",
                            "providerId": provider_id,
                            "errorType": type(exc).__name__,
                            "error": str(exc),
                        }
                    )

                if verify_importable and not _module_spec_exists(registration.provider_module):
                    errors.append(
                        {
                            "code": "provider_module_not_found",
                            "providerId": provider_id,
                            "providerModule": registration.provider_module,
                        }
                    )

        if errors:
            raise WorldRegistryError(
                "World registry validation failed.",
                details={
                    "errors": errors,
                    "defaultWorldId": self.default_world_id,
                    "providerIds": self.provider_ids(include_disabled=True),
                },
            )

    def to_dict(
        self,
        *,
        include_disabled: bool = True,
        include_registrations: bool = True,
    ) -> dict[str, Any]:
        """
        Serialisiert den Registry-Zustand für Diagnose/Tests.
        """
        data: dict[str, Any] = {
            "registryVersion": WORLD_REGISTRY_VERSION,
            "defaultWorldId": self.default_world_id,
            "providerIds": self.provider_ids(include_disabled=include_disabled),
            "aliases": self.aliases(),
            "strict": self.strict,
        }

        if include_registrations:
            data["registrations"] = [
                registration.to_dict(camel_case=True)
                for registration in self.registrations(include_disabled=include_disabled)
            ]

        return data


# ---------------------------------------------------------------------------
# Default registry factory
# ---------------------------------------------------------------------------

def create_default_world_registry(
    *,
    verify_importable: bool = False,
    strict: bool = True,
) -> WorldRegistry:
    """
    Erstellt eine neue Default-Registry mit den bekannten Phase-1-Providern.

    Aktuell wird nur registriert:

        flat → src.world.flat.provider

    Wichtig:
    Dies erzeugt jedes Mal eine neue Registry-Instanz.
    Für eine gecachte Singleton-Instanz siehe get_default_world_registry().
    """
    registry = WorldRegistry(
        default_world_id=DEFAULT_WORLD_ID,
        strict=strict,
    )

    registry.register_many(
        DEFAULT_WORLD_PROVIDER_DEFINITIONS,
        verify_importable=verify_importable,
        replace_existing=False,
    )

    return registry


@lru_cache(maxsize=1)
def get_default_world_registry() -> WorldRegistry:
    """
    Gibt die pro Prozess gecachte Default-Registry zurück.

    Diese Registry importiert Provider-Module nicht automatisch.
    Dadurch kann der Service schrittweise aufgebaut werden, ohne dass
    src.world.flat.provider bereits beim Registry-Import existieren muss.

    Wenn nachträglich Provider-Dateien im laufenden Prozess erstellt oder
    verändert werden, kann der Cache geleert werden mit:

        get_default_world_registry.cache_clear()
    """
    return create_default_world_registry(
        verify_importable=False,
        strict=True,
    )


def reset_default_world_registry_cache() -> None:
    """
    Leert den Cache der Default-Registry.

    Nützlich für Tests oder lokale Entwicklungsprozesse.
    """
    get_default_world_registry.cache_clear()


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def resolve_world_provider(
    world_id: str | None = None,
    *,
    registry: WorldRegistry | None = None,
    include_disabled: bool = False,
) -> WorldProviderRegistration:
    """
    Komfortfunktion zum Auflösen eines Providers über die Default-Registry
    oder eine explizit übergebene Registry.
    """
    active_registry = registry or get_default_world_registry()

    return active_registry.resolve(
        world_id,
        include_disabled=include_disabled,
    )


def list_registered_worlds(
    *,
    registry: WorldRegistry | None = None,
    include_disabled: bool = False,
) -> WorldListResult:
    """
    Komfortfunktion zum Auflisten registrierter Welten.
    """
    active_registry = registry or get_default_world_registry()

    return active_registry.list_worlds(
        include_disabled=include_disabled,
    )


def ensure_world_registered(
    world_id: str,
    *,
    registry: WorldRegistry | None = None,
    include_disabled: bool = False,
) -> None:
    """
    Erzwingt, dass eine Welt registriert ist.

    Wirft WorldNotFoundError, wenn die Welt nicht existiert.
    """
    active_registry = registry or get_default_world_registry()
    active_registry.resolve(world_id, include_disabled=include_disabled)


__all__ = (
    "WORLD_REGISTRY_VERSION",
    "DEFAULT_WORLD_ID",
    "DEFAULT_PROVIDER_MODULE_PREFIX",
    "DEFAULT_WORLD_PROVIDER_DEFINITIONS",
    "WorldProviderRegistration",
    "WorldRegistry",
    "create_default_world_registry",
    "get_default_world_registry",
    "reset_default_world_registry_cache",
    "resolve_world_provider",
    "list_registered_worlds",
    "ensure_world_registered",
)