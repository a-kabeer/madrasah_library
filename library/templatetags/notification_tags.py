"""The one thing the application shell needs to know about notifications.

A template tag rather than a context processor, deliberately. A context
processor runs for every template this project renders - every combobox
fragment, every modal body, every list partial - and would have put an
unread-count query on all of them. This runs only where it is written,
which is base.html, and base.html is rendered only for a full page load:
an HTMX navigation answers with the main-content region instead, and never
reaches this.

So the cost of the indicator is one bounded query per full page load, and
nothing at all on the navigations in between. The panel refreshes the badge
out of band whenever it is opened or something is marked read, which is
where a count going stale would actually be noticed.
"""

from django import template

from .. import notifications


register = template.Library()


@register.simple_tag(takes_context=True)
def unread_notification_count(context):
    """How many unread notifications the signed-in user has.

    Zero for anonymous visitors, without a query: the login page renders
    the same head as everything else, and there is nobody to count for.
    """

    request = context.get("request")

    user = getattr(request, "user", None)

    if user is None or not user.is_authenticated:
        return 0

    return notifications.unread_count(user)


@register.simple_tag
def notification_unread_cap():
    """The number the unread count stops at, so the badge can say "99+".

    Read from the module that enforces it rather than written into the
    template, so raising the cap is one change in one place.
    """

    return notifications.UNREAD_CAP
