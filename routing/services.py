import bisect
import hashlib
import json
import math
import re
import urllib.parse
import urllib.request

import numpy as np
from django.core.cache import cache

from routing.models import FuelStation, Place

MAX_RANGE_MILES = 500
MPG = 10
# How far from the route a station may be and still count as "on the route". Exit-located stations sit on
# the highway, so a tight corridor keeps out exits on crossing highways. Town-centre ones need slack.
EXIT_CORRIDOR_MILES = 2
CORRIDOR_MILES = 10
CLUSTER_MILES = 25
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{},{};{},{}?overview=full&geometries=geojson"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search?"
USER_AGENT = "spotter-fuel-route/1.0"
LAT_LNG = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
CITY_STATE = re.compile(r"^\s*(.+?)\s*,\s*([A-Za-z]{2})\s*$")


class TripError(Exception):
    status = 400


class UpstreamError(TripError):
    status = 502


def normalize(name):
    name = name.lower().replace("saint ", "st ").replace("st. ", "st ").replace("sainte ", "ste ")
    return re.sub(r"[^a-z0-9]", "", name)


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except (OSError, ValueError) as e:
        raise UpstreamError(f"Upstream request failed: {e}") from e


def geocode(query):
    """'lat,lng' and 'City, ST' resolve locally; anything else costs one Nominatim call."""
    if m := LAT_LNG.match(query):
        lat, lng = float(m[1]), float(m[2])
    elif (m := CITY_STATE.match(query)) and (
        place := Place.objects.filter(key=normalize(m[1]), state=m[2].upper()).first()
    ):
        lat, lng = place.lat, place.lng
    else:
        params = urllib.parse.urlencode({"q": query, "format": "json", "countrycodes": "us", "limit": 1})
        results = fetch_json(NOMINATIM_URL + params)
        if not results:
            raise TripError(f"Could not find location: {query!r}")
        lat, lng = float(results[0]["lat"]), float(results[0]["lon"])
    # Rough contiguous-US bounding box, not an exact border check.
    if not (24 <= lat <= 50 and -125 <= lng <= -66):
        raise TripError(f"Location must be in the contiguous USA: {query!r}")
    return {"query": query, "lat": lat, "lng": lng}


def fetch_route(start, finish):
    data = fetch_json(OSRM_URL.format(start["lng"], start["lat"], finish["lng"], finish["lat"]))
    if data.get("code") != "Ok":
        raise UpstreamError(f"Routing failed: {data.get('message', data.get('code'))}")
    route = data["routes"][0]
    return route["geometry"], route["distance"] / 1609.344, route["duration"] / 3600


