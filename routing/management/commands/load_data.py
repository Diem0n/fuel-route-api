import csv
import io
import math
import re
import urllib.request
import zipfile
from collections import Counter, defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from routing.models import FuelStation, Place
from routing.services import normalize

GAZETTEER = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_{}_national.zip"
# Census name suffixes: "Tomah city", "Abanda CDP", "Nashville-Davidson metropolitan government (balance)"
SUFFIX = re.compile(r"\s+(?:CDP|CCD|[a-z][a-z ]*?)(?:\s*\(balance\))?$")
EXIT = re.compile(r"\bEX(?:IT)?\.?\s*#?\s*(\d+)", re.IGNORECASE)  # "EXIT 283", "Exit 77B", "EX 360", "EXIT #12"
EXIT_SEARCH_MILES = 25  # how far from the town centre a station's exit can be


def aliases(census_name):
    name = re.sub(r"^(Town|City|Village) of ", "", SUFFIX.sub("", census_name))
    yield name
    if "-" in name:  # "Augusta-Richmond County" -> "Augusta"
        yield name.split("-")[0]
    if name.endswith(" City"):  # "Boise City" -> "Boise"
        yield name[:-5]


def read_gazetteer(kind):
    """Yield (key, state, lat, lng) rows; downloads the file once into data/."""
    path = settings.BASE_DIR / "data" / f"gaz_{kind}.zip"
    if not path.exists():
        urllib.request.urlretrieve(GAZETTEER.format(kind), path)
    with zipfile.ZipFile(path) as z:
        text = z.read(z.namelist()[0]).decode("utf-8")
    rows = csv.DictReader(io.StringIO(text), delimiter="\t")
    rows.fieldnames = [f.strip() for f in rows.fieldnames]
    for row in rows:
        for alias in aliases(row["NAME"]):
            yield normalize(alias), row["USPS"], float(row["INTPTLAT"]), float(row["INTPTLONG"])


def read_exits():
    """Map exit number -> [(lat, lng)] from data/us_exits.tsv.

    Every US motorway exit in OpenStreetMap (c) OpenStreetMap contributors, ODbL. Generated once with the
    Overpass query: node[highway=motorway_junction][ref](area["ISO3166-1"="US"]); out as lat/lon/ref.
    """
    exits = defaultdict(list)
    with open(settings.BASE_DIR / "data" / "us_exits.tsv") as f:
        for lat, lng, ref in csv.reader(f, delimiter="\t"):
            if m := re.match(r"\d+", ref):  # "13B", "1A;1B", "5A-B" -> exit 13, 1, 5
                exits[int(m[0])].append((float(lat), float(lng)))
    return exits


def locate_exit(address, lat, lng, exits):
    """Coordinates of the exit named in the address ("I-44, EXIT 283") nearest the town, or None."""
    if not (m := EXIT.search(address)):
        return None
    best, best_dist = None, EXIT_SEARCH_MILES
    for e_lat, e_lng in exits.get(int(m[1]), ()):
        dist = math.hypot((e_lat - lat) * 69.0, (e_lng - lng) * 69.17 * math.cos(math.radians(lat)))
        if dist < best_dist:
            best, best_dist = (e_lat, e_lng), dist
    return best


class Command(BaseCommand):
    help = "Load Census place centroids and geocode the fuel price CSV into the database."

    def add_arguments(self, parser):
        parser.add_argument("--csv", default=settings.BASE_DIR / "data" / "fuel-prices-for-be-assessment.csv")

    def handle(self, *args, **options):
        places = {}
        for key, state, lat, lng in read_gazetteer("place"):
            places.setdefault((key, state), (lat, lng))
        # County subdivisions cover townships (NJ, MI, PA...). Same name can repeat in a state, skip those.
        subdivisions = list(read_gazetteer("cousubs"))
        counts = Counter((key, state) for key, state, _, _ in subdivisions)
        for key, state, lat, lng in subdivisions:
            if counts[key, state] == 1:
                places.setdefault((key, state), (lat, lng))

        exits = read_exits()
        stations, skipped = {}, 0
        with open(options["csv"], newline="") as f:
            for row in csv.DictReader(f):
                town = places.get((normalize(row["City"]), row["State"]))
                if town is None:  # Canadian stations and unincorporated hamlets
                    skipped += 1
                    continue
                opis_id, price = int(row["OPIS Truckstop ID"]), float(row["Retail Price"])
                if opis_id in stations and stations[opis_id].price <= price:
                    continue  # duplicate rows per truck stop: keep the cheapest
                exit_coords = locate_exit(row["Address"], *town, exits)
                coords = exit_coords or town
                stations[opis_id] = FuelStation(
                    opis_id=opis_id,
                    name=row["Truckstop Name"].strip(),
                    address=row["Address"].strip(),
                    city=row["City"].strip(),
                    state=row["State"],
                    price=price,
                    lat=coords[0],
                    lng=coords[1],
                    at_exit=exit_coords is not None,
                )

        with transaction.atomic():
            Place.objects.all().delete()
            Place.objects.bulk_create(Place(key=k, state=s, lat=lat, lng=lng) for (k, s), (lat, lng) in places.items())
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations.values())
        at_exit = sum(s.at_exit for s in stations.values())
        self.stdout.write(
            f"{len(places)} places, {len(stations)} stations loaded ({at_exit} at an exact exit, "
            f"{len(stations) - at_exit} at the town centre), {skipped} rows not geocoded"
        )
