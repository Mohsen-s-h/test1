# Canada Zone Checker

Small Python CLI for checking whether a Canadian location intersects:

- wetland data from Environment and Climate Change Canada's Canadian National
  Wetlands Inventory (CNWI)
- forest land-cover classes from Natural Resources Canada's Land Cover of Canada
  raster, with Vegetation Zones of Canada as context
- protected/conserved areas from Environment and Climate Change Canada's
  Canadian Protected and Conserved Areas Database (CPCAD)

The script uses public APIs and only the Python standard library.

## Usage

Run with coordinates. Use negative longitude for locations west of Greenwich,
including most Canadian locations:

```bash
python3 canada_zone_checker.py "43.274037, -79.922389"
```

Or run with a Canadian place/address:

```bash
python3 canada_zone_checker.py "Banff National Park, Alberta"
```

If no argument is supplied, the script prompts for a location interactively.

After the text report, the script also saves an interactive HTML map in the
current directory. The map is centered on the checked point and includes
toggleable layers for:

- CNWI detailed wetlands
- NRCan Vegetation Zones of Canada for forest/vegetation context
- NRCan Land Cover of Canada raster
- CPCAD protected/conserved areas

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

For "restricted areas", this script uses CPCAD protected and conserved areas as
a national open-data proxy. Legal restrictions can also come from local,
provincial, military, airport, or private-property rules that are not all
represented in a single national dataset.
