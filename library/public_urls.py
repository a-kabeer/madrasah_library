"""Routes for the public catalogue.

A separate URLconf mounted at `/catalog/`, kept apart from `library/urls.py`
on purpose: the two surfaces share no route, and a page added to either one
cannot appear in the other by accident.

The names are flat and prefixed `public_`, matching how every other name in
this project is written. What separates the two surfaces is the mount point
and the views behind it, not a namespace label.

Nothing here is a write. There is no POST route in this file, so there is
no public action to guard - `/library/` keeps every one of them, and keeps
its login requirement.
"""

from django.urls import path

from . import public_views


urlpatterns = [
    path(
        "",
        public_views.public_book_list,
        name="public_book_list",
    ),

    path(
        "book/<int:book_id>/",
        public_views.public_book_detail,
        name="public_book_detail",
    ),
]
