Traveling Salesman Problem (TSP) Solver
=======================================

Introduction
------------
This project provides various algorithms and methods to solve the classic Traveling Salesman Problem (TSP).
The implementation took place as part of a Project at **HSLU (Lucerne School of Information Technology)** to function as a visual tool for the Modul of Industrial Optimization. Many Thanks go to **Lino Giger** who implemented the first version of this project.

About this fork
---------------
This fork of [LinoGiger/TSP_solver](https://github.com/LinoGiger/TSP_solver) keeps Lino Giger's original structure and extends it for use in teaching (2026):

- **Bug fixes:** geocoding works again (Nominatim now requires HTTPS and an identifying User-Agent), no more crashes on API errors, correct route drawing, no Tkinter dialog (crashed on macOS).
- **Location search as you type** (Photon), with ready-made examples and CSV import/export.
- **Solvers:** 2-opt runs until no improvement is left and handles asymmetric travel times; Permutations fixes the start; FlowBased uses a cleaner MTZ model; ConstraintProgramming now uses CP-SAT with `AddCircuit` and proves optimality. Only proven results are labelled "optimal".
- **Interface:** redesigned layout with a result panel on the map, numbered stops, locations and the last route kept across page reloads.

API Key generation
------------------
To use the App, you'll need a Free OpenRouteService API Key. 

**Steps to generate one:**

1. Visit the [OpenRouteService Sign Up page](https://openrouteservice.org/sign-up/).
2. Create an account or sign in if you already have one.
3. Once logged in, navigate to the Dashboard --> "Tokens" section.
4. Here, you can create a new API token or manage existing ones.

Enter the key in the app (step 2, *Solver*, field *OpenRouteService API key*). It is stored only in your browser and sent along with each route request; the server does not keep it.

For local use you can also store a default key: copy `API_KEY.example.txt` to `API_KEY.txt` and paste your key into it (or set the environment variable `ORS_API_KEY`). It is used whenever the field in the app is empty. `API_KEY.txt` is listed in `.gitignore`, so it is never committed.

Existing TSP Solving Methods
-----------------------------
The project currently incorporates methods such as `TwoOpt`, `Permutation`, `FlowBased`, and `ConstraintProgramming`.


Adding a New TSP Solving Method
-------------------------------
1. **Define the New Method**: 
    - Create a new class for your method in `tsp_logic.py` and make sure it inherits from the interface TSPMethod.
    - The cost matrix (travel times in seconds, possibly asymmetric) is available as `self.distance_matrix`. Implement `solve(self, max_time_seconds)` returning `(optimal, tour)`: `tour` starts and ends at node 0 (e.g. `[0, 2, 1, 3, 0]`), `optimal` is `True` only if your method *proves* optimality.
2. **Update the TSPMethodFactory**:
    - Modify the `TSPMethodFactory` class to handle the creation of an instance of your new method based on its name.
3. **GUI Integration**: 
    - To make your method available as an option on the dash website, nothing else is needed: the method dropdown in `app.py` is filled automatically from `TSPMethodFactory.methods`. Optionally add a one-line description to `METHOD_HINTS` in `app.py`.

Contributing
------------
To contribute to this project, ensure that any new methods or changes are thoroughly tested and documented. Bugfixes and improvments as well as new Methods are welcome.

Running the App
-----------------
After cloning the repository, start the app in a local virtual environment:

1. **Create the virtual environment** (only once, from the project folder):

    ```bash
    python3 -m venv .venv
    ```

2. **Activate it** (every time you open a new terminal):

    ```bash
    source .venv/bin/activate        # macOS / Linux
    .venv\Scripts\activate           # Windows
    ```

3. **Install the dependencies** (only once, or after `requirements.txt` changes):

    ```bash
    pip install -r requirements.txt
    ```

4. **Start the app**:

    ```bash
    python app.py
    ```

5. Open [http://127.0.0.1:8050](http://127.0.0.1:8050) in your web browser (app was tested on Chrome).

Stop the server with `Ctrl+C` and leave the virtual environment with `deactivate`.

Using the App
-------------
- Start typing a location in **Add location**: after three letters, matching places appear (e.g. `Fribou` → Fribourg, Switzerland (city) / Fribourg (region) / …). Press **Enter** to take the first hit, or choose another one with the arrow keys or the mouse. Once you have added locations, suggestions near them are ranked higher. Remove single locations with **×** or all with **Clear all**.
- Choose transport mode, solving method and time limit, then click **Compute route**. The stops are numbered on the map in visiting order.
- **Load example…** replaces the list with a ready-made scenario: *Around Fribourg* (8 towns, small enough for Permutations), *Luzern sightseeing (walking)* (7 sights, sets walking), *European capitals* (12, too many for Permutations within a minute) and *Swiss cantonal capitals* (26, for the exact methods FlowBased / ConstraintProgramming).
- **Export CSV** saves the current list; **Import CSV** loads a list (replacing the current one). See *Location lists (CSV)* below.
- Locations and settings (transport mode, method, time limit) are stored in your browser, so they survive a page reload or a browser restart. The last computed route is kept as well, as long as the list of locations is unchanged; adding or removing a location discards it. **Clear all** empties the list.
- **Open in new tab** shows the route as a standalone page; **Download** saves that page as an HTML file.

Run it online (Hugging Face Spaces)
-----------------------------------
The app can run for free as a Hugging Face *Space*; every user then enters their own OpenRouteService key.

1. Create a free account at [huggingface.co](https://huggingface.co) and a new Space: *New → Space*, SDK **Docker**, template *Blank*, hardware *CPU basic (free)*.
2. Upload the two files from [`deploy/huggingface/`](deploy/huggingface/) (`Dockerfile` and `README.md`) to the Space (*Files → Add file → Upload files*). The Space then builds the app directly from this GitHub repository.
3. After new commits on GitHub, update the Space with *Settings → Factory rebuild*.

Notes: the free hardware is shared (2 CPUs), so the solver time limit is capped at 120 s (`MAX_TIME_LIMIT`). A Space goes to sleep after about two days without visitors and wakes up on the next visit. Do not set `ORS_API_KEY` on a public Space, otherwise everybody uses your key.

The root `Dockerfile` runs the same setup anywhere else: `docker build -t tsp-visualizer . && docker run -p 7860:7860 tsp-visualizer`.

Location lists (CSV)
--------------------
Files need a header row; column names are case-insensitive:

```
name,latitude,longitude,display_name
Bern,46.9490,7.4390,"Bern, Switzerland"
Zürich,,,
```

- Only `name` is required. Rows without coordinates are looked up by name (at most 30 per file), so a plain list of town names works too.
- Semicolon-separated files and decimal commas (as saved by Excel with Swiss/German settings) are accepted; exported files open correctly in Excel.
- **Your own examples:** put a CSV file into the `examples` folder and reload the page; it appears in *Load example…* under its file name. A suffix `(walking)`, `(cycling)` or `(driving)` in the file name selects the mode of transport, e.g. `Bern old town (walking).csv`.

Troubleshooting
---------------
- *Location search*: suggestions come from [Photon](https://photon.komoot.io) (OpenStreetMap data, free, no key, built for search-as-you-type). If Photon is unavailable, the app falls back to OpenStreetMap Nominatim, which only returns the single best hit (Nominatim's usage policy forbids search-as-you-type, allows at most one request per second and requires an identifying User-Agent; the app takes care of this, see `USER_AGENT` in `tsp_logic.py`).
- *"OpenRouteService error 401/403"*: check the key entered in the app (or in `API_KEY.txt`).
- *Long routes (e.g. across continents)*: OpenRouteService limits route length per request; the app then requests each leg separately.

Specifying Locations
--------------------

If a location name overlaps with another, you can provide additional details to distinguish them. Use the format:

```
City Name, State Name, Country
```

This is similar to the format used in Google Maps.

Examples
--------

![Example Route through Europe](images/example_route_trough_europe.png "Example Route through Europe")
![Example Route with Same Names](images/example_route_same_names.png "Example Route with Same Names")
![Example Route through USA (saved)](images/example_route_trough_USA.png "Example Route through USA (saved)")
