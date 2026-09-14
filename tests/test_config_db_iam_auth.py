"""How the service authenticates to the configuration database.

The Cloud SQL socket Cloud Run mounts does the dialling and nothing else. If
the database user is an IAM service account, logging in means presenting an
access token where a password goes -- and the failure when nobody does is a
`password authentication failed` at startup, which reads like a wrong secret
rather than like a missing step.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.db.postgres import (
    IAM_LOGIN_SCOPE,
    AccessTokenPassword,
    iam_database_user,
)


def _settings(dsn: str) -> Any:
    """Settings with the two required fields filled and the DSN under test."""

    from app.config import Settings

    return Settings(
        gcp_project_id="test-project",
        pubsub_job_topic_id="test-jobs-topic",
        config_db_dsn=dsn,
    )


PREP_DSN = (
    "postgresql://agent-middleware-sa%40karoscmo-prep.iam@/karos-prep"
    "?host=/cloudsql/karoscmo-prep:us-central1:karos-config-prep"
)


class FakeCredentials:
    """A stand-in for google.auth credentials that counts its refreshes."""

    def __init__(self, token: str | None = "token-1", valid: bool = False) -> None:
        self.token = token
        self.valid = valid
        self.refreshes = 0
        self.requests: list[Any] = []

    def refresh(self, request: Any) -> None:
        self.refreshes += 1
        self.requests.append(request)
        self.valid = True
        if self.token is not None:
            self.token = f"token-{self.refreshes}"


class TestRecognisingTheAuthMode:
    def test_the_prep_dsn_names_an_iam_user(self) -> None:
        assert iam_database_user(PREP_DSN) == "agent-middleware-sa@karoscmo-prep.iam"

    def test_the_prod_dsn_names_an_iam_user(self) -> None:
        dsn = (
            "postgresql://agent-middleware-sa%40karoscmo.iam@/karos-prod"
            "?host=/cloudsql/karoscmo:europe-west1:karos-config-prod"
        )
        assert iam_database_user(dsn) == "agent-middleware-sa@karoscmo.iam"

    def test_a_password_settles_the_question_even_for_an_iam_looking_user(
        self,
    ) -> None:
        """A DSN carrying a password has already said how it authenticates.

        Overriding that with a token would break the one configuration someone
        reaches for when IAM auth is what is broken.
        """

        dsn = "postgresql://someone%40project.iam:secret@/db?host=/cloudsql/x"
        assert iam_database_user(dsn) is None

    def test_a_built_in_user_is_left_alone(self) -> None:
        assert iam_database_user("postgresql://postgres:pw@localhost/karos") is None

    def test_a_built_in_user_without_a_password_is_still_not_iam(self) -> None:
        assert iam_database_user("postgresql://postgres@localhost/karos") is None

    def test_a_dsn_with_no_user_at_all_is_not_iam(self) -> None:
        assert iam_database_user("postgresql://localhost/karos") is None

    def test_the_login_scope_is_the_narrow_one(self) -> None:
        """cloud-platform would also work, and would hand the database a token
        that can do everything this service account can do."""

        assert IAM_LOGIN_SCOPE == "https://www.googleapis.com/auth/sqlservice.login"


class TestTheTokenPassword:
    async def test_it_returns_the_refreshed_token(self) -> None:
        credentials = FakeCredentials()
        password = AccessTokenPassword(credentials, request=object())

        assert await password() == "token-1"

    async def test_a_valid_token_is_not_refetched(self) -> None:
        """The common case is a pool opening its second connection a
        millisecond after its first."""

        credentials = FakeCredentials(token="already-good", valid=True)
        password = AccessTokenPassword(credentials, request=object())

        assert await password() == "already-good"
        assert await password() == "already-good"
        assert credentials.refreshes == 0

    async def test_an_expired_token_is_refreshed_on_the_next_connection(
        self,
    ) -> None:
        """A pool outlives a one-hour token. asyncpg calls the password for
        every new connection, which is the hook that makes that survivable."""

        credentials = FakeCredentials(token="stale", valid=True)
        password = AccessTokenPassword(credentials, request=object())
        assert await password() == "stale"

        credentials.valid = False
        assert await password() == "token-1"
        assert credentials.refreshes == 1

    async def test_concurrent_connections_refresh_once(self) -> None:
        """Cloud Run cold-starts and the pool opens min_size connections at
        once; a refresh per connection is a thundering herd at the metadata
        server."""

        credentials = FakeCredentials()
        password = AccessTokenPassword(credentials, request=object())

        tokens = await asyncio.gather(*(password() for _ in range(5)))

        assert credentials.refreshes == 1
        assert set(tokens) == {"token-1"}

    async def test_a_refresh_that_produces_no_token_is_an_explicit_error(
        self,
    ) -> None:
        """Otherwise asyncpg sends an empty password and the server answers
        `password authentication failed`, which points at the wrong thing."""

        password = AccessTokenPassword(FakeCredentials(token=None), request=object())

        with pytest.raises(RuntimeError, match="no access token"):
            await password()

    async def test_the_credentials_are_resolved_lazily(self, monkeypatch) -> None:
        """Constructed at startup, used on the first connection. A process
        without default credentials -- every test run, and local development --
        must not blow up merely for building the object."""

        credentials = FakeCredentials()
        calls: list[list[str]] = []

        def fake_default() -> FakeCredentials:
            calls.append([IAM_LOGIN_SCOPE])
            return credentials

        password = AccessTokenPassword(request=object())
        monkeypatch.setattr(AccessTokenPassword, "_default", staticmethod(fake_default))

        assert calls == []
        assert await password() == "token-1"
        assert calls == [[IAM_LOGIN_SCOPE]]


class TestWiringItIntoTheDatabase:
    async def test_an_iam_dsn_gets_a_token_password(self, monkeypatch) -> None:
        from app.db import postgres

        seen: dict[str, Any] = {}

        async def fake_build_pool(dsn: str, **kwargs: Any) -> Any:
            seen["dsn"] = dsn
            seen["password"] = kwargs.get("password")
            raise _Stop

        monkeypatch.setattr(postgres, "build_pool", fake_build_pool)
        settings = _settings(PREP_DSN)

        with pytest.raises(_Stop):
            await postgres.build_config_database(settings)

        assert isinstance(seen["password"], AccessTokenPassword)

    async def test_a_password_dsn_gets_no_token_password(self, monkeypatch) -> None:
        from app.db import postgres

        seen: dict[str, Any] = {}

        async def fake_build_pool(dsn: str, **kwargs: Any) -> Any:
            seen["password"] = kwargs.get("password")
            raise _Stop

        monkeypatch.setattr(postgres, "build_pool", fake_build_pool)
        settings = _settings("postgresql://postgres:pw@localhost/karos")

        with pytest.raises(_Stop):
            await postgres.build_config_database(settings)

        assert seen["password"] is None

    async def test_no_dsn_still_means_no_database(self) -> None:
        """Unchanged by any of this: an environment without Cloud SQL is a
        normal state, not a broken one."""

        from app.db import postgres

        assert await postgres.build_config_database(_settings("")) is None


class TestAnUnreachableDatabaseDoesNotTakeTheServiceDown:
    """build_config_database runs in the lifespan.

    An exception there is a container that never becomes ready, and Cloud Run
    cold-starts constantly -- so a Cloud SQL blip would become a total outage
    of a control plane whose other routes only talk to Firestore, on the next
    cold start, minutes after the blip.
    """

    async def test_a_refused_connection_degrades_instead_of_raising(
        self, monkeypatch, caplog
    ) -> None:
        from app.db import postgres

        async def refuse(dsn: str, **kwargs: Any) -> Any:
            raise ConnectionRefusedError("no /cloudsql socket")

        monkeypatch.setattr(postgres, "build_pool", refuse)

        with caplog.at_level("ERROR"):
            assert await postgres.build_config_database(_settings(PREP_DSN)) is None

        assert "could not connect to the configuration database" in caplog.text

    async def test_the_log_names_the_failure_and_the_three_things_to_check(
        self, monkeypatch, caplog
    ) -> None:
        """A degraded start that does not say why is a 503 nobody can act on."""

        from app.db import postgres

        async def refuse(dsn: str, **kwargs: Any) -> Any:
            raise RuntimeError("password authentication failed")

        monkeypatch.setattr(postgres, "build_pool", refuse)

        with caplog.at_level("ERROR"):
            await postgres.build_config_database(_settings(PREP_DSN))

        assert "RuntimeError" in caplog.text
        assert "password authentication failed" in caplog.text
        assert "cloudsql.instanceUser" in caplog.text

    async def test_a_bad_token_is_not_special_cased(
        self, monkeypatch, caplog
    ) -> None:
        """The token is fetched inside the pool, so it arrives here as one more
        reason the connection did not happen."""

        from app.db import postgres

        async def refuse(dsn: str, **kwargs: Any) -> Any:
            raise RuntimeError("no access token for the configuration database")

        monkeypatch.setattr(postgres, "build_pool", refuse)

        with caplog.at_level("ERROR"):
            assert await postgres.build_config_database(_settings(PREP_DSN)) is None


class _Stop(BaseException):
    """Cuts the call short once the arguments have been observed.

    A BaseException on purpose: build_config_database catches Exception so that
    an unreachable database degrades instead of failing the lifespan, and a
    test escape hatch that the code under test swallows is not an escape hatch.
    """
