from decimal import Decimal

from app.onboarding import (
    BUDGET_VALUES,
    budget_keyboard,
    departure_keyboard,
    length_keyboard,
    passport_keyboard,
    region_keyboard,
    settings_menu_keyboard,
)


def flatten(markup):
    return [
        (btn.text, btn.callback_data)
        for row in markup.inline_keyboard
        for btn in row
    ]


class TestKeyboards:
    def test_region_step_options(self):
        buttons = flatten(region_keyboard())
        assert ("🇩🇪 Germany", "ob:region:de") in buttons
        assert ("🇷🇺 Russia/CIS", "ob:region:cis") in buttons
        assert ("🌍 Other", "ob:region:other") in buttons
        assert len(buttons) == 5

    def test_passport_step_options(self):
        data = [d for _, d in flatten(passport_keyboard())]
        assert data == ["ob:pass:RU", "ob:pass:KZ", "ob:pass:EU", "ob:pass:UK", "ob:pass:other"]

    def test_budget_step_values_map_to_decimals(self):
        data = [d.rsplit(":", 1)[-1] for _, d in flatten(budget_keyboard())]
        assert data == ["60", "120", "200", "none"]
        assert BUDGET_VALUES["60"] == Decimal("60")
        assert BUDGET_VALUES["none"] is None

    def test_length_step_options(self):
        data = [d for _, d in flatten(length_keyboard())]
        assert data == ["ob:len:2-4", "ob:len:5-9", "ob:len:10+", "ob:len:any"]

    def test_settings_menu_covers_all_five_steps(self):
        data = [d for _, d in flatten(settings_menu_keyboard())]
        assert data == ["menu:region", "menu:pass", "menu:budget", "menu:len", "menu:dep"]


class TestDepartureMultiSelect:
    def test_unselected_shows_no_checkmarks(self):
        texts = [t for t, _ in flatten(departure_keyboard(set()))]
        assert not any(t.startswith("✅") for t in texts)

    def test_selected_regions_get_checkmarks(self):
        markup = departure_keyboard({"mediterranean", "black_sea"})
        checked = [t for t, _ in flatten(markup) if t.startswith("✅")]
        assert checked == ["✅ Mediterranean", "✅ Black Sea"]

    def test_any_and_done_buttons_present(self):
        data = [d for _, d in flatten(departure_keyboard(set()))]
        assert "ob:dep:any" in data
        assert "ob:dep:done" in data

    def test_callback_data_within_telegram_limit(self):
        for markup in (
            region_keyboard(), passport_keyboard(), budget_keyboard(),
            length_keyboard(), departure_keyboard(set()), settings_menu_keyboard(),
        ):
            for _, data in flatten(markup):
                assert len(data.encode()) <= 64
