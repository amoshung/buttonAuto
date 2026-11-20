"""U5：team/weather.py 即時天氣代理、30 分鐘快取與斷線降級測試。"""
import pytest

from team.weather import WeatherService, hemisphere_for_lat, wmo_to_condition


class _FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_wmo_and_hemisphere_helpers():
    assert wmo_to_condition(0) == "clear"
    assert wmo_to_condition(3) == "cloudy"
    assert wmo_to_condition(61) == "rain"
    assert wmo_to_condition(73) == "snow"
    assert hemisphere_for_lat(22.6) == "north"
    assert hemisphere_for_lat(-33.8) == "south"


def test_weather_by_coords_and_30min_cache():
    calls = []
    now = [1000.0]

    def fake_get(url, params=None, timeout=4.0):
        calls.append((url, params))
        return _FakeResp(
            200,
            {"current": {"temperature_2m": 26.5, "weather_code": 61, "is_day": 1}},
        )

    svc = WeatherService(ttl_seconds=1800, http_get=fake_get, clock=lambda: now[0])
    r1 = svc.get_weather(lat=22.6273, lon=120.3014)
    assert r1["ok"] is True
    assert r1["degraded"] is False
    assert r1["cached"] is False
    assert r1["condition"] == "rain"
    assert r1["temperature_c"] == 26.5
    assert r1["hemisphere"] == "north"
    assert len(calls) == 1

    # 30 分鐘內命中快取
    now[0] += 600.0
    r2 = svc.get_weather(lat=22.6273, lon=120.3014)
    assert r2["cached"] is True
    assert len(calls) == 1

    # 超過 30 分鐘重新抓取
    now[0] += 1300.0
    r3 = svc.get_weather(lat=22.6273, lon=120.3014)
    assert r3["cached"] is False
    assert len(calls) == 2


def test_weather_city_geocode_and_south_hemisphere():
    def fake_get(url, params=None, timeout=4.0):
        if "geocoding-api" in url:
            return _FakeResp(
                200,
                {"results": [{"latitude": -33.8688, "longitude": 151.2093, "name": "Sydney", "country": "Australia"}]},
            )
        return _FakeResp(
            200,
            {"current": {"temperature_2m": 18.0, "weather_code": 0, "is_day": 1}},
        )

    svc = WeatherService(http_get=fake_get)
    res = svc.get_weather(city="Sydney")
    assert res["degraded"] is False
    assert res["hemisphere"] == "south"
    assert res["condition"] == "clear"
    assert "Sydney" in res["city"]


def test_weather_offline_graceful_degradation():
    def fail_get(url, params=None, timeout=4.0):
        raise ConnectionError("corporate firewall blocked")

    svc = WeatherService(http_get=fail_get)
    res = svc.get_weather(lat=25.03, lon=121.56)
    assert res["ok"] is True
    assert res["degraded"] is True
    assert res["hemisphere"] == "north"
    assert res["condition"] == "clear"


def test_weather_invalid_coords_raises_value_error():
    svc = WeatherService(http_get=lambda *a, **k: None)
    with pytest.raises(ValueError):
        svc.get_weather(lat=999, lon=120)
    with pytest.raises(ValueError):
        svc.get_weather(lat=22.6, lon=None)
