"""Settings — the single source of truth for configuration.

The earning rule (PLAN.md): an env var exists only if it must differ between
deployments of the same code. Everything else is a named constant in source.
"""

import sys
from enum import StrEnum
from typing import Any

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Constants (not settings — edit source to tune)
STATEMENT_TIMEOUT_S = 10  # PG server-side statement timeout (02)


class Environment(StrEnum):
    dev = "dev"
    staging = "staging"
    prod = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Field descriptions are mandatory: they become the comments in the generated
    # .env.template (`sg config sync`) — the template is never hand-edited.
    # Defaults are passed as `default=`, never positionally: pyright only reads a field
    # specifier's default from the keyword, so `Field("")` reads to it as "required" and
    # every Settings() call lights up (mypy's pydantic plugin hides this; pyright has none).
    environment: Environment = Field(description="dev | staging | prod. No default — boot fails without it.")
    database_url: str = Field(default="", description="Postgres URL (postgresql+asyncpg://...). Empty = db features disabled (loudly).")
    db_pooler: str = Field(default="none", description="none | pgbouncer — pgbouncer flips asyncpg prepared-statement caches off.")
    redis_url: str = Field(default="redis://localhost:6379/0", description="Redis connection URL.")
    cors_origins: list[str] = Field(default=[], description='JSON list of allowed CORS origins, e.g. ["https://app.example.com"].')
    forwarded_allow_ips: str = Field(default="127.0.0.1", description="Comma-separated IPs/CIDRs trusted for X-Forwarded-* headers.")
    request_timeout_s: int = Field(default=15, description="Per-request deadline (s). statement_timeout <= this < SHUTDOWN_GRACE_S.")
    shutdown_grace_s: int = Field(default=20, description="SIGTERM drain window (s).")
    obs_transports: list[str] | None = Field(default=None, description="Obs transports (jsonl|stdout|null). Unset = derived: dev=jsonl, else stdout.")
    obs_redact_fields: list[str] = Field(default=[], description="Extra field names to redact in obs events (extends built-ins).")
    obs_flush_timeout_s: int = Field(default=5, description="Bound on the final obs flush at shutdown (s).")
    trace_mode: str = Field(default="", description="dev | on_error | off. Unset = derived from ENVIRONMENT. dev mode only allowed when ENVIRONMENT=dev.")
    trace_slow_ms: int = Field(default=1000, description="on_error mode also emits journeys slower than this (ms).")
    trace_sample_rate: float = Field(default=0.1, description="on_error mode: fraction of requests captured at T2.")
    trace_code_roots: list[str] = Field(
        default=["src/services", "src/models", "src/tasks", "src/scripts"],
        description="Directories whose code the tracer records. Add new top-level src dirs here.",
    )
    auth_providers: list[str] = Field(default=[], description='Auth chain: shipped names ("jwt", "api_key") or dotted BYO class paths.')
    jwt_secret: str = Field(default="", description="JWT HS* shared secret (secret mode).")
    jwt_jwks_url: str = Field(default="", description="JWKS URL (https only). Setting this enables JWKS mode; iss+aud become mandatory.")
    jwt_algorithms: list[str] = Field(default=["HS256"], description="Allowed JWT algorithms (allowlist checked before signature).")
    jwt_issuer: str = Field(default="", description="Expected iss claim (mandatory in JWKS mode).")
    jwt_audience: str = Field(default="", description="Expected aud claim (mandatory in JWKS mode).")
    api_key_cache_ttl: int = Field(default=60, description="API-key cache TTL (s); revoked keys stay valid up to this per process.")
    auth_dev_principal: str = Field(default="", description="Dev only: credential-less requests get this principal id. Boot error outside dev.")
    obs_retention_days: int = Field(default=7, description="records partitions older than this are dropped.")
    dashboard_token: str = Field(default="", description="Required to reach /__obs outside dev (Bearer or ?token=).")
    dataviews_db_url: str = Field(default="", description="Read-only role URL for Observatory data views (sg db grant-readonly). Empty = data views disabled.")
    dashboard_users_sql: str = Field(default="", description="SELECT returning id, email, name, created_at for Observatory's Users view. Empty = principals from records.")

    @field_validator("obs_transports", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: Any) -> Any:
        # `.env.template` ships `OBS_TRANSPORTS=` (blank means "derive it"). Blank must
        # not reach the list parser as "" — copying the template verbatim has to boot.
        return None if v == "" else v

    @model_validator(mode="after")
    def _dev_affordances(self) -> "Settings":
        if self.auth_dev_principal and not self.is_dev:
            raise ValueError("AUTH_DEV_PRINCIPAL is only allowed when ENVIRONMENT=dev")
        return self

    @model_validator(mode="after")
    def _derive_obs(self) -> "Settings":
        if self.obs_transports is None:
            self.obs_transports = ["jsonl"] if self.is_dev else ["stdout"]
        if not self.trace_mode:  # unset or blank (`TRACE_MODE=` in .env) → derive
            self.trace_mode = "dev" if self.is_dev else "on_error"
        elif self.trace_mode == "dev" and not self.is_dev:
            # Dev affordances require environment == dev exactly (01).
            raise ValueError("TRACE_MODE=dev is only allowed when ENVIRONMENT=dev")
        elif self.trace_mode not in ("dev", "on_error", "off"):
            raise ValueError(f"TRACE_MODE must be dev|on_error|off, got {self.trace_mode!r}")
        return self

    @model_validator(mode="after")
    def _timeout_hierarchy(self) -> "Settings":
        # A runaway query must die in the DB before the request deadline, and the
        # request deadline must fit inside the shutdown drain window (01).
        if not (STATEMENT_TIMEOUT_S <= self.request_timeout_s < self.shutdown_grace_s):
            raise ValueError(
                f"timeout hierarchy violated: STATEMENT_TIMEOUT_S ({STATEMENT_TIMEOUT_S}) "
                f"<= REQUEST_TIMEOUT_S ({self.request_timeout_s}) "
                f"< SHUTDOWN_GRACE_S ({self.shutdown_grace_s})"
            )
        return self

    @property
    def is_dev(self) -> bool:
        return self.environment is Environment.dev


def load_settings() -> Settings:
    """Fail boot with every problem listed at once, not one-at-a-time."""
    try:
        # ENVIRONMENT has no default (boot must fail without it) and is populated from the
        # env at runtime — a static checker can only see a missing required argument.
        return Settings()  # pyright: ignore[reportCallIssue]
    except ValidationError as e:
        print("Settings invalid — refusing to start:", file=sys.stderr)
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(model)"
            print(f"  {loc}: {err['msg']}", file=sys.stderr)
        raise SystemExit(1) from None


settings = load_settings()
