"""Чат між користувачами. Адреси /api/chat/... лишаються як були."""

from . import by_url, chat, extras


def register(core):
    core.api.add_routes(chat.bp, at_root=True)
    core.api.add_routes(by_url.bp, at_root=True)
    core.api.add_routes(extras.bp, at_root=True)
