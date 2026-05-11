#!/usr/bin/env python3
"""Check whether an Ontario location intersects wetland, wooded, or protected zones.

The script accepts either a latitude/longitude pair or a place/address string.
It uses Ontario GeoHub/Land Information Ontario public APIs and requires no
third-party Python packages.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any


USER_AGENT = "ontario-zone-checker/1.0 (public API example)"

LIO_OPEN01_MAPSERVER = (
    "https://ws.lioservices.lrc.gov.on.ca/arcgis2/rest/services/"
    "LIO_OPEN_DATA/LIO_Open01/MapServer"
)
LIO_OPEN03_MAPSERVER = (
    "https://ws.lioservices.lrc.gov.on.ca/arcgis2/rest/services/"
    "LIO_OPEN_DATA/LIO_Open03/MapServer"
)
LIO_OPEN05_MAPSERVER = (
    "https://ws.lioservices.lrc.gov.on.ca/arcgis2/rest/services/"
    "LIO_OPEN_DATA/LIO_Open05/MapServer"
)
LIO_OPEN07_MAPSERVER = (
    "https://ws.lioservices.lrc.gov.on.ca/arcgis2/rest/services/"
    "LIO_OPEN_DATA/LIO_Open07/MapServer"
)
ONTARIO_WETLAND_LAYER = f"{LIO_OPEN01_MAPSERVER}/15"
ONTARIO_CONSERVATION_RESERVE_LAYER = f"{LIO_OPEN03_MAPSERVER}/2"
ONTARIO_PROVINCIAL_PARK_LAYER = f"{LIO_OPEN03_MAPSERVER}/4"
ONTARIO_ANSI_LAYER = f"{LIO_OPEN05_MAPSERVER}/3"
ONTARIO_CROWN_GAME_PRESERVE_LAYER = f"{LIO_OPEN05_MAPSERVER}/7"
ONTARIO_WOODED_AREA_LAYER = f"{LIO_OPEN07_MAPSERVER}/29"
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"


ONTARIO_BOUNDS = {
    "min_lat": 41.0,
    "max_lat": 57.5,
    "min_lon": -96.0,
    "max_lon": -74.0,
}


@dataclass(frozen=True)
class Location:
    """Resolved user location or user-provided polygon."""

    label: str
    latitude: float
    longitude: float
    source: str
    polygon_vertices: tuple[tuple[float, float], ...] | None = None

    @property
    def is_polygon(self) -> bool:
        """Return whether this input represents an area instead of a point."""

        return self.polygon_vertices is not None


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


def http_json(
    url: str,
    params: dict[str, Any],
    timeout: int = 60,
    method: str = "GET",
    retries: int = 2,
) -> Any:
    """Fetch JSON from an HTTP endpoint with small retries for transient timeouts."""

    query = urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    method = method.upper()
    data = None
    request_url = url
    if method == "POST":
        data = query.encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        request_url = f"{url}?{query}"

    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(request_url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return json.loads(response.read().decode(charset))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code < 500 or attempt == retries:
                raise ZoneCheckError(f"HTTP {exc.code} from {url}: {body[:300]}") from exc
            last_error = exc
        except (TimeoutError, socket.timeout) as exc:
            last_error = exc
            if attempt == retries:
                raise ZoneCheckError(
                    f"Timed out while reading from {url} after {retries + 1} attempts"
                ) from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt == retries:
                raise ZoneCheckError(f"Could not reach {url}: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ZoneCheckError(f"Invalid JSON from {url}: {exc}") from exc

        time.sleep(1.5 * (attempt + 1))

    raise ZoneCheckError(f"Request failed for {url}: {last_error}")


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


def parse_polygon_coordinates(text: str) -> tuple[tuple[float, float], ...]:
    """Parse semicolon- or newline-separated 'lat, lon' polygon vertices."""

    parts = [part.strip() for part in re.split(r"[;\n|]+", text) if part.strip()]
    if len(parts) < 3:
        raise ZoneCheckError("A polygon needs at least three coordinate pairs.")

    vertices: list[tuple[float, float]] = []
    for part in parts:
        coordinate_pair = parse_coordinate_pair(part)
        if coordinate_pair is None:
            raise ZoneCheckError(
                "Polygon coordinates must use 'lat, lon' pairs separated by semicolons."
            )
        vertices.append(coordinate_pair)

    unique_vertices = set(vertices)
    if len(unique_vertices) < 3:
        raise ZoneCheckError("A polygon needs at least three unique vertices.")

    if vertices[0] == vertices[-1]:
        vertices.pop()
    return tuple(vertices)


def polygon_center(vertices: tuple[tuple[float, float], ...]) -> tuple[float, float]:
    """Return a simple centroid suitable for map centering and reporting."""

    latitude = sum(vertex[0] for vertex in vertices) / len(vertices)
    longitude = sum(vertex[1] for vertex in vertices) / len(vertices)
    return latitude, longitude


def polygon_bounds(vertices: tuple[tuple[float, float], ...]) -> dict[str, float]:
    """Return min/max bounds for polygon vertices."""

    latitudes = [vertex[0] for vertex in vertices]
    longitudes = [vertex[1] for vertex in vertices]
    return {
        "min_lat": min(latitudes),
        "max_lat": max(latitudes),
        "min_lon": min(longitudes),
        "max_lon": max(longitudes),
    }


def resolve_polygon(user_input: str) -> Location:
    """Resolve user-provided polygon coordinates."""

    vertices = parse_polygon_coordinates(user_input)
    latitude, longitude = polygon_center(vertices)
    return Location(
        label=f"Polygon with {len(vertices)} vertices",
        latitude=latitude,
        longitude=longitude,
        source="user-provided polygon coordinates",
        polygon_vertices=vertices,
    )


def resolve_location(user_input: str) -> Location:
    """Resolve coordinates directly or geocode an Ontario place/address string."""

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
        "q": user_input if "ontario" in user_input.lower() else f"{user_input}, Ontario",
        "format": "jsonv2",
        "limit": 5,
        "countrycodes": "ca",
        "addressdetails": 1,
    }
    matches = http_json(NOMINATIM_SEARCH, params)
    if not matches:
        raise ZoneCheckError(f"Could not geocode an Ontario location for: {user_input!r}")

    best = next(
        (
            match
            for match in matches
            if match.get("address", {}).get("state") == "Ontario"
            or "Ontario" in match.get("display_name", "")
        ),
        matches[0],
    )
    return Location(
        label=best.get("display_name", user_input),
        latitude=float(best["lat"]),
        longitude=float(best["lon"]),
        source="OpenStreetMap Nominatim geocoder",
    )


def is_inside_ontario_bounds(location: Location) -> bool:
    """Return whether coordinates are within a broad Ontario bounding box."""

    if location.polygon_vertices:
        bounds = polygon_bounds(location.polygon_vertices)
        return (
            ONTARIO_BOUNDS["min_lat"] <= bounds["min_lat"]
            and bounds["max_lat"] <= ONTARIO_BOUNDS["max_lat"]
            and ONTARIO_BOUNDS["min_lon"] <= bounds["min_lon"]
            and bounds["max_lon"] <= ONTARIO_BOUNDS["max_lon"]
        )
    return (
        ONTARIO_BOUNDS["min_lat"] <= location.latitude <= ONTARIO_BOUNDS["max_lat"]
        and ONTARIO_BOUNDS["min_lon"] <= location.longitude <= ONTARIO_BOUNDS["max_lon"]
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


def arcgis_polygon(vertices: tuple[tuple[float, float], ...]) -> str:
    """Build an ArcGIS REST polygon geometry in WGS84."""

    ring = [[longitude, latitude] for latitude, longitude in vertices]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return json.dumps(
        {
            "rings": [ring],
            "spatialReference": {"wkid": 4326},
        }
    )


def arcgis_envelope(vertices: tuple[tuple[float, float], ...]) -> str:
    """Build an ArcGIS REST envelope around polygon vertices in WGS84."""

    bounds = polygon_bounds(vertices)
    return json.dumps(
        {
            "xmin": bounds["min_lon"],
            "ymin": bounds["min_lat"],
            "xmax": bounds["max_lon"],
            "ymax": bounds["max_lat"],
            "spatialReference": {"wkid": 4326},
        }
    )


def arcgis_geometry(location: Location) -> tuple[str, str]:
    """Return ArcGIS query geometry JSON and geometry type for a point or area.

    Some Ontario GeoHub/LIO layers time out or reject exact polygon query
    geometries. For polygon inputs, use the polygon's bounding envelope as a
    reliable overlap-screening geometry; the exact submitted polygon is still
    drawn in the generated map.
    """

    if location.polygon_vertices:
        return arcgis_envelope(location.polygon_vertices), "esriGeometryEnvelope"
    return arcgis_point(location.longitude, location.latitude), "esriGeometryPoint"


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
    location: Location,
    out_fields: str,
    record_count: int = 5,
) -> list[dict[str, Any]]:
    """Run a spatial-intersection query against an ArcGIS Feature/MapServer layer."""

    geometry, geometry_type = arcgis_geometry(location)

    params = {
        "f": "json",
        "where": "1=1",
        "geometry": geometry,
        "geometryType": geometry_type,
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "returnGeometry": "false",
        "resultRecordCount": record_count,
    }
    data = http_json(f"{layer_url}/query", params, timeout=90, method="GET")
    if "error" in data and data["error"].get("message") == "Error performing query operation":
        data = http_json(f"{layer_url}/query", params, timeout=90, method="POST")
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


def format_area_hectares(square_meters: Any) -> str | None:
    """Convert an area value in square meters to a printable hectare string."""

    try:
        hectares = float(square_meters) / 10000
    except (TypeError, ValueError):
        return None
    return f"{hectares:.2f} ha"


def no_overlap_detail(layer_name: str, location: Location) -> str:
    """Return no-match detail text appropriate for point or polygon checks."""

    verb = "overlapped the polygon" if location.is_polygon else "returned no polygon at the point"
    return f"{layer_name} {verb}."


def check_wetland(location: Location) -> ZoneResult:
    """Check Ontario GeoHub/LIO wetland polygons against a point or polygon."""

    source = "Ontario GeoHub / LIO Wetland With Significance"
    fields = (
        "WETLAND_TYPE,EVALUATED_WETLAND_NAME,WETLAND_SIGNIFICANCE,"
        "EVALUATED_WETLAND_IND,COASTAL_IND,SYSTEM_CALCULATED_AREA,"
        "LOCATION_ACCURACY,SOURCE_NAME"
    )
    try:
        features = query_arcgis_features(
            ONTARIO_WETLAND_LAYER,
            location,
            fields,
            record_count=5,
        )
    except ZoneCheckError as exc:
        return ZoneResult("Wetland", None, source, [], error=str(exc))

    if not features:
        return ZoneResult(
            "Wetland",
            False,
            source,
            [no_overlap_detail("Ontario Wetland With Significance", location)],
        )

    details = []
    for attrs in features:
        wetland_type = attrs.get("WETLAND_TYPE") or "Wetland"
        name = attrs.get("EVALUATED_WETLAND_NAME")
        significance = attrs.get("WETLAND_SIGNIFICANCE")
        evaluated = attrs.get("EVALUATED_WETLAND_IND")
        area = format_area_hectares(attrs.get("SYSTEM_CALCULATED_AREA"))
        line = str(wetland_type)
        if name:
            line += f" - {name}"
        if significance:
            line += f" ({significance})"
        if evaluated:
            line += f", evaluated: {evaluated}"
        if area:
            line += f", area: {area}"
        details.append(line)

    return ZoneResult("Wetland", True, source, details)


def check_forest(location: Location) -> ZoneResult:
    """Check Ontario GeoHub/LIO wooded-area polygons against a point or polygon."""

    source = "Ontario GeoHub / LIO Wooded Area"
    fields = (
        "WOODED_AREA_TYPE,CLASS_SUBTYPE,SYSTEM_CALCULATED_AREA,"
        "LOCATION_ACCURACY,VERIFICATION_STATUS_FLG"
    )
    try:
        features = query_arcgis_features(
            ONTARIO_WOODED_AREA_LAYER,
            location,
            fields,
            record_count=5,
        )
    except ZoneCheckError as exc:
        return ZoneResult("Wooded/forest area", None, source, [], error=str(exc))

    if not features:
        return ZoneResult(
            "Wooded/forest area",
            False,
            source,
            [no_overlap_detail("Ontario Wooded Area", location)],
        )

    details = []
    for attrs in features:
        wooded_type = attrs.get("WOODED_AREA_TYPE") or "Wooded area"
        subtype = attrs.get("CLASS_SUBTYPE")
        area = format_area_hectares(attrs.get("SYSTEM_CALCULATED_AREA"))
        line = str(wooded_type)
        if subtype:
            line += f" ({subtype})"
        if area:
            line += f", area: {area}"
        details.append(line)

    return ZoneResult("Wooded/forest area", True, source, details)


def query_named_layer(
    layer_url: str,
    layer_name: str,
    location: Location,
    fields: str,
) -> tuple[str, list[dict[str, Any]] | None, str | None]:
    """Query one Ontario GeoHub/LIO named layer."""

    try:
        features = query_arcgis_features(
            layer_url,
            location,
            fields,
            record_count=5,
        )
    except ZoneCheckError as exc:
        return layer_name, None, str(exc)
    return layer_name, features, None


def describe_protected_feature(layer_name: str, attrs: dict[str, Any]) -> str:
    """Build a concise protected/restricted layer description."""

    name = (
        attrs.get("PROTECTED_AREA_NAME_ENG")
        or attrs.get("ANSI_NAME")
        or attrs.get("OFFICIAL_NAME")
        or attrs.get("COMMON_SHORT_NAME")
        or "Unnamed area"
    )
    area_type = (
        attrs.get("TYPE_ENG")
        or attrs.get("CLASS_SUBTYPE")
        or attrs.get("ANSI_SIGNIFICANCE")
        or attrs.get("REGULATED_IND")
        or layer_name
    )
    status = attrs.get("STATUS_ENG")
    management = attrs.get("MANAGEMENT_ENG")
    area = format_area_hectares(
        attrs.get("SYSTEM_CALCULATED_AREA") or attrs.get("REGULATED_AREA")
    )
    line = f"{layer_name}: {name} ({area_type})"
    if status:
        line += f", status: {status}"
    if management:
        line += f", management: {management}"
    if area:
        line += f", area: {area}"
    return line


def check_restricted_area(location: Location) -> ZoneResult:
    """Check Ontario regulated/protected constraint layers against a geometry."""

    source = (
        "Ontario GeoHub / LIO Provincial Park Regulated, Conservation Reserve "
        "Regulated, ANSI, and Crown Game Preserve"
    )
    layer_specs = [
        (
            ONTARIO_PROVINCIAL_PARK_LAYER,
            "Provincial Park Regulated",
            (
                "PROTECTED_AREA_NAME_ENG,TYPE_ENG,STATUS_ENG,MANAGEMENT_ENG,"
                "REGULATED_AREA,SYSTEM_CALCULATED_AREA"
            ),
        ),
        (
            ONTARIO_CONSERVATION_RESERVE_LAYER,
            "Conservation Reserve Regulated",
            (
                "PROTECTED_AREA_NAME_ENG,TYPE_ENG,STATUS_ENG,MANAGEMENT_ENG,"
                "REGULATED_AREA,SYSTEM_CALCULATED_AREA"
            ),
        ),
        (
            ONTARIO_ANSI_LAYER,
            "Area of Natural and Scientific Interest (ANSI)",
            "ANSI_NAME,CLASS_SUBTYPE,ANSI_SIGNIFICANCE,SYSTEM_CALCULATED_AREA",
        ),
        (
            ONTARIO_CROWN_GAME_PRESERVE_LAYER,
            "Crown Game Preserve",
            "OFFICIAL_NAME,REGULATED_IND,SYSTEM_CALCULATED_AREA,LOCATION_DESCR",
        ),
    ]

    details: list[str] = []
    errors: list[str] = []
    for layer_url, layer_name, fields in layer_specs:
        queried_name, features, error = query_named_layer(
            layer_url,
            layer_name,
            location,
            fields,
        )
        if error:
            errors.append(f"{queried_name}: {error}")
            continue
        for attrs in features or []:
            details.append(describe_protected_feature(queried_name, attrs))

    if details:
        return ZoneResult("Restricted/protected area", True, source, details)
    if errors and len(errors) == len(layer_specs):
        return ZoneResult("Restricted/protected area", None, source, [], error="; ".join(errors))
    if errors:
        return ZoneResult(
            "Restricted/protected area",
            False,
            source,
            [
                (
                    "No Ontario protected/restricted layer overlapped the polygon."
                    if location.is_polygon
                    else "No Ontario protected/restricted layer matched the point."
                ),
                "Some layer queries failed: " + "; ".join(errors),
            ],
        )
    return ZoneResult(
        "Restricted/protected area",
        False,
        source,
        [
            (
                "No Ontario protected/restricted layer overlapped the polygon."
                if location.is_polygon
                else "No Ontario protected/restricted layer matched the point."
            )
        ],
    )


def check_location(location: Location) -> list[ZoneResult]:
    """Run all zone checks for a resolved location."""

    return [
        check_wetland(location),
        check_forest(location),
        check_restricted_area(location),
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
    if location.polygon_vertices:
        print(f"  Geometry: polygon ({len(location.polygon_vertices)} vertices)")
        print(f"  Map center: {location.latitude:.6f}, {location.longitude:.6f}")
        print("  Query geometry: bounding box of submitted polygon")
    else:
        print(f"  Coordinates: {location.latitude:.6f}, {location.longitude:.6f}")
    print(f"  Resolved by: {location.source}")
    if not is_inside_ontario_bounds(location):
        print("  Warning: coordinates are outside a broad Ontario bounding box.")

    print("\nZone checks")
    for result in results:
        print(f"- {result.zone}: {status_word(result.matched)}")
        print(f"  Source: {result.source}")
        if result.error:
            print(f"  Error: {result.error}")
        for detail in result.details:
            print(f"  Detail: {detail}")

    print(
        "\nNote: Ontario GeoHub/LIO regulated parks, conservation reserves, ANSIs, "
        "and Crown Game Preserves are useful open-data indicators for protected or "
        "restricted-style constraints. Confirm legal access restrictions with the "
        "responsible Ontario or local authority."
    )


def safe_map_filename(location: Location) -> str:
    """Build a portable default map filename for a location."""

    label = re.sub(r"[^A-Za-z0-9._-]+", "_", location.label).strip("_")
    if not label:
        label = "location"
    label = label[:50].strip("_") or "location"
    return f"zone_map_{label}_{location.latitude:.5f}_{location.longitude:.5f}.html"


def safe_image_filename(location: Location) -> str:
    """Build a portable default static image filename for a location."""

    label = re.sub(r"[^A-Za-z0-9._-]+", "_", location.label).strip("_")
    if not label:
        label = "location"
    label = label[:50].strip("_") or "location"
    return f"zone_map_{label}_{location.latitude:.5f}_{location.longitude:.5f}.svg"


def default_map_path(location: Location) -> Path:
    """Return the default output path for a generated map."""

    return Path(safe_map_filename(location)).resolve()


def default_image_path(location: Location) -> Path:
    """Return the default output path for a generated static map image."""

    return Path(safe_image_filename(location)).resolve()


def result_summary_for_map(results: list[ZoneResult]) -> list[dict[str, Any]]:
    """Return JSON-serializable check results for the map sidebar."""

    return [
        {
            "zone": result.zone,
            "status": status_word(result.matched),
            "source": result.source,
            "details": result.details,
            "error": result.error,
        }
        for result in results
    ]


def query_arcgis_geojson(
    layer_url: str,
    location: Location,
    out_fields: str,
    record_count: int = 50,
) -> dict[str, Any]:
    """Return overlapping ArcGIS features as GeoJSON for map display."""

    geometry, geometry_type = arcgis_geometry(location)
    params = {
        "f": "geojson",
        "where": "1=1",
        "geometry": geometry,
        "geometryType": geometry_type,
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "returnGeometry": "true",
        "outSR": 4326,
        "geometryPrecision": 6,
        "resultRecordCount": record_count,
    }
    data = http_json(f"{layer_url}/query", params, timeout=90, method="GET")
    if "error" in data and data["error"].get("message") == "Error performing query operation":
        data = http_json(f"{layer_url}/query", params, timeout=90, method="POST")
    if "error" in data:
        message = data["error"].get("message", "ArcGIS GeoJSON query error")
        raise ZoneCheckError(message)
    if data.get("type") != "FeatureCollection":
        raise ZoneCheckError(f"Unexpected GeoJSON response from {layer_url}")
    return data


def build_map_feature_overlays(location: Location) -> list[dict[str, Any]]:
    """Fetch overlapping Ontario GeoHub/LIO features for aligned vector map layers."""

    specs = [
        {
            "key": "wetlands",
            "label": "Wetland overlaps",
            "source": "Ontario Wetland With Significance",
            "layer_url": ONTARIO_WETLAND_LAYER,
            "fields": (
                "WETLAND_TYPE,EVALUATED_WETLAND_NAME,WETLAND_SIGNIFICANCE,"
                "EVALUATED_WETLAND_IND,SYSTEM_CALCULATED_AREA"
            ),
            "color": "#0284c7",
            "fillColor": "#38bdf8",
        },
        {
            "key": "wooded",
            "label": "Wooded area overlaps",
            "source": "Ontario Wooded Area",
            "layer_url": ONTARIO_WOODED_AREA_LAYER,
            "fields": "WOODED_AREA_TYPE,CLASS_SUBTYPE,SYSTEM_CALCULATED_AREA",
            "color": "#15803d",
            "fillColor": "#22c55e",
        },
        {
            "key": "parks_reserves",
            "label": "Parks and conservation reserve overlaps",
            "source": "Provincial Park Regulated / Conservation Reserve Regulated",
            "layer_url": LIO_OPEN03_MAPSERVER,
            "layers": [
                (
                    ONTARIO_PROVINCIAL_PARK_LAYER,
                    "PROTECTED_AREA_NAME_ENG,TYPE_ENG,STATUS_ENG,SYSTEM_CALCULATED_AREA",
                ),
                (
                    ONTARIO_CONSERVATION_RESERVE_LAYER,
                    "PROTECTED_AREA_NAME_ENG,TYPE_ENG,STATUS_ENG,SYSTEM_CALCULATED_AREA",
                ),
            ],
            "color": "#dc2626",
            "fillColor": "#f87171",
        },
        {
            "key": "natural_heritage",
            "label": "ANSI and Crown Game Preserve overlaps",
            "source": "ANSI / Crown Game Preserve",
            "layer_url": LIO_OPEN05_MAPSERVER,
            "layers": [
                (
                    ONTARIO_ANSI_LAYER,
                    "ANSI_NAME,CLASS_SUBTYPE,ANSI_SIGNIFICANCE,SYSTEM_CALCULATED_AREA",
                ),
                (
                    ONTARIO_CROWN_GAME_PRESERVE_LAYER,
                    "OFFICIAL_NAME,REGULATED_IND,SYSTEM_CALCULATED_AREA",
                ),
            ],
            "color": "#ca8a04",
            "fillColor": "#facc15",
        },
    ]

    overlays: list[dict[str, Any]] = []
    for spec in specs:
        feature_collection = {"type": "FeatureCollection", "features": []}
        error = None
        try:
            if "layers" in spec:
                for layer_url, fields in spec["layers"]:
                    layer_features = query_arcgis_geojson(layer_url, location, fields)
                    feature_collection["features"].extend(layer_features.get("features", []))
            else:
                feature_collection = query_arcgis_geojson(
                    spec["layer_url"],
                    location,
                    spec["fields"],
                )
        except ZoneCheckError as exc:
            error = str(exc)

        overlays.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "source": spec["source"],
                "color": spec["color"],
                "fillColor": spec["fillColor"],
                "featureCollection": feature_collection,
                "error": error,
            }
        )
    return overlays


def iter_geojson_positions(geometry: dict[str, Any]) -> list[tuple[float, float]]:
    """Return lon/lat positions from a GeoJSON geometry."""

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    positions: list[tuple[float, float]] = []

    def walk(value: Any) -> None:
        if (
            isinstance(value, list)
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            positions.append((float(value[0]), float(value[1])))
        elif isinstance(value, list):
            for item in value:
                walk(item)

    if geometry_type == "GeometryCollection":
        for sub_geometry in geometry.get("geometries", []):
            positions.extend(iter_geojson_positions(sub_geometry))
    else:
        walk(coordinates)
    return positions


def map_bounds(
    location: Location,
    feature_overlays: list[dict[str, Any]],
) -> dict[str, float]:
    """Compute lon/lat bounds for static map rendering."""

    positions: list[tuple[float, float]] = []
    if location.polygon_vertices:
        positions.extend((longitude, latitude) for latitude, longitude in location.polygon_vertices)
    else:
        positions.append((location.longitude, location.latitude))

    for overlay in feature_overlays:
        features = overlay.get("featureCollection", {}).get("features", [])
        for feature in features:
            positions.extend(iter_geojson_positions(feature.get("geometry", {})))

    if not positions:
        positions.append((location.longitude, location.latitude))

    longitudes = [position[0] for position in positions]
    latitudes = [position[1] for position in positions]
    min_lon = min(longitudes)
    max_lon = max(longitudes)
    min_lat = min(latitudes)
    max_lat = max(latitudes)
    lon_pad = max((max_lon - min_lon) * 0.12, 0.002)
    lat_pad = max((max_lat - min_lat) * 0.12, 0.002)
    return {
        "min_lon": min_lon - lon_pad,
        "max_lon": max_lon + lon_pad,
        "min_lat": min_lat - lat_pad,
        "max_lat": max_lat + lat_pad,
    }


def svg_points(points: list[tuple[float, float]]) -> str:
    """Convert projected points to an SVG point string."""

    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def render_geojson_geometry_svg(
    geometry: dict[str, Any],
    project: Any,
    color: str,
    fill_color: str,
    opacity: float = 0.28,
) -> str:
    """Render a GeoJSON geometry as SVG elements."""

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not geometry_type or coordinates is None:
        return ""

    def project_ring(ring: list[list[float]]) -> list[tuple[float, float]]:
        return [project(float(lon), float(lat)) for lon, lat, *_ in ring]

    elements: list[str] = []
    if geometry_type == "Polygon":
        for ring_index, ring in enumerate(coordinates):
            points = svg_points(project_ring(ring))
            fill = fill_color if ring_index == 0 else "#ffffff"
            elements.append(
                f'<polygon points="{points}" fill="{fill}" fill-opacity="{opacity}" '
                f'stroke="{color}" stroke-width="2" stroke-opacity="0.95" />'
            )
    elif geometry_type == "MultiPolygon":
        for polygon in coordinates:
            elements.append(
                render_geojson_geometry_svg(
                    {"type": "Polygon", "coordinates": polygon},
                    project,
                    color,
                    fill_color,
                    opacity,
                )
            )
    elif geometry_type == "LineString":
        points = svg_points(project_ring(coordinates))
        elements.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2" />'
        )
    elif geometry_type == "MultiLineString":
        for line in coordinates:
            elements.append(
                render_geojson_geometry_svg(
                    {"type": "LineString", "coordinates": line},
                    project,
                    color,
                    fill_color,
                    opacity,
                )
            )
    elif geometry_type == "Point":
        x, y = project(float(coordinates[0]), float(coordinates[1]))
        elements.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{fill_color}" '
            f'stroke="{color}" stroke-width="2" />'
        )
    elif geometry_type == "MultiPoint":
        for point in coordinates:
            elements.append(
                render_geojson_geometry_svg(
                    {"type": "Point", "coordinates": point},
                    project,
                    color,
                    fill_color,
                    opacity,
                )
            )
    elif geometry_type == "GeometryCollection":
        for sub_geometry in geometry.get("geometries", []):
            elements.append(
                render_geojson_geometry_svg(
                    sub_geometry,
                    project,
                    color,
                    fill_color,
                    opacity,
                )
            )
    return "\n".join(element for element in elements if element)


def render_static_map_svg(
    location: Location,
    results: list[ZoneResult],
    feature_overlays: list[dict[str, Any]],
    width: int = 1400,
    height: int = 950,
) -> str:
    """Render a static SVG map image with all returned layers visible."""

    bounds = map_bounds(location, feature_overlays)
    map_x = 40
    map_y = 92
    map_width = width - 410
    map_height = height - 145
    lon_span = max(bounds["max_lon"] - bounds["min_lon"], 0.000001)
    lat_span = max(bounds["max_lat"] - bounds["min_lat"], 0.000001)

    def project(lon: float, lat: float) -> tuple[float, float]:
        x = map_x + ((lon - bounds["min_lon"]) / lon_span) * map_width
        y = map_y + ((bounds["max_lat"] - lat) / lat_span) * map_height
        return x, y

    layer_elements: list[str] = []
    for overlay in feature_overlays:
        features = overlay.get("featureCollection", {}).get("features", [])
        for feature in features:
            layer_elements.append(
                render_geojson_geometry_svg(
                    feature.get("geometry", {}),
                    project,
                    overlay["color"],
                    overlay["fillColor"],
                )
            )

    submitted_elements = []
    if location.polygon_vertices:
        submitted_points = [
            project(longitude, latitude) for latitude, longitude in location.polygon_vertices
        ]
        submitted_elements.append(
            f'<polygon points="{svg_points(submitted_points)}" fill="#ffffff" '
            f'fill-opacity="0.12" stroke="#111827" stroke-width="4" '
            f'stroke-dasharray="12 8" />'
        )
    else:
        x, y = project(location.longitude, location.latitude)
        submitted_elements.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="#111827" stroke="#ffffff" '
            f'stroke-width="3" />'
        )

    grid_elements = []
    for index in range(1, 5):
        x = map_x + (map_width / 5) * index
        y = map_y + (map_height / 5) * index
        grid_elements.append(
            f'<line x1="{x:.1f}" y1="{map_y}" x2="{x:.1f}" y2="{map_y + map_height}" '
            f'stroke="#cbd5e1" stroke-width="1" />'
        )
        grid_elements.append(
            f'<line x1="{map_x}" y1="{y:.1f}" x2="{map_x + map_width}" y2="{y:.1f}" '
            f'stroke="#cbd5e1" stroke-width="1" />'
        )

    legend_x = map_x + map_width + 35
    legend_y = map_y
    legend_rows = []
    for index, overlay in enumerate(feature_overlays):
        count = len(overlay.get("featureCollection", {}).get("features", []))
        y = legend_y + 62 + index * 54
        label = html.escape(overlay["label"])
        error = " (error)" if overlay.get("error") else ""
        legend_rows.append(
            f'<rect x="{legend_x}" y="{y}" width="28" height="18" rx="4" '
            f'fill="{overlay["fillColor"]}" fill-opacity="0.55" stroke="{overlay["color"]}" />'
        )
        legend_rows.append(
            f'<text x="{legend_x + 40}" y="{y + 14}" class="legend">{label}: '
            f'{count} feature{"s" if count != 1 else ""}{error}</text>'
        )

    status_rows = []
    for index, result in enumerate(results):
        y = legend_y + 340 + index * 42
        status = status_word(result.matched)
        status_rows.append(
            f'<text x="{legend_x}" y="{y}" class="status-line">'
            f'{html.escape(result.zone)}: {status}</text>'
        )

    title = html.escape(f"Ontario GeoHub assessment - {location.label}")
    subtitle = (
        "Static overlap image. Submitted polygon is dashed black; Ontario layer overlaps are colored."
        if location.is_polygon
        else "Static overlap image. Checked point is black; Ontario layer overlaps are colored."
    )
    coord_text = (
        f"Map center: {location.latitude:.6f}, {location.longitude:.6f}"
        if location.is_polygon
        else f"Point: {location.latitude:.6f}, {location.longitude:.6f}"
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <style>
    .title {{ font: 700 28px Arial, sans-serif; fill: #0f172a; }}
    .subtitle {{ font: 16px Arial, sans-serif; fill: #475569; }}
    .legend-title {{ font: 700 20px Arial, sans-serif; fill: #0f172a; }}
    .legend {{ font: 15px Arial, sans-serif; fill: #1f2937; }}
    .status-line {{ font: 700 16px Arial, sans-serif; fill: #111827; }}
    .small {{ font: 13px Arial, sans-serif; fill: #64748b; }}
  </style>
  <rect width="100%" height="100%" fill="#f8fafc" />
  <text x="40" y="42" class="title">{title}</text>
  <text x="40" y="70" class="subtitle">{html.escape(subtitle)}</text>
  <rect x="{map_x}" y="{map_y}" width="{map_width}" height="{map_height}" rx="18" fill="#eef6ef" stroke="#94a3b8" stroke-width="2" />
  <g opacity="0.75">{"".join(grid_elements)}</g>
  <g>{"".join(layer_elements)}</g>
  <g>{"".join(submitted_elements)}</g>
  <rect x="{legend_x - 18}" y="{legend_y}" width="340" height="{map_height}" rx="18" fill="#ffffff" stroke="#d9e2ec" />
  <text x="{legend_x}" y="{legend_y + 34}" class="legend-title">Visible layers</text>
  {"".join(legend_rows)}
  <text x="{legend_x}" y="{legend_y + 300}" class="legend-title">Assessment</text>
  {"".join(status_rows)}
  <text x="{legend_x}" y="{legend_y + map_height - 70}" class="small">{html.escape(coord_text)}</text>
  <text x="{legend_x}" y="{legend_y + map_height - 48}" class="small">Source: Ontario GeoHub / LIO</text>
  <text x="{legend_x}" y="{legend_y + map_height - 26}" class="small">Generated by canada_zone_checker.py</text>
</svg>
"""


