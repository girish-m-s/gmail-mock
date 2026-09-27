"""Point google-api-python-client at gmail-mock.

    from gmail_mock.client import build_service
    gmail = build_service("gmail", "http://127.0.0.1:12411", token="me@example.com")

``client_options={"api_endpoint": ...}`` alone is not enough: the client still
sends batch requests to ``https://<api>.googleapis.com/batch`` and builds media
upload URLs with Google's ``https`` scheme. This helper routes every
``*.googleapis.com`` request, and any scheme mismatch, to the mock. It needs
``google-api-python-client`` and ``google-auth`` installed.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def build_service(api: str, base_url: str, token: str = "me@example.com", version: str = "v1"):
    import google_auth_httplib2
    import httplib2
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    target = urlsplit(base_url)

    class _MockHttp(google_auth_httplib2.AuthorizedHttp):
        def request(self, uri, *args, **kwargs):
            parts = urlsplit(uri)
            if parts.netloc == target.netloc or parts.netloc.endswith("googleapis.com"):
                uri = urlunsplit(parts._replace(scheme=target.scheme, netloc=target.netloc))
            return super().request(uri, *args, **kwargs)

    http = _MockHttp(Credentials(token), http=httplib2.Http(disable_ssl_certificate_validation=target.scheme == "https"))
    return build(api, version, http=http, client_options={"api_endpoint": base_url}, static_discovery=True, cache_discovery=False)
