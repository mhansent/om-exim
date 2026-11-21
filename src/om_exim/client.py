"""OpenMetadata read-only REST transport and version preflight."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic, sleep
from typing import Any
from urllib.parse import quote

import requests

from .registry import REGISTRY
from .settings import Connection, HttpSettings

BASELINE_SDK = "2.0.0.0"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 30

Document = dict[str, Any]


class ReadError(ValueError):
    pass


class RequestPacer:
    def __init__(self, settings: HttpSettings):
        self.settings = settings
        self._interval = (
            1 / settings.requests_per_second if settings.requests_per_second else 0
        )
        self._next_request = 0.0

    def wait(self) -> None:
        if self._interval:
            delay = self._next_request - monotonic()
            if delay > 0:
                sleep(delay)
            self._next_request = monotonic() + self._interval


def _retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        if value.isascii() and value.isdigit():
            return float(value)
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0, (when - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0


class ReadClient:
    def __init__(
        self,
        connection: Connection,
        label: str,
        session: requests.Session,
        *,
        pacer: RequestPacer | None = None,
    ):
        self.connection = connection
        self.label = label
        self.session = session
        self.pacer = pacer or RequestPacer(HttpSettings())

    def _authenticate(
        self, request: requests.PreparedRequest
    ) -> requests.PreparedRequest:
        request.headers["Authorization"] = f"Bearer {self.connection.token}"
        return request

    def _get(
        self,
        route: str,
        params: dict | None = None,
        *,
        unavailable: tuple[int, ...] = (),
    ) -> Document | None:
        response = self._get_response(route, params, unavailable=unavailable)
        return self._decode(response) if response is not None else None

    def _url(self, route: str) -> str:
        base = self.connection.server_url.removesuffix("/v1")
        return f"{base}/v1/{route}"

    def _get_response(
        self, route: str, params: dict | None, *, unavailable: tuple[int, ...]
    ) -> requests.Response | None:
        response = self._read_with_retry(route, params)
        if response.status_code in unavailable:
            return None
        if response.status_code != 200:
            guidance = ""
            if (
                response.status_code in RETRY_STATUSES
                and _retry_after(response.headers.get("Retry-After")) > MAX_RETRY_AFTER
            ):
                guidance = " The server requested a longer cooldown. Retry after it has elapsed."
            raise ReadError(
                f"{self.label}: GET {route.split('/')[0]} returned HTTP {response.status_code}.{guidance}"
            )
        return response

    def _read_with_retry(self, route: str, params: dict | None) -> requests.Response:
        settings = self.pacer.settings
        for attempt in range(settings.read_retries + 1):
            self.pacer.wait()
            backoff = min(0.5 * 2**attempt, 8)
            try:
                response = self.session.get(
                    self._url(route),
                    params=params,
                    timeout=settings.timeout_seconds,
                    allow_redirects=False,
                    auth=self._authenticate,
                )
            except requests.RequestException as error:
                retryable = isinstance(
                    error, requests.Timeout | requests.ConnectionError
                ) and not isinstance(error, requests.exceptions.SSLError)
                if not retryable or attempt == settings.read_retries:
                    raise ReadError(
                        f"{self.label}: GET {route.split('/')[0]} failed."
                    ) from None
                sleep(backoff)
                continue
            if (
                response.status_code not in RETRY_STATUSES
                or attempt == settings.read_retries
            ):
                return response
            retry_after = _retry_after(response.headers.get("Retry-After"))
            if retry_after > MAX_RETRY_AFTER:
                return response
            response.close()
            sleep(max(backoff, retry_after))
        raise AssertionError("Read retry loop exhausted without a response.")

    def _decode(self, response: requests.Response) -> Document:
        try:
            payload = response.json()
        except ValueError:
            raise ReadError(f"{self.label}: API response is not valid JSON.") from None
        if not isinstance(payload, dict):
            raise ReadError(f"{self.label}: API response must be an object.")
        return payload

    def version(self) -> str:
        payload = self._get("system/version")
        version = payload.get("version") if payload is not None else None
        if not isinstance(version, str) or not version.strip():
            raise ReadError(f"{self.label}: server version could not be verified.")
        return version

    @staticmethod
    def _page(payload: Document | None) -> tuple[list[Document], Document]:
        if payload is None:
            raise ReadError("The API did not return a page.")
        data, paging = payload.get("data"), payload.get("paging")
        if (
            not isinstance(data, list)
            or any(not isinstance(row, dict) for row in data)
            or not isinstance(paging, dict)
        ):
            raise ReadError("The API returned an invalid entity page.")
        return data, paging

    def list_entities(
        self,
        kind: str,
        limit: int,
        *,
        project_fields: bool = False,
        optional: bool = False,
        max_pages: int | None = None,
        domain: str | None = None,
    ) -> Iterator[Document]:
        spec = REGISTRY[kind]
        params: dict[str, str | int] = {"limit": limit}
        if domain is not None:
            params["domain"] = domain
        if project_fields and spec.fields:
            params["fields"] = ",".join(spec.fields)
        cursors: set[str] = set()
        ids: set[str] = set()
        pages = 0
        while True:
            payload = self._get(
                spec.route, params, unavailable=(403, 404) if optional else ()
            )
            if payload is None:
                return
            data, paging = self._page(payload)
            for row in data:
                entity_id = row.get("id")
                if not isinstance(entity_id, str) or not entity_id or entity_id in ids:
                    raise ReadError(
                        f"The {kind} listing contains a missing or duplicate ID."
                    )
                ids.add(entity_id)
                yield row
            after = paging.get("after")
            if after is None:
                return
            if not isinstance(after, str) or not after or after in cursors or not data:
                raise ReadError(
                    f"The {kind} listing returned an invalid pagination cursor."
                )
            cursors.add(after)
            params["after"] = after
            pages += 1
            if max_pages is not None and pages >= max_pages:
                return

    def detail(
        self,
        kind: str,
        value: str,
        *,
        by_name: bool = False,
        fields: tuple[str, ...] | None = None,
    ) -> Document | None:
        route, params = self._detail_request(
            kind, value, by_name=by_name, fields=fields
        )
        return self._get(route, params, unavailable=(404,))

    @staticmethod
    def _detail_request(
        kind: str, value: str, *, by_name: bool, fields: tuple[str, ...] | None
    ) -> tuple[str, dict[str, str] | None]:
        spec = REGISTRY[kind]
        if value in {".", ".."}:
            raise ReadError("The API returned an invalid reference identifier.")
        path = ("name/" if by_name else "") + quote(value, safe="")
        projection = spec.fields if fields is None else fields
        params = {"fields": ",".join(projection)} if projection else None
        return f"{spec.route}/{path}", params

    def product_members(self, entity_id: str, limit: int) -> Iterator[Document]:
        return self._asset_references("dataProducts", entity_id, limit)

    def domain_assets(self, entity_id: str, limit: int) -> Iterator[Document]:
        return self._asset_references("domains", entity_id, limit)

    def _asset_references(
        self, collection: str, entity_id: str, limit: int
    ) -> Iterator[Document]:
        if entity_id in {".", ".."}:
            raise ReadError("The API returned an invalid reference identifier.")
        route = f"{collection}/{quote(entity_id, safe='')}/assets"
        offset = 0
        while True:
            data, paging = self._page(
                self._get(route, {"limit": limit, "offset": offset})
            )
            total = paging.get("total")
            if type(total) is not int or total < offset + len(data):
                raise ReadError(f"The {collection} asset page has an invalid total.")
            yield from data
            offset += len(data)
            if offset == total:
                return
            if not data:
                raise ReadError(f"The {collection} asset listing ended early.")


def preflight(source: ReadClient, target: ReadClient) -> str:
    source_version, target_version = source.version(), target.version()
    if source_version != target_version:
        raise ReadError("Source and target must report the same exact server version.")
    if re.fullmatch(r"2\.0\.(?:0|[1-9][0-9]*)", source_version) is None:
        raise ReadError(
            "Preflight requires a stable OpenMetadata 2.0.x server version."
        )
    return source_version
