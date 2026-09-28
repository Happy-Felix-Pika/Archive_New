"""Джерело e621 для вкладки «Онлайн»."""

from .source import build


def register(core):
    registry = core.services.get("online.sources")   # дає застосунок online
    registry.register(build(registry.base))
