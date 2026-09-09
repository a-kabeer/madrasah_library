"""The pages that look across everything else.

The home page, which leads with the day's work; the activity log, which is
the record of who changed what; and the analytics page, which is what the
records add up to over a period.
"""


from django.core.paginator import Paginator
from django.shortcuts import render
from django.core.cache import cache
from django.utils import timezone
from django.db import models

from ..models import (
    ActivityLog,
    Author,
    Book,
    BookCopy,
    Borrower,
    Category,
    Loan,
    Publisher,
    User,
)

from .. import analytics as analytics_module
from .. import features

from ..permissions import can_edit_library, feature_required

from .common import (
    ACTIVITY_LOG_CACHE_KEY,
    DASHBOARD_CACHE_KEY,
    PAGE_SIZE,
    activity_log_target,
    date_param,
    day_bounds,
    label_activity_log,
    numeric_param,
    query_with,
)


def activity_log_filter_options():
    """The values the two filter dropdowns offer, cached.

    Both lists come from what is actually recorded, not from a list written
    by hand. The hand-written ones had drifted: they offered six actions
    while the log held ten, so a librarian could not filter for a
    reservation, a cancellation, a fulfilment or an import at all - the
    option simply was not there. Reading them back cannot drift.

    What reading them back does cost is a DISTINCT over the whole table,
    twice, on every page load - and the activity log is the fastest-growing
    table in the database, one row per action anybody takes. Measured on
    60,000 rows: 11.0 ms and 10.8 ms, both sequential scans, and neither
    helped by any index, since a DISTINCT has to see every row. That is 22
    ms of the page's own time, growing linearly and forever.

    So they are cached, and `create_activity_log` already deletes
    ACTIVITY_LOG_CACHE_KEY on every write - the invalidation was written
    before anything stored under that key, which is why this could be added
    without inventing a new one. A new action or entity type appears in the
    dropdown as soon as the entry that introduced it is recorded, because
    recording it is what clears the cache.
    """

    cached = cache.get(ACTIVITY_LOG_CACHE_KEY)

    if cached is not None:
        return cached

    actions = list(
        ActivityLog.objects.order_by()
        .values_list("action", flat=True)
        .distinct()
        .order_by("action")
    )

    entity_types = [
        kind
        for kind in ActivityLog.objects.order_by()
        .values_list("entity_type", flat=True)
        .distinct()
        .order_by("entity_type")
        if kind
    ]

    options = (actions, entity_types)
    # Same 300 s as the dashboard counts below, and for the same
    # reason: long enough to matter under a burst, short enough that
    # a stale list cannot outlive a shift.
    cache.set(ACTIVITY_LOG_CACHE_KEY, options, timeout=300)

    return options


@feature_required("activity_log")
def activity_log_list(request):

    search = request.GET.get("search", "").strip()
    user_id = numeric_param(request, "user")
    action = request.GET.get("action", "").strip()
    entity_type = request.GET.get("entity_type", "").strip()
    date = date_param(request, "date")

    logs_query = ActivityLog.objects.select_related(
        "user"
    )

    if search:

        query = (
            models.Q(action__icontains=search)
            | models.Q(entity_type__icontains=search)
            | models.Q(description__icontains=search)
            | models.Q(user__username__icontains=search)
            | models.Q(user__full_name__icontains=search)
        )

        if search.isdigit():
            query |= models.Q(
                entity_id=int(search)
            )

        logs_query = logs_query.filter(query)

    if user_id:
        logs_query = logs_query.filter(
            user_id=user_id
        )

    if action:
        logs_query = logs_query.filter(
            action=action
        )

    if entity_type:
        logs_query = logs_query.filter(
            entity_type=entity_type
        )

    if date:
        # A range on the bare column, not `created_at__date=`, so the
        # index on created_at can serve it - see `day_bounds`.
        start, end = day_bounds(date)

        logs_query = logs_query.filter(
            created_at__gte=start,
            created_at__lt=end,
        )

    logs = logs_query.order_by(
        "-created_at"
    )

    paginator = Paginator(logs, PAGE_SIZE)
    logs = paginator.get_page(request.GET.get("page"))

    # Readable action name, colour, and a link to the record.
    for log in logs:
        label_activity_log(log)

    users = User.objects.all().order_by(
        "full_name"
    )

    actions, entity_types = activity_log_filter_options()

    return render(
        request,
        "library/activity_log_list.html",
        {
            "logs": logs,
            "search": search,
            "user_id": user_id,
            "action": action,
            "entity_type": entity_type,
            "date": date.isoformat() if date else "",
            "users": users,
            "actions": actions,
            "entity_types": entity_types,
            "paginator": paginator,
            # Everything except `page`, so the search and all four filters
            # survive being paged through.
            "pagination_query": query_with(request, page=None),
            "elided_page_range": list(
                paginator.get_elided_page_range(
                    logs.number,
                    on_each_side=1,
                    on_ends=1,
                )
            ),
            "page_ellipsis": Paginator.ELLIPSIS,
        }
    )



