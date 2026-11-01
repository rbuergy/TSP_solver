import html
import re
import threading
import time
from functools import lru_cache
from itertools import permutations

import folium
import numpy as np
import requests
from ortools.sat.python import cp_model
from ortools.linear_solver import pywraplp

# Nominatim's usage policy (https://operations.osmfoundation.org/policies/nominatim/) requires
# HTTPS, an identifying User-Agent and at most 1 request per second. Requests sent with the
# default "python-requests/x.y" agent are blocked with an HTML "Access blocked" page.
USER_AGENT = "TSP-Visualizer/2.0 (teaching tool, Decision Science course)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
ORS_URL = "https://api.openrouteservice.org/v2"
REQUEST_TIMEOUT = 30  # seconds

ROUTE_COLOR = "#e11d48"  # same red as in the app (assets/app.css)


class ServiceError(Exception):
    """Raised when an external service (geocoding / routing) fails. The message is shown in the UI."""


# --------------------------------------------------------------------------------------------
# Geocoding
# --------------------------------------------------------------------------------------------
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})
_nominatim_lock = threading.Lock()
_last_nominatim_call = 0.0


def geocode(query):
    """Return (lat, lon, display_name) for a free-text location, or None if nothing was found."""
    global _last_nominatim_call
    with _nominatim_lock:  # respect the 1 request/second limit
        wait = 1.0 - (time.time() - _last_nominatim_call)
        if wait > 0:
            time.sleep(wait)
        try:
            response = _session.get(
                NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": 1},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            raise ServiceError(f"Could not reach the geocoding service: {e}") from e
        finally:
            _last_nominatim_call = time.time()

    if response.status_code != 200:
        raise ServiceError(f"Geocoding service answered with HTTP {response.status_code}.")
    try:
        results = response.json()
    except ValueError as e:
        raise ServiceError("Geocoding service returned an unexpected (non-JSON) answer.") from e
    if not results:
        return None
    first = results[0]
    return float(first["lat"]), float(first["lon"]), first.get("display_name", query)


# Suggestions while typing. Nominatim's policy forbids search-as-you-type on its public server,
# so suggestions come from Photon (komoot), an OSM geocoder built for this purpose.
PHOTON_URL = "https://photon.komoot.io/api/"
PHOTON_TYPES = {"city": "city", "town": "town", "village": "village", "hamlet": "hamlet",
                "suburb": "suburb", "county": "district", "district": "district", "state": "region",
                "country": "country", "street": "street", "house": "address"}


def _photon_label(props):
    parts = []
    for key in ("name", "street", "city", "county", "state", "country"):
        value = props.get(key)
        if key == "street" and value and props.get("housenumber"):
            value = f"{value} {props['housenumber']}"
        if value and value not in parts:
            parts.append(value)
    label = ", ".join(parts) or "?"
    kind = PHOTON_TYPES.get(props.get("type")) or props.get("osm_value", "").replace("_", " ")
    return f"{label} ({kind})" if kind else label


@lru_cache(maxsize=512)
def suggest(query, limit=6, bias=None):
    """Return up to `limit` candidates [{'name', 'display_name', 'lat', 'lon'}] for a partial query.
    bias: optional (lat, lon) to prefer results near the locations already chosen."""
    params = {"q": query, "limit": limit}
    if bias:
        params.update({"lat": round(bias[0], 2), "lon": round(bias[1], 2), "location_bias_scale": 0.3})
    try:
        response = _session.get(PHOTON_URL, params=params, timeout=10)
        response.raise_for_status()
        features = response.json().get("features", [])
    except (requests.RequestException, ValueError) as e:
        print("Photon suggestion service failed, falling back to Nominatim:", e)
        hit = geocode(query)
        return tuple([] if hit is None else [{"name": hit[2].split(",")[0], "display_name": hit[2],
                                              "lat": hit[0], "lon": hit[1]}])
    results, seen = [], set()
    for f in features:
        lon, lat = f["geometry"]["coordinates"]
        label = _photon_label(f.get("properties", {}))
        if label in seen:
            continue
        seen.add(label)
        results.append({"name": f["properties"].get("name") or label, "display_name": label, "lat": lat, "lon": lon})
    return tuple(results)


# --------------------------------------------------------------------------------------------
# Travel times and routes (OpenRouteService)
# --------------------------------------------------------------------------------------------
class TravelTimeCalculator:
    mode_mapping = {
        "driving": "driving-car",
        "walking": "foot-walking",
        "cycling": "cycling-regular",
    }

    def __init__(self, api_key):
        self.api_key = (api_key or "").strip()

    @staticmethod
    def _label_coordinates(message, labels):
        """ORS refers to points by their index in the request, e.g. 'coordinate 1: 7.10 46.67' or
        'points 1 (7.10 46.67) and 2 (...)'. Add the location name so the user knows which one is meant."""
        if not labels:
            return message

        def add_label(match):
            idx = int(match.group(1))
            lon, lat = float(match.group(3)), float(match.group(4))
            name = labels[idx] if 0 <= idx < len(labels) else "?"
            return f"{idx} '{name}' ({lat:.5f}, {lon:.5f})"

        return re.sub(r"\b(\d+)(:\s*|\s*\()(-?\d+(?:\.\d+)?)[ ,]+(-?\d+(?:\.\d+)?)\)?", add_label, str(message))

    def _post(self, path, body, labels=None):
        if not self.api_key:
            raise ServiceError("Please enter your OpenRouteService API key (in step 2, Solver).")
        headers = {
            "Accept": "application/json, application/geo+json",
            "Authorization": self.api_key,
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
        }
        try:
            response = requests.post(f"{ORS_URL}/{path}", headers=headers, json=body, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            raise ServiceError(f"Could not reach OpenRouteService: {e}") from e
        if response.status_code != 200:
            try:
                detail = response.json().get("error", {})
                detail = detail.get("message", detail) if isinstance(detail, dict) else detail
            except ValueError:
                detail = response.text[:200]
            detail = self._label_coordinates(detail, labels)
            if response.status_code in (401, 403):
                detail = f"{detail} (is your OpenRouteService API key valid?)"
            elif "routable point" in str(detail):
                detail = (f"{str(detail).rstrip('.')}. This location is too far from a road usable for this mode of transport; "
                          f"try a more precise address (e.g. 'Bahnhof, <town>') or another mode of transport.")
            raise ServiceError(f"OpenRouteService error {response.status_code}: {detail}")
        return response.json()

    def get_travel_time(self, coordinates, mode, labels=None):
        """coordinates: list of (lat, lon). Returns an n x n matrix of durations in seconds (None = unreachable)."""
        body = {
            "locations": [[lon, lat] for lat, lon in coordinates],  # ORS expects [lon, lat]
            "metrics": ["duration"],
        }
        return self._post(f"matrix/{self.mode_mapping[mode]}", body, labels)["durations"]

    def get_route(self, coordinates, mode, labels=None):
        """Road geometry through the given (lat, lon) points in order. Returns a list of (lat, lon)."""
        body = {"coordinates": [[lon, lat] for lat, lon in coordinates]}
        data = self._post(f"directions/{self.mode_mapping[mode]}/geojson", body, labels)
        if not data.get("features"):
            raise ServiceError("OpenRouteService returned no route.")
        return [(p[1], p[0]) for p in data["features"][0]["geometry"]["coordinates"]]

    def get_tour_geometry(self, coordinates, mode, labels=None):
        """Geometry for a closed tour. Tries a single request first, falls back to one request per leg
        (ORS limits the total length of a single directions request)."""
        try:
            return self.get_route(coordinates, mode, labels)
        except ServiceError as e:
            if len(coordinates) <= 2 or "routable point" in str(e):  # splitting into legs would not help
                raise
        labels = labels or [None] * len(coordinates)
        geometry = []
        for k in range(len(coordinates) - 1):
            geometry.extend(self.get_route(coordinates[k:k + 2], mode, labels[k:k + 2]))
        return geometry

    @staticmethod
    def visualize_tsp_tour(route, ordered_names, ordered_coordinates, total_duration_minutes,
                           for_file=False, subtitle=""):
        """Return a standalone HTML page (as text) with the route, used by 'Open in new tab' and 'Download'.

        Base maps: OpenStreetMap's tile servers refuse requests without an HTTP Referer header, which is
        what a page opened from disk (file://) sends. So a page meant to be saved as a file starts with
        Esri's light grey map; the page served by the app (http://127.0.0.1:8050/route-page) starts with
        OpenStreetMap. Both layers can be switched in the page (top right)."""
        m = folium.Map(tiles=None, control_scale=True)
        osm = folium.TileLayer("OpenStreetMap", name="OpenStreetMap", max_zoom=19)
        esri = folium.TileLayer(
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
            attr="Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors",
            name="Light grey (Esri)", max_zoom=16)
        first, second = (esri, osm) if for_file else (osm, esri)
        second.show = False  # otherwise folium shows all base layers and the last one ends up on top
        first.add_to(m)
        second.add_to(m)
        folium.LayerControl(position="topright").add_to(m)

        folium.PolyLine(route, color=ROUTE_COLOR, weight=4, opacity=0.85).add_to(m)
        for k, (name, pos) in enumerate(zip(ordered_names[:-1], ordered_coordinates[:-1]), start=1):
            folium.Marker(
                location=pos, tooltip=html.escape(name), popup=html.escape(name),
                icon=folium.DivIcon(icon_size=(24, 24), icon_anchor=(12, 12), class_name="stop",
                                    html=f'<div class="stop-num">{k}</div>'),
            ).add_to(m)
        lats = [p[0] for p in route]
        lons = [p[1] for p in route]
        m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], padding_top_left=(320, 20),
                     padding_bottom_right=(20, 20))

        hours, minutes = divmod(int(round(total_duration_minutes)), 60)
        duration = f"{hours} h {minutes} min" if hours else f"{minutes} min"
        stops = "".join(f'<li><span class="num">{k}</span>{html.escape(n)}</li>'
                        for k, n in enumerate(ordered_names[:-1], start=1))
        stops += f'<li class="back"><span class="num">&#8617;</span>back to {html.escape(ordered_names[0])}</li>'
        panel = f"""
        <style>
          .route-panel {{ position: absolute; top: 12px; left: 56px; z-index: 1000; width: 260px;
            max-height: calc(100% - 24px); overflow-y: auto; background: #fff; border-radius: 10px;
            box-shadow: 0 6px 24px rgba(15,23,42,.16); padding: 14px;
            font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; color: #1f2933; }}
          .route-panel h1 {{ font-size: 16px; margin: 0 0 2px; }}
          .route-panel .sub {{ color: #66727f; font-size: 12.5px; margin-bottom: 10px; }}
          .route-panel .dur {{ font-size: 20px; font-weight: 650; }}
          .route-panel .lbl {{ color: #66727f; font-size: 12px; margin-bottom: 10px; }}
          .route-panel ol {{ list-style: none; margin: 0; padding: 0; }}
          .route-panel li {{ display: flex; align-items: center; gap: 8px; padding: 2px 0; }}
          .route-panel .num, .stop-num {{ width: 22px; height: 22px; border-radius: 50%; background: {ROUTE_COLOR};
            color: #fff; font-size: 11.5px; font-weight: 700; display: inline-flex; align-items: center;
            justify-content: center; flex: none; font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }}
          .stop-num {{ border: 2px solid #fff; box-shadow: 0 1px 3px rgba(0,0,0,.3); }}
          .route-panel .back {{ color: #66727f; }}
          .route-panel .back .num {{ background: none; color: {ROUTE_COLOR}; border: 1.5px solid {ROUTE_COLOR}; }}
        </style>
        <div class="route-panel">
          <h1>TSP route</h1>
          <div class="sub">{html.escape(subtitle)}</div>
          <div class="dur">{duration}</div>
          <div class="lbl">total travel time</div>
          <ol>{stops}</ol>
        </div>"""
        m.get_root().html.add_child(folium.Element(panel))
        m.get_root().title = "TSP route"
        return m.get_root().render()


