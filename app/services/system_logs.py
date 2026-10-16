import re
from pathlib import Path

from app.core.errors import require
from app.core.logging import log_root

GROUPS = {"modules", "services"}
FILE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.log(?:\.\d+)?$")
MAX_READ_BYTES = 512 * 1024


def resolve_log(group: str, filename: str) -> Path:
    require(group in GROUPS and bool(FILE_PATTERN.fullmatch(filename)), "Invalid log path", 400)
    directory = (log_root() / group).resolve()
    path = (directory / filename).resolve()
    require(path.parent == directory and path.is_file(), "Log file not found", 404)
    return path


def browse():
    result = []
    root = log_root()
    for group in sorted(GROUPS):
        directory = root / group
        files = []
        if directory.exists():
            for path in sorted(directory.iterdir(), key=lambda item: item.name):
                if path.is_file() and FILE_PATTERN.fullmatch(path.name):
                    stat = path.stat()
                    files.append(
                        {
                            "name": path.name,
                            "path": f"{group}/{path.name}",
                            "size": stat.st_size,
                            "modifiedAt": stat.st_mtime,
                        }
                    )
        result.append({"name": group, "files": files})
    return {"groups": result}


def tail(path: Path) -> dict:
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size > MAX_READ_BYTES:
            stream.seek(size - MAX_READ_BYTES)
            stream.readline()
        content = stream.read().decode("utf-8", errors="replace")
    return {"content": content, "size": size, "truncated": size > MAX_READ_BYTES}
