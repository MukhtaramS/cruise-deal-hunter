from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://cruise:cruise@localhost:5432/cruise"

    # which deal-hunting configuration to run: a profile name from
    # app/profiles.py ("default", "visa_ru") or "all" to run every profile
    profile: str = "all"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    scrape_interval_hours: int = 4

    # Hot deal = price < ratio * 30-day median, OR price/night < this cap
    hot_deal_median_ratio: float = 0.40
    hot_deal_max_price_per_night: float = 60.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
