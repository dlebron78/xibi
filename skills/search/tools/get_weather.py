"""
get_weather.py — Xibi weather tool

Fetches current conditions and 3-day forecast from wttr.in.
Free, no API key, structured JSON. Works for any city or location.
"""

import json
import urllib.parse
import urllib.request

_WTTR_URL = "https://wttr.in"
_TIMEOUT = 10


def run(params: dict) -> dict:
    location = params.get("location", "").strip()
    if not location:
        return {"status": "error", "message": "No location provided."}

    encoded = urllib.parse.quote(location)
    url = f"{_WTTR_URL}/{encoded}?format=j1"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "xibi-weather/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return {"status": "error", "message": f"Weather fetch failed: {e}"}

    # Extract current conditions
    current = data.get("current_condition", [{}])[0]
    area = data.get("nearest_area", [{}])[0]
    area_name = area.get("areaName", [{}])[0].get("value", location)
    region = area.get("region", [{}])[0].get("value", "")

    result = {
        "status": "success",
        "location": f"{area_name}, {region}" if region else area_name,
        "current": {
            "temp_f": current.get("temp_F"),
            "temp_c": current.get("temp_C"),
            "feels_like_f": current.get("FeelsLikeF"),
            "humidity": current.get("humidity"),
            "description": current.get("weatherDesc", [{}])[0].get("value", ""),
            "wind_mph": current.get("windspeedMiles"),
            "wind_dir": current.get("winddir16Point"),
            "uv_index": current.get("uvIndex"),
        },
        "forecast": [],
    }

    # Extract daily forecasts (3 days)
    for day in data.get("weather", []):
        result["forecast"].append(
            {
                "date": day.get("date"),
                "high_f": day.get("maxtempF"),
                "low_f": day.get("mintempF"),
                "high_c": day.get("maxtempC"),
                "low_c": day.get("mintempC"),
                "sun_hours": day.get("sunHour"),
                "uv_index": day.get("uvIndex"),
            }
        )

    return result
