from __future__ import annotations


class DomainError(Exception):
    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class AuthenticationError(DomainError):
    def __init__(
        self,
        message: str = "Authentication required",
        code: str = "authentication_required",
    ) -> None:
        super().__init__(status_code=401, code=code, message=message)


class AuthorizationError(DomainError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(status_code=403, code=code, message=message)


class ConflictError(DomainError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(status_code=409, code=code, message=message)


class NotFoundError(DomainError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(status_code=404, code=code, message=message)


class ValidationDomainError(DomainError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(status_code=422, code=code, message=message)


class PayloadTooLargeError(DomainError):
    def __init__(self, message: str = "Uploaded file exceeds the configured limit") -> None:
        super().__init__(status_code=413, code="file_too_large", message=message)
