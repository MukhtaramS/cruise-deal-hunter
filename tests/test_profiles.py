import pytest

from app.config import settings
from app.profiles import PROFILES, active_profiles, wanted_sources
from app.visa import VISA_FREE_RU


class TestActiveProfiles:
    def test_default_env_value_runs_all_profiles(self):
        # settings.profile defaults to "all"
        assert settings.profile == "all"
        assert {p.name for p in active_profiles()} == {"default", "visa_ru"}

    def test_single_profile_selection(self, monkeypatch):
        monkeypatch.setattr(settings, "profile", "visa_ru")
        profiles = active_profiles()
        assert len(profiles) == 1
        assert profiles[0].name == "visa_ru"
        assert profiles[0].visa_filter == VISA_FREE_RU

    def test_default_profile_has_no_filters(self):
        profile = PROFILES["default"]
        assert profile.sources is None  # all scrapers
        assert profile.visa_filter is None

    def test_unknown_profile_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "profile", "typo")
        with pytest.raises(ValueError, match="unknown PROFILE"):
            active_profiles()


class TestWantedSources:
    def test_default_profile_means_all_sources(self):
        assert wanted_sources([PROFILES["default"]]) is None
        assert wanted_sources(list(PROFILES.values())) is None  # union incl. None

    def test_explicit_sources(self):
        assert wanted_sources([PROFILES["visa_ru"]]) == {"seascanner"}
