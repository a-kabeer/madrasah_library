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
    BookCopy,
    Borrower,
    Category,
    Loan,
    Publisher,
    User,
)

from .. import analytics as analytics_module
from .. import queries

from ..permissions import can_edit_library, feature_required

from .common import (
    DASHBOARD_CACHE_KEY,
    PAGE_SIZE,
    activity_log_target,
)


@feature_required("activity_log")
def activity_log_list(request):

    search = request.GET.get("search", "").strip()
    user_id = request.GET.get("user", "").strip()
    action = request.GET.get("action", "").strip()
    entity_type = request.GET.get("entity_type", "").strip()
    date = request.GET.get("date", "").strip()

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
            query |= models.Q(entity_id=int(search))

        logs_query = logs_query.filter(query)

    if user_id:
        logs_query = logs_query.filter(user_id=user_id)

    if action:
        logs_query = logs_query.filter(action=action)

    if entity_type:
        logs_query = logs_query.filter(entity_type=entity_type)

    if date:
        logs_query = logs_query.filter(created_at__date=date)

    logs = logs_query.order_by("-created_at")

    paginator = Paginator(logs, PAGE_SIZE)
    logs = paginator.get_page(request.GET.get("page"))

    users = User.objects.all().order_by("full_name")

    actions = [
        "CREATE",
        "UPDATE",
        "DELETE",
        "ISSUE",
        "RETURN",
        "RENEW",
    ]

    entity_types = [
        "Book",
        "Author",
        "Category",
        "Publisher",
        "BookVolume",
        "BookContent",
        "BookCopy",
        "Borrower",
        "Loan",
        "Location",
        "Shelf",
        "User",
    ]

    return render(
        request,
        "library/activity_log_list.html",
        {
            "logs": logs,
            "search": search,
            "user_id": user_id,
            "action": action,
            "entity_type": entity_type,
            "date": date,
            "users": users,
            "actions": actions,
            "entity_types": entity_types,
        }
    )


@feature_required("dashboard")
def library_home(request):

    dashboard_stats = cache.get(DASHBOARD_CACHE_KEY)

    if dashboard_stats is None:

        today = timezone.now().date()

        dashboard_stats = {
            # "Books" on the dashboard means books currently in the
            # catalogue, matching the default Books page. Archived books
            # are intentionally excluded; they are an administrative
            # history state, not part of the active collection count.
            "total_books": queries.active_books().count(),
            "total_authors": Author.objects.count(),
            "total_categories": Category.objects.count(),
            "total_publishers": Publisher.objects.count(),
            "total_book_copies": BookCopy.objects.count(),
            "available_copies": BookCopy.objects.filter(
                status="Available"
            ).count(),
            "issued_copies": BookCopy.objects.filter(
                status="Issued"
            ).count(),
            "total_borrowers": Borrower.objects.count(),
            "active_borrowers": Borrower.objects.filter(
                is_active=True
            ).count(),
            "active_loans": Loan.objects.filter(
                return_date__isnull=True
            ).count(),
            "overdue_loans": Loan.objects.filter(
                return_date__isnull=True,
                due_date__lt=today
            ).count(),
            "due_today_loans": Loan.objects.filter(
                return_date__isnull=True,
                due_date=today
            ).count(),
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

    recent_logs = list(
        ActivityLog.objects.select_related(
            "user"
        ).order_by(
            "-created_at"
        )[:5]
    )

    for log in recent_logs:
        log.target_url = activity_log_target(log)

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
