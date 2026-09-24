class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status


def require(condition, message="Not permitted", status=403):
    if not condition:
        raise AppError(message, status)
