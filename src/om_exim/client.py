"""OpenMetadata read-only REST transport and version preflight."""

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import requests

from .registry import REGISTRY
from .settings import Connection

BASELINE_SDK = "2.0.0.0"

Document = dict[str, Any]


class ReadError(ValueError):
    pass


class ReadClient:
    def __init__(self, connection: Connection, label: str, session: requests.Session):
        self.connection = connection
        self.label = label
        self.session = session

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
        base = self.connection.server_url
        if not base.endswith("/v1"):
            base += "/v1"
        try:
            response = self.session.get(
                f"{base}/{route}",
                params=params,
                timeout=30,
                allow_redirects=False,
                auth=self._authenticate,
            )
        except requests.RequestException:
            raise ReadError(
                f"{self.label}: GET {route.split('/')[0]} failed."
            ) from None
        if response.status_code in unavailable:
            return None
        if response.status_code != 200:
            raise ReadError(
                f"{self.label}: GET {route.split('/')[0]} returned HTTP {response.status_code}."
            )
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
        extra_fields: tuple[str, ...] = (),
        optional: bool = False,
        max_pages: int | None = None,
    ) -> Iterator[Document]:
        spec = REGISTRY[kind]
        params: dict[str, str | int] = {"limit": limit}
        fields = (spec.fields if project_fields else ()) + extra_fields
        if fields:
            params["fields"] = ",".join(fields)
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
        extra_fields: tuple[str, ...] = (),
    ) -> Document | None:
        spec = REGISTRY[kind]
        if value in {".", ".."}:
            raise ReadError("The API returned an invalid reference identifier.")
        path = ("name/" if by_name else "") + quote(value, safe="")
        projection = spec.fields + extra_fields
        fields = {"fields": ",".join(projection)} if projection else None
        return self._get(f"{spec.route}/{path}", fields, unavailable=(404,))

    def product_members(self, entity_id: str, limit: int) -> Iterator[Document]:
        route = f"dataProducts/{quote(entity_id, safe='')}/assets"
        offset = 0
        while True:
            data, paging = self._page(
                self._get(route, {"limit": limit, "offset": offset})
            )
            total = paging.get("total")
            if type(total) is not int or total < offset + len(data):
                raise ReadError(
                    "The data product membership page has an invalid total."
                )
            yield from data
            offset += len(data)
            if offset == total:
                return
            if not data:
                raise ReadError("The data product membership listing ended early.")


def preflight(source: ReadClient, target: ReadClient) -> str:
    source_version, target_version = source.version(), target.version()
    if source_version != target_version:
        raise ReadError("Source and target must report the same exact server version.")
    if re.fullmatch(r"2\.0\.(?:0|[1-9][0-9]*)", source_version) is None:
        raise ReadError(
            "Preflight requires a stable OpenMetadata 2.0.x server version."
        )
    return source_version
