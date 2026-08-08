"""Validated access to the simulator's source climatology CSV."""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

from django.conf import settings


CLIMATE_CSV_PATH = Path(settings.BASE_DIR).parent / 'simulator' / 'data' / 'solar_data.csv'
EXPECTED_COLUMNS = (
    'city', 'latitude_deg', 'longitude_deg', 'month', 'season',
    'typical_weather', 'ghi_kwh_m2_day', 'clearsky_ghi_kwh_m2_day',
    'cloud_amount_percent', 'precip_mm_day', 'temp_C', 'humidity_percent',
)
WEATHER_CONDITIONS = {'sunny', 'partly_cloudy', 'cloudy', 'rainy'}


class ClimateDataError(ValueError):
    """The source climate file is absent or cannot safely drive simulation."""


def _number(raw, field, line, *, integer=False):
    try:
        value = int(raw) if integer else float(raw)
    except (TypeError, ValueError) as exc:
        raise ClimateDataError(f'line {line}: invalid {field}') from exc
    return value


@lru_cache(maxsize=4)
def load_climate_rows(path=None):
    """Return normalized rows, caching by path after complete-file validation."""
    source = Path(path or CLIMATE_CSV_PATH)
    try:
        handle = source.open(newline='', encoding='utf-8')
    except OSError as exc:
        raise ClimateDataError(f'unable to read climate data: {exc}') from exc

    rows = []
    seen = set()
    city_months = {}
    with handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != EXPECTED_COLUMNS:
            raise ClimateDataError('climate data columns do not match the expected schema')
        for line, raw in enumerate(reader, start=2):
            city = (raw['city'] or '').strip()
            season = (raw['season'] or '').strip().lower()
            weather = (raw['typical_weather'] or '').strip().lower()
            month = _number(raw['month'], 'month', line, integer=True)
            if not city or month not in range(1, 13):
                raise ClimateDataError(f'line {line}: invalid city or month')
            if season not in {'winter', 'summer'}:
                raise ClimateDataError(f'line {line}: unsupported season')
            if weather not in WEATHER_CONDITIONS:
                raise ClimateDataError(f'line {line}: unsupported typical_weather')
            key = (city, month)
            if key in seen:
                raise ClimateDataError(f'line {line}: duplicate city/month record')
            seen.add(key)
            city_months.setdefault(city, set()).add(month)
            row = {
                'city': city,
                'latitude_deg': _number(raw['latitude_deg'], 'latitude_deg', line),
                'longitude_deg': _number(raw['longitude_deg'], 'longitude_deg', line),
                'month': month,
                'season': season,
                'typical_weather': weather,
                'ghi_kwh_m2_day': _number(raw['ghi_kwh_m2_day'], 'ghi_kwh_m2_day', line),
                'clearsky_ghi_kwh_m2_day': _number(raw['clearsky_ghi_kwh_m2_day'], 'clearsky_ghi_kwh_m2_day', line),
                'cloud_amount_percent': _number(raw['cloud_amount_percent'], 'cloud_amount_percent', line),
                'precip_mm_day': _number(raw['precip_mm_day'], 'precip_mm_day', line),
                'temp_C': _number(raw['temp_C'], 'temp_C', line),
                'humidity_percent': _number(raw['humidity_percent'], 'humidity_percent', line),
            }
            if not (-90 <= row['latitude_deg'] <= 90 and -180 <= row['longitude_deg'] <= 180):
                raise ClimateDataError(f'line {line}: invalid coordinates')
            if row['ghi_kwh_m2_day'] < 0 or row['clearsky_ghi_kwh_m2_day'] <= 0:
                raise ClimateDataError(f'line {line}: invalid irradiance')
            if not (0 <= row['cloud_amount_percent'] <= 100 and 0 <= row['humidity_percent'] <= 100):
                raise ClimateDataError(f'line {line}: percentage outside 0..100')
            if row['precip_mm_day'] < 0:
                raise ClimateDataError(f'line {line}: precipitation cannot be negative')
            rows.append(row)

    if len(city_months) != 7:
        raise ClimateDataError('climate data must contain exactly seven cities')
    if any(months != set(range(1, 13)) for months in city_months.values()):
        raise ClimateDataError('each climate city must contain months 1 through 12')
    if len(rows) != 84:
        raise ClimateDataError('climate data must contain exactly 84 monthly rows')
    return tuple(rows)
