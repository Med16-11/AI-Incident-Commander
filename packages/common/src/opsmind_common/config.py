"""Validated environment-based configuration."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings shared by all OpsMind processes."""

    model_config = SettingsConfigDict(env_prefix="OPSMIND_", extra="ignore")

    environment: str = "local"
    local_demo_auth_enabled: bool = False
    local_demo_token: SecretStr | None = None
    log_level: str = "INFO"
    log_json: bool = True
    database_url: str = "postgresql+psycopg://opsmind@localhost:5432/opsmind"
    redis_url: str = "redis://localhost:6379/0"
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_insecure: bool = True
    prometheus_url: str = "http://prometheus:9090"
    loki_url: str = "http://loki:3100"
    detection_prometheus_job_regex: str = Field(default="shopstack", min_length=1, max_length=256)
    detection_poll_interval_seconds: int = Field(default=30, gt=0, le=3600)
    detection_baseline_window: int = Field(default=30, ge=2, le=10000)
    detection_baseline_min_samples: int = Field(default=8, ge=2, le=10000)
    detection_baseline_deviation_ratio: float = Field(default=0.50, gt=0)
    detection_error_rate_threshold: float = Field(default=0.05, gt=0, le=1)
    detection_dependency_error_rate_threshold: float = Field(default=0.10, gt=0, le=1)
    detection_latency_p95_threshold_seconds: float = Field(default=1.0, gt=0)
    detection_z_score_threshold: float = Field(default=3.5, gt=0)
    detection_traffic_drop_ratio: float = Field(default=0.50, gt=0, le=1)
    detection_log_occurrence_threshold: int = Field(default=5, ge=1)
    detection_log_window_seconds: int = Field(default=60, ge=1, le=86400)
    detection_correlation_window_seconds: int = Field(default=120, ge=1)
    detection_deduplication_ttl_seconds: int = Field(default=900, ge=1)
    correlation_before_seconds: int = Field(default=600, ge=0, le=604800)
    correlation_after_seconds: int = Field(default=300, ge=0, le=86400)
    correlation_api_token: SecretStr | None = None
    investigation_api_token: SecretStr | None = None
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    investigation_max_tool_calls: int = Field(default=12, ge=1, le=100)
    investigation_max_execution_seconds: float = Field(default=120.0, gt=0, le=1800)
    investigation_tool_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    investigation_max_context_chars: int = Field(default=48000, ge=10_000, le=1_000_000)
    investigation_confidence_threshold: float = Field(default=0.75, gt=0, le=1)
    remediation_approval_ttl_seconds: int = Field(default=900, ge=60, le=604800)
    slack_bot_token: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None
    slack_incident_channel: str | None = Field(default=None, min_length=1, max_length=100)
    slack_remediation_api_url: str | None = None
    slack_user_tokens: SecretStr | None = None
    kubernetes_namespace: str = Field(default="shopstack", min_length=1, max_length=63)
    kubernetes_request_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    kubernetes_max_retries: int = Field(default=3, ge=0, le=10)
    kubernetes_retry_backoff_seconds: float = Field(default=0.2, ge=0, le=10)
    kubernetes_dry_run: bool = False
    kubernetes_enabled: bool = False
    kubernetes_rollout_timeout_seconds: float = Field(default=120.0, gt=0, le=900)

    @field_validator("slack_incident_channel", mode="before")
    @classmethod
    def empty_optional_slack_channel_is_unset(cls, value: object) -> object:
        """Treat the empty placeholder in .env.example as an omitted integration setting."""
        return None if isinstance(value, str) and not value.strip() else value
