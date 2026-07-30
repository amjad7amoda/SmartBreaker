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
    """Build the weather context for one site.

    latitude_deg:  site latitude; negative = southern hemisphere (degrees)
    longitude_deg: site longitude (degrees)
    local_now:     current local time at the site (datetime)
    """
    # TODO(weather-api): call the chosen external weather API here with the
    # site coordinates and fill `condition`, `sunrise` and `sunset`. Until
    # then the engine uses the configured day_start/day_end fallback and
    # reports the condition as unknown.
    return WeatherContext(
        season=season_at(local_now.month, latitude_deg),
        condition=None,
        sunrise=None,
        sunset=None,
    )
