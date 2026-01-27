#!/usr/bin/env python3
"""mcp_server MCP server"""

import os
import sys
from datetime import date, datetime
from typing import Annotated, Optional
from zoneinfo import ZoneInfo

import httpx
from arcade_mcp_server import Context, MCPApp, mcp_app as arcade_mcp_app_module
from arcade_mcp_server.auth import Reddit
from arcade_mcp_server.worker import create_arcade_mcp as _create_arcade_mcp
from fastapi.middleware.cors import CORSMiddleware
from arcade_mcp_server import types as arcade_mcp_types

# Temporarily pin protocol to match the UI SDK (sdk supports up to 2025-03-26).
arcade_mcp_types.LATEST_PROTOCOL_VERSION = "2025-03-26"

# Patch the FastAPI app factory used by MCPApp to inject CORS support for the OAP web UI.
def create_arcade_mcp_with_cors(*args, **kwargs):
    fastapi_app = _create_arcade_mcp(*args, **kwargs)
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )
    return fastapi_app


arcade_mcp_app_module.create_arcade_mcp = create_arcade_mcp_with_cors

app = MCPApp(name="mcp_server", version="1.0.0", log_level="DEBUG")


@app.tool
def greet(name: Annotated[str, "The name of the person to greet"]) -> str:
    """Greet a person by name."""
    return f"Hello, {name}!"


# To use this tool locally, you need to either set the secret in the .env file or as an environment variable
@app.tool(requires_secrets=["MY_SECRET_KEY"])
def whisper_secret(context: Context) -> Annotated[str, "The last 4 characters of the secret"]:
    """Reveal the last 4 characters of a secret"""
    # Secrets are injected into the context at runtime.
    # LLMs and MCP clients cannot see or access your secrets
    # You can define secrets in a .env file.
    try:
        secret = context.get_secret("MY_SECRET_KEY")
    except Exception as e:
        return str(e)

    return "The last 4 characters of the secret are: " + secret[-4:]

# To use this tool locally, you need to install the Arcade CLI (uv tool install arcade-mcp)
# and then run 'arcade login' to authenticate.
@app.tool(requires_auth=Reddit(scopes=["read"]))
async def get_posts_in_subreddit(
    context: Context, subreddit: Annotated[str, "The name of the subreddit"]
) -> dict:
    """Get posts from a specific subreddit"""
    # Normalize the subreddit name
    subreddit = subreddit.lower().replace("r/", "").replace(" ", "")

    # Prepare the httpx request
    # OAuth token is injected into the context at runtime.
    # LLMs and MCP clients cannot see or access your OAuth tokens.
    oauth_token = context.get_auth_token_or_empty()
    headers = {
        "Authorization": f"Bearer {oauth_token}",
        "User-Agent": "mcp_server-mcp-server",
    }
    params = {"limit": 5}
    url = f"https://oauth.reddit.com/r/{subreddit}/hot"

    # Make the request
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()

        # Return the response
        return response.json()


@app.tool
async def get_weather(
    city: Annotated[str, "City name to fetch weather for"],
    country_code: Annotated[
        Optional[str], "Optional country code to disambiguate the city"
    ] = None,
    units: Annotated[
        str,
        "Units system: 'metric' (C, m/s) or 'imperial' (F, mph). Defaults to metric.",
    ] = "metric",
) -> dict:
    """
    Fetch current weather for a city using the Open-Meteo APIs (geocoding + forecast).
    No API key or auth required.
    """
    query = city.strip()
    if country_code:
        query = f"{query},{country_code.strip()}"

    # Open-Meteo uses metric; convert to imperial if requested
    want_imperial = units.lower().startswith("imp")

    # Geocode the city to lat/lon
    geocode_url = "https://geocoding-api.open-meteo.com/v1/search"
    weather_url = "https://api.open-meteo.com/v1/forecast"
    async with httpx.AsyncClient(timeout=10) as client:
        geocode_resp = await client.get(geocode_url, params={"name": query, "count": 1})
        geocode_resp.raise_for_status()
        geocode_data = geocode_resp.json()
        results = geocode_data.get("results") or []
        if not results:
            return {"error": f"Could not find location for '{query}'"}
        location = results[0]
        lat = location["latitude"]
        lon = location["longitude"]
        name = location.get("name", city)
        country = location.get("country", country_code or "")

        params = {
            "latitude": lat,
            "longitude": lon,
            "current_weather": True,
        }
        weather_resp = await client.get(weather_url, params=params)
        weather_resp.raise_for_status()
        weather_data = weather_resp.json()

    current = weather_data.get("current_weather", {}) or {}
    # Normalize units if imperial requested
    temp_c = current.get("temperature")
    wind_ms = current.get("windspeed")
    if want_imperial:
        if temp_c is not None:
            current["temperature_f"] = round((temp_c * 9 / 5) + 32, 1)
        if wind_ms is not None:
            current["windspeed_mph"] = round(wind_ms * 2.23694, 1)

    return {
        "location": {
            "name": name,
            "country": country,
            "latitude": lat,
            "longitude": lon,
        },
        "current_weather": current,
        "units": "imperial" if want_imperial else "metric",
        "source": "open-meteo.com (no API key required)",
    }


