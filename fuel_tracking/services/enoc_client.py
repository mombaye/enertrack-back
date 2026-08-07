import hashlib
import hmac
import uuid
from datetime import datetime
from urllib.parse import urlencode, urlparse

import requests
from django.conf import settings


class EnocIntegrationError(Exception):
    pass


def _utc_timestamp() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _body_hash(body: bytes | None = None) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def _build_signature(
    secret: str,
    method: str,
    canonical_path: str,
    timestamp: str,
    nonce: str,
    body_hash: str,
) -> str:
    string_to_sign = "\n".join(
        [
            method.upper(),
            canonical_path,
            timestamp,
            nonce,
            body_hash,
        ]
    )

    digest = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return f"sha256={digest}"


class EnocFuelClient:
    def __init__(self):
        self.base_url = getattr(settings, "ENOC_BASE_URL", "").rstrip("/")
        self.client_id = getattr(settings, "ENOC_INTEGRATION_CLIENT_ID", "enertrack")
        self.secret = getattr(settings, "ENOC_INTEGRATION_SHARED_SECRET", "")
        self.timeout = getattr(settings, "ENOC_INTEGRATION_TIMEOUT", 30)

        if not self.base_url:
            raise EnocIntegrationError("ENOC_BASE_URL est vide.")

        if not self.secret:
            raise EnocIntegrationError("ENOC_INTEGRATION_SHARED_SECRET est vide.")

    def _build_url_and_canonical_path(self, path: str, params: dict | None = None):
        params = {
            key: value
            for key, value in (params or {}).items()
            if value not in (None, "", [])
        }

        query = urlencode(params)

        base_path = urlparse(self.base_url).path.rstrip("/")
        canonical_path = f"{base_path}{path}"

        if query:
            canonical_path = f"{canonical_path}?{query}"

        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        return url, canonical_path

    def _headers(self, method: str, canonical_path: str, body: bytes | None = None):
        timestamp = _utc_timestamp()
        nonce = str(uuid.uuid4())
        body_hash = _body_hash(body)

        signature = _build_signature(
            secret=self.secret,
            method=method,
            canonical_path=canonical_path,
            timestamp=timestamp,
            nonce=nonce,
            body_hash=body_hash,
        )

        return {
            "X-Integration-Client": self.client_id,
            "X-Integration-Timestamp": timestamp,
            "X-Integration-Nonce": nonce,
            "X-Integration-Signature": signature,
        }

    def get_operations(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        updated_since: str | None = None,
        site: str | None = None,
        zone: str | None = None,
        page: int = 1,
        limit: int = 100,
    ) -> dict:
        path = "/fuel/integrations/enertrack/operations"

        params = {
            "startDate": start_date,
            "endDate": end_date,
            "updated_since": updated_since,
            "site": site,
            "zone": zone,
            "page": page,
            "limit": limit,
        }

        url, canonical_path = self._build_url_and_canonical_path(path, params)
        headers = self._headers("GET", canonical_path)

        response = requests.get(url, headers=headers, timeout=self.timeout)

        if response.status_code >= 400:
            raise EnocIntegrationError(
                f"Erreur ENOC {response.status_code}: {response.text[:1000]}"
            )

        return response.json()