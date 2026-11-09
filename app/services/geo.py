"""Geo helpers: delivery-radius enforcement (5-8 km) and nearest-partner lookup."""

import math

from flask import current_app

EARTH_RADIUS_KM = 6371.0088

#: Greater Lagos launch hub - used as the default reference point.
LAGOS_CENTER = (6.5244, 3.3792)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return round(2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a)), 3)


def delivery_radius_km() -> float:
    return float(current_app.config["DELIVERY_RADIUS_KM"])


def within_delivery_radius(distance_km: float) -> bool:
    return distance_km <= delivery_radius_km()


def distance_between_stores(a_lat, a_lng, b_lat, b_lng) -> float:
    return haversine_km(a_lat, a_lng, b_lat, b_lng)


def nearest_store(stores, lat: float, lng: float):
    """Return (store, distance_km) for the closest active partner."""
    candidates = [
        (store, distance_between_stores(lat, lng, store.gps_latitude, store.gps_longitude))
        for store in stores
    ]
    if not candidates:
        return None, None
    return min(candidates, key=lambda pair: pair[1])
