"""Application configuration, loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.roles import Role


class Settings(BaseSettings):
    """Strongly-typed application settings.

    All values can be overridden via environment variables (or a local .env
    file). Google Cloud credentials are resolved by the underlying client
    libraries via the standard ``GOOGLE_APPLICATION_CREDENTIALS`` mechanism.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---
    app_name: str = "agent-middleware"
    environment: str = Field(default="local", description="local | staging | production")
    log_level: str = Field(default="INFO")

    # --- Google Cloud / Pub/Sub ---
    gcp_project_id: str = Field(..., description="GCP project id used for all Pub/Sub resources")

    pubsub_emulator_host: str | None = Field(
        default=None,
        description="Set to host:port to talk to a local Pub/Sub emulator instead of GCP",
    )

    publish_timeout_seconds: float = Field(default=10.0, gt=0)

    # --- Job dispatch (control plane -> agent-engine) ---
    pubsub_job_topic_id: str = Field(
        ...,
        description=(
            "The one topic this service publishes to: agent-engine's run-jobs topic "
            "(e.g. karos-agent-runs-prep). Dispatch through POST /agents/{id}/jobs is "
            "the only thing that publishes, so there is exactly one destination."
        ),
    )

    # --- Service-to-service authentication ---
    auth_enabled: bool = Field(
        default=True,
        description=(
            "Verify the caller's identity on every non-health route. Leave on "
            "everywhere except local development."
        ),
    )
    auth_audience: str | None = Field(
        default=None,
        description=(
            "Expected 'aud' claim of an inbound OIDC token — this service's own Cloud "
            "Run URL. Required when auth_enabled and no dev token is configured."
        ),
    )
    auth_allowed_service_accounts: list[str] = Field(
        default_factory=list,
        description=(
            "Caller service-account emails allowed through. Empty means any identity "
            "Google will vouch for, which is only safe behind Cloud Run IAM."
        ),
    )
    auth_dev_token: str | None = Field(
        default=None,
        description=(
            "Static bearer token accepted INSTEAD of an OIDC token — for local dev and "
            "integration tests. Refused outright when environment=production."
        ),
    )
    auth_role_bindings: dict[str, Role] = Field(
        default_factory=dict,
        description=(
            "Caller principal -> role, as a JSON object keyed by service-account email "
            'e.g. {"portal@karoscmo.iam.gserviceaccount.com": "editor"}. A principal '
            "absent from this map falls back to auth_default_role."
        ),
    )
    # --- The configuration plane (Postgres) -------------------------------
    config_db_dsn: str | None = Field(
        default=None,
        description=(
            "asyncpg DSN for the configuration schema. Unset means the Configuration "
            "API reports itself unavailable and every other route is unaffected -- "
            "Cloud SQL does not exist in every environment yet, and a control plane "
            "that refused to start without it would make the Postgres migration a "
            "flag day for routes that have nothing to do with it. For Cloud SQL over "
            "a unix socket: postgresql://USER@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE"
        ),
    )
    config_db_pool_min_size: int = Field(
        default=1,
        ge=0,
        description=(
            "Minimum pooled connections. 1 rather than 0 so the first request after "
            "a cold start does not pay the connection handshake."
        ),
    )
    config_db_pool_max_size: int = Field(
        default=5,
        ge=1,
        description=(
            "Maximum pooled connections PER INSTANCE. Cloud Run multiplies this by "
            "the instance count against Cloud SQL's own connection limit, which is "
            "how a scale-out event becomes 'too many connections' rather than a "
            "capacity increase."
        ),
    )
    config_db_idle_lifetime_seconds: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Recycle a connection idle for longer than this. Below Cloud SQL's own "
            "idle timeout on purpose: a connection the server has already dropped is "
            "a first-request 500 that looks like a code fault."
        ),
    )
    config_db_command_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Per-statement timeout. A publish validates a whole version and is the "
            "longest statement here; anything past this is a lock someone else holds."
        ),
    )
    auth_default_role: Role = Field(
        default=Role.VIEWER,
        description=(
            "Role for an authenticated caller with no entry in auth_role_bindings, "
            "once at least one binding exists. Defaults to the least authority, so a "
            "caller nobody bound can read and not write."
        ),
    )
    model_pricing_enforced: bool = Field(
        default=False,
        description=(
            "Refuse to dispatch a run whose agent names a model the catalog cannot "
            "price. Defaults to FALSE because turning it on before "
            "scripts/seed_model_catalog.py has run against the environment turns "
            "every dispatch into a 422, and the way that gets noticed is a client "
            "asking why nothing ran. Check GET /models/pricing-coverage, seed, then "
            "set this to true. While it is false an unpriceable model is logged at "
            "WARNING with the agent and stage that name it, so the gap is visible "
            "rather than silent."
        ),
    )

    # --- GCS (binary template assets) ---
    gcs_artifacts_bucket: str | None = Field(
        default=None,
        description=(
            "Bucket holding binary template assets (images, fonts). Firestore stores "
            "the gs:// URIs; the bytes live here."
        ),
    )

    # --- Firestore (control plane store) ---
    firestore_project_id: str | None = Field(
        default=None,
        description="Firebase/Firestore project. Falls back to GCP_PROJECT_ID when unset.",
    )
    firestore_database: str = Field(
        default="(default)", description="Named Firestore database within the project"
    )
    firestore_collection_prefix: str = Field(
        default="",
        description=(
            "Prefix for every root collection, e.g. 'dev_' - lets several "
            "environments share one Firestore database"
        ),
    )
    firestore_emulator_host: str | None = Field(
        default=None,
        description="host:port of the Firebase emulator; bypasses credentials entirely",
    )

    # --- Context assembly defaults ---
    default_context_example_limit: int = Field(
        default=10, ge=0, le=200, description="Few-shot examples injected into a job payload"
    )

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def resolved_firestore_project_id(self) -> str:
        """Project the Firestore client should target."""

        return self.firestore_project_id or self.gcp_project_id

    @property
    def job_topic_id(self) -> str:
        """Topic agent job payloads are published to."""

        return self.pubsub_job_topic_id

    def role_for(self, principal: str | None) -> Role:
        """The role a verified principal holds.

        ``principal`` is the caller's service-account email, or its subject when
        Google issued a token without one.

        **An empty ``auth_role_bindings`` means authorization is not configured,
        and every verified caller gets ``admin``** -- which is exactly the
        behaviour this service had before roles existed. That is not a weak
        default, it is the difference between shipping a role model and taking
        production down with one: ``AUTH_ENABLED`` is hardcoded ``true`` in
        ``cloudbuild.yaml`` for BOTH environments, so the alternative -- an
        unbound caller falling to ``viewer`` -- would 403 every write the portal
        makes on the very next deploy. Startup logs an error while this is the
        case, so it is loud rather than quiet.

        Bind ONE principal and the model becomes real in the same instant:
        anyone unbound then falls to ``auth_default_role`` (``viewer``), so a
        forgotten binding is a refused write with the principal named in the
        message. Migration is therefore additive and reversible, and no flag
        day is needed.
        """

        if not self.auth_role_bindings:
            return Role.ADMIN
        if principal and principal in self.auth_role_bindings:
            return self.auth_role_bindings[principal]
        return self.auth_default_role

    @property
    def role_bindings_missing(self) -> bool:
        """Auth is on and nothing is bound -- every caller is on the default."""

        return self.auth_enabled and not self.auth_role_bindings

    @property
    def dev_token_permitted(self) -> bool:
        """Whether the static dev token may be honoured.

        Never in production, regardless of what is configured: a shared secret in
        an environment variable is a development affordance, and the failure mode
        of leaving one set on a production deploy is silent, total auth bypass.
        """

        return self.auth_dev_token is not None and not self.is_production


@lru_cache
def get_settings() -> Settings:
    """Return a cached, process-wide ``Settings`` instance."""

    return Settings()  # type: ignore[call-arg]
