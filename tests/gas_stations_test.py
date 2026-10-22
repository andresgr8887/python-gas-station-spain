# pylint: disable=protected-access,unused-argument,missing-function-docstring,missing-class-docstring,redefined-outer-name
"""Deterministic tests of Spain's official fuel data API (no live HTTP)."""

from unittest.mock import AsyncMock

import pytest

import gas_station_spain_api as gss
from gas_station_spain_api import gas_station as api


STATION = {
    "IDEESS": "6738",
    "Rótulo": "PLENOIL",
    "Dirección": "CALLE PRUEBA",
    "Provincia": "ALMERÍA",
    "Municipio": "RIOJA",
    "Latitud": "36,840",
    "Longitud_x0020__x0028_WGS84_x0029_": "-2,467",
    "Precio_x0020_Gasolina_x0020_95_x0020_E5": "1,459",
    "Precio_x0020_Gasoleo_x0020_A": "1,389",
    "Precio_x0020_Gasoleo_x0020_Premium": "",
    "IDMunicipio": "355",
    "IDProvincia": "04",
}

ALICANTE = {
    **STATION,
    "IDEESS": "9131",
    "Provincia": "ALICANTE",
    "Municipio": "SAN FULGENCIO",
    "IDMunicipio": "256",
    "IDProvincia": "03",
}


@pytest.fixture
def ministry_data(monkeypatch):
    """Serve realistic API structures entirely from memory."""
    results = {
        f"{api.STATIONS_URL}/FiltroProvincia/04": {"ListaEESSPrecio": [STATION]},
        f"{api.STATIONS_URL}/FiltroProvincia/03": {"ListaEESSPrecio": [ALICANTE]},
        f"{api.STATIONS_URL}/FiltroMunicipio/355": {"ListaEESSPrecio": [STATION]},
        f"{api.STATIONS_URL}/FiltroMunicipio/256": {"ListaEESSPrecio": [ALICANTE]},
        f"{api.LISTS_URL}/Provincias/": [
            {"IDPovincia": "04", "Provincia": "ALMERÍA"},
            {"IDPovincia": "03", "Provincia": "ALICANTE"},
        ],
        f"{api.LISTS_URL}/MunicipiosPorProvincia/04": [
            {"IDMunicipio": "355", "Municipio": "RIOJA"}
        ],
    }
    fetch = AsyncMock(side_effect=lambda url: results[url])
    monkeypatch.setattr(api, "_cached_json", fetch)
    return fetch


@pytest.mark.asyncio
async def test_get_by_province(ministry_data):
    """A province URL retains the official two-digit province identifier."""
    stations = await gss.get_gas_stations(province_id=4)
    assert len(stations) == 1
    assert stations[0].province == "ALMERÍA"
    ministry_data.assert_awaited_with(f"{api.STATIONS_URL}/FiltroProvincia/04")


@pytest.mark.asyncio
async def test_second_province(ministry_data):
    """Province 03 does not get mixed with province 04."""
    stations = await gss.get_gas_stations(province_id=3)
    assert [s.id for s in stations] == [9131]


@pytest.mark.asyncio
async def test_get_by_municipality(ministry_data):
    """Filter by official municipality identifier."""
    stations = await gss.get_gas_stations(province_id=4, municipality_id=355)
    assert [s.id for s in stations] == [6738]


@pytest.mark.asyncio
async def test_product_filter_and_price(ministry_data):
    """Filter stations to fuels having a reported price."""
    available = await gss.get_gas_stations(municipality_id=355, product_id=1)
    missing = await gss.get_gas_stations(municipality_id=355, product_id=5)
    assert len(available) == 1
    assert not missing
    assert await gss.get_price(6738, 1, municipality_id=355) == 1.459
    assert await gss.get_price(6738, 4, municipality_id=355) == 1.389
    assert await gss.get_price(6738, 5, municipality_id=355) is None


@pytest.mark.asyncio
async def test_provinces(ministry_data):
    """Convert and retain province IDs and Spanish names."""
    provinces = await gss.get_provinces()
    assert [(p.id, p.name) for p in provinces] == [
        ("04", "Almería"),
        ("03", "Alicante"),
    ]


@pytest.mark.asyncio
async def test_municipalities(ministry_data):
    """Convert the official municipality list without string truncation."""
    cities = await gss.get_municipalities("04")
    assert [(c.id, c.name) for c in cities] == [("355", "Rioja")]


@pytest.mark.asyncio
async def test_station_lookup_and_coordinates(ministry_data):
    """Station IDs and signed coordinates survive parsing unchanged."""
    station = await gss.get_gas_station(6738, municipality_id=355)
    assert station.id == 6738
    assert station.latitude == pytest.approx(36.840)
    assert station.longitude == pytest.approx(-2.467)


@pytest.mark.asyncio
async def test_missing_station_does_not_use_nearby_price(ministry_data):
    """Never assign a different station's fuel price to the requested ID."""
    with pytest.raises(LookupError, match="9999"):
        await gss.get_price(9999, 1, municipality_id=355)


@pytest.mark.asyncio
async def test_disallow_unbounded_national_download(ministry_data):
    """Without a geographical filter, do not query all of Spain."""
    with pytest.raises(ValueError, match="municipality_id"):
        await gss.get_price(6738, 1)
    with pytest.raises(ValueError, match="municipality_id"):
        await gss.get_gas_station(6738)
    with pytest.raises(ValueError, match="municipality_id or province_id"):
        await gss.get_gas_stations()
    ministry_data.assert_not_awaited()


def test_products_keep_existing_ids():
    """Keep original product IDs for HA's existing saved config entries."""
    ids = [p.id for p in gss.get_products()]
    assert {1, 3, 4, 5, 17, 26}.issubset(ids)
