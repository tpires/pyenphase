import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import partial
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx
import orjson
from awesomeversion import AwesomeVersion
from envoy_utils.envoy_utils import EnvoyUtils
from tenacity import retry, retry_if_exception_type, wait_random_exponential

from pyenphase.models.dry_contacts import DryContactStatus

from .auth import EnvoyAuth, EnvoyLegacyAuth, EnvoyTokenAuth
from .const import (
    AUTH_TOKEN_MIN_VERSION,
    LOCAL_TIMEOUT,
    URL_DRY_CONTACT_SETTINGS,
    URL_DRY_CONTACT_STATUS,
    URL_GRID_RELAY,
    SupportedFeatures,
)
from .exceptions import (
    EnvoyAuthenticationRequired,
    EnvoyFeatureNotAvailable,
    EnvoyProbeFailed,
)
from .firmware import EnvoyFirmware
from .json import json_loads
from .models.envoy import EnvoyData
from .ssl import NO_VERIFY_SSL_CONTEXT
from .updaters.api_v1_production import EnvoyApiV1ProductionUpdater
from .updaters.api_v1_production_inverters import EnvoyApiV1ProductionInvertersUpdater
from .updaters.base import EnvoyUpdater
from .updaters.ensemble import EnvoyEnembleUpdater
from .updaters.production import (
    EnvoyProductionJsonFallbackUpdater,
    EnvoyProductionJsonUpdater,
    EnvoyProductionUpdater,
)

_LOGGER = logging.getLogger(__name__)


DEFAULT_HEADERS = {
    "Accept": "application/json",
}

UPDATERS: list[type["EnvoyUpdater"]] = [
    EnvoyProductionUpdater,
    EnvoyProductionJsonUpdater,
    EnvoyApiV1ProductionUpdater,
    EnvoyProductionJsonFallbackUpdater,
    EnvoyApiV1ProductionInvertersUpdater,
    EnvoyEnembleUpdater,
]


def register_updater(updater: type[EnvoyUpdater]) -> Callable[[], None]:
    """Register an updater."""
    UPDATERS.append(updater)

    def _remove_updater() -> None:
        UPDATERS.remove(updater)

    return _remove_updater


def get_updaters() -> list[type[EnvoyUpdater]]:
    return UPDATERS


