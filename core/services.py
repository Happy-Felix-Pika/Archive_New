"""
Ядро: сервіси — як застосунки дають свій API іншим застосункам.

Правило системи: застосунок не імпортує код іншого застосунку напряму.
Але іноді одному треба скористатись можливостями іншого — наприклад,
джерело e621 має зареєструватись у застосунку «Онлайн». Для цього
«Онлайн» оголошує сервіс, а e621 бере його через ядро:

    # у застосунку online
    core.services.provide("online.sources", registry)

    # у застосунку e621 (залежить від online — тож вантажиться пізніше)
    core.services.get("online.sources").register(E621Source)

Порядок гарантує маніфест: той, хто бере сервіс, мусить мати в
dependencies того, хто його дає. Тоді на момент get() сервіс уже є.
"""

_services: dict = {}


def provide(name: str, obj):
    """Оголосити сервіс. Повторне оголошення замінює попереднє."""
    _services[name] = obj


def get(name: str):
    """
    Взяти сервіс. Якщо його немає — зрозуміла помилка замість
    'NoneType has no attribute', бо найчастіша причина — забута
    залежність у маніфесті.
    """
    if name not in _services:
        raise LookupError(
            f"Сервіс '{name}' не знайдено. Перевір, чи застосунок, що його дає, "
            f"указано в dependencies маніфесту і чи він увімкнений.")
    return _services[name]


def has(name: str) -> bool:
    return name in _services


def names() -> list:
    return sorted(_services)
