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
import sys
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
    """Resolved user location."""

    label: str
    latitude: float
    longitude: float
    source: str


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


def format_area_hectares(square_meters: Any) -> str | None:
    """Convert an area value in square meters to a printable hectare string."""

    try:
        hectares = float(square_meters) / 10000
    except (TypeError, ValueError):
        return None
    return f"{hectares:.2f} ha"


def check_wetland(longitude: float, latitude: float) -> ZoneResult:
    """Check Ontario GeoHub/LIO wetland polygons at a point."""

    source = "Ontario GeoHub / LIO Wetland With Significance"
    fields = (
        "WETLAND_TYPE,EVALUATED_WETLAND_NAME,WETLAND_SIGNIFICANCE,"
        "EVALUATED_WETLAND_IND,COASTAL_IND,SYSTEM_CALCULATED_AREA,"
        "LOCATION_ACCURACY,SOURCE_NAME"
    )
    try:
        features = query_arcgis_features(
            ONTARIO_WETLAND_LAYER,
            longitude,
            latitude,
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
            ["Ontario Wetland With Significance returned no polygon at the point."],
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


def check_forest(longitude: float, latitude: float) -> ZoneResult:
    """Check Ontario GeoHub/LIO wooded-area polygons at a point."""

    source = "Ontario GeoHub / LIO Wooded Area"
    fields = (
        "WOODED_AREA_TYPE,CLASS_SUBTYPE,SYSTEM_CALCULATED_AREA,"
        "LOCATION_ACCURACY,VERIFICATION_STATUS_FLG"
    )
    try:
        features = query_arcgis_features(
            ONTARIO_WOODED_AREA_LAYER,
            longitude,
            latitude,
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
            ["Ontario Wooded Area returned no polygon at the point."],
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
    longitude: float,
    latitude: float,
    fields: str,
) -> tuple[str, list[dict[str, Any]] | None, str | None]:
    """Query one Ontario GeoHub/LIO named layer."""

    try:
        features = query_arcgis_features(
            layer_url,
            longitude,
            latitude,
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


def check_restricted_area(longitude: float, latitude: float) -> ZoneResult:
    """Check Ontario regulated/protected and natural-heritage constraint layers."""

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
            longitude,
            latitude,
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
                "No Ontario protected/restricted layer matched the point.",
                "Some layer queries failed: " + "; ".join(errors),
            ],
        )
    return ZoneResult(
        "Restricted/protected area",
        False,
        source,
        ["No Ontario protected/restricted layer matched the point."],
    )


