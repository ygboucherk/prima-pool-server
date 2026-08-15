"""RFC 7807 problem details for API errors."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class ProblemError(Exception):
    """An error rendered as application/problem+json (RFC 7807)."""

    def __init__(
        self,
        status: int,
        type: str,
        title: str,
        detail: str | None = None,
        instance: str | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.type = type
        self.title = title
        self.detail = detail
        self.instance = instance


class UnauthorizedError(ProblemError):
    def __init__(self, detail: str = "The provided credentials are not valid.") -> None:
        super().__init__(
            401,
            "https://prima-pool.dev/errors/unauthorized",
            "Unauthorized",
            detail,
        )


class ForbiddenError(ProblemError):
    def __init__(self, detail: str = "Authenticated but not allowed.") -> None:
        super().__init__(
            403,
            "https://prima-pool.dev/errors/forbidden",
            "Forbidden",
            detail,
        )


class NotFoundError(ProblemError):
    def __init__(self, detail: str = "Resource does not exist.") -> None:
        super().__init__(
            404,
            "https://prima-pool.dev/errors/not_found",
            "Not Found",
            detail,
        )


class ConflictError(ProblemError):
    def __init__(self, type: str = "conflict", title: str = "Conflict", detail: str | None = None) -> None:
        super().__init__(409, f"https://prima-pool.dev/errors/{type}", title, detail)


class BadRequestError(ProblemError):
    def __init__(self, detail: str = "Malformed request.") -> None:
        super().__init__(
            400,
            "https://prima-pool.dev/errors/invalid_request",
            "Invalid Request",
            detail,
        )


class PaymentRequiredError(ProblemError):
    def __init__(self, detail: str = "Insufficient balance for this model.") -> None:
        super().__init__(
            402,
            "https://prima-pool.dev/errors/insufficient_balance",
            "Payment Required",
            detail,
        )


class TooManyRequestsError(ProblemError):
    def __init__(self, detail: str = "Rate limited.") -> None:
        super().__init__(
            429,
            "https://prima-pool.dev/errors/too_many_requests",
            "Too Many Requests",
            detail,
        )


async def problem_exception_handler(request: Request, exc: ProblemError) -> JSONResponse:
    """FastAPI exception handler that renders ProblemError as RFC 7807 JSON."""
    return JSONResponse(
        status_code=exc.status,
        content={
            "type": exc.type,
            "title": exc.title,
            "status": exc.status,
            **({"detail": exc.detail} if exc.detail else {}),
            **({"instance": exc.instance} if exc.instance else {}),
        },
        media_type="application/problem+json",
    )
