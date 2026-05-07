"""Importing this package registers all tools via @tool decorators."""
from . import (  # noqa: F401
    browser,
    filesystem,
    goals,
    imessage,
    memory,
    notify,
    scheduler,
    self_modify,
    sentiment,
    shell,
    time,
    weather,
)
