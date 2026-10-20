import glob
import os

from django.conf import settings

_V = None


def assets(request):
    """Adds ?v=<mtime> to static URLs so phones/browsers never reuse a stale cached script."""
    global _V
    if _V is None or settings.DEBUG:
        files = glob.glob(os.path.join(settings.BASE_DIR, "static", "**", "*.*"), recursive=True)
        _V = int(max((os.path.getmtime(f) for f in files), default=0))
    return {"asset_v": _V}
