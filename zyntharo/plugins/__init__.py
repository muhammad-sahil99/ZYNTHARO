"""Self-extension: the plugin host and the Forge (the Artificer's workshop)."""

from .forge import ActionSpec, Forge, PluginSpec
from .loader import MANIFEST_NAME, SCHEMA_VERSION, PluginHost, PluginManifest, ValidationResult

__all__ = [
    "ActionSpec",
    "Forge",
    "PluginSpec",
    "MANIFEST_NAME",
    "SCHEMA_VERSION",
    "PluginHost",
    "PluginManifest",
    "ValidationResult",
]
