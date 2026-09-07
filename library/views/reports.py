"""The six reports, and the CSV of each one.

Every report reads the same validated date filters the page did, so an
export and the report it came from can never be two different answers.
"""

import csv

from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from ..permissions import feature_required

from ..models import (
    BookCopy,
    Borrower,
    Location,
    Shelf,
)

from .. import reports

from .common import (
    COPY_STATE_LABELS,
    PAGE_SIZE,
    describe_loans,
    filter_copies_by_state,
    numeric_param,
    query_with,
    selected_name,
)


# ==========================================================================
# REPORTS
#
# Six operational questions, each answered from the records that already
# hold the answer - see library/reports.py. Every one takes its filters
# from the query string, so a report is a URL: bookmarkable, shareable,
# printable, and exactly what the CSV export reads.
#
# Open to the same roles as the pages they summarise. A report shows no
# borrower detail that `borrower_detail` does not already show to every
# role, and building a second permission model for the same data would
# leave two answers to one question.
# ==========================================================================


def csv_response(filename, header, rows):
    """`rows` as a CSV download.

    The same records the page showed, from the same filtered queryset - the
    export is the report, not a second query that could answer differently.

    A UTF-8 BOM, because the librarians open these in Excel and without it
    the Arabic and Urdu titles arrive as mojibake.
    """

    response = HttpResponse(content_type="text/csv; charset=utf-8")

    # Only what a filename may safely hold, and always ending in .csv.
    safe = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in filename
    ).strip("-") or "report"

    response["Content-Disposition"] = (
        'attachment; filename="%s-%s.csv"'
        % (safe, timezone.now().date().isoformat())
    )

    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow(header)

    for row in rows:
        writer.writerow(row)

    return response


def wants_csv(request):
    return request.GET.get("format") == "csv"


def report_context(request, dates, **extra):
    """What every report page needs: its period, its filters, its links."""

    context = {
        "dates": dates,
        "start": dates.start.isoformat() if dates.start else "",
        "end": dates.end.isoformat() if dates.end else "",
        "error": dates.error,
        "printed_on": timezone.now(),
        # The current filters, so Print and Export carry them.
        "export_query": query_with(request, format="csv"),
        "locations": Location.objects.order_by("name"),
    }
    context.update(extra)

    return context


@feature_required("reports")
def reports_home(request):
    """One page listing the reports, rather than six sidebar entries."""

    return render(
        request,
        "library/reports_home.html",
        {
            "reports": [
                {
                    "url": reverse(name),
                    "title": title,
                    "icon": icon,
                    "blurb": blurb,
                }
                for name, title, icon, blurb in reports.REPORTS
            ],
        },
    )


@feature_required("reports")
def report_circulation(request):
    """What went out and came back over a period."""

    dates = reports.parse_range(request)

    location_id = numeric_param(request, "location")
    borrower_id = numeric_param(request, "borrower")
    title = (request.GET.get("title") or "").strip()

    if dates.error:
        # Nothing is reported on a period nobody can read. Saying so beats
        # answering a question that was not asked.
        return render(
            request,
            "library/report_circulation.html",
            report_context(request, dates, tallies=None, loans=[]),
        )

    tallies, loans = reports.circulation(
        dates, location_id, borrower_id, title
    )

    if wants_csv(request):
        return csv_response(
            "circulation",
            [
                "Copy code", "Book", "Author", "Borrower",
                "Issued", "Due", "Returned",
            ],
            (
                [
                    loan.copy.copy_code,
                    loan.copy.volume.book.title,
                    (
                        loan.copy.volume.book.author.name
                        if loan.copy.volume.book.author else ""
                    ),
                    loan.borrower.name,
                    loan.issue_date,
                    loan.due_date,
                    loan.return_date or "",
                ]
                for loan in loans
            ),
        )

    paginator = Paginator(loans, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/report_circulation.html",
        report_context(
            request,
            dates,
            tallies=tallies,
            # Named and ordered here rather than in the template, so the
            # page and the CSV describe the same five numbers.
            tallies_shown=[
                ("Issued", tallies["issued"]),
                ("Returned", tallies["returned"]),
                ("Renewals", tallies["renewals"]),
                ("Out now", tallies["out"]),
                ("Overdue now", tallies["overdue"]),
            ],
            loans=page,
            paginator=paginator,
            pagination_query=query_with(request, page=None),
            location_id=str(location_id or ""),
            borrower_id=str(borrower_id or ""),
            borrower_name=selected_name(Borrower, borrower_id),
            title_filter=title,
        ),
    )


@feature_required("reports")
def report_overdue(request):
    """Everything out past its due date, longest first."""

    today = timezone.now().date()
    location_id = numeric_param(request, "location")

    loans = describe_loans(reports.overdue(location_id), today)

    if wants_csv(request):
        return csv_response(
            "overdue",
            [
                "Borrower", "Registration no", "Phone", "Book", "Author",
                "Copy code", "Issued", "Due", "Days overdue",
            ],
            (
                [
                    loan.borrower.name,
                    loan.borrower.registration_no or "",
                    loan.borrower.phone,
                    loan.copy.volume.book.title,
                    (
                        loan.copy.volume.book.author.name
                        if loan.copy.volume.book.author else ""
                    ),
                    loan.copy.copy_code,
                    loan.issue_date,
                    loan.due_date,
                    loan.days_overdue,
                ]
                for loan in loans
            ),
        )

    return render(
        request,
        "library/report_overdue.html",
        report_context(
            request,
            reports.DateRange(None, None, ""),
            loans=loans,
            total=len(loans),
            location_id=str(location_id or ""),
        ),
    )