class Envoy:
    """Class for communicating with an envoy."""

    def __init__(
        self,
        host: str,
        client: httpx.AsyncClient | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> None:
        """Initialize the Envoy class."""
        # We use our own httpx client session so we can disable SSL verification (Envoys use self-signed SSL certs)
        self._timeout = timeout or LOCAL_TIMEOUT
        self._client = client or httpx.AsyncClient(
            verify=NO_VERIFY_SSL_CONTEXT
        )  # nosec
        self.auth: EnvoyAuth | None = None
        self._host = host
        self._firmware = EnvoyFirmware(self._client, self._host)
        self._supported_features: SupportedFeatures | None = None
        self._updaters: list[EnvoyUpdater] = []
        self._endpoint_cache: dict[str, httpx.Response] = {}
        self.data: EnvoyData | None = None

    async def setup(self) -> None:
        """Obtain the firmware version for later Envoy authentication."""
        await self._firmware.setup()

    async def authenticate(
        self,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
    ) -> None:
        """Authenticate to the Envoy based on firmware version."""
        if self._firmware.version < AUTH_TOKEN_MIN_VERSION:
            # Envoy firmware using old envoy/installer authentication
            _LOGGER.debug(
                "Authenticating to Envoy using envoy/installer authentication"
            )
            full_serial = self._firmware.serial
            if not username or username == "installer":
                username = "installer"
                password = EnvoyUtils.get_password(full_serial, username)
            elif username == "envoy" and not password:
                # The default password for the envoy user is the last 6 digits of the serial number
                assert full_serial is not None, "Serial must be set"  # nosec
                password = full_serial[-6:]

            if username and password:
                self.auth = EnvoyLegacyAuth(self.host, username, password)

        else:
            # Envoy firmware using new token authentication
            _LOGGER.debug("Authenticating to Envoy using token authentication")
            if token or (username and password):
                # Always pass all the data to the token auth class, even if some of it is None
                # so that we can refresh the token if needed
                self.auth = EnvoyTokenAuth(
                    self.host,
                    cloud_username=username,
                    cloud_password=password,
                    envoy_serial=self._firmware.serial,
                    token=token,
                )

        if not self.auth:
            _LOGGER.error(
                "You must include username/password or token to authenticate to the Envoy."
            )
            raise EnvoyAuthenticationRequired("Could not setup authentication object.")

        await self.auth.setup(self._client)

    @retry(
        retry=retry_if_exception_type(
            (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)
        ),
        wait=wait_random_exponential(multiplier=2, max=3),
    )
    async def probe_request(self, endpoint: str) -> httpx.Response:
        """Make a probe request.

        Probe requests are not retried on bad JSON responses.
        """
        return await self._request(endpoint)

    @retry(
        retry=retry_if_exception_type(
            (
                httpx.NetworkError,
                httpx.TimeoutException,
                httpx.RemoteProtocolError,
                orjson.JSONDecodeError,
            )
        ),
        wait=wait_random_exponential(multiplier=2, max=3),
    )
    async def request(
        self, endpoint: str, data: dict[str, Any] | None = None
    ) -> httpx.Response:
        """Make a request to the Envoy.

        Request retries on bad JSON responses which the Envoy sometimes returns.
        """
        return await self._request(endpoint, data)

    async def _request(
        self, endpoint: str, data: dict[str, Any] | None = None
    ) -> httpx.Response:
        """Make a request to the Envoy."""
        if self.auth is None:
            raise EnvoyAuthenticationRequired(
                "You must authenticate to the Envoy before making requests."
            )

        url = self.auth.get_endpoint_url(endpoint)

        if data:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Sending POST to %s with data %s", url, orjson.dumps(data)
                )
            response = await self._client.post(
                url,
                headers={**DEFAULT_HEADERS, **self.auth.headers},
                cookies=self.auth.cookies,
                follow_redirects=True,
                auth=self.auth.auth,
                timeout=self._timeout,
                data=orjson.dumps(data),
            )
        else:
            _LOGGER.debug("Requesting %s with timeout %s", url, self._timeout)
            response = await self._client.get(
                url,
                headers={**DEFAULT_HEADERS, **self.auth.headers},
                cookies=self.auth.cookies,
                follow_redirects=True,
                auth=self.auth.auth,
                timeout=self._timeout,
            )

        status_code = response.status_code
        if status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise EnvoyAuthenticationRequired(
                f"Authentication failed for {url} with status {status_code}, "
                "please check your username/password or token."
            )

        return response

    @property
    def host(self) -> str:
        """Return the Envoy host."""
        return self._host

    @property
    def firmware(self) -> AwesomeVersion:
        """Return the Envoy firmware version."""
        return self._firmware.version

    @property
    def part_number(self) -> str | None:
        """Return the Envoy part number."""
        return self._firmware.part_number

    @property
    def serial_number(self) -> str | None:
        """Return the Envoy serial number."""
        return self._firmware.serial

    @property
    def supported_features(self) -> SupportedFeatures:
        """Return the supported features."""
        assert self._supported_features is not None, "Call setup() first"  # nosec
        return self._supported_features

    async def _make_cached_request(
        self, request_func: Callable[[str], Awaitable[httpx.Response]], endpoint: str
    ) -> httpx.Response:
        """Make a cached request."""
        if cached_response := self._endpoint_cache.get(endpoint):
            return cached_response
        response = await request_func(endpoint)
        if response.status_code == 200:
            self._endpoint_cache[endpoint] = response
        return response

    async def probe(self) -> None:
        """Probe for model and supported features."""
        supported_features = SupportedFeatures(0)
        updaters: list[EnvoyUpdater] = []
        version = self._firmware.version
        self._endpoint_cache.clear()
        cached_probe = partial(self._make_cached_request, self.probe_request)
        cached_request = partial(self._make_cached_request, self.request)

        for updater in get_updaters():
            klass = updater(version, cached_probe, cached_request)
            if updater_features := await klass.probe(supported_features):
                supported_features |= updater_features
                updaters.append(klass)

        if not supported_features & SupportedFeatures.PRODUCTION:
            raise EnvoyProbeFailed("Unable to determine production endpoint")

        self._updaters = updaters
        self._supported_features = supported_features

    async def update(self) -> EnvoyData:
        """Update data."""
        # Some of the updaters user the same endpoint
        # so we cache the 200 responses for each update
        # cycle to not make duplicate requests
        self._endpoint_cache.clear()
        if not self._supported_features:
            await self.probe()

        data = EnvoyData()
        for updater in self._updaters:
            await updater.update(data)

        self.data = data
        return data

    async def _json_request(self, end_point: str, data: dict[str, Any] | None) -> Any:
        """Make a request to the Envoy and return the JSON response."""
        response = await self._request(end_point, data)
        return json_loads(end_point, response.content)

    async def go_on_grid(self) -> dict[str, Any]:
        """Make a request to the Envoy to go on grid."""
        if not self.supported_features & SupportedFeatures.ENPOWER:
            raise EnvoyFeatureNotAvailable(
                "This feature is not available on this Envoy."
            )
        return await self._json_request(URL_GRID_RELAY, {"mains_admin_state": "closed"})

    async def go_off_grid(self) -> dict[str, Any]:
        """Make a request to the Envoy to go off grid."""
        if not self.supported_features & SupportedFeatures.ENPOWER:
            raise EnvoyFeatureNotAvailable(
                "This feature is not available on this Envoy."
            )
        return await self._json_request(URL_GRID_RELAY, {"mains_admin_state": "open"})

    async def update_dry_contact(self, new_data: dict[str, Any]) -> dict[str, Any]:
        """Update settings for an Enpower dry contact relay."""
        # All settings for the relay must be sent in the POST or it may crash the Envoy

        if not self.supported_features & SupportedFeatures.ENPOWER:
            raise EnvoyFeatureNotAvailable(
                "This feature is not available on this Envoy."
            )

        if not (id_ := new_data.get("id")):
            raise ValueError("You must specify the dry contact ID in the data object.")
        # Get the current settings for the relay from EnvoyData and merge with the new settings
        if not (current_data := self.data):
            raise ValueError(
                "Tried to set dry contact settings before the Envoy was queried."
            )
        current_model = current_data.dry_contact_settings[id_]
        new_model = replace(current_model, **new_data)

        return await self._json_request(
            URL_DRY_CONTACT_SETTINGS, {"dry_contacts": new_model.to_api()}
        )

    async def open_dry_contact(self, id: str) -> dict[str, Any]:
        """Open a dry contact relay."""
        if not self.supported_features & SupportedFeatures.ENPOWER:
            raise EnvoyFeatureNotAvailable(
                "This feature is not available on this Envoy."
            )

        result = await self._json_request(
            URL_DRY_CONTACT_STATUS, {"dry_contacts": {"id": id, "status": "open"}}
        )
        # The Envoy takes a few seconds before it will reflect the new state of the relay
        # so we preemptively update it
        if TYPE_CHECKING:
            assert self.data is not None  # nosec
        self.data.dry_contact_status[id].status = DryContactStatus.OPEN
        return result

    async def close_dry_contact(self, id: str) -> dict[str, Any]:
        """Open a dry contact relay."""
        if not self.supported_features & SupportedFeatures.ENPOWER:
            raise EnvoyFeatureNotAvailable(
                "This feature is not available on this Envoy."
            )

        result = await self._json_request(
            URL_DRY_CONTACT_STATUS, {"dry_contacts": {"id": id, "status": "closed"}}
        )
        # The Envoy takes a few seconds before it will reflect the new state of the relay
        # so we preemptively update it
        if TYPE_CHECKING:
            assert self.data is not None  # nosec
        self.data.dry_contact_status[id].status = DryContactStatus.CLOSED
        return result
