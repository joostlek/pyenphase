"""Envoy authentication methods."""

import ssl
from abc import abstractmethod, abstractproperty
from typing import Any, cast

import httpx
import jwt
import orjson
from tenacity import retry, retry_if_exception_type, wait_random_exponential

from .exceptions import EnvoyAuthenticationError


def create_default_ssl_context() -> ssl.SSLContext:
    """Return an default SSL context."""
    return ssl.create_default_context()


_SSL_CONTEXT = create_default_ssl_context()


class EnvoyAuth:
    """Base class for Envoy authentication."""

    def __init__(self, host: str) -> None:
        """Initialize the EnvoyAuth class."""
        pass

    @abstractmethod
    async def setup(self, client: httpx.AsyncClient) -> None:
        """Obtain the token for Envoy authentication."""

    @abstractproperty
    def cookies(self) -> dict[str, str]:
        """Return the Envoy cookie."""

    @abstractproperty
    def auth(self) -> httpx.DigestAuth | None:
        """Return the httpx auth object."""

    @abstractproperty
    def headers(self) -> dict[str, str]:
        """Return the auth headers."""

    @abstractmethod
    def get_endpoint_url(self, endpoint: str) -> str:
        """Return the URL for the endpoint."""


class EnvoyTokenAuth(EnvoyAuth):
    JSON_LOGIN_URL = "https://enlighten.enphaseenergy.com/login/login.json?"
    TOKEN_URL = "https://entrez.enphaseenergy.com/tokens"  # nosec

    def __init__(
        self,
        host: str,
        cloud_username: str | None = None,
        cloud_password: str | None = None,
        envoy_serial: str | None = None,
        token: str | None = None,
    ) -> None:
        self.host = host
        self.cloud_username = cloud_username
        self.cloud_password = cloud_password
        self.envoy_serial = envoy_serial
        self._token = token

    async def setup(self, client: httpx.AsyncClient) -> None:
        """Obtain the token for Envoy authentication."""
        if not self._token:
            self._token = await self._obtain_token()

        # Verify we have adequate credentials
        if not self._token:
            raise EnvoyAuthenticationError(
                "Unable to obtain token for Envoy authentication."
            )

        # Verify the token and obtain cookie with session ID necessary for some API calls
        req = await client.get(
            f"https://{self.host}/auth/check_jwt",
            headers={"Authorization": f"Bearer {self.token}"},
        )

        if req.status_code != 200:
            raise EnvoyAuthenticationError(
                "Unable to verify token for Envoy authentication."
            )

        self._cookies = req.cookies

    async def _obtain_token(self) -> None:
        """Obtain the token for Envoy authentication."""
        # Raise if we don't have cloud credentials
        if not self.cloud_username or not self.cloud_password:
            raise EnvoyAuthenticationError(
                "Your firmware requires token authentication, "
                " but no cloud credentials were provided to obtain the token."
            )
        # Raise if we are missing the envoy serial number
        if not self.envoy_serial:
            raise EnvoyAuthenticationError(
                "Your firmware requires token authentication, "
                "but no envoy serial number was provided to obtain the token."
            )
        # We require a new client that checks SSL certs
        async with httpx.AsyncClient(
            verify=_SSL_CONTEXT, timeout=10, follow_redirects=True
        ) as cloud_client:
            # Login to Enlighten to obtain a session ID
            response = await self._post_json_with_cloud_client(
                cloud_client,
                self.JSON_LOGIN_URL,
                data={
                    "user[email]": self.cloud_username,
                    "user[password]": self.cloud_password,
                },
            )
            if response.status_code != 200:
                raise EnvoyAuthenticationError(
                    "Unable to login to Enlighten to obtain session ID from "
                    f"{self.JSON_LOGIN_URL}: "
                    f"{response.status_code}: {response.text}"
                )
            try:
                response = orjson.loads(response.text)
            except orjson.JSONDecodeError as err:
                raise EnvoyAuthenticationError(
                    "Unable to decode response from Enlighten: "
                    f"{response.status_code}: {response.text}"
                ) from err

            self._is_consumer = response["is_consumer"]
            self._manager_token = response["manager_token"]

            # Obtain the token
            response = await self._post_json_with_cloud_client(
                cloud_client,
                self.TOKEN_URL,
                json={
                    "session_id": response["session_id"],
                    "serial_num": self.envoy_serial,
                    "username": self.cloud_username,
                },
            )
            if response.status_code != 200:
                raise EnvoyAuthenticationError(
                    "Unable to obtain token for Envoy authentication from "
                    f"{self.TOKEN_URL}: "
                    f"{response.status_code}: {response.text}"
                )
            return response.text

    async def refresh(self) -> None:
        """Refresh the token for Envoy authentication."""
        self._token = await self._obtain_token()

    @property
    def expire_timestamp(self) -> int:
        """Return the remaining seconds for the token."""
        jwt_payload = jwt.decode(self.token, options={"verify_signature": False})
        return cast(int, jwt_payload["exp"])

    @retry(
        retry=retry_if_exception_type(
            (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)
        ),
        wait=wait_random_exponential(multiplier=2, max=3),
    )
    async def _post_json_with_cloud_client(
        self,
        cloud_client: httpx.AsyncClient,
        url: str,
        data: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Post to the Envoy API with the cloud client."""
        return await cloud_client.post(url, json=json, data=data)

    @property
    def token(self) -> str:
        assert self._token is not None  # nosec
        return self._token

    @property
    def manager_token(self) -> str:
        assert self._manager_token is not None  # nosec
        return self._manager_token

    @property
    def cookies(self) -> dict[str, str]:
        return self._cookies

    @property
    def is_consumer(self) -> bool:
        return self._is_consumer

    @property
    def auth(self) -> None:
        """No auth required for token authentication."""
        return None

    @property
    def headers(self) -> dict[str, str]:
        """Return the headers for Envoy authentication."""
        return {"Authorization": f"Bearer {self.token}"}

    def get_endpoint_url(self, endpoint: str) -> str:
        """Return the URL for the endpoint."""
        return f"https://{self.host}{endpoint}"


class EnvoyLegacyAuth(EnvoyAuth):
    """Class for legacy Envoy authentication."""

    def __init__(self, host: str, username: str, password: str) -> None:
        self.host = host
        self.local_username = username
        self.local_password = password

    @property
    def auth(self) -> httpx.DigestAuth:
        """Digest authentication for local Envoy."""
        if not self.local_username or not self.local_password:
            return None
        return httpx.DigestAuth(self.local_username, self.local_password)

    async def setup(self, client: httpx.AsyncClient) -> None:
        """Setup auth"""
        # No setup required for legacy authentication

    @property
    def headers(self) -> dict[str, str]:
        """Return the headers for legacy Envoy authentication."""
        return {}

    def get_endpoint_url(self, endpoint: str) -> str:
        """Return the URL for the endpoint."""
        return f"http://{self.host}{endpoint}"

    @property
    def cookies(self) -> dict[str, str]:
        return {}