def check_location(location: Location) -> list[ZoneResult]:
    """Run all zone checks for a resolved location."""

    return [
        check_wetland(location.longitude, location.latitude),
        check_forest(location.longitude, location.latitude),
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


def default_map_path(location: Location) -> Path:
    """Return the default output path for a generated map."""

    return Path(safe_map_filename(location)).resolve()


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


def render_map_html(location: Location, results: list[ZoneResult], zoom: int = 13) -> str:
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
    location_json = json.dumps(location_data, ensure_ascii=True)
    results_json = json.dumps(result_summary_for_map(results), ensure_ascii=True)
    services_json = json.dumps(service_data, ensure_ascii=True)
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
    body {{
      font-family: Arial, Helvetica, sans-serif;
      margin: 0;
      color: #1f2933;
      background: #f5f7fa;
    }}
    header {{
      padding: 16px 20px;
      background: #12355b;
      color: white;
    }}
    header h1 {{
      margin: 0 0 6px;
      font-size: 1.35rem;
    }}
    header p {{
      margin: 0;
      font-size: 0.95rem;
    }}
    main {{
      display: grid;
      grid-template-columns: minmax(320px, 420px) 1fr;
      min-height: calc(100vh - 76px);
    }}
    aside {{
      padding: 16px;
      overflow: auto;
      border-right: 1px solid #d9e2ec;
      background: white;
    }}
    #map {{
      min-height: 620px;
      height: calc(100vh - 76px);
    }}
    .card {{
      border: 1px solid #d9e2ec;
      border-radius: 10px;
      padding: 12px;
      margin-bottom: 12px;
      background: #ffffff;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.06);
    }}
    .card h2 {{
      margin: 0 0 8px;
      font-size: 1rem;
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
    .detail {{
      margin: 6px 0;
      line-height: 1.35;
    }}
    .small {{
      color: #52606d;
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
      border: 1px solid #52606d;
      opacity: 0.8;
    }}
    @media (max-width: 850px) {{
      main {{
        grid-template-columns: 1fr;
      }}
      aside {{
        border-right: 0;
        border-bottom: 1px solid #d9e2ec;
      }}
      #map {{
        height: 70vh;
        min-height: 440px;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <p>Interactive feasibility map with Ontario GeoHub/LIO wetland, wooded, and regulated/natural-heritage layers.</p>
  </header>
  <main>
    <aside>
      <section class="card" id="location-card"></section>
      <section id="results"></section>
      <section class="card">
        <h2>Layer guide</h2>
        <div class="legend-item"><span class="swatch" style="background:#4dabf7"></span>Ontario wetlands</div>
        <div class="legend-item"><span class="swatch" style="background:#51cf66"></span>Ontario wooded areas</div>
        <div class="legend-item"><span class="swatch" style="background:#ff6b6b"></span>Provincial parks and conservation reserves</div>
        <div class="legend-item"><span class="swatch" style="background:#ffd43b"></span>ANSIs and Crown Game Preserves</div>
        <p class="small">Use the layer control on the map to turn layers on or off. Some services only draw at certain zoom levels.</p>
      </section>
      <section class="card">
        <h2>Important note</h2>
        <p class="small">Ontario regulated and natural-heritage layers are shown as practical indicators for constraints. Always confirm legal restrictions with the responsible authority.</p>
      </section>
    </aside>
    <div id="map"></div>
  </main>
  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin="">
  </script>
  <script src="https://unpkg.com/esri-leaflet@3.0.12/dist/esri-leaflet.js"></script>
  <script>
    const locationData = {location_json};
    const zoneResults = {results_json};
    const services = {services_json};

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

    document.getElementById("location-card").innerHTML = `
      <h2>Checked location</h2>
      <p class="detail"><strong>${{escapeHtml(locationData.label)}}</strong></p>
      <p class="detail">Latitude: ${{locationData.latitude.toFixed(6)}}<br>Longitude: ${{locationData.longitude.toFixed(6)}}</p>
      <p class="small">Resolved by: ${{escapeHtml(locationData.source)}}</p>
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

    const imagery = L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}",
      {{
        maxZoom: 19,
        attribution: "Tiles &copy; Esri"
      }}
    );

    const wetlands = L.esri.dynamicMapLayer({{
      url: services.wetland,
      layers: [15],
      opacity: 0.58,
      attribution: "Ontario GeoHub / LIO Wetlands"
    }});

    const woodedAreas = L.esri.dynamicMapLayer({{
      url: services.wooded,
      layers: [29],
      opacity: 0.42,
      attribution: "Ontario GeoHub / LIO Wooded Area"
    }}).addTo(map);

    const parksAndReserves = L.esri.dynamicMapLayer({{
      url: services.parksAndReserves,
      layers: [2, 4],
      opacity: 0.62,
      attribution: "Ontario GeoHub / LIO Parks and Conservation Reserves"
    }}).addTo(map);

    const naturalHeritage = L.esri.dynamicMapLayer({{
      url: services.naturalHeritage,
      layers: [3, 7],
      opacity: 0.52,
      attribution: "Ontario GeoHub / LIO ANSI and Crown Game Preserve"
    }});

    const marker = L.marker([locationData.latitude, locationData.longitude])
      .addTo(map)
      .bindPopup(`<strong>${{escapeHtml(locationData.label)}}</strong><br>${{locationData.latitude.toFixed(6)}}, ${{locationData.longitude.toFixed(6)}}`)
      .openPopup();

    const oneKmRadius = L.circle([locationData.latitude, locationData.longitude], {{
      radius: 1000,
      color: "#1c7ed6",
      weight: 2,
      fillColor: "#74c0fc",
      fillOpacity: 0.08
    }}).addTo(map);

    L.control.layers(
      {{
        "OpenStreetMap": osm,
        "Esri World Imagery": imagery
      }},
      {{
        "Ontario wetlands": wetlands,
        "Ontario wooded areas": woodedAreas,
        "Provincial parks and conservation reserves": parksAndReserves,
        "ANSIs and Crown Game Preserves": naturalHeritage,
        "1 km context radius": oneKmRadius,
        "Checked location marker": marker
      }},
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
) -> Path:
    """Write an interactive map HTML file and return its absolute path."""

    path = Path(output_path).expanduser() if output_path else default_map_path(location)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_map_html(location, results), encoding="utf-8")
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
        "--map-output",
        default=None,
        help=(
            "Path for the generated interactive HTML map. Defaults to a "
            "zone_map_<location>.html file in the current directory."
        ),
    )
    parser.add_argument(
        "--no-map",
        action="store_true",
        help="Only print the text report; do not generate the interactive map.",
    )
    parser.add_argument(
        "--open-map",
        action="store_true",
        help="Open the generated map in the default web browser.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""

    args = build_parser().parse_args(argv)
    user_location = args.location or input(
        "Enter an Ontario address/place or coordinates as 'latitude, longitude': "
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
    if not args.no_map:
        map_path = write_map_html(location, results, args.map_output)
        print(f"\nInteractive map saved to: {map_path}")
        if args.open_map:
            webbrowser.open(map_path.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