def resample(coords, route_miles):
    """Resample the route geometry to one point per mile: (mile_markers, lat, lng) arrays in degrees."""
    lng, lat = np.radians(np.array(coords)).T
    # Haversine segment lengths -> cumulative miles, scaled to OSRM's road distance.
    a = np.sin(np.diff(lat) / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(np.diff(lng) / 2) ** 2
    cum = np.concatenate([[0], np.cumsum(2 * 3958.8 * np.arcsin(np.sqrt(a)))])
    marks = np.append(np.arange(0, cum[-1], 1.0), cum[-1])
    scale = route_miles / cum[-1] if cum[-1] else 0
    return marks * scale, np.degrees(np.interp(marks, cum, lat)), np.degrees(np.interp(marks, cum, lng))


def stations_along(marks, s_lat, s_lng):
    """Return (mile_marker, station) for stations within CORRIDOR_MILES of the route, sorted by mile."""
    pad = CORRIDOR_MILES / 69
    stations = list(
        FuelStation.objects.filter(
            lat__range=(s_lat.min() - pad, s_lat.max() + pad),
            lng__range=(s_lng.min() - 2 * pad, s_lng.max() + 2 * pad),
        )
    )
    found = []
    for i in range(0, len(stations), 256):  # chunked to bound the distance matrix size
        chunk = stations[i : i + 256]
        p_lat = np.array([s.lat for s in chunk])[:, None]
        p_lng = np.array([s.lng for s in chunk])[:, None]
        dx = (p_lng - s_lng) * np.cos(np.radians(p_lat)) * 69.17
        dy = (p_lat - s_lat) * 69.0
        d2 = dx * dx + dy * dy
        nearest = d2.argmin(axis=1)
        for station, j, dist2 in zip(chunk, nearest, d2[np.arange(len(chunk)), nearest]):
            if dist2 <= (EXIT_CORRIDOR_MILES if station.at_exit else CORRIDOR_MILES) ** 2:
                found.append((float(marks[j]), station))
    found.sort(key=lambda x: (x[0], x[1].price))
    return found


def local_cheapest(along, window=CLUSTER_MILES):
    """Keep only stations that are the cheapest within +/- window miles.

    Without this the optimal plan stops for 1 gallon to save 0.6 cents a few miles later.
    Trade-off: can widen gaps between stations by up to 2*window miles.
    """
    miles = [m for m, _ in along]
    kept = []
    for m, s in along:
        nearby = along[bisect.bisect_left(miles, m - window) : bisect.bisect_right(miles, m + window)]
        if all(s.price <= other.price for _, other in nearby):
            kept.append((m, s))
    return kept


def plan_fuel_stops(miles, prices, total, max_range=MAX_RANGE_MILES):
    """Cheapest refuelling plan along a fixed route, starting with a full tank.

    Classic greedy: at a station, if a cheaper one is within range, buy just enough to reach it;
    else if the destination is in range, buy just enough to finish; else fill up and go to the
    cheapest station in range. Returns [(station_index, miles_of_fuel_bought)].
    """
    stops = []
    i, here, price, fuel = -1, 0.0, math.inf, max_range  # start isn't a station: nothing to buy
    while here + fuel < total:
        reach = here + max_range
        ahead = range(i + 1, bisect.bisect_right(miles, reach))
        cheaper = next((j for j in ahead if prices[j] < price), None)
        if cheaper is not None:
            nxt, target = cheaper, miles[cheaper]
        elif total <= reach:
            nxt, target = None, total
        elif ahead:
            nxt, target = min(ahead, key=prices.__getitem__), reach
        else:
            raise TripError(f"No fuel station within {max_range} miles after mile {here:.0f}")
        if (buy := target - here - fuel) > 0:
            stops.append((i, buy))
            fuel += buy
        if nxt is None:
            break
        fuel -= miles[nxt] - here
        i, here, price = nxt, miles[nxt], prices[nxt]
    return stops


def plan_trip(start_query, finish_query):
    key = "trip:" + hashlib.sha256(f"{start_query.strip().lower()}|{finish_query.strip().lower()}".encode()).hexdigest()
    if (trip := cache.get(key)) is not None:
        return trip

    start, finish = geocode(start_query), geocode(finish_query)
    geometry, route_miles, hours = fetch_route(start, finish)
    marks, lat, lng = resample(geometry["coordinates"], route_miles)
    along = local_cheapest(stations_along(marks, lat, lng))
    miles = [m for m, _ in along]
    plan = plan_fuel_stops(miles, [s.price for _, s in along], route_miles)

    fuel_stops, total_gallons, total_cost = [], 0.0, 0.0
    for idx, bought in plan:
        mile, s = along[idx]
        gallons = bought / MPG
        total_gallons += gallons
        total_cost += gallons * s.price
        fuel_stops.append(
            {
                "mile": round(mile, 1),
                "name": s.name,
                "address": s.address,
                "city": s.city,
                "state": s.state,
                "lat": s.lat,
                "lng": s.lng,
                "price_per_gallon": round(s.price, 3),
                "gallons": round(gallons, 2),
                "cost": round(gallons * s.price, 2),
            }
        )
    trip = {
        "start": start,
        "finish": finish,
        "distance_miles": round(route_miles, 1),
        "duration_hours": round(hours, 1),
        "total_gallons": round(total_gallons, 2),
        "total_fuel_cost": round(total_cost, 2),
        "fuel_stops": fuel_stops,
        "assumptions": {"max_range_miles": MAX_RANGE_MILES, "mpg": MPG, "starts_with_full_tank": True},
        # The 1-mile resample is plenty for a map and ~10x smaller than OSRM's full geometry.
        "route": {"type": "LineString", "coordinates": np.column_stack([lng, lat]).round(5).tolist()},
    }
    cache.set(key, trip)
    return trip
