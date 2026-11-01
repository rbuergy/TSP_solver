"""Reading and writing location lists as CSV (import / export / ready-made examples).

CSV format (header required, column names are case-insensitive):
    name,latitude,longitude,display_name
    Bern,46.9490,7.4390,"Bern, Switzerland"

- latitude/longitude may also be called lat/lon/lng; display_name is optional.
- Rows without coordinates are looked up by their name (geocoding), e.g. a plain list of town names.
- Comma, semicolon (Swiss/German Excel) and tab separated files are accepted, as well as decimal commas.
"""
import csv
import io
import re
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"
MAX_GEOCODE_ROWS = 30  # rows without coordinates are geocoded one by one, so keep it reasonable

_ALIASES = {
    "name": "name", "location": "name", "ort": "name", "city": "name",
    "latitude": "lat", "lat": "lat", "breite": "lat",
    "longitude": "lon", "lon": "lon", "lng": "lon", "long": "lon", "länge": "lon",
    "display_name": "display_name", "description": "display_name", "beschreibung": "display_name",
}
_TRANSPORT_IN_NAME = re.compile(r"\((driving|walking|cycling)\)\s*$", re.IGNORECASE)


def list_examples():
    """[(label, filename)] for every CSV in the examples folder. A suffix like '(walking)' in the file name
    selects the mode of transport when the example is loaded."""
    if not EXAMPLES_DIR.is_dir():
        return []
    return [(p.stem, p.name) for p in sorted(EXAMPLES_DIR.glob("*.csv"))]


def transport_for_example(filename):
    match = _TRANSPORT_IN_NAME.search(Path(filename).stem)
    return match.group(1).lower() if match else None


def load_example(filename):
    path = (EXAMPLES_DIR / Path(filename).name)  # .name: never read outside the examples folder
    return parse_csv(path.read_text(encoding="utf-8-sig"))


def _number(text):
    return float(text.strip().replace("'", "").replace(",", "."))


def parse_csv(text, geocoder=None):
    """Returns (locations, warnings). locations: [{'name', 'display_name', 'position': [lat, lon]}].
    geocoder(name) -> {'name', 'display_name', 'lat', 'lon'} or None, used for rows without coordinates."""
    text = text.lstrip("﻿").strip()
    if not text:
        return [], ["The file is empty."]
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header = [_ALIASES.get(h.strip().lower()) for h in next(reader)]
    if "name" not in header:
        return [], ["The CSV needs a header row with at least a 'name' column (optional: latitude, longitude)."]

    locations, warnings, geocoded = [], [], 0
    for line_no, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        rec = {key: row[i].strip() for i, key in enumerate(header) if key and i < len(row)}
        name = rec.get("name", "")
        if not name:
            warnings.append(f"Line {line_no}: no name, skipped.")
            continue
        if rec.get("lat") and rec.get("lon"):
            try:
                lat, lon = _number(rec["lat"]), _number(rec["lon"])
            except ValueError:
                warnings.append(f"Line {line_no}: invalid coordinates for '{name}', skipped.")
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                warnings.append(f"Line {line_no}: coordinates of '{name}' out of range, skipped.")
                continue
            locations.append({"name": name, "display_name": rec.get("display_name") or name, "position": [lat, lon]})
            continue
        # no coordinates: look the name up
        if geocoder is None:
            warnings.append(f"Line {line_no}: no coordinates for '{name}', skipped.")
            continue
        if geocoded >= MAX_GEOCODE_ROWS:
            warnings.append(f"Line {line_no}: '{name}' skipped, at most {MAX_GEOCODE_ROWS} names are looked up per file.")
            continue
        geocoded += 1
        try:
            hit = geocoder(name)
        except Exception as e:  # service errors should not abort the whole import
            hit = None
            warnings.append(f"Line {line_no}: lookup of '{name}' failed ({e}).")
        if hit is None:
            warnings.append(f"Line {line_no}: '{name}' not found, skipped.")
            continue
        locations.append({"name": name, "display_name": hit["display_name"], "position": [hit["lat"], hit["lon"]]})
    if not locations and not warnings:
        warnings.append("The file contains no locations.")
    return locations, warnings


def to_csv(locations):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["name", "latitude", "longitude", "display_name"])
    for loc in locations:
        lat, lon = loc["position"]
        writer.writerow([loc["name"], f"{lat:.6f}", f"{lon:.6f}", loc.get("display_name", loc["name"])])
    return out.getvalue()
