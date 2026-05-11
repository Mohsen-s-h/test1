import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from canada_zone_checker import (
    CANADA_BOUNDS,
    CNWI_MAPSERVER,
    CPCAD_MAPSERVER,
    LandCover,
    VEGETATION_MAPSERVER,
    ZoneCheckError,
    ZoneResult,
    default_map_path,
    extract_pixel_value,
    is_inside_canada_bounds,
    parse_coordinate_pair,
    render_map_html,
    safe_map_filename,
    status_word,
    Location,
    write_map_html,
)


class CanadaZoneCheckerTests(unittest.TestCase):
    def test_parse_coordinate_pair_accepts_comma_or_space(self):
        self.assertEqual(parse_coordinate_pair("45.409, -75.500"), (45.409, -75.5))
        self.assertEqual(parse_coordinate_pair("45.409 -75.500"), (45.409, -75.5))

    def test_parse_coordinate_pair_rejects_out_of_range_values(self):
        with self.assertRaises(ZoneCheckError):
            parse_coordinate_pair("200, -75")

    def test_parse_coordinate_pair_returns_none_for_address(self):
        self.assertIsNone(parse_coordinate_pair("Ottawa, ON"))

    def test_extract_pixel_value_handles_arcgis_attribute_names(self):
        self.assertEqual(extract_pixel_value({"Pixel Value": "14"}), 14)
        self.assertEqual(extract_pixel_value({"PixelValue": "17.0"}), 17)
        self.assertIsNone(extract_pixel_value({"Pixel Value": "not a number"}))

    def test_inside_canada_bounds(self):
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
        self.assertTrue(is_inside_canada_bounds(ottawa))
        self.assertFalse(is_inside_canada_bounds(paris))
        self.assertLess(CANADA_BOUNDS["min_lon"], ottawa.longitude)

    def test_status_word(self):
        self.assertEqual(status_word(True), "YES")
        self.assertEqual(status_word(False), "NO")
        self.assertEqual(status_word(None), "UNKNOWN")

    def test_land_cover_dataclass_stores_error(self):
        result = LandCover(code=None, label="Unknown", raw_attributes={}, error="offline")
        self.assertEqual(result.error, "offline")

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

        self.assertIn("Leaflet", rendered)
        self.assertIn(CNWI_MAPSERVER, rendered)
        self.assertIn(CPCAD_MAPSERVER, rendered)
        self.assertIn(VEGETATION_MAPSERVER, rendered)
        self.assertIn('"status": "YES"', rendered)
        self.assertIn("Example &lt;Place&gt;", rendered)

    def test_write_map_html_writes_requested_path(self):
        location = Location("Example", 45.0, -75.0, "test")
        results = [ZoneResult("Forest land", False, "source", ["detail"])]

        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "map.html"
            written_path = write_map_html(location, results, output_path)

            self.assertEqual(written_path, output_path.resolve())
            self.assertIn("Forest land", written_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
