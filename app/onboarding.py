"""Onboarding flow definitions: conversation states, inline keyboards,
prompts, and callback-data <-> preference-value mappings. Kept free of
handler logic so keyboards and mappings are unit-testable without Telegram.

Callback data scheme (all under 64 bytes, Telegram's limit):
    ob:region:<de|uk|fr|cis|other>
    ob:pass:<RU|KZ|EU|UK|other>
    ob:budget:<60|120|200|none>
    ob:len:<2-4|5-9|10+|any>
    ob:dep:<region-slug|any|done>
    menu:<region|pass|budget|len|dep>       (/settings menu)
"""

from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.matching import BLACK_SEA, CARIBBEAN, MEDITERRANEAN, NORTHERN_EUROPE

# conversation states
MENU, REGION, PASSPORT, BUDGET, LENGTH, DEPART = range(6)

PROMPTS = {
    REGION: "Where do you book from?",
    PASSPORT: (
        "Your passport affects which cruises you can board (visas). "
        "We use this only to filter deals."
    ),
    BUDGET: "Max budget per night?",
    LENGTH: "Preferred trip length?",
    DEPART: "Departure regions? Toggle any that apply, then hit Done.",
}

REGION_OPTIONS = [
    ("🇩🇪 Germany", "de"),
    ("🇬🇧 UK", "uk"),
    ("🇫🇷 France", "fr"),
    ("🇷🇺 Russia/CIS", "cis"),
    ("🌍 Other", "other"),
]

PASSPORT_OPTIONS = [
    ("🇷🇺 Russia", "RU"),
    ("🇰🇿 Kazakhstan", "KZ"),
    ("🇪🇺 EU", "EU"),
    ("🇬🇧 UK", "UK"),
    ("Other/Skip", "other"),
]

BUDGET_OPTIONS = [
    ("Under €60", "60"),
    ("€60-120", "120"),
    ("€120-200", "200"),
    ("No limit", "none"),
]

# callback value -> stored budget_per_night_max
BUDGET_VALUES: dict[str, Decimal | None] = {
    "60": Decimal("60"),
    "120": Decimal("120"),
    "200": Decimal("200"),
    "none": None,
}

LENGTH_OPTIONS = [
    ("2-4 nights", "2-4"),
    ("5-9 nights", "5-9"),
    ("10+ nights", "10+"),
    ("Any", "any"),
]

DEPARTURE_OPTIONS = [
    ("Mediterranean", MEDITERRANEAN),
    ("Northern Europe", NORTHERN_EUROPE),
    ("Caribbean", CARIBBEAN),
    ("Black Sea", BLACK_SEA),
]

SETTINGS_MENU = [
    ("📍 Booking region", "menu:region"),
    ("🛂 Passport", "menu:pass"),
    ("💶 Budget per night", "menu:budget"),
    ("🌙 Trip length", "menu:len"),
    ("🧭 Departure regions", "menu:dep"),
]


def _rows(options: list[tuple[str, str]], prefix: str, per_row: int = 2):
    buttons = [
        InlineKeyboardButton(label, callback_data=f"{prefix}{value}")
        for label, value in options
    ]
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def region_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_rows(REGION_OPTIONS, "ob:region:"))


def passport_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_rows(PASSPORT_OPTIONS, "ob:pass:"))


def budget_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_rows(BUDGET_OPTIONS, "ob:budget:"))


def length_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_rows(LENGTH_OPTIONS, "ob:len:"))


def departure_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    """Multi-select: one region per row with a toggle check, then Any/Done."""
    rows = [
        [
            InlineKeyboardButton(
                ("✅ " if value in selected else "") + label,
                callback_data=f"ob:dep:{value}",
            )
        ]
        for label, value in DEPARTURE_OPTIONS
    ]
    rows.append(
        [
            InlineKeyboardButton("🌐 Any", callback_data="ob:dep:any"),
            InlineKeyboardButton("Done ✔️", callback_data="ob:dep:done"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def settings_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data)] for label, data in SETTINGS_MENU]
    )