# --------------------------------------------------------------------------------------------
# TSP methods
# --------------------------------------------------------------------------------------------
# Contract: solve(max_time_seconds) returns (optimal, tour)
#   optimal: True only if the method PROVED that the tour is optimal
#   tour:    array of node indices starting and ending at node 0, e.g. [0, 2, 1, 3, 0] (None if no tour)
# The distance matrix may be asymmetric (travel times A->B and B->A differ).

class TSPMethod:
    def __init__(self, distance_matrix):
        self.distance_matrix = np.array(distance_matrix, dtype=float)

    def tour_length(self, tour):
        return sum(self.distance_matrix[tour[i], tour[i + 1]] for i in range(len(tour) - 1))

    def solve(self, max_time_seconds):
        raise NotImplementedError("This method should be overridden in subclasses")


class TwoOptMethod(TSPMethod):
    """Nearest-neighbour start tour, improved by 2-opt moves until no improvement is left
    (local optimum) or the time limit is reached. A heuristic: optimality is not proven."""

    def solve(self, max_time_seconds):
        start_time = time.time()
        d = self.distance_matrix
        n = d.shape[0]

        # Step 1: nearest-neighbour construction starting at node 0
        tour = [0]
        unvisited = set(range(1, n))
        while unvisited:
            nxt = min(unvisited, key=lambda j: d[tour[-1], j])
            tour.append(nxt)
            unvisited.remove(nxt)
        tour.append(0)

        # Step 2: 2-opt. Reversing tour[i+1..j] also reverses the direction of the inner edges,
        # which matters for asymmetric matrices, so the whole affected segment is re-evaluated.
        best = self.tour_length(tour)
        improved = True
        while improved:
            improved = False
            for i in range(0, n - 1):
                for j in range(i + 2, n):
                    if time.time() - start_time > max_time_seconds:
                        print("TwoOpt: time limit reached.")
                        return False, np.array(tour)
                    candidate = tour[:i + 1] + tour[i + 1:j + 1][::-1] + tour[j + 1:]
                    length = self.tour_length(candidate)
                    if length < best - 1e-9:
                        tour, best, improved = candidate, length, True
        return n <= 3, np.array(tour)


