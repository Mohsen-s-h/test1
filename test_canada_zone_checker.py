import unittest
from socket import timeout as SocketTimeout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from canada_zone_checker import (
    LIO_OPEN01_MAPSERVER,
    LIO_OPEN03_MAPSERVER,
    LIO_OPEN05_MAPSERVER,
    LIO_OPEN07_MAPSERVER,
    ONTARIO_BOUNDS,
    ZoneCheckError,
    ZoneResult,
    arcgis_geometry,
    default_map_path,
    format_area_hectares,
    http_json,
    is_inside_ontario_bounds,
    parse_coordinate_pair,
    parse_polygon_coordinates,
    render_map_html,
    resolve_polygon,
    safe_map_filename,
    status_word,
    Location,
    write_map_html,
)


class OntarioZoneCheckerTests(unittest.TestCase):
    def test_parse_coordinate_pair_accepts_comma_or_space(self):
        self.assertEqual(parse_coordinate_pair("45.409, -75.500"), (45.409, -75.5))
        self.assertEqual(parse_coordinate_pair("45.409 -75.500"), (45.409, -75.5))

    def test_http_json_converts_socket_timeout_to_zone_error(self):
        with patch("urllib.request.urlopen", side_effect=SocketTimeout("timed out")):
            with self.assertRaisesRegex(ZoneCheckError, "Timed out"):
                http_json("https://example.com", {"f": "json"}, timeout=1, retries=0)

    def test_parse_coordinate_pair_rejects_out_of_range_values(self):
        with self.assertRaises(ZoneCheckError):
            parse_coordinate_pair("200, -75")

    def test_parse_coordinate_pair_returns_none_for_address(self):
        self.assertIsNone(parse_coordinate_pair("Ottawa, ON"))

    def test_parse_polygon_coordinates_accepts_semicolon_vertices(self):
        vertices = parse_polygon_coordinates(
            "43.273, -79.923; 43.273, -79.921; 43.275, -79.921; 43.275, -79.923"
        )

        self.assertEqual(len(vertices), 4)
        self.assertEqual(vertices[0], (43.273, -79.923))

    def test_parse_polygon_coordinates_rejects_too_few_vertices(self):
        with self.assertRaises(ZoneCheckError):
            parse_polygon_coordinates("43.273, -79.923; 43.275, -79.921")

    def test_resolve_polygon_sets_center_and_geometry(self):
        location = resolve_polygon(
            "43.273, -79.923; 43.273, -79.921; 43.275, -79.921; 43.275, -79.923"
        )
        geometry, geometry_type = arcgis_geometry(location)

        self.assertTrue(location.is_polygon)
        self.assertAlmostEqual(location.latitude, 43.274)
        self.assertEqual(geometry_type, "esriGeometryEnvelope")
        self.assertIn('"xmin"', geometry)

    def test_format_area_hectares(self):
        self.assertEqual(format_area_hectares(12345), "1.23 ha")
        self.assertIsNone(format_area_hectares("not a number"))

    def test_inside_ontario_bounds(self):
        ottawa = Location(
            label="Ottawa",
            latitude=45.4215,
            longitude=-75.6972,
            source="test",
        )
        paris = Location(
            label="Paris",
            latitude=48.8566,
            longitude=2.3522,
            source="test",
        )
        self.assertTrue(is_inside_ontario_bounds(ottawa))
        self.assertFalse(is_inside_ontario_bounds(paris))
        self.assertLess(ONTARIO_BOUNDS["min_lon"], ottawa.longitude)

    def test_inside_ontario_bounds_checks_polygon_vertices(self):
        ontario_polygon = resolve_polygon(
            "43.273, -79.923; 43.273, -79.921; 43.275, -79.921; 43.275, -79.923"
        )
        outside_polygon = Location(
            "Outside",
            48.0,
            2.0,
            "test",
            ((48.0, 2.0), (48.0, 2.1), (48.1, 2.1)),
        )

        self.assertTrue(is_inside_ontario_bounds(ontario_polygon))
        self.assertFalse(is_inside_ontario_bounds(outside_polygon))

    def test_status_word(self):
        self.assertEqual(status_word(True), "YES")
        self.assertEqual(status_word(False), "NO")
        self.assertEqual(status_word(None), "UNKNOWN")

    def test_safe_map_filename_is_portable(self):
        location = Location("Cootes Paradise / Hamilton, ON", 43.274037, -79.922389, "test")

        filename = safe_map_filename(location)

        self.assertTrue(filename.startswith("zone_map_Cootes_Paradise_Hamilton_ON"))
        self.assertTrue(filename.endswith("43.27404_-79.92239.html"))
        self.assertNotIn("/", filename)

    def test_default_map_path_uses_html_filename(self):
        location = Location("Example", 45.0, -75.0, "test")

        self.assertEqual(default_map_path(location).suffix, ".html")

    def test_render_map_html_includes_services_and_results(self):
        location = Location("Example <Place>", 43.274037, -79.922389, "test")
        results = [
            ZoneResult(
                zone="Wetland",
                matched=True,
                source="source",
                details=["detail"],
            )
        ]

        rendered = render_map_html(location, results)

        self.assertIn("leaflet", rendered)
        self.assertIn(LIO_OPEN01_MAPSERVER, rendered)
        self.assertIn(LIO_OPEN03_MAPSERVER, rendered)
        self.assertIn(LIO_OPEN05_MAPSERVER, rendered)
        self.assertIn(LIO_OPEN07_MAPSERVER, rendered)
        self.assertIn('"status": "YES"', rendered)
        self.assertIn("Example &lt;Place&gt;", rendered)

    def test_render_map_html_includes_submitted_polygon(self):
        location = resolve_polygon(
            "43.273, -79.923; 43.273, -79.921; 43.275, -79.921; 43.275, -79.923"
        )

        rendered = render_map_html(location, [ZoneResult("Wetland", True, "source", [])])

        self.assertIn("Submitted polygon", rendered)
        self.assertIn("polygonCoordinates", rendered)
        self.assertIn("-79.923", rendered)

    def test_write_map_html_writes_requested_path(self):
        location = Location("Example", 45.0, -75.0, "test")
        results = [ZoneResult("Wooded/forest area", False, "source", ["detail"])]

        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "map.html"
            written_path = write_map_html(location, results, output_path)

            self.assertEqual(written_path, output_path.resolve())
            self.assertIn("Wooded/forest area", written_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
