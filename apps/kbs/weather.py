"""Backend weather seam used by the Django KBS adapter."""

from dataclasses import dataclass
from datetime import time

from .engine.derived import season_at


@dataclass(frozen=True)
class WeatherContext:
    season: str
    condition: str | None
    sunrise: time | None
    sunset: time | None


def get_weather_context(latitude_deg, longitude_deg, local_now):
    # External condition/sunrise lookup remains a service plug-in point.
    return WeatherContext(
        season=season_at(local_now.month, latitude_deg),
        condition=None,
        sunrise=None,
        sunset=None,
    )