class PermutationsMethod(TSPMethod):
    """Complete enumeration. Node 0 is fixed as start, so (n-1)! tours are checked."""

    def solve(self, max_time_seconds):
        start_time = time.time()
        n = self.distance_matrix.shape[0]
        best_tour, best_length = None, np.inf

        for count, perm in enumerate(permutations(range(1, n))):
            if count % 1000 == 0 and time.time() - start_time > max_time_seconds:
                print("Permutations: time limit reached.")
                return False, (np.array(best_tour) if best_tour is not None else None)
            tour = (0,) + perm + (0,)
            length = self.tour_length(tour)
            if length < best_length:
                best_tour, best_length = tour, length

        return True, np.array(best_tour)


class FlowBasedMethod(TSPMethod):
    """MIP with Miller-Tucker-Zemlin subtour elimination, solved with SCIP."""

    def solve(self, max_time_seconds):
        d = self.distance_matrix
        n = d.shape[0]
        solver = pywraplp.Solver.CreateSolver("SCIP")
        solver.set_time_limit(int(max_time_seconds * 1000))

        x = {(i, j): solver.BoolVar(f"x_{i}_{j}") for i in range(n) for j in range(n) if i != j}
        u = [None] + [solver.IntVar(1, n - 1, f"u_{i}") for i in range(1, n)]

        for i in range(n):
            solver.Add(solver.Sum(x[i, j] for j in range(n) if j != i) == 1)  # leave each city once
            solver.Add(solver.Sum(x[j, i] for j in range(n) if j != i) == 1)  # enter each city once
        for i in range(1, n):
            for j in range(1, n):
                if i != j:
                    solver.Add(u[i] - u[j] + (n - 1) * x[i, j] <= n - 2)  # MTZ subtour elimination

        solver.Minimize(solver.Sum(d[i, j] * x[i, j] for (i, j) in x))
        status = solver.Solve()

        if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
            print("FlowBased: no solution found within the time limit.")
            return False, None

        tour, current = [0], 0
        while len(tour) < n:
            current = next(j for j in range(n) if j != current and x[current, j].solution_value() > 0.5)
            tour.append(current)
        tour.append(0)
        return status == pywraplp.Solver.OPTIMAL, np.array(tour)


