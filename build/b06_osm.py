"""The OSM bike network for the five boroughs, with edge lengths and bike travel times.

Writes data/raw/nyc_bike.graphml. b07 turns it into the parquet the app loads, so the deployed
app doesn't need OSMnx.
"""

import osmnx as ox

from common import RAW

PLACES = ["Manhattan, New York, USA", "Brooklyn, New York, USA", "Queens, New York, USA",
          "The Bronx, New York, USA", "Staten Island, New York, USA"]


def main() -> None:
    path = RAW / "nyc_bike.graphml"
    if path.exists():
        graph = ox.load_graphml(path)
    else:
        ox.settings.timeout = 600
        boroughs = ox.geocode_to_gdf(PLACES).union_all()
        # retain_all: Staten Island has no bike link to the rest of the city, so it is its own component.
        graph = ox.graph_from_polygon(boroughs, network_type="bike", simplify=True, retain_all=True)
        ox.save_graphml(graph, path)
    print(f"graph: {graph.number_of_nodes():,} nodes, {graph.number_of_edges():,} directed edges")


if __name__ == "__main__":
    main()
