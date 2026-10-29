from django.urls import path

from routing import views

urlpatterns = [
    path("api/route/", views.route),
    path("api/route/map/", views.route_map),
]