class ConstraintProgrammingMethod(TSPMethod):
    """Constraint programming with OR-Tools CP-SAT: one Boolean per arc and the global AddCircuit
    constraint (the chosen arcs must form a single Hamiltonian circuit). CP-SAT stops as soon as it has
    proven optimality, otherwise at the time limit with the best tour found so far.
    Costs are rounded to whole seconds (CP-SAT works with integers)."""

    def solve(self, max_time_seconds):
        d = np.round(self.distance_matrix).astype(int)
        n = d.shape[0]

        model = cp_model.CpModel()
        x = {(i, j): model.NewBoolVar(f"x_{i}_{j}") for i in range(n) for j in range(n) if i != j}
        model.AddCircuit([(i, j, lit) for (i, j), lit in x.items()])
        model.Minimize(sum(int(d[i, j]) * lit for (i, j), lit in x.items()))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(max_time_seconds)
        solver.parameters.num_workers = 8
        status = solver.Solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            print(f"ConstraintProgramming: no solution found ({solver.StatusName(status)}).")
            return False, None

        successor = {i: j for (i, j), lit in x.items() if solver.Value(lit)}
        tour = [0]
        while len(tour) <= n:
            tour.append(successor[tour[-1]])
        if status != cp_model.OPTIMAL:
            print("ConstraintProgramming: time limit reached, optimality not proven.")
        return status == cp_model.OPTIMAL, np.array(tour)


