"""Weather/season context for the KBS.

The season is derived locally from the date and hemisphere. Condition and
sunrise/sunset come from an external weather API — plug-in point below.
"""

from dataclasses import dataclass
from datetime import datetime, time

from .derived import season_at


@dataclass
class WeatherContext:
    season: str            # meteorological season at the site: 'winter'|'spring'|'summer'|'autumn'
    condition: str | None  # short condition code from the weather API ('clear','cloudy','storm',...); None while the API is not wired in
    sunrise: time | None   # local sunrise from the API (local clock time); None -> engine falls back to KBSSettings.day_start
    sunset: time | None    # local sunset from the API (local clock time); None -> engine falls back to KBSSettings.day_end


def get_weather_context(latitude_deg, longitude_deg, local_now):
    return WeatherContext(
        season=season_at(local_now.month, latitude_deg),
        condition=None,
        sunrise=None,
        sunset=None,
    )
