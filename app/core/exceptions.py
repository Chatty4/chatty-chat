from app.core.codes import ErrorCode


class AppError(Exception):
    status_code: int = 500
    code: str | None = None
    message: str = "Error"

    def __init__(
        self,
        code: str | None = None,
        message: str | None = None,
        *,
        fields: dict[str, str] | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.code = code or self.code
        if self.code is None:
            raise TypeError(f"{type(self).__name__} requires an error code")
        self.message = message or self.message
        self.fields = fields or {}
        self.retry_after = retry_after
        super().__init__(self.message)


class BadRequest(AppError):
    status_code = 400
    message = "Bad request"


class NotFound(AppError):
    status_code = 404
    message = "Not found"


class Conflict(AppError):
    status_code = 409
    message = "Conflict"


class ValidationFailed(AppError):
    """`fields` maps a field name to a short reason, e.g. {"body": "too_long"}."""

    status_code = 400
    code = ErrorCode.VALIDATION_ERROR
    message = "Invalid request"


class Unauthorized(AppError):
    status_code = 401
    code = ErrorCode.UNAUTHORIZED
    message = "Authentication required"


class PermissionDenied(AppError):
    status_code = 403
    code = ErrorCode.FORBIDDEN
    message = "You are not allowed to do this"


class UserInactive(PermissionDenied):
    code = ErrorCode.USER_INACTIVE
    message = "This account has been deactivated"


class RateLimited(AppError):
    """`retry_after` (seconds) becomes the Retry-After response header."""

    status_code = 429
    code = ErrorCode.RATE_LIMITED
    message = "Too many requests"

    def __init__(
        self,
        retry_after: int,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(code, message, retry_after=retry_after)


class InternalError(AppError):
    status_code = 500
    code = ErrorCode.INTERNAL_ERROR
    message = "Internal server error"


class ServiceUnavailable(AppError):
    status_code = 503
    code = ErrorCode.CORE_UNAVAILABLE
    message = "chatty-core could not be reached. Try again."
