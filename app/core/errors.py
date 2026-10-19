class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status


def require(condition, message="Not permitted", status=403):
    if not condition:
        raise AppError(message, status)


def validation_message(exc):
    """Return the first validation issue without echoing submitted values."""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    if not errors:
        return "Invalid request data"
    issue = errors[0]
    location = ".".join(str(part) for part in issue.get("loc", ()) if part != "body")
    detail = str(issue.get("msg", "Invalid value")).removeprefix("Value error, ")
    return f"{location}: {detail}" if location else detail
