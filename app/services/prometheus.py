import logging

import requests
from requests.adapters import HTTPAdapter, Retry

from ..config import PROMETHEUS_URL

logger = logging.getLogger(__name__)


def _build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,          # 0.5s, 1s, 2s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class PrometheusService:

    def __init__(self):
        self.base_url = PROMETHEUS_URL
        if not self.base_url:
            raise RuntimeError(
                "PROMETHEUS_URL is not configured — set it in your .env"
            )
        self.session = _build_session()

    def instant_query(self, query: str):
        """
        Execute a Prometheus instant query.
        """

        url = f"{self.base_url}/api/v1/query"

        response = self.session.get(
            url,
            params={"query": query},
            # (connect timeout, read timeout) — a hung Prometheus must not
            # be able to stall the whole RCA pipeline indefinitely
            timeout=(5, 30),
        )

        response.raise_for_status()

        return response.json()

    def range_query(
        self,
        query: str,
        start: str,
        end: str,
        step: str = "30s"
    ):
        """
        Execute a Prometheus range query.
        """

        url = f"{self.base_url}/api/v1/query_range"

        response = self.session.get(
            url,
            params={
                "query": query,
                "start": start,
                "end": end,
                "step": step
            },
            timeout=(5, 30),
        )

        response.raise_for_status()

        return response.json()
