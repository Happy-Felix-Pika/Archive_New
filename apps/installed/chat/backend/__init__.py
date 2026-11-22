"""Чат між користувачами. Адреси /api/chat/... лишаються як були."""

from . import by_url, chat, federation_in


def register(core):
    core.api.add_routes(chat.bp, at_root=True)
    core.api.add_routes(federation_in.bp, at_root=True)
    core.api.add_routes(by_url.bp, at_root=True)
