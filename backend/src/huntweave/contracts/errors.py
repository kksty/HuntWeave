class ServiceError(Exception):
    def __init__(self, reason_code: str, status_code: int, *, retry_after: int | None = None):
        self.reason_code = reason_code
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(reason_code)