@app.tool
def get_time(
    timezone: Annotated[Optional[str], "IANA timezone, e.g. 'UTC' or 'Asia/Tokyo'"] = None,
) -> dict:
    """Get the current time in a specific timezone (defaults to UTC)."""
    tz_name = (timezone or "UTC").strip()
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        return {"error": f"Unknown timezone '{tz_name}'"}
    now = datetime.now(tzinfo)
    return {
        "timezone": tzinfo.key,
        "iso": now.isoformat(),
        "unix": int(now.timestamp()),
        "date": now.date().isoformat(),
    }


@app.tool
def convert_timezone(
    datetime_str: Annotated[str, "Datetime in ISO format, e.g. '2025-10-12 09:00'"],
    from_tz: Annotated[str, "IANA source timezone, e.g. 'Asia/Tokyo'"],
    to_tz: Annotated[str, "IANA target timezone, e.g. 'America/Chicago'"],
) -> dict:
    """Convert a datetime from one timezone to another."""
    try:
        source_tz = ZoneInfo(from_tz.strip())
    except Exception:
        return {"error": f"Unknown timezone '{from_tz}'"}
    try:
        target_tz = ZoneInfo(to_tz.strip())
    except Exception:
        return {"error": f"Unknown timezone '{to_tz}'"}

    raw = datetime_str.strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return {
            "error": (
                "Invalid datetime format. Use ISO like '2025-10-12 09:00' or "
                "'2025-10-12T09:00'."
            )
        }

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=source_tz)

    converted = parsed.astimezone(target_tz)
    return {
        "input": raw,
        "from_tz": source_tz.key,
        "to_tz": target_tz.key,
        "converted_iso": converted.isoformat(),
        "converted_date": converted.date().isoformat(),
    }


@app.tool
async def currency_exchange(
    from_currency: Annotated[str, "3-letter currency code, e.g. 'USD'"],
    to_currency: Annotated[str, "3-letter currency code, e.g. 'JPY'"],
    amount: Annotated[float, "Amount in the source currency"] = 1.0,
) -> dict:
    """Convert between currencies using open.er-api.com (no API key required)."""
    base = from_currency.strip().upper()
    quote = to_currency.strip().upper()
    if len(base) != 3 or len(quote) != 3:
        return {"error": "Currency codes must be 3-letter ISO codes (e.g. USD, JPY)."}
    if amount < 0:
        return {"error": "Amount must be non-negative."}

    url = f"https://open.er-api.com/v6/latest/{base}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    if data.get("result") != "success":
        return {"error": "Currency exchange API error.", "details": data}

    rates = data.get("rates") or {}
    if quote not in rates:
        return {"error": f"Unsupported currency '{quote}'"}

    rate = rates[quote]
    converted = round(amount * rate, 4)
    return {
        "from": base,
        "to": quote,
        "amount": amount,
        "rate": rate,
        "converted_amount": converted,
        "last_update_utc": data.get("time_last_update_utc"),
        "source": "open.er-api.com (no API key required)",
    }


