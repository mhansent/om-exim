"""Explicit write operations for the import apply path."""

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
        try:
            response = self.session.request(
                method,
                self._url(route),
                json=payload,
                headers=headers,
                auth=self._authenticate,
                timeout=30,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise ReadError(
                f"Target: {method} {route.split('/')[0]} failed. Review target state before retrying."
            ) from None
        if response.status_code not in (200, 201):
            raise ReadError(
                f"Target: {method} {route.split('/')[0]} returned HTTP {response.status_code}."
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
        return self._decode(response), response.headers.get("ETag")

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
