from enum import StrEnum


class ErrorCode(StrEnum):
    # common
    VALIDATION_ERROR = "validation_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    USER_INACTIVE = "user_inactive"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    CORE_UNAVAILABLE = "core_unavailable"

    # 400
    INVALID_FILE = "invalid_file"
    INVALID_CURSOR = "invalid_cursor"

    # 403
    NOT_A_MEMBER = "not_a_member"
    CANNOT_READ_CHANNEL = "cannot_read_channel"
    NOT_A_TEAM_MEMBER = "not_a_team_member"

    # 404
    CHANNEL_NOT_FOUND = "channel_not_found"
    MESSAGE_NOT_FOUND = "message_not_found"
    ATTACHMENT_NOT_FOUND = "attachment_not_found"

    # 409
    CHANNEL_ARCHIVED = "channel_archived"
    MESSAGE_DELETED = "message_deleted"
    PIN_LIMIT_REACHED = "pin_limit_reached"
    GAP_TOO_LARGE = "gap_too_large"