def inventory_report(request, template, states, heading):
    """Shared by the two reports that count copies by state."""

    today = timezone.now().date()

    location_id = numeric_param(request, "location")
    shelf_id = numeric_param(request, "shelf")
    state = (request.GET.get("status") or "").strip().casefold()

    if state not in states:
        state = ""

    copies = BookCopy.objects.select_related(
        "volume__book__author", "shelf__location"
    )

    if location_id:
        copies = copies.filter(shelf__location_id=location_id)

    if shelf_id:
        copies = copies.filter(shelf_id=shelf_id)

    # Counted over everything the location and shelf filters allow, so the
    # tiles keep describing the same set whichever state is being listed.
    tallies = reports.inventory_counts(copies, today, states)

    if state:
        listed = filter_copies_by_state(copies, state, today)

    elif states == reports.CONDITION_STATES:
        # With no state chosen, this report is about those states and
        # nothing else - it exists to answer "what is not usable".
        listed = copies.filter(
            status__in=[name.title() for name in states]
        )

    else:
        listed = copies

    listed = listed.order_by("copy_code")

    if wants_csv(request):
        return csv_response(
            heading.lower().replace(" ", "-"),
            ["Copy code", "Book", "Author", "Location", "Shelf", "Status"],
            (
                [
                    copy.copy_code,
                    copy.volume.book.title,
                    (
                        copy.volume.book.author.name
                        if copy.volume.book.author else ""
                    ),
                    copy.shelf.location.name if copy.shelf_id else "",
                    copy.shelf.shelf_code if copy.shelf_id else "",
                    copy.status,
                ]
                for copy in listed
            ),
        )

    paginator = Paginator(listed, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        template,
        report_context(
            request,
            reports.DateRange(None, None, ""),
            heading=heading,
            tallies=tallies,
            states=[
                (name, COPY_STATE_LABELS[name], tallies.get(name, 0))
                for name in states
            ],
            copies=page,
            paginator=paginator,
            pagination_query=query_with(request, page=None),
            location_id=str(location_id or ""),
            shelf_id=str(shelf_id or ""),
            state=state,
            shelves=Shelf.objects.select_related("location").order_by(
                "location__name", "shelf_code"
            ),
        ),
    )


@feature_required("reports")
def report_inventory(request):
    """Where the copies are and what state they are in."""

    return inventory_report(
        request,
        "library/report_inventory.html",
        reports.INVENTORY_STATES,
        "Inventory Status",
    )


@feature_required("reports")
def report_condition(request):
    """The copies that are not on the shelf in usable condition."""

    return inventory_report(
        request,
        "library/report_inventory.html",
        reports.CONDITION_STATES,
        "Lost, Damaged and Withdrawn",
    )


@feature_required("reports")
def report_borrowers(request):
    """What a borrower, or everyone, did over a period."""

    dates = reports.parse_range(request)
    borrower_id = numeric_param(request, "borrower")

    if dates.error:
        return render(
            request,
            "library/report_borrowers.html",
            report_context(request, dates, tallies=None, rows=[]),
        )

    tallies, rows = reports.borrower_activity(dates, borrower_id)

    if wants_csv(request):
        return csv_response(
            "borrower-activity",
            [
                "Borrower", "Registration no", "Issued", "Returned",
                "Currently out", "Overdue",
            ],
            (
                [
                    row.name,
                    row.registration_no or "",
                    row.issued,
                    row.returned,
                    row.out,
                    row.overdue,
                ]
                for row in rows
            ),
        )

    paginator = Paginator(rows, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/report_borrowers.html",
        report_context(
            request,
            dates,
            tallies=tallies,
            tallies_shown=[
                ("Issued", tallies["issued"]),
                ("Returned", tallies["returned"]),
                ("Renewals", tallies["renewals"]),
                ("Out now", tallies["out"]),
                ("Overdue now", tallies["overdue"]),
            ],
            rows=page,
            paginator=paginator,
            pagination_query=query_with(request, page=None),
            borrower_id=str(borrower_id or ""),
            borrower_name=selected_name(Borrower, borrower_id),
        ),
    )


@feature_required("reports")
def report_popular(request):
    """Books ranked by how often they were actually lent."""

    dates = reports.parse_range(request, days=365)

    if dates.error:
        return render(
            request,
            "library/report_popular.html",
            report_context(request, dates, books=[]),
        )

    books = reports.popular(dates)

    if wants_csv(request):
        return csv_response(
            "popular-books",
            ["Rank", "Book", "Author", "Times issued", "Copies available"],
            (
                [
                    rank,
                    book.title,
                    book.author.name if book.author else "",
                    book.times_issued,
                    book.copies_available,
                ]
                for rank, book in enumerate(books, start=1)
            ),
        )

    return render(
        request,
        "library/report_popular.html",
        report_context(request, dates, books=books),
    )
