import csv
import io
import re
import urllib.request
import zipfile
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from routing.models import FuelStation, Place
from routing.services import normalize

GAZETTEER = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_{}_national.zip"
# Census name suffixes: "Tomah city", "Abanda CDP", "Nashville-Davidson metropolitan government (balance)"
SUFFIX = re.compile(r"\s+(?:CDP|CCD|[a-z][a-z ]*?)(?:\s*\(balance\))?$")


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

        stations, skipped = {}, 0
        with open(options["csv"], newline="") as f:
            for row in csv.DictReader(f):
                coords = places.get((normalize(row["City"]), row["State"]))
                if coords is None:  # Canadian stations and unincorporated hamlets
                    skipped += 1
                    continue
                opis_id, price = int(row["OPIS Truckstop ID"]), float(row["Retail Price"])
                if opis_id in stations and stations[opis_id].price <= price:
                    continue  # duplicate rows per truck stop: keep the cheapest
                stations[opis_id] = FuelStation(
                    opis_id=opis_id,
                    name=row["Truckstop Name"].strip(),
                    address=row["Address"].strip(),
                    city=row["City"].strip(),
                    state=row["State"],
                    price=price,
                    lat=coords[0],
                    lng=coords[1],
                )

        with transaction.atomic():
            Place.objects.all().delete()
            Place.objects.bulk_create(Place(key=k, state=s, lat=lat, lng=lng) for (k, s), (lat, lng) in places.items())
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations.values())
        self.stdout.write(f"{len(places)} places, {len(stations)} stations loaded, {skipped} rows not geocoded")
