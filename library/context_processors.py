"""Template context shared by every page.

`branding` has to be available on templates rendered for anonymous users
(the login page most importantly), so nothing here may touch
`request.user`. It also has to work before anything has been configured, so
`OrganizationSettings.load()` returns an instance carrying defaults rather
than None.
"""

import os

from django.conf import settings
from django.core.cache import cache

from .models import OrganizationSettings


BRANDING_CACHE_KEY = "organization_branding"
BRANDING_CACHE_TIMEOUT = 300


def get_branding():
    """The organisation's branding, cached.

    Follows the same read/write/invalidate pattern the views use for the
    catalogue caches: read through the cache, and let the writer call
    `clear_branding_cache()`.
    """

    branding = cache.get(BRANDING_CACHE_KEY)

    if branding is None:
        branding = OrganizationSettings.load()

        cache.set(
            BRANDING_CACHE_KEY,
            branding,
            timeout=BRANDING_CACHE_TIMEOUT,
        )

    return branding


def clear_branding_cache():
    """Call after saving branding, so the next render picks it up."""

    cache.delete(BRANDING_CACHE_KEY)


# The project's own CSS and JS, relative to the app's static directory.
LOCAL_ASSETS = ("library/css/style.css", "library/js/app.js")

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "static")


def get_asset_version():
    """Cache-busting token for the project's own CSS and JS.

    Only needed while DEBUG is on. In production
    CompressedManifestStaticFilesStorage puts a content hash in the
    filename, so the URL changes by itself. The development server serves
    `/static/library/js/app.js` at a stable URL with no Cache-Control and
    only a Last-Modified header, which lets browsers cache it heuristically
    and skip revalidation — so templates appear to update while script and
    style changes silently do not. Keying the URL on the files' modification
    time makes each edit a new URL.
    """

    if not settings.DEBUG:
        return ""

    newest = 0

    for path in LOCAL_ASSETS:

        try:
            newest = max(
                newest,
                int(os.path.getmtime(os.path.join(_ASSET_DIR, path))),
            )

        except OSError:
            # Missing file: let {% static %} deal with it.
            continue

    return str(newest)


def get_theme_preference(request):
    """The signed-in user's appearance choice, or "" for anonymous visitors.

    The empty string matters: it tells the layout's pre-paint script that
    there is no server-side choice to honour, so it should fall back to the
    localStorage mirror and then the OS setting. Returning "system" instead
    would wrongly override a choice the browser already remembers.
    """

    user = getattr(request, "user", None)

    if user is None or not user.is_authenticated:
        return ""

    return user.theme


def branding(request):
    return {
        "branding": get_branding(),
        "theme_preference": get_theme_preference(request),
        "asset_version": get_asset_version(),
    }


# --------------------------------------------------------------------------
# Main-content navigation
#
# A converted page writes `{% extends layout %}` instead of naming
# base.html, and this decides which layout that is: the whole application
# shell for an ordinary request, or just the main-content region for an
# HTMX navigation. One template, one URL, one view - the request decides how
# much of the page comes back.
# --------------------------------------------------------------------------

# The id of the single swap target in base.html. Named here because both the
# layout and the check below have to agree on it.
MAIN_CONTENT_ID = "mainContent"

MAIN_LAYOUT = "library/partials/main_layout.html"
FULL_LAYOUT = "library/base.html"


def is_main_nav_request(request):
    """True only for a link asking to replace the main-content region.

    The HX-Request header alone would not do. Every combobox, modal and
    list-fragment request in this project carries it too, and treating those
    as navigation would answer them with the wrong body. `HX-Target` names
    the element htmx is aiming at, so requiring it to be the main-content
    container is what separates "navigate" from all the other HTMX traffic -
    without putting a marker in the URL, which `hx-push-url` would then put
    in the address bar.
    """

    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-Target") == MAIN_CONTENT_ID
    )


def navigation(request):
    """`layout` for templates that support main-content navigation.

    Templates that do not use it are unaffected: they still name base.html
    directly, so nothing about their rendering changes.
    """

    return {
        "layout": (
            MAIN_LAYOUT if is_main_nav_request(request) else FULL_LAYOUT
        ),
        "main_content_id": MAIN_CONTENT_ID,
    }
