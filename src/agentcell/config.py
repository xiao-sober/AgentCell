"""Validated application configuration loaded from AgentCell TOML."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from agentcell.errors import ProviderConfigurationError
from agentcell.providers.models import ModelSpec, ModelSpecDefinition, ProviderName


class AgentCellSettings(BaseSettings):
    """Top-level settings with typed model references and explicit source order."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTCELL_",
        extra="forbid",
        frozen=True,
        toml_file="agentcell.toml",
    )

    models: dict[str, ModelSpecDefinition] = Field(min_length=1)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            TomlConfigSettingsSource(settings_cls),
        )

    @classmethod
    def from_toml(cls, path: str | Path) -> AgentCellSettings:
        """Load one explicit TOML file, useful for tests and non-default launch paths."""

        source = TomlConfigSettingsSource(cls, Path(path))
        return cls.model_validate(source())

    def model_spec(self, model_ref: str) -> ModelSpec[ProviderName]:
        """Resolve a stable model reference without exposing mapping internals."""

        try:
            return self.models[model_ref]
        except KeyError as error:
            raise ProviderConfigurationError(f"Unknown model reference {model_ref!r}") from error