@app.tool
async def geocode(
    city: Annotated[str, "City name to geocode, e.g. 'Tokyo' or 'Dallas,US'"],
    country_code: Annotated[
        Optional[str], "Optional country code to disambiguate the city"
    ] = None,
    limit: Annotated[int, "Max results to return (1-10)"] = 5,
    language: Annotated[
        Optional[str], "Optional language code for results, e.g. 'en'"
    ] = None,
) -> dict:
    """Geocode a city name to lat/lon using Open-Meteo."""
    normalized_city = city.strip()
    normalized_country_code = country_code.strip().upper() if country_code else None
    if "," in normalized_city:
        parts = [part.strip() for part in normalized_city.split(",") if part.strip()]
        if parts:
            normalized_city = parts[0]
            if not normalized_country_code and len(parts) >= 2:
                candidate = parts[-1]
                if len(candidate) == 2 and candidate.isalpha():
                    normalized_country_code = candidate.upper()

    capped_limit = min(max(limit, 1), 10)
    params = {"name": normalized_city, "count": capped_limit}
    if normalized_country_code:
        params["country"] = normalized_country_code
    if language:
        params["language"] = language.strip()

    geocode_url = "https://geocoding-api.open-meteo.com/v1/search"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(geocode_url, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results") or []
    formatted = []
    for item in results:
        formatted.append(
            {
                "name": item.get("name"),
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
                "country": item.get("country"),
                "country_code": item.get("country_code"),
                "admin1": item.get("admin1"),
                "admin2": item.get("admin2"),
                "timezone": item.get("timezone"),
                "population": item.get("population"),
            }
        )

    return {
        "query": normalized_city,
        "country_code": normalized_country_code,
        "results": formatted,
        "source": "open-meteo.com (no API key required)",
    }


@app.tool
async def weather_forecast(
    latitude: Annotated[float, "Latitude of the location"],
    longitude: Annotated[float, "Longitude of the location"],
    days: Annotated[int, "Number of forecast days (1-16)"] = 7,
    units: Annotated[
        str, "Units system: 'metric' (C, m/s) or 'imperial' (F, mph)."
    ] = "metric",
    timezone: Annotated[
        Optional[str], "IANA timezone for daily buckets; defaults to auto"
    ] = None,
) -> dict:
    """Fetch a multi-day forecast for a lat/lon using Open-Meteo."""
    want_imperial = units.lower().startswith("imp")
    forecast_days = min(max(days, 1), 16)

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": ",".join(
            [
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "precipitation_sum",
                "weathercode",
            ]
        ),
        "forecast_days": forecast_days,
        "timezone": (timezone or "auto").strip(),
    }
    if want_imperial:
        params.update(
            {
                "temperature_unit": "fahrenheit",
                "windspeed_unit": "mph",
                "precipitation_unit": "inch",
            }
        )

    weather_url = "https://api.open-meteo.com/v1/forecast"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(weather_url, params=params)
        resp.raise_for_status()
        data = resp.json()

    daily = data.get("daily") or {}
    daily_units = data.get("daily_units") or {}
    times = daily.get("time") or []
    def value_at(values, idx):
        if not values or idx >= len(values):
            return None
        return values[idx]

    forecast = []
    for idx, day in enumerate(times):
        forecast.append(
            {
                "date": day,
                "temperature_max": value_at(daily.get("temperature_2m_max"), idx),
                "temperature_min": value_at(daily.get("temperature_2m_min"), idx),
                "precipitation_probability_max": value_at(
                    daily.get("precipitation_probability_max"), idx
                ),
                "precipitation_sum": value_at(daily.get("precipitation_sum"), idx),
                "weathercode": value_at(daily.get("weathercode"), idx),
            }
        )

    return {
        "location": {"latitude": latitude, "longitude": longitude},
        "timezone": data.get("timezone"),
        "units": daily_units,
        "forecast": forecast,
        "source": "open-meteo.com (no API key required)",
    }


@app.tool
async def public_holidays(
    country_code: Annotated[str, "2-letter country code, e.g. 'JP'"],
    year: Annotated[Optional[int], "Year, e.g. 2025"] = None,
) -> dict:
    """Fetch public holidays for a given country and year using Nager.Date."""
    code = country_code.strip().upper()
    if len(code) != 2:
        return {"error": "Country code must be a 2-letter ISO code (e.g. JP)."}
    target_year = year or date.today().year
    url = f"https://date.nager.at/api/v3/PublicHolidays/{target_year}/{code}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        holidays = resp.json()

    return {
        "country_code": code,
        "year": target_year,
        "count": len(holidays),
        "holidays": holidays,
        "source": "date.nager.at (no API key required)",
    }

# Run with specific transport
if __name__ == "__main__":
    # Decide transport/host/port from flags or environment so Docker can bind to 0.0.0.0.
    # Default transport keeps the existing "stdio" behaviour.
    transport = "stdio"
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        transport = sys.argv[1]

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8000"))

    # Lightweight flag parsing for --host/--port
    for idx, arg in enumerate(sys.argv[1:], start=1):
        if arg == "--host" and idx + 1 < len(sys.argv):
            host = sys.argv[idx + 1]
        if arg == "--port" and idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    # Run the server
    app.run(transport=transport, host=host, port=port)
