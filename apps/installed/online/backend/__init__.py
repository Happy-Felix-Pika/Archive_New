"""
Онлайн — перегляд зовнішніх сайтів прямо з архіву.

Сам застосунок нічого не знає про конкретні сайти. Він дає вкладку,
пошук, улюблене і реєстр джерел. Джерела (e621, Inkbunny, ...) — окремі
застосунки, які реєструються через сервіс 'online.sources'.
"""

from . import live
from .sources import SourceRegistry


def register(core):
    core.api.add_routes(live.bp, at_root=True)   # адреси /api/live/... як і були
    core.services.provide("online.sources", SourceRegistry())
