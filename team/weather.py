"""免金鑰即時天氣代理與 30 分鐘快取降級服務（U5）。

- 使用 Open-Meteo（免金鑰）查詢即時天氣與地理編碼，未提供座標時嘗試免費 IP 定位。
- 依量化座標／城市名快取 30 分鐘（預設 1800 秒），避免頻繁外連。
- 企業內網斷線、防火牆阻擋或 TLS 異常時一律優雅降級回傳 ``degraded: True``，絕不阻塞或回傳 500。
"""
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 1800.0
DEFAULT_LAT = 22.6273
DEFAULT_LON = 120.3014
DEFAULT_CITY = "高雄（預設）"

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
IP_GEO_URL = "https://get.geojs.io/v1/ip/geo.json"


def wmo_to_condition(code: Optional[int]) -> str:
    """將 WMO weather_code 標準化為 clear / cloudy / rain / snow。"""
    if code is None:
        return "clear"
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "clear"
    if c in (0, 1):
        return "clear"
    if c in (2, 3, 45, 48):
        return "cloudy"
    if c in (71, 73, 75, 77, 85, 86):
        return "snow"
    if (51 <= c <= 67) or (80 <= c <= 82) or (95 <= c <= 99):
        return "rain"
    return "cloudy"


def hemisphere_for_lat(lat: float) -> str:
    return "south" if lat < 0 else "north"


def _parse_coord(raw: Any, name: str, lo: float, hi: float) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必須是有效數字") from exc
    if not (lo <= val <= hi):
        raise ValueError(f"{name} 超出有效範圍（{lo} ~ {hi}）")
    return val


class WeatherService:
    """具 30 分鐘 TTL 快取與斷線降級的即時天氣代理。"""

    def __init__(
        self,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        timeout: float = 4.0,
        http_get: Optional[Callable[..., Any]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._timeout = float(timeout)
        self._get = http_get or requests.get
        self._now = clock or time.time
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def _fetch_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self._get(url, params=params, timeout=self._timeout)
        if getattr(resp, "status_code", 0) != 200:
            raise RuntimeError(f"HTTP {getattr(resp, 'status_code', 0)}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("非預期 JSON 格式")
        return data

    def _resolve_city(self, city: str) -> Tuple[float, float, str]:
        data = self._fetch_json(
            OPEN_METEO_GEOCODE_URL,
            params={"name": city, "count": 1, "language": "zh", "format": "json"},
        )
        results = data.get("results") or []
        if not results or not isinstance(results[0], dict):
            raise RuntimeError(f"找不到城市：{city}")
        top = results[0]
        lat = float(top["latitude"])
        lon = float(top["longitude"])
        name = str(top.get("name") or city)
        country = str(top.get("country") or "")
        label = f"{name}, {country}" if country and country != name else name
        return lat, lon, label

    def _resolve_ip_location(self) -> Tuple[float, float, str]:
        data = self._fetch_json(IP_GEO_URL)
        lat = float(data["latitude"])
        lon = float(data["longitude"])
        city = str(data.get("city") or data.get("region") or data.get("country") or "本機位置")
        return lat, lon, city

    def _cache_key(self, lat: Optional[float], lon: Optional[float], city: str) -> str:
        if lat is not None and lon is not None:
            return f"coord:{round(lat, 2):.2f},{round(lon, 2):.2f}"
        if city:
            return f"city:{city.strip().lower()}"
        return "auto:ip"

    def _degraded_response(
        self,
        key: str,
        lat: Optional[float],
        lon: Optional[float],
        city: str,
        reason: str,
    ) -> Dict[str, Any]:
        cached = self._cache.get(key)
        if cached is not None:
            stale = dict(cached[1])
            stale["degraded"] = True
            stale["stale"] = True
            stale["message"] = f"使用上次快取（上游無法連線：{reason[:120]}）"
            return stale
        use_lat = DEFAULT_LAT if lat is None else lat
        use_lon = DEFAULT_LON if lon is None else lon
        return {
            "ok": True,
            "degraded": True,
            "stale": False,
            "cached": False,
            "lat": round(use_lat, 4),
            "lon": round(use_lon, 4),
            "city": city or DEFAULT_CITY,
            "hemisphere": hemisphere_for_lat(use_lat),
            "condition": "clear",
            "weather_code": None,
            "temperature_c": None,
            "is_day": None,
            "fetched_at": None,
            "message": f"離線降級模式：{reason[:120]}",
        }

    def get_weather(
        self,
        lat: Any = None,
        lon: Any = None,
        city: str = "",
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """查詢即時天氣；參數錯誤拋 ValueError，網路／上游錯誤則回傳降級結果。"""
        parsed_lat = _parse_coord(lat, "lat", -90.0, 90.0)
        parsed_lon = _parse_coord(lon, "lon", -180.0, 180.0)
        if (parsed_lat is None) != (parsed_lon is None):
            raise ValueError("lat 與 lon 必須同時提供或同時省略")
        city_clean = (city or "").strip()

        key = self._cache_key(parsed_lat, parsed_lon, city_clean)
        now = self._now()
        if not force_refresh and key in self._cache:
            ts, payload = self._cache[key]
            if (now - ts) < self._ttl:
                hit = dict(payload)
                hit["cached"] = True
                return hit

        try:
            if parsed_lat is not None and parsed_lon is not None:
                use_lat, use_lon = parsed_lat, parsed_lon
                label = city_clean or f"{use_lat:.2f}, {use_lon:.2f}"
            elif city_clean:
                use_lat, use_lon, label = self._resolve_city(city_clean)
            else:
                use_lat, use_lon, label = self._resolve_ip_location()

            wx = self._fetch_json(
                OPEN_METEO_FORECAST_URL,
                params={
                    "latitude": f"{use_lat:.4f}",
                    "longitude": f"{use_lon:.4f}",
                    "current": "temperature_2m,weather_code,is_day",
                    "timezone": "auto",
                },
            )
            cur = wx.get("current") or {}
            wcode = cur.get("weather_code")
            temp_c = cur.get("temperature_2m")
            is_day = cur.get("is_day")
            result: Dict[str, Any] = {
                "ok": True,
                "degraded": False,
                "stale": False,
                "cached": False,
                "lat": round(use_lat, 4),
                "lon": round(use_lon, 4),
                "city": label,
                "hemisphere": hemisphere_for_lat(use_lat),
                "condition": wmo_to_condition(wcode),
                "weather_code": wcode,
                "temperature_c": float(temp_c) if temp_c is not None else None,
                "is_day": bool(is_day) if is_day is not None else None,
                "fetched_at": int(now),
                "message": "即時天氣已更新",
            }
            self._cache[key] = (now, result)
            return dict(result)
        except Exception as exc:
            log.debug("天氣查詢降級：%s", exc)
            return self._degraded_response(key, parsed_lat, parsed_lon, city_clean, str(exc))
