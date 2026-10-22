# pylint: disable=protected-access,unused-argument,missing-function-docstring,missing-class-docstring
"""Offline regression tests for the Ministry's official fuel prices format."""

import pytest

from gas_station_spain_api import gas_station as api


STATION = {
    "IDEESS": "6738",
    "Rótulo": "PLENOIL",
    "Dirección": "CALLE PRUEBA",
    "Provincia": "SEVILLA",
    "Municipio": "SAN JOSÉ DE LA RINCONADA",
    "Latitud": "37,50223",
    "Longitud_x0020__x0028_WGS84_x0029_": "-5,98247",
    "Precio_x0020_Gasolina_x0020_95_x0020_E5": "1,459",
    "Precio_x0020_Gasoleo_x0020_A": "",
}


def test_parse_ministry_station_and_spanish_decimal():
    station = api.GasStation.from_list(STATION)
    assert station.id == 6738
    assert station.latitude == pytest.approx(37.50223)
    assert station.longitude == pytest.approx(-5.98247)
    assert api._get_product_price(STATION, 1) == pytest.approx(1.459)
    assert api._get_product_price(STATION, 4) is None


@pytest.mark.asyncio
async def test_station_lookup_and_product_filter_share_municipality(monkeypatch):
    urls = []

    async def cached(url):
        urls.append(url)
        return {"ListaEESSPrecio": [STATION]}

    monkeypatch.setattr(api, "_cached_json", cached)
    station = await api.get_gas_station(6738, municipality_id=1234)
    assert station.id == 6738
    assert await api.get_price(6738, 1, municipality_id=1234) == pytest.approx(1.459)
    assert await api.get_price(6738, 4, municipality_id=1234) is None
    assert len(await api.get_gas_stations(municipality_id=1234, product_id=1)) == 1
    assert len(await api.get_gas_stations(municipality_id=1234, product_id=4)) == 0
    assert all("/FiltroMunicipio/1234" in url for url in urls)


@pytest.mark.asyncio
async def test_missing_station_is_not_replaced_by_another(monkeypatch):
    async def cached(url):
        return {"ListaEESSPrecio": [STATION]}

    monkeypatch.setattr(api, "_cached_json", cached)
    with pytest.raises(LookupError, match="9999"):
        await api.get_price(9999, 1, municipality_id=1234)


@pytest.mark.asyncio
async def test_session_closes_on_unexpected_json(monkeypatch):
    """Parsing failures must close the session, not leak aiohttp connectors."""

    class Response:
        status = 200
        released = False

        async def json(self):
            raise ValueError("HTML instead of JSON")

        def raise_for_status(self):
            return None

        def release(self):
            self.released = True

    class Session:
        def __init__(self):
            self.response = Response()
            self.closed = False

        async def get(self, url):
            return self.response

        async def close(self):
            self.closed = True

    session = Session()
    monkeypatch.setattr(api, "_create_session", lambda headers: session)
    with pytest.raises(ValueError, match="HTML"):
        await api._fetch_json("https://example.invalid/api")
    assert session.closed
    assert session.response.released


@pytest.mark.asyncio
async def test_deduplicates_cached_requests(monkeypatch):
    api._cache.clear()
    api._cache_locks.clear()
    calls = []

    async def fetch(url):
        calls.append(url)
        return {"ListaEESSPrecio": [STATION]}

    monkeypatch.setattr(api, "_fetch_json", fetch)
    first = await api._cached_json("test-cache-url")
    second = await api._cached_json("test-cache-url")
    assert first == second
    assert len(calls) == 1


def test_decoded_field_compatibility_and_invalid_prices():
    """Validate historic decoded keys and reject nonnumeric prices."""
    assert api._get_product_price({"Precio Gasolina 95 E5": "1,629"}, 1) == 1.629
    assert api._get_product_price({}, 1) is None
    assert api._decimal("no disponible") is None
    assert api._decimal(None) is None
