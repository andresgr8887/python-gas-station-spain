"""Read Spanish fuel prices from the Ministry's public REST API.

All methods preserve the original gas_station_spain_api public surface.
Responses are cached by geographical filter so several HA sensors share requests.
"""

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import ssl
import time
from typing import Any

import aiohttp

from .exceptions import GasStationServerUnavailableException


BASE_URL = (
    "https://sedeaplicaciones.minetur.gob.es/"
    "ServiciosRESTCarburantes/PreciosCarburantes"
)
STATIONS_URL = f"{BASE_URL}/EstacionesTerrestres"
LISTS_URL = f"{BASE_URL}/Listados"
CACHE_SECONDS = 3600
_EXTRA_CA_ENV = "GAS_STATION_SPAIN_EXTRA_CA"
_EXTRA_CA_FILENAME = "ACComponentesInformaticos.pem"
_DEFAULT_CERT_PATH = Path(__file__).resolve().parent / "certs" / _EXTRA_CA_FILENAME


def _build_ssl_context() -> ssl.SSLContext:
    """Use system CA trust, with an optional supplemental certificate."""
    context = ssl.create_default_context()
    candidates = []
    if os.environ.get(_EXTRA_CA_ENV):
        candidates.append(Path(os.environ[_EXTRA_CA_ENV]).expanduser())
    if _DEFAULT_CERT_PATH.exists():
        candidates.append(_DEFAULT_CERT_PATH)
    for candidate in candidates:
        try:
            context.load_verify_locations(cafile=str(candidate))
        except OSError:
            continue
    return context


_SSL_CONTEXT = _build_ssl_context()


def _create_session(headers: dict[str, str]) -> aiohttp.ClientSession:
    """Create a bounded session; _fetch_json always closes it."""
    return aiohttp.ClientSession(
        headers=headers,
        connector=aiohttp.TCPConnector(ssl=_SSL_CONTEXT),
        timeout=aiohttp.ClientTimeout(total=25),
    )


@dataclass
class Product:
    """Petroleum product."""
    id: int
    name: str
    code: str


@dataclass
class Province:
    """Province."""
    id: str
    name: str


@dataclass
class Municipality:
    """Municipality."""
    id: str
    name: str


_PRODUCTS = {
    1: Product(id=1, name="Gasolina 95 E5", code="Gasolina95E5"),
    3: Product(id=3, name="Gasolina 98 E5", code="Gasolina98E5"),
    4: Product(id=4, name="Gasóleo A habitual", code="GasoleoA"),
    5: Product(id=5, name="Gasóleo Premium", code="GasoleoPremium"),
    6: Product(id=6, name="Gasóleo B", code="GasoleoB"),
    7: Product(id=7, name="Gasóleo C", code="GasoleoC"),
    8: Product(id=8, name="Biodiésel", code="Biodiesel"),
    16: Product(id=16, name="Bioetanol", code="Biotanol"),
    17: Product(id=17, name="Gases licuados del petróleo (GLP)", code="GasesLicuados"),
    18: Product(id=18, name="Gas natural comprimido", code="GasNatComp"),
    19: Product(id=19, name="Gas natural licuado", code="GasNatLicuado"),
    20: Product(id=20, name="Gasolina 95 E5 Premium", code="Gasolina95E5Premium"),
    21: Product(id=21, name="Gasolina 98 E10", code="Gasolina98E10"),
    22: Product(id=22, name="Hidrógeno", code="H2"),
    23: Product(id=23, name="Gasolina 95 E10", code="Gasolina95E10"),
    24: Product(id=24, name="Gasolina 95 E25", code="Gasolina95E25"),
    25: Product(id=25, name="Gasolina 95 E85", code="Gasolina95E85"),
    26: Product(id=26, name="Adblue", code="Adb"),
    27: Product(id=27, name="Diésel renovable", code="Dren"),
    28: Product(id=28, name="Gasolina renovable", code="Gren"),
    29: Product(id=29, name="Metanol", code="Met"),
    30: Product(id=30, name="Amoniaco", code="Amo"),
    31: Product(id=31, name="Biogas natural comprimido", code="Bgnc"),
    32: Product(id=32, name="Biogas natural licuado", code="Bgnl"),
}


# Public REST data fields do not match the historic Geoportal's API keys.
PRODUCT_FIELDS = {
    1: "Precio Gasolina 95 E5",
    3: "Precio Gasolina 98 E5",
    4: "Precio Gasoleo A",
    5: "Precio Gasoleo Premium",
    6: "Precio Gasoleo B",
    7: "Precio Gasoleo C",
    8: "Precio Biodiesel",
    16: "Precio Bioetanol",
    17: "Precio Gases licuados del petróleo",
    18: "Precio Gas Natural Comprimido",
    19: "Precio Gas Natural Licuado",
    20: "Precio Gasolina 95 E5 Premium",
    21: "Precio Gasolina 98 E10",
    22: "Precio Hidrogeno",
    23: "Precio Gasolina 95 E10",
    24: "Precio Gasolina 95 E25",
    25: "Precio Gasolina 95 E85",
    26: "Precio Adblue",
    27: "Precio Diésel Renovable",
    28: "Precio Gasolina Renovable",
    29: "Precio Metanol",
    30: "Precio Amoniaco",
    31: "Precio Biogas Natural Comprimido",
    32: "Precio Biogas Natural Licuado",
}

