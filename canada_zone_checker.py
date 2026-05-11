#!/usr/bin/env python3
"""Check whether a Canadian location intersects wetland, forest, or protected zones.

The script accepts either a latitude/longitude pair or a place/address string. It
uses public web APIs only and requires no third-party Python packages.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


USER_AGENT = "canada-zone-checker/1.0 (public API example)"

CNWI_MAPSERVER = "https://maps-cartes.ec.gc.ca/arcgis/rest/services/CWS_SCF/CNWI/MapServer"
CPCAD_LAYER = "https://maps-cartes.ec.gc.ca/arcgis/rest/services/CWS_SCF/CPCAD/MapServer/0"
LAND_COVER_MAPSERVER = "https://geoappext.nrcan.gc.ca/arcgis/rest/services/FGP/LandCover_EN/MapServer"
VEGETATION_LAYER = (
    "https://maps-cartes.services.geo.ca/server_serveur/rest/services/"
    "NRCan/vegetation_zones_of_canada_2020_en/MapServer/0"
)
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"


CANADA_BOUNDS = {
    "min_lat": 41.0,
    "max_lat": 84.5,
    "min_lon": -142.0,
    "max_lon": -52.0,
}


LAND_COVER_CLASSES = {
    1: "Temperate or sub-polar needleleaf forest",
    2: "Sub-polar taiga needleleaf forest",
    3: "Tropical or subtropical broadleaf evergreen forest",
    4: "Tropical or subtropical broadleaf deciduous forest",
    5: "Temperate or sub-polar broadleaf deciduous forest",
    6: "Mixed forest",
    7: "Tropical or subtropical shrubland",
    8: "Temperate or sub-polar shrubland",
    9: "Tropical or subtropical grassland",
    10: "Temperate or sub-polar grassland",
    11: "Sub-polar or polar shrubland-lichen-moss",
    12: "Sub-polar or polar grassland-lichen-moss",
    13: "Sub-polar or polar barren-lichen-moss",
    14: "Wetland",
    15: "Cropland",
    16: "Barren lands",
    17: "Urban and built-up",
    18: "Water",
    19: "Snow and ice",
}
FOREST_LAND_COVER_CODES = {1, 2, 3, 4, 5, 6}
WETLAND_LAND_COVER_CODE = 14


@dataclass(frozen=True)
class Location:
    """Resolved user location."""

    label: str
    latitude: float
    longitude: float
    source: str


@dataclass(frozen=True)
class LandCover:
    """A single raster land-cover lookup result."""

    code: int | None
    label: str
    raw_attributes: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class ZoneResult:
    """Result for one zone family."""

    zone: str
    matched: bool | None
    source: str
    details: list[str]
    error: str | None = None


class ZoneCheckError(RuntimeError):
    """Raised when a location cannot be resolved or checked."""


def http_json(url: str, params: dict[str, Any], timeout: int = 30) -> Any:
    """Fetch JSON from an HTTP GET endpoint."""

    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ZoneCheckError(f"HTTP {exc.code} from {url}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise ZoneCheckError(f"Could not reach {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ZoneCheckError(f"Invalid JSON from {url}: {exc}") from exc


def parse_coordinate_pair(text: str) -> tuple[float, float] | None:
    """Parse 'lat, lon' or 'lat lon' coordinates."""

    match = re.fullmatch(
        r"\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*",
        text,
    )
    if not match:
        return None

    latitude = float(match.group(1))
    longitude = float(match.group(2))
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ZoneCheckError("Coordinates must be in 'latitude, longitude' order.")
    return latitude, longitude


def resolve_location(user_input: str) -> Location:
    """Resolve coordinates directly or geocode a Canadian place/address string."""

    coordinate_pair = parse_coordinate_pair(user_input)
    if coordinate_pair:
        latitude, longitude = coordinate_pair
        return Location(
            label=f"{latitude:.6f}, {longitude:.6f}",
            latitude=latitude,
            longitude=longitude,
            source="user-provided coordinates",
        )

    params = {
        "q": user_input,
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "ca",
        "addressdetails": 1,
    }
    matches = http_json(NOMINATIM_SEARCH, params)
    if not matches:
        raise ZoneCheckError(f"Could not geocode a Canadian location for: {user_input!r}")

    best = matches[0]
    return Location(
        label=best.get("display_name", user_input),
        latitude=float(best["lat"]),
        longitude=float(best["lon"]),
        source="OpenStreetMap Nominatim geocoder",
    )


def is_inside_canada_bounds(location: Location) -> bool:
    """Return whether coordinates are within a broad Canada bounding box."""

    return (
        CANADA_BOUNDS["min_lat"] <= location.latitude <= CANADA_BOUNDS["max_lat"]
        and CANADA_BOUNDS["min_lon"] <= location.longitude <= CANADA_BOUNDS["max_lon"]
    )


def arcgis_point(longitude: float, latitude: float) -> str:
    """Build an ArcGIS REST point geometry in WGS84."""

    return json.dumps(
        {
            "x": longitude,
            "y": latitude,
            "spatialReference": {"wkid": 4326},
        }
    )


def arcgis_extent(longitude: float, latitude: float, delta: float = 0.001) -> str:
    """Build a tiny WGS84 map extent around a point for ArcGIS identify calls."""

    return json.dumps(
        {
            "xmin": longitude - delta,
            "ymin": latitude - delta,
            "xmax": longitude + delta,
            "ymax": latitude + delta,
            "spatialReference": {"wkid": 4326},
        }
    )


def query_arcgis_features(
    layer_url: str,
    longitude: float,
    latitude: float,
    out_fields: str,
    record_count: int = 5,
) -> list[dict[str, Any]]:
    """Run a point-in-polygon query against an ArcGIS Feature/MapServer layer."""

    params = {
        "f": "json",
        "where": "1=1",
        "geometry": arcgis_point(longitude, latitude),
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "returnGeometry": "false",
        "resultRecordCount": record_count,
    }
    data = http_json(f"{layer_url}/query", params)
    if "error" in data:
        message = data["error"].get("message", "ArcGIS query error")
        raise ZoneCheckError(message)
    return [feature.get("attributes", {}) for feature in data.get("features", [])]


def identify_arcgis_map(
    mapserver_url: str,
    layer_ids: str,
    longitude: float,
    latitude: float,
    tolerance: int = 3,
) -> list[dict[str, Any]]:
    """Run an ArcGIS MapServer identify request at a point."""

    params = {
        "f": "json",
        "geometry": arcgis_point(longitude, latitude),
        "geometryType": "esriGeometryPoint",
        "sr": 4326,
        "layers": f"all:{layer_ids}",
        "tolerance": tolerance,
        "mapExtent": arcgis_extent(longitude, latitude),
        "imageDisplay": "800,800,96",
        "returnGeometry": "false",
    }
    data = http_json(f"{mapserver_url}/identify", params)
    if "error" in data:
        message = data["error"].get("message", "ArcGIS identify error")
        raise ZoneCheckError(message)
    return data.get("results", [])


def extract_pixel_value(attributes: dict[str, Any]) -> int | None:
    """Extract an integer raster pixel value from ArcGIS identify attributes."""

    for key in ("Pixel Value", "PixelValue", "pixel_value"):
        if key in attributes:
            try:
                return int(float(str(attributes[key])))
            except ValueError:
                return None
    return None


def get_land_cover(longitude: float, latitude: float) -> LandCover:
    """Identify the NRCan land-cover raster class at a point."""

    try:
        results = identify_arcgis_map(LAND_COVER_MAPSERVER, "0", longitude, latitude)
    except ZoneCheckError as exc:
        return LandCover(code=None, label="Unknown", raw_attributes={}, error=str(exc))

    if not results:
        return LandCover(code=None, label="No land-cover pixel returned", raw_attributes={})

    attributes = results[0].get("attributes", {})
    code = extract_pixel_value(attributes)
    label = LAND_COVER_CLASSES.get(code, f"Unrecognized land-cover code {code}")
    return LandCover(code=code, label=label, raw_attributes=attributes)


def check_wetland(
    longitude: float,
    latitude: float,
    land_cover: LandCover,
) -> ZoneResult:
    """Check CNWI detailed wetlands, with NRCan land cover as supplemental evidence."""

    source = (
        "ECCC Canadian National Wetlands Inventory (CNWI) detailed wetland polygons; "
        "supplemented by NRCan Land Cover of Canada raster"
    )
    details: list[str] = []
    matched = False

    try:
        results = identify_arcgis_map(CNWI_MAPSERVER, "1", longitude, latitude, tolerance=5)
    except ZoneCheckError as exc:
        return ZoneResult(
            zone="Wetland",
            matched=None,
            source=source,
            details=details,
            error=str(exc),
        )

    if results:
        matched = True
        attrs = results[0].get("attributes", {})
        wetland_class = attrs.get("CNWI WETLAND CLASS") or attrs.get("value")
        area = attrs.get("WETLAND AREA (m2)") or attrs.get("WETLAND AREA (m²)")
        if wetland_class:
            details.append(f"CNWI wetland class: {wetland_class}")
        if area:
            details.append(f"CNWI wetland area: {area} m2")
        if attrs.get("SOURCE FEATURE ID"):
            details.append(f"Source feature ID: {attrs['SOURCE FEATURE ID']}")
    else:
        details.append("CNWI returned no detailed wetland polygon at the point.")

    if land_cover.code == WETLAND_LAND_COVER_CODE:
        matched = True
        details.append("NRCan land-cover pixel class is Wetland.")
    elif land_cover.error:
        details.append(f"NRCan land-cover lookup unavailable: {land_cover.error}")
    elif land_cover.code is not None:
        details.append(f"NRCan land-cover pixel class: {land_cover.label}.")

    return ZoneResult(zone="Wetland", matched=matched, source=source, details=details)


def check_forest(
    longitude: float,
    latitude: float,
    land_cover: LandCover,
) -> ZoneResult:
    """Check forest land cover, with vegetation zone context as a fallback."""

    source = (
        "NRCan Land Cover of Canada raster; supplemented by NRCan Vegetation Zones "
        "of Canada"
    )
    details: list[str] = []
    matched: bool | None

    if land_cover.error:
        matched = None
        details.append(f"Land-cover lookup unavailable: {land_cover.error}")
    elif land_cover.code is None:
        matched = None
        details.append(land_cover.label)
    else:
        matched = land_cover.code in FOREST_LAND_COVER_CODES
        details.append(f"Land-cover pixel class: {land_cover.label}.")

    try:
        vegetation = query_arcgis_features(
            VEGETATION_LAYER,
            longitude,
            latitude,
            "level_1,level_2",
            record_count=1,
        )
    except ZoneCheckError as exc:
        details.append(f"Vegetation-zone lookup unavailable: {exc}")
        return ZoneResult(zone="Forest land", matched=matched, source=source, details=details)

    if vegetation:
        attrs = vegetation[0]
        level_1 = attrs.get("level_1")
        level_2 = attrs.get("level_2")
        zone_text = " / ".join(str(part) for part in (level_1, level_2) if part)
        if zone_text:
            details.append(f"Vegetation zone context: {zone_text}.")
        if matched is None:
            lowered = zone_text.lower()
            matched = any(word in lowered for word in ("forest", "rainforest", "woodland"))
    else:
        details.append("No vegetation-zone polygon returned at the point.")

    return ZoneResult(zone="Forest land", matched=matched, source=source, details=details)


def check_restricted_area(longitude: float, latitude: float) -> ZoneResult:
    """Check protected/conserved areas as a practical national restricted-area layer."""

    source = "ECCC Canadian Protected and Conserved Areas Database (CPCAD)"
    fields = "NAME_E,TYPE_E,PA_BIOME,OWNER_E,MGMT_E,STATUS"
    try:
        features = query_arcgis_features(
            CPCAD_LAYER,
            longitude,
            latitude,
            fields,
            record_count=5,
        )
    except ZoneCheckError as exc:
        return ZoneResult(
            zone="Restricted/protected area",
            matched=None,
            source=source,
            details=[],
            error=str(exc),
        )

    if not features:
        return ZoneResult(
            zone="Restricted/protected area",
            matched=False,
            source=source,
            details=["CPCAD returned no protected or conserved area at the point."],
        )

    details = []
    for attrs in features:
        name = attrs.get("NAME_E") or "Unnamed area"
        area_type = attrs.get("TYPE_E") or attrs.get("PA_BIOME") or "protected/conserved area"
        manager = attrs.get("MGMT_E")
        owner = attrs.get("OWNER_E")
        line = f"{name} ({area_type})"
        if manager:
            line += f", managed by {manager}"
        elif owner:
            line += f", owner: {owner}"
        details.append(line)

    return ZoneResult(
        zone="Restricted/protected area",
        matched=True,
        source=source,
        details=details,
    )


def check_location(location: Location) -> list[ZoneResult]:
    """Run all zone checks for a resolved location."""

    land_cover = get_land_cover(location.longitude, location.latitude)
    return [
        check_wetland(location.longitude, location.latitude, land_cover),
        check_forest(location.longitude, location.latitude, land_cover),
        check_restricted_area(location.longitude, location.latitude),
    ]


def status_word(matched: bool | None) -> str:
    """Convert tri-state match status to printable text."""

    if matched is True:
        return "YES"
    if matched is False:
        return "NO"
    return "UNKNOWN"


def print_report(location: Location, results: list[ZoneResult]) -> None:
    """Print a human-readable report."""

    print("Location")
    print(f"  Label: {location.label}")
    print(f"  Coordinates: {location.latitude:.6f}, {location.longitude:.6f}")
    print(f"  Resolved by: {location.source}")
    if not is_inside_canada_bounds(location):
        print("  Warning: coordinates are outside a broad Canada bounding box.")

    print("\nZone checks")
    for result in results:
        print(f"- {result.zone}: {status_word(result.matched)}")
        print(f"  Source: {result.source}")
        if result.error:
            print(f"  Error: {result.error}")
        for detail in result.details:
            print(f"  Detail: {detail}")

    print(
        "\nNote: CPCAD represents protected and conserved areas, which is a useful "
        "national proxy for restricted/protected lands. Legal access restrictions can "
        "also come from local, provincial, military, airport, or private-property rules "
        "that are not all represented in one national open dataset."
    )


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Check whether a Canadian location is in a wetland, forest land-cover "
            "class, or protected/conserved area."
        )
    )
    parser.add_argument(
        "location",
        nargs="?",
        help="Address/place in Canada, or coordinates as 'latitude, longitude'.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""

    args = build_parser().parse_args(argv)
    user_location = args.location or input(
        "Enter a Canadian address/place or coordinates as 'latitude, longitude': "
    ).strip()
    if not user_location:
        print("No location provided.", file=sys.stderr)
        return 2

    try:
        location = resolve_location(user_location)
        results = check_location(location)
    except ZoneCheckError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print_report(location, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
