import base64
import json
import re
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import dash_leaflet as dl
import flask
from dash import ALL, Dash, Input, Output, State, callback_context, dcc, html, no_update

import locations_io
from tsp_logic import ROUTE_HTML_PATH, ServiceError, TravelTimeCalculator, TSPMethodFactory, TSPSolverInterface, suggest

BASE_DIR = Path(__file__).resolve().parent
KEY_FILE = BASE_DIR / "API_KEY.txt"
DOWNLOAD_HTML_PATH = BASE_DIR / "temp_route_download.html"  # separate from the page served at /route-page
API_KEY = KEY_FILE.read_text(encoding="utf-8").strip() if KEY_FILE.exists() else ""
if API_KEY.lower().startswith("paste"):  # placeholder text from API_KEY.example.txt
    API_KEY = ""

TSPInterface = TSPSolverInterface(API_KEY)

# No external stylesheet: the look is defined in assets/app.css (served by Dash automatically),
# so the app also works without access to a CDN.
app = Dash(__name__, title="TSP Visualizer")

METHOD_HINTS = {
    "TwoOpt": "Heuristic: nearest neighbour + 2-opt. Fast, but optimality is not proven.",
    "Permutations": "Exact: tries all (n−1)! tours. Only practical up to about 10 locations.",
    "FlowBased": "Exact: MIP with MTZ subtour elimination (SCIP). Proves optimality if it finishes in time.",
    "ConstraintProgramming": "Exact: CP-SAT with the AddCircuit constraint. Proves optimality if it finishes in time.",
}
TRANSPORT_LABELS = {"driving": "Car", "cycling": "Bike", "walking": "Walk"}

ROUTE_COLOR = "#e11d48"  # keep in sync with --route in assets/app.css
STOP_COLOR = "#334155"   # --stop
EMPTY_RESULT = html.Div("No route yet – add at least two locations and click Compute route.", className="result-empty")


def card(step, title, *children, extra=None, class_name="card"):
    head = [html.Span(str(step), className="step"), html.Span(title)]
    if extra is not None:
        head.append(extra)
    return html.Section([html.H2(head, className="card-title"), *children], className=class_name)


def field(label, *children):
    return html.Div([html.Div(label, className="field-label"), *children], className="field")


sidebar = html.Aside([
    html.Header([
        html.H1("TSP Visualizer"),
        html.P("Shortest round trip through all locations – the Travelling Salesman Problem"),
    ], className="app-header"),

    html.Div("No OpenRouteService API key found: copy API_KEY.example.txt to API_KEY.txt and paste your key. "
             "Locations can be added, but routes cannot be computed.",
             className="alert alert-warning", style={"display": "none" if API_KEY else "block"}),

    card(1, "Locations",
         # Searchable dropdown filled from the server while typing: arrows / click to choose, Enter = first hit.
         dcc.Dropdown(id="location-search", options=[], value=None, searchable=True, clearable=False,
                      placeholder="Search a place, e.g. Fribourg", search_order="original"),
         html.Div(id="error-message", className="message"),
         html.Div([
             dcc.Dropdown(id="example-select", value=None, clearable=False, searchable=False,
                          options=[{"label": label, "value": fn} for label, fn in locations_io.list_examples()],
                          placeholder="Load example…", className="grow"),
             dcc.Upload(html.Button("Import", className="btn",
                                    title="Load a CSV file (columns name, latitude, longitude; "
                                          "rows without coordinates are looked up)"),
                        id="csv-upload", accept=".csv,text/csv,text/plain", multiple=False),
             html.Button("Export", id="export-csv-button", n_clicks=0, className="btn",
                         title="Save the list as a CSV file"),
         ], className="row"),
         dcc.Download(id="download-locations"),
         html.Div(id="locations-list"),
         extra=html.Span([html.Span(id="locations-title", children="0 stops"),
                          html.Button("Clear all", id="clear-button", n_clicks=0, className="btn-link btn",
                                      title="Remove all locations")],
                         className="count"),
         ),

    card(2, "Solver",
         field("Mode of transport",
               dcc.RadioItems(id="transport", value="driving", className="segmented", inline=True,
                              options=[{"label": TRANSPORT_LABELS[m], "value": m} for m in TRANSPORT_LABELS],
                              persistence=True, persistence_type="local")),
         field("Method",
               dcc.Dropdown(id="method", options=[{"label": m, "value": m} for m in TSPMethodFactory.methods],
                            value="TwoOpt", clearable=False, searchable=False,
                            persistence=True, persistence_type="local"),
               html.Div(id="method-hint", className="hint")),
         html.Div([
             html.Div("Time limit", className="field-label grow"),
             dcc.Input(type="number", id="time-limit", min=1, step=1, value=60, className="number-input",
                       persistence=True, persistence_type="local"),
             html.Span("s", className="suffix"),
         ], className="row"),
         html.Button("Compute route", id="submit-button", n_clicks=0, className="btn btn-primary btn-block"),
         ),

], className="sidebar")

