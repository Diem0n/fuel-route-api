from django.db import models


class Place(models.Model):
    """US city/town centroid from the Census gazetteer, keyed by normalized name."""

    key = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    lat = models.FloatField()
    lng = models.FloatField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["key", "state"], name="unique_place")]


class FuelStation(models.Model):
    opis_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    price = models.FloatField()
    # Town centroid, not the exact truck stop: the CSV has no coordinates.
    lat = models.FloatField(db_index=True)
    lng = models.FloatField()
