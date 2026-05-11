import unittest

from canada_zone_checker import (
    CANADA_BOUNDS,
    LandCover,
    ZoneCheckError,
    extract_pixel_value,
    is_inside_canada_bounds,
    parse_coordinate_pair,
    status_word,
    Location,
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


if __name__ == "__main__":
    unittest.main()