class TSPMethodFactory:
    # add new methods here
    methods = {
        "TwoOpt": TwoOptMethod,
        "Permutations": PermutationsMethod,
        "FlowBased": FlowBasedMethod,
        "ConstraintProgramming": ConstraintProgrammingMethod,
    }

    @classmethod
    def create_method(cls, method_name, distance_matrix):
        if method_name not in cls.methods:
            raise ValueError(f"Unknown method: {method_name}")
        return cls.methods[method_name](distance_matrix)


# --------------------------------------------------------------------------------------------
# Facade used by the app
# --------------------------------------------------------------------------------------------
class TSPSolverInterface:
    def __init__(self, api_key):
        self.calculator = TravelTimeCalculator(api_key)

    def solve_tsp(self, names, coordinates, mode, method_name, max_time_seconds):
        """names: list of labels, coordinates: list of (lat, lon). Raises ServiceError on failure."""
        travel_times = self.calculator.get_travel_time(coordinates, mode, names)
        print("Cost matrix (seconds):", travel_times)

        unreachable = [f"{names[i]} -> {names[j]}"
                       for i, row in enumerate(travel_times) for j, t in enumerate(row) if t is None]
        if unreachable:
            raise ServiceError("No route possible for: " + ", ".join(unreachable))

        method = TSPMethodFactory.create_method(method_name, travel_times)
        t0 = time.time()
        optimal, tour = method.solve(max_time_seconds)
        solve_seconds = time.time() - t0
        if tour is None:
            raise ServiceError(f"{method_name} found no tour within {max_time_seconds} s. Try a larger time limit.")

        tour = [int(i) for i in tour]
        total_minutes = method.tour_length(tour) / 60
        ordered_names = [names[i] for i in tour]
        ordered_coordinates = [tuple(coordinates[i]) for i in tour]
        print(f"{method_name}: tour {tour}, {total_minutes:.1f} min, optimal={optimal}, {solve_seconds:.2f} s")

        route = self.calculator.get_tour_geometry(ordered_coordinates, mode, ordered_names)
        self.calculator.visualize_tsp_tour(route, ordered_names, ordered_coordinates, total_minutes)

        return {
            "optimal": bool(optimal),
            "tour": tour,
            "ordered_names": ordered_names,
            "total_minutes": total_minutes,
            "solve_seconds": solve_seconds,
            "route": route,
        }
