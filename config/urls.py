"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.static import serve


urlpatterns = [
    path("admin/", admin.site.urls),

    # The staff application. Everything under here needs a login -
    # `LoginRequiredMiddleware` requires one by default and only the views
    # marked `@login_not_required` are exempt, of which there is exactly
    # one in this tree (the sign-in page itself).
    path("library/", include("library.urls")),

    # The public catalogue: read-only, anonymous, and mounted apart from
    # the staff application so the two share no route. Its views carry
    # `@login_not_required` individually - see library/public_views.py for
    # why the exemption is per-view rather than by URL prefix.
    path("catalog/", include("library.public_urls")),
]


# Serve uploaded media (book covers). django.conf.urls.static.static() is a
# no-op unless DEBUG, so the non-debug branch wires the same view up
# explicitly — WhiteNoise only handles static files, not MEDIA_ROOT, and this
# project has no separate web server in front of it. See the note on
# MEDIA_ROOT in settings.py before using this for anything high traffic.

if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT,
    )

else:
    urlpatterns += [
        path(
            f"{settings.MEDIA_URL.strip('/')}/<path:path>",
            serve,
            {"document_root": settings.MEDIA_ROOT},
        ),
    ]