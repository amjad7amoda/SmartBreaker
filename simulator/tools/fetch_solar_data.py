"""Fetch real monthly solar/weather climatology for 7 Syrian cities from the
NASA POWER API (https://power.larc.nasa.gov) and write:
  - simulator/data/solar_data.csv   (the real-data deliverable)
  - simulator/data.js               (same rows embedded as JS, so the page works from file://)

Parameters fetched (monthly climatology, ~2001-2020 averages):
  ALLSKY_SFC_SW_DWN  all-sky surface shortwave irradiance (kWh/m^2/day) - what the panels actually receive on average
  CLRSKY_SFC_SW_DWN  clear-sky surface shortwave irradiance (kWh/m^2/day) - the sunny-day ceiling
  CLOUD_AMT          average cloud amount (%)
  PRECTOTCORR        precipitation (mm/day)
  T2M                air temperature at 2 m (degC)
  RH2M               relative humidity at 2 m (%)
"""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

CITIES = {  # city -> (latitude_deg, longitude_deg)
    'Damascus':    (33.51, 36.29),
    'Aleppo':      (36.20, 37.13),
    'Latakia':     (35.52, 35.79),
    'Idlib':       (35.93, 36.63),
    'Homs':        (34.73, 36.71),
    'Daraa':       (32.62, 36.10),
    'Deir Ezzour': (35.34, 40.14),
}
PARAMS = 'ALLSKY_SFC_SW_DWN,CLRSKY_SFC_SW_DWN,CLOUD_AMT,PRECTOTCORR,T2M,RH2M'
MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']


def season_for(month_num):
    """Two-season Syrian split: May-Oct = summer, Nov-Apr = winter."""
    return 'summer' if 5 <= month_num <= 10 else 'winter'


def typical_weather(cloud_pct, precip_mm):
    """Dominant weather label for a month, from real cloud/precip averages."""
    if precip_mm >= 2.0:
        return 'rainy'
    if cloud_pct >= 55:
        return 'cloudy'
    if cloud_pct >= 30:
        return 'partly_cloudy'
    return 'sunny'


rows = []  # csv rows
for city, (lat, lon) in CITIES.items():
    url = (
        'https://power.larc.nasa.gov/api/temporal/climatology/point'
        f'?parameters={PARAMS}&community=RE&longitude={lon}&latitude={lat}&format=JSON'
    )
    print(f'fetching {city} ...')
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.load(resp)
    p = data['properties']['parameter']
    for i, mon in enumerate(MONTHS, start=1):
        ghi = p['ALLSKY_SFC_SW_DWN'][mon]
        clr = p['CLRSKY_SFC_SW_DWN'][mon]
        cloud = p['CLOUD_AMT'][mon]
        precip = p['PRECTOTCORR'][mon]
        t2m = p['T2M'][mon]
        rh = p['RH2M'][mon]
        rows.append({
            'city': city, 'latitude_deg': lat, 'longitude_deg': lon,
            'month': i, 'season': season_for(i),
            'typical_weather': typical_weather(cloud, precip),
            'ghi_kwh_m2_day': round(ghi, 2),
            'clearsky_ghi_kwh_m2_day': round(clr, 2),
            'cloud_amount_percent': round(cloud, 1),
            'precip_mm_day': round(precip, 2),
            'temp_C': round(t2m, 1),
            'humidity_percent': round(rh, 1),
        })

header = list(rows[0].keys())
csv_lines = [','.join(header)]
for r in rows:
    csv_lines.append(','.join(str(r[k]) for k in header))
csv_text = '\n'.join(csv_lines) + '\n'

import os
os.makedirs(r'C:\Users\alayham\Desktop\SmartBreaker\simulator\data', exist_ok=True)
with open(r'C:\Users\alayham\Desktop\SmartBreaker\simulator\data\solar_data.csv', 'w', encoding='utf-8') as f:
    f.write(csv_text)

js = (
    '// AUTO-GENERATED from data/solar_data.csv (NASA POWER climatology) - do not edit by hand.\n'
    '// Each row: real monthly averages for one city. Units are in the field names.\n'
    'const SOLAR_DATA = ' + json.dumps(rows, indent=1) + ';\n'
)
with open(r'C:\Users\alayham\Desktop\SmartBreaker\simulator\data.js', 'w', encoding='utf-8') as f:
    f.write(js)

print(f'wrote {len(rows)} rows ({len(CITIES)} cities x 12 months)')
