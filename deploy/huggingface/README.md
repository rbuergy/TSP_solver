---
title: TSP Visualizer
emoji: 🗺️
colorFrom: indigo
colorTo: red
sdk: docker
app_port: 7860
pinned: false
short_description: Travelling Salesman Problem visualizer for teaching
---

# TSP Visualizer

Interactive Travelling Salesman Problem solver for teaching decision science: add places, pick a solving
method (2-opt heuristic, enumeration, MIP, CP-SAT) and compare the resulting round trips on the map.

You need a free [OpenRouteService API key](https://openrouteservice.org/dev/#/signup); enter it in the app
(step 2, Solver). It is stored only in your browser.

Source code: <https://github.com/rbuergy/TSP_solver>. This Space builds the app from that repository;
after a new commit there, use *Settings → Factory rebuild* to update.
