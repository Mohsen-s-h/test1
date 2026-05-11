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

Run with coordinates:

```bash
python3 canada_zone_checker.py "45.409, -75.500"
```

Or run with a Canadian place/address:

```bash
python3 canada_zone_checker.py "Banff National Park, Alberta"
```

If no argument is supplied, the script prompts for a location interactively.

## Notes

For "restricted areas", this script uses CPCAD protected and conserved areas as
a national open-data proxy. Legal restrictions can also come from local,
provincial, military, airport, or private-property rules that are not all
represented in a single national dataset.
