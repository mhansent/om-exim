"""Explicit write operations for the import apply path."""

from math import isfinite
from urllib.parse import quote

import requests

from .client import Document, ReadClient, ReadError
from .registry import REGISTRY


class TargetClient(ReadClient):
    def _write(
        self,
        method: str,
        route: str,
        payload: Document | list[Document],
        *,
        etag: str | None = None,
    ) -> Document:
        headers = (
            {"Content-Type": "application/json-patch+json"} if method == "PATCH" else {}
        )
        if etag:
            headers["If-Match"] = etag
        self.pacer.wait()
        try:
            response = self.session.request(
                method,
                self._url(route),
                json=payload,
                headers=headers,
                auth=self._authenticate,
                timeout=self.pacer.settings.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise ReadError(
                f"Target: {method} {route.split('/')[0]} failed. Review target state before retrying."
            ) from None
        if response.status_code not in (200, 201):
            collection = route.split("/", 1)[0]
            guidance = ""
            if (
                response.status_code == 403
                and method == "PATCH"
                and collection == "users"
            ):
                guidance = (
                    " The target token must allow editing other users."
                    " Closed team membership requires a target OpenMetadata administrator."
                    " Resume with the same package and checkpoint after restoring access."
                )
            elif response.status_code in {409, 412, 429} or response.status_code >= 500:
                guidance = " Review the target and resume from the checkpoint."
            raise ReadError(
                f"Target: {method} {collection} returned HTTP {response.status_code}.{guidance}"
            )
        try:
            body = response.json()
        except ValueError:
            raise ReadError("Target write returned invalid JSON.") from None
        if not isinstance(body, dict):
            raise ReadError("Target write returned an invalid response.")
        return body

    def create(self, kind: str, payload: Document) -> Document:
        return self._write("POST", REGISTRY[kind].route, payload)

    def detail_for_patch(
        self, kind: str, entity_id: str, fields: tuple[str, ...]
    ) -> tuple[Document | None, str | None]:
        route, params = self._detail_request(
            kind, entity_id, by_name=False, fields=fields
        )
        response = self._get_response(route, params, unavailable=(404,))
        if response is None:
            return None, None
        entity = self._decode(response)
        version = entity.get("version")
        etag = response.headers.get("ETag")
        # OpenMetadata hashes the GET field projection for its strong ETag.
        # PATCH uses a different projection and accepts a version-based validator.
        if type(version) in (int, float) and isfinite(version) and version >= 0:
            etag = f'W/"{float(version)}"'
        return entity, etag

    def patch(
        self, kind: str, entity_id: str, changes: list[Document], *, etag: str | None
    ) -> Document:
        if entity_id in {".", ".."}:
            raise ReadError("Target returned an invalid entity ID.")
        return self._write(
            "PATCH",
            f"{REGISTRY[kind].route}/{quote(entity_id, safe='')}",
            changes,
            etag=etag,
        )

    def add_product_assets(self, fqn: str, assets: list[Document]) -> None:
        if fqn in {".", ".."}:
            raise ReadError("The data product has an invalid FQN.")
        result = self._write(
            "PUT", f"dataProducts/{quote(fqn, safe='')}/assets/add", {"assets": assets}
        )
        if result.get("status") != "success" or result.get("numberOfRowsFailed"):
            raise ReadError(
                "Target reported an incomplete data product membership update."
            )