def render_map_html(
    location: Location,
    results: list[ZoneResult],
    feature_overlays: list[dict[str, Any]] | None = None,
    zoom: int = 13,
) -> str:
    """Render a standalone interactive HTML map for the checked location."""

    location_data = {
        "label": location.label,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "source": location.source,
    }
    service_data = {
        "wetland": LIO_OPEN01_MAPSERVER,
        "parksAndReserves": LIO_OPEN03_MAPSERVER,
        "naturalHeritage": LIO_OPEN05_MAPSERVER,
        "wooded": LIO_OPEN07_MAPSERVER,
    }
    polygon_data = None
    if location.polygon_vertices:
        polygon_data = [
            [latitude, longitude] for latitude, longitude in location.polygon_vertices
        ]
    if feature_overlays is None:
        feature_overlays = []
    location_json = json.dumps(location_data, ensure_ascii=True)
    polygon_json = json.dumps(polygon_data, ensure_ascii=True)
    results_json = json.dumps(result_summary_for_map(results), ensure_ascii=True)
    services_json = json.dumps(service_data, ensure_ascii=True)
    overlays_json = json.dumps(feature_overlays, ensure_ascii=True)
    title = html.escape(f"Zone map for {location.label}")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIINfQH8fL3pFfF7hdE5Pzu8mpYB2lMAFIk="
    crossorigin=""
  >
  <style>
    :root {{
      --panel-bg: rgba(255, 255, 255, 0.96);
      --border: #d9e2ec;
      --text: #1f2933;
      --muted: #52606d;
      --shadow: 0 12px 32px rgba(15, 23, 42, 0.18);
    }}
    body {{
      font-family: Arial, Helvetica, sans-serif;
      margin: 0;
      color: var(--text);
      background: #eef2f7;
    }}
    header {{
      padding: 14px 20px;
      background: linear-gradient(135deg, #12355b, #0f766e);
      color: white;
    }}
    header h1 {{
      margin: 0 0 6px;
      font-size: 1.25rem;
    }}
    header p {{
      margin: 0;
      font-size: 0.9rem;
      opacity: 0.92;
    }}
    main {{
      position: relative;
      height: calc(100vh - 74px);
    }}
    #map {{
      position: absolute;
      inset: 0;
      z-index: 1;
    }}
    #panel {{
      position: absolute;
      z-index: 500;
      top: 16px;
      left: 16px;
      width: min(410px, calc(100vw - 32px));
      max-height: calc(100vh - 116px);
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 16px;
      background: var(--panel-bg);
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }}
    #panel-content {{
      padding: 14px;
    }}
    .card {{
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 11px 12px;
      margin-bottom: 12px;
      background: #ffffff;
    }}
    .card h2 {{
      margin: 0 0 8px;
      font-size: 0.98rem;
    }}
    .status {{
      display: inline-block;
      padding: 3px 8px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 0.78rem;
      letter-spacing: 0.03em;
    }}
    .YES {{ background: #d3f9d8; color: #1b5e20; }}
    .NO {{ background: #ffe3e3; color: #9b1c1c; }}
    .UNKNOWN {{ background: #fff3bf; color: #7c5c00; }}
    .pill {{
      display: inline-block;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 3px 8px;
      margin: 2px 4px 2px 0;
      background: #f8fafc;
      font-size: 0.78rem;
      color: var(--muted);
    }}
    .detail {{
      margin: 6px 0;
      line-height: 1.35;
    }}
    .small {{
      color: var(--muted);
      font-size: 0.85rem;
    }}
    .legend-item {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 6px 0;
    }}
    .swatch {{
      width: 18px;
      height: 12px;
      border: 1px solid var(--muted);
      opacity: 0.8;
      border-radius: 3px;
    }}
    .leaflet-control-layers {{
      border: 0 !important;
      border-radius: 12px !important;
      box-shadow: var(--shadow) !important;
      font-size: 0.9rem;
    }}
    @media (max-width: 760px) {{
      header {{
        padding: 11px 14px;
      }}
      main {{
        height: calc(100vh - 92px);
      }}
      #panel {{
        left: 10px;
        top: 10px;
        width: calc(100vw - 20px);
        max-height: 42vh;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <p>Clean vector map of the checked geometry and Ontario GeoHub/LIO overlap results.</p>
  </header>
  <main>
    <div id="map"></div>
    <aside id="panel">
      <div id="panel-content">
        <section class="card" id="location-card"></section>
        <section class="card" id="layer-summary"></section>
        <section id="results"></section>
        <section class="card">
          <h2>Notes</h2>
          <p class="small">The colored overlays are GeoJSON features returned by the Ontario GeoHub/LIO overlap queries, so they align directly with the basemap.</p>
          <p class="small">For polygon checks, the submitted polygon is shown exactly. The API overlap screening uses its bounding box for reliability with LIO services.</p>
        </section>
      </div>
    </aside>
  </main>
  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin="">
  </script>
  <script>
    const locationData = {location_json};
    const polygonCoordinates = {polygon_json};
    const zoneResults = {results_json};
    const dataSources = {services_json};
    const featureOverlays = {overlays_json};

    function escapeHtml(value) {{
      const replacements = {{
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }};
      return String(value).replace(/[&<>"']/g, (character) => replacements[character]);
    }}

    function propertiesHtml(properties) {{
      const entries = Object.entries(properties || {{}})
        .filter(([, value]) => value !== null && value !== undefined && value !== "")
        .slice(0, 8);
      if (!entries.length) {{
        return "<em>No attributes returned</em>";
      }}
      return entries
        .map(([key, value]) => `<strong>${{escapeHtml(key)}}:</strong> ${{escapeHtml(value)}}`)
        .join("<br>");
    }}

    document.getElementById("location-card").innerHTML = `
      <h2>Checked location</h2>
      <p class="detail"><strong>${{escapeHtml(locationData.label)}}</strong></p>
      <p class="detail">
        ${{polygonCoordinates ? `Polygon vertices: ${{polygonCoordinates.length}}<br>Map center: ` : "Latitude: "}}${{locationData.latitude.toFixed(6)}}<br>
        ${{polygonCoordinates ? "Center longitude: " : "Longitude: "}}${{locationData.longitude.toFixed(6)}}
      </p>
      <p class="small">Resolved by: ${{escapeHtml(locationData.source)}}</p>
    `;

    const overlaySummary = featureOverlays.map((overlay) => {{
      const count = overlay.featureCollection?.features?.length || 0;
      const error = overlay.error ? ` <span class="UNKNOWN status">error</span>` : "";
      return `
        <div class="legend-item">
          <span class="swatch" style="background:${{overlay.fillColor}}; border-color:${{overlay.color}}"></span>
          <span>${{escapeHtml(overlay.label)}} <span class="pill">${{count}} feature${{count === 1 ? "" : "s"}}</span>${{error}}</span>
        </div>
      `;
    }}).join("");

    document.getElementById("layer-summary").innerHTML = `
      <h2>Map layers</h2>
      ${{overlaySummary || '<p class="small">No feature overlays were requested.</p>'}}
      <p class="small">Layer controls are in the top-right corner of the map.</p>
    `;

    document.getElementById("results").innerHTML = zoneResults.map((result) => `
      <article class="card">
        <h2>${{escapeHtml(result.zone)}} <span class="status ${{result.status}}">${{result.status}}</span></h2>
        <p class="small">Source: ${{escapeHtml(result.source)}}</p>
        ${{result.error ? `<p class="detail"><strong>Error:</strong> ${{escapeHtml(result.error)}}</p>` : ""}}
        ${{result.details.map((detail) => `<p class="detail">${{escapeHtml(detail)}}</p>`).join("")}}
      </article>
    `).join("");

    const map = L.map("map").setView([locationData.latitude, locationData.longitude], {zoom});
    const osm = L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);

    const topo = L.tileLayer("https://{{s}}.tile.opentopomap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 17,
      attribution: "Map data &copy; OpenStreetMap contributors, SRTM | OpenTopoMap"
    }});

    const submittedOverlays = {{}};
    const fitGroup = L.featureGroup().addTo(map);
    if (polygonCoordinates) {{
      const submittedPolygon = L.polygon(polygonCoordinates, {{
        color: "#0f172a",
        weight: 3,
        dashArray: "7 5",
        fillColor: "#ffffff",
        fillOpacity: 0.08
      }})
        .addTo(map)
        .bindPopup(`<strong>${{escapeHtml(locationData.label)}}</strong><br>${{polygonCoordinates.length}} vertices`);
      fitGroup.addLayer(submittedPolygon);
      submittedOverlays["Submitted polygon"] = submittedPolygon;
    }} else {{
      const marker = L.marker([locationData.latitude, locationData.longitude])
        .addTo(map)
        .bindPopup(`<strong>${{escapeHtml(locationData.label)}}</strong><br>${{locationData.latitude.toFixed(6)}}, ${{locationData.longitude.toFixed(6)}}`)
        .openPopup();

      const oneKmRadius = L.circle([locationData.latitude, locationData.longitude], {{
        radius: 1000,
        color: "#0f172a",
        weight: 2,
        fillColor: "#74c0fc",
        fillOpacity: 0.08
      }}).addTo(map);
      fitGroup.addLayer(marker);
      fitGroup.addLayer(oneKmRadius);
      submittedOverlays["1 km context radius"] = oneKmRadius;
      submittedOverlays["Checked location marker"] = marker;
    }}

    const overlapLayers = {{}};
    featureOverlays.forEach((overlay) => {{
      const features = overlay.featureCollection?.features || [];
      if (!features.length) {{
        return;
      }}
      const layer = L.geoJSON(overlay.featureCollection, {{
        style: {{
          color: overlay.color,
          weight: 2,
          opacity: 0.95,
          fillColor: overlay.fillColor,
          fillOpacity: 0.28
        }},
        pointToLayer: (feature, latlng) => L.circleMarker(latlng, {{
          radius: 6,
          color: overlay.color,
          weight: 2,
          fillColor: overlay.fillColor,
          fillOpacity: 0.85
        }}),
        onEachFeature: (feature, layer) => {{
          layer.bindPopup(`<strong>${{escapeHtml(overlay.label)}}</strong><br>${{propertiesHtml(feature.properties)}}`);
        }}
      }}).addTo(map);
      fitGroup.addLayer(layer);
      overlapLayers[`${{overlay.label}} (${{features.length}})`] = layer;
    }});

    if (fitGroup.getLayers().length) {{
      map.fitBounds(fitGroup.getBounds().pad(0.18));
    }}

    L.control.layers(
      {{
        "OpenStreetMap": osm,
        "OpenTopoMap": topo
      }},
      Object.assign({{}}, overlapLayers, submittedOverlays),
      {{ collapsed: false }}
    ).addTo(map);
  </script>
</body>
</html>
"""


def write_map_html(
    location: Location,
    results: list[ZoneResult],
    output_path: Path | str | None = None,
    feature_overlays: list[dict[str, Any]] | None = None,
) -> Path:
    """Write an interactive map HTML file and return its absolute path."""

    path = Path(output_path).expanduser() if output_path else default_map_path(location)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_map_html(location, results, feature_overlays=feature_overlays),
        encoding="utf-8",
    )
    return path


def write_static_map_image(
    location: Location,
    results: list[ZoneResult],
    feature_overlays: list[dict[str, Any]],
    output_path: Path | str | None = None,
) -> Path:
    """Write a static SVG map image and return its absolute path."""

    path = Path(output_path).expanduser() if output_path else default_image_path(location)
    if path.suffix.lower() not in {".svg", ""}:
        path = path.with_suffix(".svg")
    elif not path.suffix:
        path = path.with_suffix(".svg")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_static_map_svg(location, results, feature_overlays), encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Check whether an Ontario location is in a wetland, wooded area, "
            "or regulated/protected natural-heritage area."
        )
    )
    parser.add_argument(
        "location",
        nargs="?",
        help="Address/place in Ontario, or coordinates as 'latitude, longitude'.",
    )
    parser.add_argument(
        "--polygon",
        help=(
            "Assess an Ontario polygon instead of a point. Provide vertices as "
            "'lat, lon; lat, lon; lat, lon'. The polygon is closed automatically."
        ),
    )
    parser.add_argument(
        "--map-output",
        default=None,
        help=(
            "Path for the generated static SVG map image. Defaults to a "
            "zone_map_<location>.svg file in the current directory."
        ),
    )
    parser.add_argument(
        "--html-output",
        default=None,
        help="Optional path for also writing the old interactive HTML map.",
    )
    parser.add_argument(
        "--no-map",
        action="store_true",
        help="Only print the text report; do not generate the static map image.",
    )
    parser.add_argument(
        "--open-map",
        action="store_true",
        help="Open the generated static SVG image in the default viewer/browser.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""

    args = build_parser().parse_args(argv)
    try:
        if args.polygon:
            location = resolve_polygon(args.polygon)
        else:
            user_location = args.location or input(
                "Enter an Ontario address/place or coordinates as 'latitude, longitude': "
            ).strip()
            if not user_location:
                print("No location provided.", file=sys.stderr)
                return 2
            location = resolve_location(user_location)
        results = check_location(location)
    except ZoneCheckError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print_report(location, results)
    if not args.no_map:
        feature_overlays = build_map_feature_overlays(location)
        map_path = write_static_map_image(
            location,
            results,
            feature_overlays,
            args.map_output,
        )
        print(f"\nStatic map image saved to: {map_path}")
        if args.html_output:
            html_path = write_map_html(
                location,
                results,
                args.html_output,
                feature_overlays=feature_overlays,
            )
            print(f"Interactive HTML map also saved to: {html_path}")
        if args.open_map:
            webbrowser.open(map_path.as_uri())
    elif args.html_output:
        feature_overlays = build_map_feature_overlays(location)
        html_path = write_map_html(
            location,
            results,
            args.html_output,
            feature_overlays=feature_overlays,
        )
        print(f"\nInteractive HTML map saved to: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
