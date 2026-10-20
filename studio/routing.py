from django.urls import path
from . import consumers

websocket_urlpatterns = [
    path("ws/stream/<int:pid>/", consumers.StreamConsumer.as_asgi()),
    path("ws/telemetry/<int:pid>/", consumers.TelemetryConsumer.as_asgi()),
]
