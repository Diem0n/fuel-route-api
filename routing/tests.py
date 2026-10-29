from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from routing.management.commands.load_data import locate_exit
from routing.models import FuelStation
from routing.services import TripError, plan_fuel_stops

# Straight east-west line along lat 40, roughly 790 miles.
FAKE_OSRM = {
    "code": "Ok",
    "routes": [
        {
            "distance": 790 * 1609.344,
            "duration": 12 * 3600,
            "geometry": {"type": "LineString", "coordinates": [[-100, 40], [-85, 40]]},
        }
    ],
}


class PlanFuelStopsTests(SimpleTestCase):
    def test_short_trip_needs_no_fuel(self):
        self.assertEqual(plan_fuel_stops([100], [3.0], total=400), [])

    def test_buys_only_enough_to_reach_cheaper_station(self):
        # Arrive at the expensive 400 with 100 miles left: buy 100 to reach the cheap 600, buy the rest there.
        stops = plan_fuel_stops([400, 600], [4.0, 3.0], total=1000)
        self.assertEqual(stops, [(0, 100), (1, 400)])

    def test_fills_up_at_cheap_station_when_nothing_cheaper_ahead(self):
        # Cheap at 300, expensive at 700: fill the tank at 300 so we barely buy at 700.
        stops = plan_fuel_stops([300, 700], [3.0, 4.0], total=1200)
        self.assertEqual(stops, [(0, 300), (1, 400)])

    def test_gap_longer_than_range_is_an_error(self):
        with self.assertRaises(TripError):
            plan_fuel_stops([100, 700], [3.0, 3.0], total=1000)


class RouteViewTests(TestCase):
    def setUp(self):
        cache.clear()
        for opis_id, lng, price in [(1, -97, 3.5), (2, -94, 3.0), (3, -90, 2.0)]:
            FuelStation.objects.create(
                opis_id=opis_id,
                name=f"Stop {opis_id}",
                address="I-80",
                city="X",
                state="NE",
                price=price,
                lat=40,
                lng=lng,
            )
        # Far from the route, must be ignored.
        FuelStation.objects.create(
            opis_id=4, name="Off route", address="", city="Y", state="TX", price=1.0, lat=30, lng=-95
        )

    @patch("routing.services.fetch_json", return_value=FAKE_OSRM)
    def test_route_makes_one_external_call_and_caches(self, fetch):
        url = "/api/route/?start=40,-100&finish=40,-85"
        data = self.client.get(url).json()
        self.client.get(url)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(data["distance_miles"], 790)
        # Stop 3 (cheapest) is ~527 mi in, past the starting range: top up just enough at Stop 2.
        stops = data["fuel_stops"]
        self.assertEqual([s["name"] for s in stops], ["Stop 2", "Stop 3"])
        self.assertLess(stops[0]["gallons"], 5)
        self.assertAlmostEqual(data["total_fuel_cost"], sum(s["cost"] for s in stops), places=1)
        self.assertIn("/api/route/map/", data["map_url"])

    def test_missing_params_is_400(self):
        self.assertEqual(self.client.get("/api/route/?start=40,-100").status_code, 400)

    def test_outside_usa_is_400(self):
        self.assertEqual(self.client.get("/api/route/?start=51.5,-0.1&finish=40,-85").status_code, 400)


class LocateExitTests(SimpleTestCase):
    exits = {283: [(36.62, -95.15), (40.0, -80.0)]}

    def test_picks_named_exit_nearest_the_town(self):
        self.assertEqual(locate_exit("I-44, EXIT 283 & US-69", 36.6, -95.2, self.exits), (36.62, -95.15))

    def test_no_exit_or_too_far_falls_back(self):
        self.assertIsNone(locate_exit("US-87 & Dorsey Rd", 36.6, -95.2, self.exits))
        self.assertIsNone(locate_exit("I-44, EXIT 283", 30.0, -100.0, self.exits))
