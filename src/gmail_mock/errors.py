"""Google-style API errors.

Google APIs return errors shaped like::

    {"error": {"code": 404, "message": "Requested entity was not found.",
               "errors": [{"message": "...", "domain": "global", "reason": "notFound"}],
               "status": "NOT_FOUND"}}
"""

from __future__ import annotations

_STATUS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ALREADY_EXISTS",
    412: "FAILED_PRECONDITION",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    501: "UNIMPLEMENTED",
    503: "UNAVAILABLE",
}

_REASON = {
    400: "invalidArgument",
    401: "authError",
    403: "forbidden",
    404: "notFound",
    409: "alreadyExists",
    412: "failedPrecondition",
    429: "rateLimitExceeded",
    500: "backendError",
    501: "notImplemented",
    503: "backendError",
}


class ApiError(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        *,
        reason: str | None = None,
        status: str | None = None,
        domain: str = "global",
        location: str | None = None,
        location_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason or _REASON.get(code, "unknown")
        self.status = status or _STATUS.get(code, "UNKNOWN")
        self.domain = domain
        self.location = location
        self.location_type = location_type

    def to_dict(self) -> dict:
        detail = {"message": self.message, "domain": self.domain, "reason": self.reason}
        if self.location:
            detail["location"] = self.location
            detail["locationType"] = self.location_type or "parameter"
        return {"error": {"code": self.code, "message": self.message, "errors": [detail], "status": self.status}}


def not_found(message: str = "Requested entity was not found.") -> ApiError:
    return ApiError(404, message)


def bad_request(message: str, reason: str = "invalidArgument") -> ApiError:
    return ApiError(400, message, reason=reason)


def failed_precondition(message: str) -> ApiError:
    return ApiError(400, message, reason="failedPrecondition", status="FAILED_PRECONDITION")


def unauthenticated() -> ApiError:
    return ApiError(
        401,
        "Request is missing required authentication credential. Expected OAuth 2 access token, "
        "login cookie or other valid authentication credential. "
        "See https://developers.google.com/identity/sign-in/web/devconsole-project.",
        reason="required",
        location="Authorization",
        location_type="header",
    )
