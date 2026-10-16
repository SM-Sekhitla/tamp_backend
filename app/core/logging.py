import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import settings

MODULE_LOGGERS = {
    "auth": "tamp.module.auth",
    "parties": "tamp.module.parties",
    "commands": "tamp.module.commands",
    "loads": "tamp.module.loads",
    "matching": "tamp.module.matching",
    "tracking": "tamp.module.tracking",
    "notifications": "tamp.module.notifications",
    "routes": "tamp.module.routes",
    "webhooks": "tamp.module.webhooks",
    "snapshot": "tamp.module.snapshot",
    "operations": "tamp.module.operations",
}

SERVICE_LOGGERS = {
    "requests": "tamp.requests",
    "bootstrap": "tamp.bootstrap",
    "api": "tamp.api",
    "email": "tamp.email",
    "maps": "tamp.maps",
    "matching": "tamp.matching",
    "domain": "tamp.domain",
    "live_tracking": "tamp.live_tracking",
    "webhook_worker": "tamp.webhook_worker",
}


def log_root() -> Path:
    path = Path(settings.log_path)
    return path if path.is_absolute() else Path.cwd() / path


def _file_handler(path: Path) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    return handler


def configure_file_logging() -> None:
    root = log_root()
    root.mkdir(parents=True, exist_ok=True)
    for group, channels in (("modules", MODULE_LOGGERS), ("services", SERVICE_LOGGERS)):
        directory = root / group
        directory.mkdir(exist_ok=True)
        for filename, logger_name in channels.items():
            logger = logging.getLogger(logger_name)
            path = directory / f"{filename}.log"
            if not any(
                isinstance(handler, RotatingFileHandler)
                and Path(handler.baseFilename) == path.resolve()
                for handler in logger.handlers
            ):
                logger.addHandler(_file_handler(path))
            logger.setLevel(logging.INFO)
