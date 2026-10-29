# Fuel Route API

Django API that takes a start and finish location in the USA and returns the driving route, the
cheapest places to refuel along it, and the total fuel cost. The vehicle has a 500-mile range and
gets 10 MPG.

## Setup

Requires Python 3.12+.

```bash
uv sync && source .venv/bin/activate   # or: python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
python manage.py migrate
python manage.py load_data             # geocodes the fuel price CSV (~5s, downloads the Census gazetteer once)
python manage.py runserver
```

## API

`GET /api/route/?start=<location>&finish=<location>`

A location can be `City, ST` (resolved from the local DB), `lat,lng`, or any free-text address
(resolved with one Nominatim call).

```
GET /api/route/?start=New York, NY&finish=Los Angeles, CA
```

```json
{
  "start": {"query": "New York, NY", "lat": 40.662712, "lng": -73.938677},
  "finish": {"query": "Los Angeles, CA", "lat": 34.019394, "lng": -118.410825},
  "distance_miles": 2810.4,
  "duration_hours": 50.3,
  "total_gallons": 231.04,
  "total_fuel_cost": 698.82,
  "fuel_stops": [
    {"mile": 395.6, "name": "SHEETZ #639", "address": "I-80 Exit 223", "city": "Youngstown", "state": "OH",
     "lat": 41.099095, "lng": -80.645902, "price_per_gallon": 3.059, "gallons": 6.19, "cost": 18.92}
  ],
  "assumptions": {"max_range_miles": 500, "mpg": 10, "starts_with_full_tank": true},
  "route": {"type": "LineString", "coordinates": [[-74.0, 40.71], "..."]},
  "map_url": "http://localhost:8000/api/route/map/?start=..."
}
```

`GET /api/route/map/?start=...&finish=...` renders the same trip on a Leaflet map. Open `map_url`
in a browser.

Import `postman_collection.json` into Postman for ready-made requests.

## How it works

1. **Station geocoding (offline, once).** The CSV has no coordinates, so `load_data` places each
   station in two steps:
   - The city/state is matched against the US Census places and county-subdivision gazetteers.
     97% of US rows match; Canadian rows are dropped.
   - When the address names an exit (`I-44, EXIT 283 & US-69`), the station is moved to that exit,
     using the exit nearest the town in `data/us_exits.tsv`. That file holds every US motorway exit
     from OpenStreetMap. This puts 53% of stations at their exact exit. The rest stay at the town
     centre.

   Duplicate rows for the same truck stop keep the cheapest price.
2. **Routing: one external call.** OSRM's public server (free, no API key) returns the full route
   geometry. `City, ST` and `lat,lng` inputs are geocoded locally, so a typical request makes exactly
   one external API call. Results are cached, so repeat requests return in about 1 ms.
3. **Stations on the route.** The geometry is resampled to one point per mile, and numpy finds each
   candidate station's nearest route point. A station placed at its exit counts as on the route
   within 2 miles, which keeps out exits on crossing highways. A town-centre station gets 10 miles.
4. **Cheapest plan.** Standard greedy algorithm for refuelling on a fixed route: at each station, if a
   cheaper station is within range, buy just enough fuel to get there. If not, fill the tank and go
   to the cheapest station in range. Before planning, a station is dropped when a cheaper one is
   within 25 miles. That avoids stopping for 1 gallon to save half a cent.

## Assumptions and limits

- The vehicle starts with a full tank. `total_fuel_cost` is the money spent at fuel stops.
- Stations without an exit number in their address are placed at the town centre, so their mile
  marker is approximate. Stations at an exit are on the highway, so there is no detour to price in.
- If two highways near a town share an exit number, the nearest one wins.
- OSRM's demo server is rate-limited and has no SLA. Production would self-host OSRM or use a paid
  routing API.

## Tests

```bash
python manage.py test
```

## Data credits

Exit locations: © OpenStreetMap contributors, available under the
[Open Database License](https://www.openstreetmap.org/copyright). Town centroids: US Census Bureau gazetteer files.