# Floating panel on the map: the result is visible next to the route without scrolling the sidebar.
result_panel = card(
    3, "Result",
    dcc.Loading(type="dot", color="#4f46e5", children=html.Div([
        html.Div(id="info", children=EMPTY_RESULT),
        html.Div(id="travel-time-display"),
        html.Div(id="path-display"),
    ], className="result-body")),
    html.Div([
        html.Button("Open in new tab", id="open-tab-button", n_clicks=0, className="btn grow"),
        html.Button("Download", id="save-route-button", n_clicks=0, className="btn grow",
                    title="Save the route as a standalone HTML page"),
    ], className="row result-actions"),
    class_name="card result-panel",
)


def serve_layout():
    # A layout function (instead of a static layout) gives every browser tab its own session id.
    return html.Div([
        sidebar,
        html.Div([dl.Map(id="map", center=[46.8, 8.2], zoom=8, className="map", children=[
            dl.LayersControl([
                # "Light": the standard OpenStreetMap tiles (free, no API key), faded with a CSS filter
                # (class tiles-light in assets/app.css) so the route and the stops stand out
                dl.BaseLayer(dl.TileLayer(
                    url="https://tile.openstreetmap.org/{z}/{x}/{y}.png", maxZoom=19, className="tiles-light",
                    attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'),
                    name="Light", checked=True),
                dl.BaseLayer(dl.TileLayer(
                    url="https://tile.openstreetmap.org/{z}/{x}/{y}.png", maxZoom=19,
                    attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'),
                    name="OpenStreetMap"),
            ], position="topright"),
            dl.LayerGroup(id="markers-layer"),
            dl.LayerGroup(id="route-layer"),
        ]), result_panel], className="map-wrap"),
        # storage_type="local": the locations survive page reloads and browser restarts (stored in the browser).
        dcc.Store(id="locations-store", data=[], storage_type="local"),  # [{'name', 'display_name', 'position'}]
        dcc.Store(id="result-store", data=None, storage_type="local"),  # last computed route, survives reloads
        dcc.Download(id="download-route"),
        dcc.Store(id="session-id", data=uuid.uuid4().hex),
        dcc.Store(id="bounds-store"),  # area to show; turned into the map viewport in the browser (see below)
    ], className="app")


app.layout = serve_layout


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------
def compute_bounds(locations):
    if not locations:
        return no_update
    lats = [loc["position"][0] for loc in locations]
    lngs = [loc["position"][1] for loc in locations]
    pad = 0.02  # degrees, so a single location does not zoom in to street level
    return [[min(lats) - pad, min(lngs) - pad], [max(lats) + pad, max(lngs) + pad]]


def make_markers(locations, order=None):
    """Circle markers. Without order: neutral dots with the name on hover.
    With order (location index -> position in the tour, 1-based): route-coloured dots showing the number."""
    markers = []
    for i, loc in enumerate(locations):
        popup = dl.Popup(html.Div([html.B(loc["name"]), html.Br(), loc.get("display_name", "")]))
        if order is None:
            markers.append(dl.CircleMarker(center=loc["position"], radius=7, color="#ffffff", weight=2,
                                           fillColor=STOP_COLOR, fillOpacity=1,
                                           children=[dl.Tooltip(loc["name"], direction="top", offset=[0, -6]), popup]))
        else:
            markers.append(dl.CircleMarker(center=loc["position"], radius=11, color="#ffffff", weight=2,
                                           fillColor=ROUTE_COLOR, fillOpacity=1,
                                           children=[dl.Tooltip(str(order[i]), permanent=True, direction="center",
                                                                className="stop-number"), popup]))
    return markers


def format_duration(minutes):
    hours, mins = divmod(int(round(minutes)), 60)
    return f"{hours} h {mins} min" if hours else f"{mins} min"


def error_box(text):
    return html.Div(text, className="alert alert-error")


# --------------------------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------------------------
SEARCH_DEBOUNCE = 0.35  # seconds without typing before suggestions are requested
_latest_search = {}     # session id -> last search text (server-side debounce)
_latest_lock = threading.Lock()


@app.callback(
    Output("location-search", "options"),
    Output("error-message", "children", allow_duplicate=True),
    Input("location-search", "search_value"),
    State("session-id", "data"),
    State("locations-store", "data"),
    prevent_initial_call=True,
)
def update_suggestions(search, session_id, locations):
    search = (search or "").strip()
    if len(search) < 3:
        return no_update, no_update
    # Dash calls this on every keystroke; only the last call of a typing burst asks the geocoder.
    with _latest_lock:
        _latest_search[session_id] = search
    time.sleep(SEARCH_DEBOUNCE)
    with _latest_lock:
        if _latest_search.get(session_id) != search:
            return no_update, no_update

    bias = None
    if locations:  # prefer candidates near the locations already chosen
        bias = (sum(l["position"][0] for l in locations) / len(locations),
                sum(l["position"][1] for l in locations) / len(locations))
    try:
        candidates = suggest(search, bias=bias)
    except ServiceError as e:
        return [], str(e)
    if not candidates:
        return [], f"No match for '{search}'."
    # 'search' = typed text, so the dropdown does not hide fuzzy matches (e.g. 'Fribou' -> 'Freiburg').
    options = [{"label": c["display_name"], "value": json.dumps(c), "search": search} for c in candidates]
    return options, ""


@app.callback(
    Output("locations-store", "data"),
    Output("location-search", "value"),
    Output("location-search", "options", allow_duplicate=True),
    Output("error-message", "children"),
    Input("location-search", "value"),
    Input("clear-button", "n_clicks"),
    Input({"type": "delete-button", "index": ALL}, "n_clicks"),
    State("locations-store", "data"),
    prevent_initial_call=True,
)
def update_locations(selected, _clear, _delete, locations):
    locations = list(locations or [])
    trigger = callback_context.triggered_id
    triggered_value = callback_context.triggered[0]["value"] if callback_context.triggered else None

    if trigger == "location-search":
        if not selected:
            return no_update, no_update, no_update, no_update
        c = json.loads(selected)
        lat, lon = c["lat"], c["lon"]
        if any(abs(l["position"][0] - lat) < 1e-6 and abs(l["position"][1] - lon) < 1e-6 for l in locations):
            return no_update, None, [], f"'{c['name']}' is already in the list."
        new = {"name": c["name"], "display_name": c["display_name"], "position": [lat, lon]}
        clashes = [l for l in locations if l["name"] == new["name"]]
        if clashes:  # same name twice (e.g. two Paris): show the full label for all of them
            for loc in clashes + [new]:
                loc["name"] = re.sub(r" \([^)]*\)$", "", loc["display_name"])
        locations.append(new)
        return locations, None, [], ""  # reset the search box for the next location

    if trigger == "clear-button":
        return [], no_update, no_update, ""

    if isinstance(trigger, dict) and trigger.get("type") == "delete-button":
        # Dash also fires this callback when delete buttons are (re)rendered; ignore those (n_clicks is None).
        if not triggered_value:
            return no_update, no_update, no_update, no_update
        index = trigger["index"]
        if 0 <= index < len(locations):
            del locations[index]
        return locations, no_update, no_update, ""

    return no_update, no_update, no_update, no_update


def _decode_upload(contents):
    """dcc.Upload delivers 'data:<type>;base64,<data>'. Excel may save CSV as UTF-8 (with BOM) or Windows-1252."""
    raw = base64.b64decode(contents.split(",", 1)[1])
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _first_suggestion(name):
    hits = suggest(name)
    return hits[0] if hits else None


@app.callback(
    Output("locations-store", "data", allow_duplicate=True),
    Output("error-message", "children", allow_duplicate=True),
    Output("transport", "value"),
    Output("example-select", "value"),
    Output("csv-upload", "contents"),
    Input("example-select", "value"),
    Input("csv-upload", "contents"),
    State("csv-upload", "filename"),
    prevent_initial_call=True,
)
def load_location_list(example, upload, upload_name):
    """Replaces the location list by a ready-made example or an imported CSV file."""
    transport = no_update
    if callback_context.triggered_id == "example-select":
        if not example:
            return no_update, no_update, no_update, no_update, no_update
        locations, warnings = locations_io.load_example(example)
        transport = locations_io.transport_for_example(example) or "driving"  # an example is a full scenario
        source = f"example '{example.rsplit('.', 1)[0]}'"
    else:
        if not upload:
            return no_update, no_update, no_update, no_update, no_update
        try:
            locations, warnings = locations_io.parse_csv(_decode_upload(upload), geocoder=_first_suggestion)
        except Exception as e:
            print("CSV import failed:", e)
            return no_update, f"Could not read '{upload_name}': {e}", no_update, None, None
        source = f"'{upload_name}'"

    if not locations:
        return no_update, " ".join(warnings), no_update, None, None
    text = f"Loaded {len(locations)} locations from {source}."
    if transport is not no_update:
        text += f" Mode of transport: {TRANSPORT_LABELS.get(transport, transport)}."
    message = [html.Span(text, className="success")]
    if warnings:
        message.append(html.Div(" ".join(warnings[:5]) + (" …" if len(warnings) > 5 else ""), className="details"))
    return locations, message, transport, None, None  # reset both controls so the same choice works again


@app.callback(Output("example-select", "options"), Input("session-id", "data"))
def refresh_examples(_session):
    """Runs on every page load, so CSV files added to the examples folder appear without a restart."""
    return [{"label": label, "value": fn} for label, fn in locations_io.list_examples()]


@app.callback(
    Output("download-locations", "data"),
    Output("error-message", "children", allow_duplicate=True),
    Input("export-csv-button", "n_clicks"),
    State("locations-store", "data"),
    prevent_initial_call=True,
)
def export_locations(_n, locations):
    if not locations:
        return no_update, "There are no locations to export."
    # BOM so that Excel opens umlauts (Zürich, Neuchâtel) correctly
    return dcc.send_string("\ufeff" + locations_io.to_csv(locations), filename="locations.csv"), ""


def route_signature(locations):
    """Identifies a location list; a stored route is only shown again if the locations still match."""
    return [[round(c, 6) for c in loc["position"]] for loc in locations]


def render_result(locations, result):
    """Map layer content and sidebar parts for a computed route (fresh or restored from the browser)."""
    order = [0] * len(locations)
    for position, idx in enumerate(result["tour"][:-1], start=1):
        order[idx] = position
    if result["optimal"]:
        info = html.Span("Optimal route – proven", className="badge badge-ok")
    else:
        info = html.Span("Good route – optimality not proven", className="badge badge-warn",
                         title="Heuristic method or time limit reached")
    stops = result["ordered_names"][:-1]
    stats = html.Div([
        html.Div([html.Div(format_duration(result["total_minutes"]), className="stat-value"),
                  html.Div("total travel time", className="stat-label")], className="stat"),
        html.Div([html.Div(str(len(stops)), className="stat-value"),
                  html.Div("stops", className="stat-label")], className="stat"),
        html.Div([html.Div(f"{result['solve_seconds']:.2f} s", className="stat-value"),
                  html.Div("solver time", className="stat-label")], className="stat"),
    ], className="stats")
    meta = html.Div(f"{result['method']} · {TRANSPORT_LABELS.get(result['transport'], result['transport'])}",
                    className="hint")
    tour = html.Ol([html.Li([html.Span(str(k), className="tour-num"), html.Span(name, className="loc-name")])
                    for k, name in enumerate(stops, start=1)]
                   + [html.Li([html.Span("↩", className="tour-num back"),
                               html.Span(f"back to {stops[0]}", className="loc-name hint")])],
                   className="tour")
    polyline = dl.Polyline(positions=result["route"], color=ROUTE_COLOR, weight=4, opacity=0.85)
    # Numbered markers go into the (freshly mounted) route layer: Leaflet cannot turn an existing tooltip permanent.
    return [polyline] + make_markers(locations, order), info, tour, html.Div([stats, meta])


@app.callback(
    Output("locations-list", "children"),
    Output("locations-title", "children"),
    Output("markers-layer", "children"),
    Output("bounds-store", "data"),
    Output("route-layer", "children"),
    Output("info", "children"),
    Output("path-display", "children"),
    Output("travel-time-display", "children"),
    Output("result-store", "data"),
    Input("locations-store", "data"),
    State("result-store", "data"),
)
def show_locations(locations, result):
    """Shows the location list. A stored route is kept (e.g. after a page reload) as long as it was computed
    for exactly these locations; any change of the list invalidates it."""
    locations = locations or []
    items = [
        html.Li([
            html.Span(str(i + 1), className="loc-index"),
            html.Span(loc["name"], title=loc.get("display_name", ""), className="loc-name"),
            html.Button("×", id={"type": "delete-button", "index": i}, title=f"Remove {loc['name']}",
                        className="loc-delete"),
        ], className="loc-item")
        for i, loc in enumerate(locations)
    ]
    listing = (html.Ul(items, className="loc-list") if items else
               html.Div("No locations yet – search above or load an example.", className="empty"))
    head = (listing, f"{len(locations)} stop{'s' if len(locations) != 1 else ''}")

    if result and result.get("signature") == route_signature(locations):
        route_children, info, path, timing = render_result(locations, result)
        return (*head, [], compute_bounds(locations), route_children, info, path, timing, no_update)
    return (*head, make_markers(locations), compute_bounds(locations), [], EMPTY_RESULT, "", "", None)


# Runs in the browser: zoom the map to the stops. On wide screens, leave room on the left for the
# floating result panel (it is 330 px wide and starts 56 px from the map edge).
app.clientside_callback(
    """
    function(bounds) {
        if (!bounds) { return window.dash_clientside.no_update; }
        const left = window.innerWidth > 900 ? 410 : 30;
        return {bounds: bounds, transition: "flyToBounds",
                options: {paddingTopLeft: [left, 30], paddingBottomRight: [30, 30], duration: 0.6}};
    }
    """,
    Output("map", "viewport"),
    Input("bounds-store", "data"),
)


@app.callback(Output("method-hint", "children"), Input("method", "value"))
def show_method_hint(method):
    return METHOD_HINTS.get(method, "")


@app.callback(
    Output("route-layer", "children", allow_duplicate=True),
    Output("markers-layer", "children", allow_duplicate=True),
    Output("info", "children", allow_duplicate=True),
    Output("path-display", "children", allow_duplicate=True),
    Output("travel-time-display", "children", allow_duplicate=True),
    Output("result-store", "data", allow_duplicate=True),
    Input("submit-button", "n_clicks"),
    State("locations-store", "data"),
    State("transport", "value"),
    State("method", "value"),
    State("time-limit", "value"),
    prevent_initial_call=True,
)
def compute_route(_n, locations, transport_mode, tsp_method, time_limit):
    locations = locations or []
    if len(locations) < 2:
        return no_update, no_update, error_box("Please add at least two locations to compute a route."), "", "", None
    time_limit = max(1, int(time_limit or 60))

    names = [loc["name"] for loc in locations]
    coordinates = [loc["position"] for loc in locations]
    try:
        result = TSPInterface.solve_tsp(names, coordinates, transport_mode, tsp_method, time_limit)
    except ServiceError as e:
        print("Route error:", e)
        return [], make_markers(locations), error_box(str(e)), "", "", None

    store = {
        "signature": route_signature(locations),
        "method": tsp_method,
        "transport": transport_mode,
        "optimal": result["optimal"],
        "tour": result["tour"],
        "ordered_names": result["ordered_names"],
        "total_minutes": result["total_minutes"],
        "solve_seconds": result["solve_seconds"],
        # 5 decimals (~1 m) keep the browser storage small for long routes
        "route": [[round(lat, 5), round(lon, 5)] for lat, lon in result["route"]],
    }
    route_children, info, path, timing = render_result(locations, store)
    return route_children, [], info, path, timing, store


def write_route_page(result, for_file=False):
    """(Re)creates the standalone route page from the stored result, so it always matches what is shown."""
    coordinates = [result["signature"][i] for i in result["tour"]]  # stop positions in visiting order
    subtitle = f"{result['method']} · {TRANSPORT_LABELS.get(result['transport'], result['transport'])}"
    return TravelTimeCalculator.visualize_tsp_tour(result["route"], result["ordered_names"], coordinates,
                                                   result["total_minutes"], for_file=for_file, subtitle=subtitle,
                                                   out_path=DOWNLOAD_HTML_PATH if for_file else ROUTE_HTML_PATH)


@app.server.route("/route-page")
def route_page():
    """Serves the last route page over http, so the browser sends a Referer header with the map tile
    requests (OpenStreetMap blocks tile requests from pages opened as local files)."""
    if not ROUTE_HTML_PATH.exists():
        return "No route computed yet.", 404
    return ROUTE_HTML_PATH.read_text(encoding="utf-8"), 200, {"Cache-Control": "no-store"}


@app.callback(
    Output("info", "children", allow_duplicate=True),
    Input("open-tab-button", "n_clicks"),
    State("result-store", "data"),
    prevent_initial_call=True,
)
def open_in_tab(_n, result):
    if not result:
        return error_box("Please compute a route first.")
    write_route_page(result)
    webbrowser.open(f"{flask.request.host_url}route-page", new=2)
    return no_update


@app.callback(
    Output("download-route", "data"),
    Output("info", "children", allow_duplicate=True),
    Input("save-route-button", "n_clicks"),
    State("result-store", "data"),
    prevent_initial_call=True,
)
def download_route(_n, result):
    if not result:
        return no_update, error_box("Please compute a route first.")
    path = write_route_page(result, for_file=True)
    stops = result["ordered_names"][:-1]
    slug = re.sub(r"[^A-Za-z0-9]+", "_", "_".join(s.split(",")[0] for s in stops[:4])).strip("_") or "route"
    if len(stops) > 4:
        slug += f"_and_{len(stops) - 4}_more"
    return dcc.send_file(str(path), filename=f"route_{slug}.html"), no_update


if __name__ == "__main__":
    # debug=True: auto-reload on code changes and Dash dev tools (callback errors shown in the browser).
    # use_debugger=False: without Werkzeug's interactive debugger, which on macOS leaves a
    # "leaked semaphore" warning behind when the server is stopped with Ctrl+C.
    app.run(debug=True, use_debugger=False)