# Entries are shared between coordinators within the same HA event loop.
_cache: dict[str, tuple[float, Any]] = {}
_cache_locks: dict[str, asyncio.Lock] = {}


def _decimal(value: Any) -> float | None:
    """Parse numbers from the API's Spanish decimal notation."""
    if value is None or str(value).strip() in ("", "-"):
        return None
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


@dataclass
class GasStation:
    """Station information; station IDs are preserved across HA upgrades."""
    id: int
    marquee: str
    address: str
    province: str
    latitude: float
    longitude: float
    municipality: str

    @staticmethod
    def from_list(data: dict) -> "GasStation":
        """Parse the Ministry's station fields."""
        return GasStation(
            id=int(data["IDEESS"]),
            marquee=str(data.get("Rótulo") or "").strip().title(),
            address=str(data.get("Dirección") or "").strip(),
            province=str(data.get("Provincia") or "").strip(),
            latitude=_decimal(data.get("Latitud")),
            longitude=_decimal(data.get("Longitud (WGS84)")),
            municipality=str(data.get("Municipio") or "").strip().title(),
        )

    @staticmethod
    def from_individual(data: dict) -> "GasStation":
        """Keep the former API's entry point for callers."""
        return GasStation.from_list(data)


async def _fetch_json(url: str) -> Any:
    """Fetch JSON, closing the HTTP response and session even on exceptions.

    Retry timeouts, connection errors and HTTP 5xx; never retry HTTP 4xx.
    """
    for attempt in range(3):
        session = _create_session({"Accept": "application/json"})
        try:
            response = await session.get(url)
            try:
                if response.status >= 500:
                    raise GasStationServerUnavailableException(response.status)
                response.raise_for_status()
                return await response.json()
            finally:
                response.release()
        except (GasStationServerUnavailableException, aiohttp.ClientConnectionError, asyncio.TimeoutError):
            if attempt == 2:
                raise
            await asyncio.sleep(2)
        finally:
            await session.close()
    raise RuntimeError("Unexpected retry state")


async def _cached_json(url: str) -> Any:
    """Deduplicate in-flight requests and cache only successful responses."""
    cached = _cache.get(url)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    lock = _cache_locks.setdefault(url, asyncio.Lock())
    async with lock:
        cached = _cache.get(url)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        data = await _fetch_json(url)
        _cache[url] = (time.monotonic(), data)
        return data


async def _station_records(
    province_id: int | None = None,
    municipality_id: int | None = None,
) -> list[dict]:
    if municipality_id is not None:
        url = f"{STATIONS_URL}/FiltroMunicipio/{int(municipality_id)}"
    elif province_id is not None:
        url = f"{STATIONS_URL}/FiltroProvincia/{int(province_id)}"
    else:
        url = f"{STATIONS_URL}/"
    data = await _cached_json(url)
    if not isinstance(data, dict) or not isinstance(data.get("ListaEESSPrecio"), list):
        raise ValueError("Unexpected Ministry station response")
    return data["ListaEESSPrecio"]


def _get_product_price(data: dict, product_id: int) -> float | None:
    field = PRODUCT_FIELDS[int(product_id)]
    return _decimal(data.get(field))


async def get_gas_stations(
    province_id: int | None = None,
    municipality_id: int | None = None,
    product_id: int | None = None,
) -> list[GasStation]:
    """Filter stations; use the same cached municipality data for all products."""
    records = await _station_records(province_id, municipality_id)
    if product_id is not None:
        records = [r for r in records if _get_product_price(r, product_id) is not None]
    return [GasStation.from_list(record) for record in records]


async def get_gas_station(
    station_id: int, municipality_id: int | None = None,
) -> GasStation:
    """Read a station using its municipality, or fall back to the national list."""
    records = await _station_records(municipality_id=municipality_id)
    match = next((r for r in records if int(r["IDEESS"]) == int(station_id)), None)
    if match is None:
        raise LookupError(f"Station {station_id} was not found in the Ministry data")
    return GasStation.from_individual(match)


async def get_price(
    station_id: int, product_id: int, municipality_id: int | None = None,
) -> float | None:
    """Price in euros; None means the product is absent or unpriced."""
    records = await _station_records(municipality_id=municipality_id)
    match = next((r for r in records if int(r["IDEESS"]) == int(station_id)), None)
    if match is None:
        raise LookupError(f"Station {station_id} was not found in the Ministry data")
    return _get_product_price(match, product_id)


def get_products() -> list[Product]:
    return list(_PRODUCTS.values())


async def get_provinces() -> list[Province]:
    payload = await _cached_json(f"{LISTS_URL}/Provincias/")
    if not isinstance(payload, list):
        raise ValueError("Unexpected Ministry province list")
    return [
        Province(id=str(p.get("IDPovincia", p.get("IDProvincia"))), name=p["Provincia"].title())
        for p in payload
    ]


async def get_municipalities(id_province: str) -> list[Municipality]:
    payload = await _cached_json(f"{LISTS_URL}/MunicipiosPorProvincia/{int(id_province)}")
    if not isinstance(payload, list):
        raise ValueError("Unexpected Ministry municipality list")
    return [
        Municipality(id=str(m["IDMunicipio"]), name=m["Municipio"].strip().title())
        for m in payload
    ]
