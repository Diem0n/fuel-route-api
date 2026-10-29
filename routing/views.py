from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from routing.services import TripError, plan_trip


def _trip(request):
    start, finish = request.GET.get("start", "").strip(), request.GET.get("finish", "").strip()
    if not start or not finish:
        raise TripError("Query params 'start' and 'finish' are required, e.g. ?start=Chicago, IL&finish=Denver, CO")
    return plan_trip(start, finish)


@require_GET
def route(request):
    try:
        trip = _trip(request)
    except TripError as e:
        return JsonResponse({"error": str(e)}, status=e.status)
    map_url = request.build_absolute_uri(f"/api/route/map/?{request.GET.urlencode()}")
    return JsonResponse({**trip, "map_url": map_url})


@require_GET
def route_map(request):
    try:
        trip = _trip(request)
    except TripError as e:
        return JsonResponse({"error": str(e)}, status=e.status)
    return render(request, "routing/map.html", {"trip": trip})
