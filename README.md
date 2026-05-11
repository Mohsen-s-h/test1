# Ontario Zone Checker

Small Python CLI for checking whether an Ontario location intersects:

- wetland data from Ontario GeoHub / Land Information Ontario (LIO)
  `Wetland With Significance`
- wooded or forested polygons from Ontario GeoHub / LIO `Wooded Area`
- regulated/protected or constraint-style areas from Ontario GeoHub / LIO:
  `Provincial Park Regulated`, `Conservation Reserve Regulated`, `ANSI`, and
  `Crown Game Preserve`

The script uses public APIs and only the Python standard library.

## Usage

Run with coordinates. Use negative longitude for Ontario locations:

```bash
python3 canada_zone_checker.py "43.274037, -79.922389"
```

Or run with an Ontario place/address:

```bash
python3 canada_zone_checker.py "Cootes Paradise, Hamilton, Ontario"
```

Or assess a polygon by passing vertices in `latitude, longitude` order. Separate
vertices with semicolons; the script closes the polygon automatically:

```bash
python3 canada_zone_checker.py --polygon "43.2735, -79.9230; 43.2735, -79.9200; 43.2760, -79.9200; 43.2760, -79.9230"
```

If no argument is supplied, the script prompts for a location interactively.

After the text report, the script also saves an interactive HTML map in the
current directory. The map is centered on the checked point or submitted polygon
and includes toggleable layers for:

- Ontario wetlands
- Ontario wooded areas
- provincial parks and conservation reserves
- Areas of Natural and Scientific Interest (ANSI) and Crown Game Preserves

Open the generated `.html` file in a browser to view the map. You can choose
the output path or skip map generation:

```bash
python3 canada_zone_checker.py "43.274037, -79.922389" --map-output cootes_map.html
python3 canada_zone_checker.py "43.274037, -79.922389" --no-map
```

If you are running on a computer with a graphical browser, you can ask Python to
open the generated map automatically:

```bash
python3 canada_zone_checker.py "43.274037, -79.922389" --open-map
```

## Notes

For "restricted areas", this script uses Ontario GeoHub/LIO regulated parks,
conservation reserves, ANSIs, and Crown Game Preserves as open-data indicators.
Legal restrictions can also come from municipal, conservation-authority, private
property, airport, military, or other rules that are not all represented in a
single Ontario open-data layer.
