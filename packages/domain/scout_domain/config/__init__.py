"""Repository-declared configuration parsing and validation."""

from __future__ import annotations

from scout_domain.config.repository import (
    CONFIG_PATH,
    CommandsConfig,
    CommandSpec,
    FeatureVerificationConfig,
    KnowledgeConfig,
    NotificationsConfig,
    ProjectConfig,
    ScoutRepositoryConfig,
    SecurityConfig,
    VerificationConfig,
    default_config_document,
    parse_repository_config,
)

__all__ = [
    "CONFIG_PATH",
    "CommandSpec",
    "CommandsConfig",
    "FeatureVerificationConfig",
    "KnowledgeConfig",
    "NotificationsConfig",
    "ProjectConfig",
    "ScoutRepositoryConfig",
    "SecurityConfig",
    "VerificationConfig",
    "default_config_document",
    "parse_repository_config",
]