@feature_required("dashboard")
def library_home(request):

    dashboard_stats = cache.get(
        DASHBOARD_CACHE_KEY
    )

    if dashboard_stats is None:

        today = timezone.now().date()

        # One pass per table rather than one per number. These were twelve
        # separate COUNT(*) round trips; the copies, borrowers and loans
        # figures are all counts over the same rows with different
        # conditions, which is what a filtered Count is for - the same
        # shape analytics.py already uses. Twelve queries become seven.
        copies = BookCopy.objects.aggregate(
            total=models.Count("id"),
            available=models.Count("id", filter=models.Q(status="Available")),
            issued=models.Count("id", filter=models.Q(status="Issued")),
        )

        borrowers = Borrower.objects.aggregate(
            total=models.Count("id"),
            active=models.Count("id", filter=models.Q(is_active=True)),
        )

        loans = Loan.objects.filter(return_date__isnull=True).aggregate(
            active=models.Count("id"),
            overdue=models.Count("id", filter=models.Q(due_date__lt=today)),
            due_today=models.Count("id", filter=models.Q(due_date=today)),
        )

        dashboard_stats = {
            "total_books": Book.objects.count(),
            "total_authors": Author.objects.count(),
            "total_categories": Category.objects.count(),
            "total_publishers": Publisher.objects.count(),

            "total_book_copies": copies["total"],
            "available_copies": copies["available"],
            "issued_copies": copies["issued"],

            "total_borrowers": borrowers["total"],
            "active_borrowers": borrowers["active"],

            "active_loans": loans["active"],
            "overdue_loans": loans["overdue"],
            "due_today_loans": loans["due_today"],
        }

        cache.set(
            DASHBOARD_CACHE_KEY,
            dashboard_stats,
            timeout=300
        )

    # Outside the cached block above: these are lists of records, not
    # counts, and they are cheap - five rows each, with the joins the rows
    # actually name.
    #
    # `-id` as well as the date, because `issue_date` and `return_date` are
    # DateFields: without a tiebreaker, everything that happened today came
    # back in whatever order the database felt like, so "most recent" was
    # not reliably most recent.
    recent_loans = Loan.objects.select_related(
        "copy__volume__book",
        "borrower",
        "issued_by",
    ).order_by(
        "-issue_date",
        "-id",
    )[:5]

    # The other half of recent circulation. Taken from the loans themselves
    # rather than from the activity log: `return_date` and `returned_to` are
    # the record of a return, and reading the log instead would mean parsing
    # a description to find out which book it was.
    recent_returns = Loan.objects.filter(
        return_date__isnull=False
    ).select_related(
        "copy__volume__book",
        "borrower",
        "returned_to",
    ).order_by(
        "-return_date",
        "-id",
    )[:5]

    # Gated on the same feature as the Activity Log page itself. Turning
    # that page off for a role used to hide the page and the sidebar entry
    # while the last five entries still sat on their dashboard, which is
    # the data the toggle exists to withhold.
    if features.user_has(request.user, "activity_log"):
        recent_logs = list(
            ActivityLog.objects.select_related(
                "user"
            ).order_by(
                "-created_at"
            )[:5]
        )

        for log in recent_logs:
            log.target_url = activity_log_target(log)

    else:
        recent_logs = []

    dashboard_stats["recent_loans"] = recent_loans
    dashboard_stats["recent_returns"] = recent_returns
    dashboard_stats["recent_logs"] = recent_logs

    # Which quick actions to offer. Issuing, returning and adding a borrower
    # are open to all three roles; adding a book is not, so offering it to
    # an Assistant would be offering a 403. The decorators on those views
    # are the enforcement - this only decides what is worth showing.
    dashboard_stats["can_edit"] = can_edit_library(request.user)

    return render(
        request,
        "library/dashboard.html",
        dashboard_stats
    )


# ==========================================================================
# ANALYTICS
#
# Read-only insights over the records the rest of the application already
# keeps - see library/analytics.py for what counts as what, and why. There
# is no POST here, nothing is written, and no number is stored: everything
# on the page is computed from the live rows at the moment it is drawn.
#
# Admin and Librarian, which is the same line `can_edit_library` draws
# through the catalogue: these are figures a library acts on, and an
# Assistant does not make those decisions.
# ==========================================================================


@feature_required("analytics", "Admin", "Librarian")
def analytics(request):
    """Everything on one page, each section its own bounded query.

    The filters are the whole of the input: a period and, optionally, a
    category. Neither is trusted - an unrecognised period falls back to the
    default and a category id nobody has is dropped - and which one is in
    force is stated on the page, so a fallback is never silent.

    One `Period` is built and handed to every section, so nothing here can
    disagree with anything else about which window it is describing.

    The query count is fixed. Every ranking is limited in SQL, every tally
    is a conditional aggregate, and the only loop is over a fixed number of
    recent stock checks - so this page costs the same on a library with
    four hundred loans as on one with four hundred thousand.
    """

    period = analytics_module.Period(
        analytics_module.resolve_period(request.GET.get("period")),
        timezone.localdate(),
        category_id=analytics_module.resolve_category(
            request.GET.get("category")
        ),
    )

    trend = analytics_module.borrowing_trend(period)

    return render(
        request,
        "library/analytics.html",
        {
            "period": period,
            "periods": [
                (key, analytics_module.PERIOD_LABELS[key])
                for key in analytics_module.PERIODS
            ],
            "categories": Category.objects.order_by("name"),

            "loans": analytics_module.loan_summary(period),
            "collection": analytics_module.collection_summary(period),

            "trend": trend,
            "trend_format": analytics_module.TREND_FORMATS[period.grain],
            # The busiest bucket, so each row's bar is a share of the peak
            # rather than of whichever bucket happened to come first.
            "trend_max": max([row["loans"] for row in trend] or [0]),

            "most_borrowed": analytics_module.most_borrowed(period),
            "underused": analytics_module.underused(period),
            "never_borrowed": analytics_module.never_borrowed_list(period),

            "top_borrowers": analytics_module.most_active_borrowers(period),
            "category_usage": analytics_module.category_usage(period),

            "duration": analytics_module.loan_duration(period),

            "attention": analytics_module.collection_attention(period),
            "stock_checks": analytics_module.recent_stock_check_findings(),

            "top_n": analytics_module.TOP_N,
            "recent_sessions": analytics_module.RECENT_SESSIONS,
        },
    )
