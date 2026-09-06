"""What the library's own records say about how it is being used.

Decision support, not a second set of reports. Task 15 answers "what
happened between these two dates, and print it"; this answers "what should
we do about the collection" - which books are carrying the library, which
have never left the shelf, who is borrowing, and what is quietly wrong with
the stock. The two are separate pages on purpose and neither is built from
the other.

Everything here is read from `Loan`, `BookCopy`, `Book`, `Borrower`,
`Category` and Task 13's stock checks, live, at the moment the page is
drawn. There is no analytics table, no nightly rollup, no snapshot, no
cached counter and no background job: a number that disagreed with the loan
list would be a number the library could not trust, so every figure is
computed from the row the loan list would show.

And it is read-only. Nothing in this module writes, and the view that uses
it accepts no POST.


Which records count
-------------------

Two rules, applied consistently, because a page that mixed them would
answer two different questions under one heading:

  * A **usage** figure - most borrowed, category use, trends, durations,
    borrower rankings - includes archived books. Taking a book out of the
    catalogue does not unlend it, and a history that dropped last year's
    most borrowed title the day it was archived would mislead. This is
    Task 15's `popular()` rule, kept.

  * A **collection** figure - how many books there are, which have never
    been borrowed, which are underused - counts only books still in the
    catalogue, through Task 5's `active_books()`. Each of those is a
    question about what to do next, and an archived book has already been
    dealt with.

Period boundaries follow Task 15's convention exactly: `today - N days` to
`today` inclusive, on `issue_date`, which is a date rather than a
timestamp. Dates come from `timezone.localdate()`, so the day the library
is in is the day the numbers are about.
"""

from datetime import timedelta

from django.db import models
from django.db.models.functions import TruncDay, TruncMonth, TruncWeek
from django.utils import timezone

from . import inventory
from .models import (
    Book,
    BookCopy,
    Borrower,
    Category,
    InventorySession,
    Loan,
)
from .reports import CONDITION_STATES, inventory_counts


# The periods offered, and what "all" means. `90d` is the default because a
# term is the unit a madrasah actually plans in - a month is too short to
# see a pattern and a year is too long to act on.
PERIODS = ("30d", "90d", "365d", "all")

DEFAULT_PERIOD = "90d"

PERIOD_DAYS = {"30d": 30, "90d": 90, "365d": 365}

PERIOD_LABELS = {
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "365d": "Last 12 months",
    "all": "All time",
}

# How the trend is bucketed at each period. A day is too fine to read over
# a year and a month too coarse to read over a fortnight.
TREND_GRAIN = {
    "30d": "day",
    "90d": "week",
    "365d": "month",
    "all": "month",
}

TREND_TRUNC = {"day": TruncDay, "week": TruncWeek, "month": TruncMonth}

TREND_FORMATS = {"day": "j M", "week": "j M", "month": "M Y"}

# Every ranking on the page. Ten is what fits on a screen and what somebody
# can act on in an afternoon; the limit is applied in SQL, so a longer list
# would not cost more to *not* show.
TOP_N = 10

# How many finished stock checks the collection section reports on. Fixed,
# so the page costs the same after the fiftieth stock check as after the
# fifth - and the recent ones are the ones still worth acting on.
RECENT_SESSIONS = 5


class Period:
    """The window a page is about, and the loans inside it.

    A small object rather than a tuple because every section needs the same
    three things - the two dates and the filtered loan relation - and
    passing them separately is how two sections end up disagreeing about
    which loans they are counting.
    """

    def __init__(self, key, today, category_id=""):
        self.key = key
        self.label = PERIOD_LABELS[key]
        self.grain = TREND_GRAIN[key]
        self.today = today
        self.category_id = category_id

        days = PERIOD_DAYS.get(key)

        # `all` has no start. Everything else is `today - N days` through
        # today inclusive, which is the convention library/reports.py has
        # used since Task 15 - one meaning of "the last 30 days" in this
        # application, not two.
        self.start = None if days is None else today - timedelta(days=days)
        self.end = today

    @property
    def is_all_time(self):
        return self.start is None

    def issued_in_period(self, prefix=""):
        """`Q` matching a loan issued inside this window.

        `prefix` is the path to the loan from wherever it is being applied,
        so the same one definition serves the loan table, the book join and
        the borrower join. Writing the range out at each call site is how a
        boundary ends up inclusive in one place and exclusive in another.
        """

        field = "%sissue_date" % prefix

        if self.is_all_time:
            # Still a condition rather than nothing, so callers can use it
            # unconditionally - and `__isnull=False` is what makes a
            # conditional Count skip rows with no loan at all.
            return models.Q(**{"%s__isnull" % field: False})

        return models.Q(**{
            "%s__gte" % field: self.start,
            "%s__lte" % field: self.end,
        })

    def returned_in_period(self, prefix=""):
        """`Q` matching a loan *returned* inside this window.

        A different question from the one above, and deliberately so: "how
        long did loans take" is about the loans that finished in the
        period, not the ones that started in it.
        """

        field = "%sreturn_date" % prefix

        if self.is_all_time:
            return models.Q(**{"%s__isnull" % field: False})

        return models.Q(**{
            "%s__gte" % field: self.start,
            "%s__lte" % field: self.end,
        })

    def in_category(self, prefix=""):
        """`Q` narrowing to the chosen category, or an empty one.

        Empty rather than None so it can be `&`-ed in unconditionally; an
        empty `Q` adds nothing to the SQL.
        """

        if not self.category_id:
            return models.Q()

        return models.Q(**{"%scategory_id" % prefix: self.category_id})

    def loans(self):
        """Every loan issued in this window, category filter applied.

        The one place the filtered loan relation is built. Sections that
        need loans start here rather than repeating the range, so there is
        no way for two of them to disagree about the window.
        """

        loans = Loan.objects.filter(self.issued_in_period())

        if self.category_id:
            loans = loans.filter(
                copy__volume__book__category_id=self.category_id
            )

        return loans

    def books(self):
        """The catalogue this page is about: active books, category applied.

        `active_books()` is Task 5's own exclusion - imported at call time
        because views.py imports this module and the other direction can
        only be taken here.
        """

        from .views import active_books

        books = active_books()

        if self.category_id:
            books = books.filter(category_id=self.category_id)

        return books


def resolve_period(raw):
    """One of `PERIODS`, falling back to the default.

    Anything unrecognised is read as the default rather than refused: this
    page is a dashboard, and answering a typo with an error page helps
    nobody. Which period is in force is stated on the page, so the fallback
    is never silent.
    """

    value = (raw or "").strip()

    return value if value in PERIODS else DEFAULT_PERIOD


def resolve_category(raw):
    """A category id that exists, or "".

    Checked against the table rather than merely parsed: a numeric id for a
    category nobody has would otherwise produce a page of zeroes that
    looked like a finding.
    """

    value = (raw or "").strip()

    if not value.isdigit():
        return ""

    return value if Category.objects.filter(id=value).exists() else ""


# --------------------------------------------------------------------------
# The numbers at the top
# --------------------------------------------------------------------------


def loan_summary(period):
    """Issued, returned, out and overdue - in one pass over the loans.

    Two scopes, and the page says which is which rather than leaving it to
    be guessed. `issued` and `returned` are about the period; `active` and
    `overdue` are the position *today*, because "how many are out" is not a
    question about a date range. That split is Task 15's, in `circulation`,
    and using the same one keeps this page and that report agreeing.

    `overdue` is `due_date < today`, strictly - a book due today is not
    late until tomorrow, which is the rule the loan list, the dashboard and
    the overdue report all already apply.
    """

    # Everything relevant, not just the period, because two of the four are
    # current-state figures. The category filter still applies.
    loans = Loan.objects.all()

    if period.category_id:
        loans = loans.filter(
            copy__volume__book__category_id=period.category_id
        )

    tallies = loans.aggregate(
        issued=models.Count("id", filter=period.issued_in_period()),
        returned=models.Count("id", filter=period.returned_in_period()),
        active=models.Count("id", filter=models.Q(return_date__isnull=True)),
        overdue=models.Count(
            "id",
            filter=models.Q(
                return_date__isnull=True, due_date__lt=period.today
            ),
        ),
        # Distinct people, not loans - the same borrower taking six books
        # is one borrower.
        borrowers=models.Count(
            "borrower_id", filter=period.issued_in_period(), distinct=True
        ),
    )

    active = tallies["active"]

    # Nothing out is not a hundred per cent on time and it is not a
    # division by zero either; it is nothing out.
    tallies["overdue_percent"] = (
        round(tallies["overdue"] * 100.0 / active, 1) if active else 0.0
    )

    borrowers = tallies["borrowers"]

    tallies["loans_per_borrower"] = (
        round(tallies["issued"] / borrowers, 1) if borrowers else 0.0
    )

    return tallies


def collection_summary(period):
    """How much of the catalogue is actually being used.

    `never_borrowed` is the one figure on this page that ignores the
    period entirely, and it has to: a book nobody has borrowed *this term*
    may be borrowed every other term, and treating the two as the same
    thing would send a librarian to withdraw a title that is doing fine.
    It means no loan has ever existed for any copy of the book.

    Books with no copies are excluded from both of the last two. A title
    with nothing on the shelf has not been ignored - there is nothing to
    ignore.
    """

    books = period.books()

    return {
        "total_books": books.count(),
        "borrowed_books": books.filter(
            self_loaned(period)
        ).distinct().count(),
        "never_borrowed": never_borrowed_books(period).count(),
    }


def self_loaned(period):
    """`Q` for "this book was borrowed in the period", as an EXISTS.

    A subquery rather than a join, so counting distinct books does not mean
    de-duplicating a multiplied result set in Python.
    """

    return models.Exists(
        Loan.objects.filter(
            period.issued_in_period(),
            copy__volume__book_id=models.OuterRef("pk"),
        )
    )


def has_copies():
    """`Q` for "at least one physical copy exists"."""

    return models.Exists(
        BookCopy.objects.filter(volume__book_id=models.OuterRef("pk"))
    )


def ever_loaned():
    """`Q` for "this book has been borrowed at least once, ever"."""

    return models.Exists(
        Loan.objects.filter(copy__volume__book_id=models.OuterRef("pk"))
    )


# --------------------------------------------------------------------------
# Trend
# --------------------------------------------------------------------------


def borrowing_trend(period):
    """Loans per bucket across the window, grouped in the database.

    One `GROUP BY` over the truncated issue date - nothing is pulled into
    Python to be counted, and the number of rows returned is the number of
    buckets rather than the number of loans.

    Empty buckets are filled in afterwards for the fixed periods, because a
    fortnight with no borrowing is a finding and a trend that silently
    skipped it would read as a flat line. "All time" is left as it comes:
    its length is however long the library has existed, and inventing every
    month back to the first loan would be a table nobody scrolls.
    """

    grain = period.grain

    counted = period.loans().annotate(
        bucket=TREND_TRUNC[grain]("issue_date")
    ).values("bucket").annotate(
        loans=models.Count("id")
    ).order_by("bucket")

    found = {row["bucket"]: row["loans"] for row in counted}

    if period.is_all_time:
        return [
            {"bucket": bucket, "loans": loans}
            for bucket, loans in sorted(found.items())
        ]

    return [
        {"bucket": bucket, "loans": found.get(bucket, 0)}
        for bucket in trend_buckets(period)
    ]


def trend_buckets(period):
    """Every bucket start in the window, including the empty ones.

    Worked out from the dates rather than from the data, which is the whole
    point: a bucket with no loans in it has nothing in the data to find.
    Bounded by the period - 31 days, 14 weeks or 13 months.
    """

    grain = period.grain

    if grain == "day":
        return [
            period.start + timedelta(days=offset)
            for offset in range((period.end - period.start).days + 1)
        ]

    if grain == "week":
        # Postgres truncates a week to its Monday, so the buckets have to
        # start from the Monday of the first week or the fill would miss
        # every one of them.
        first = period.start - timedelta(days=period.start.weekday())

        return [
            first + timedelta(weeks=offset)
            for offset in range(((period.end - first).days // 7) + 1)
        ]

    first = period.start.replace(day=1)

    months = []
    cursor = first

    while cursor <= period.end:
        months.append(cursor)

        cursor = (
            cursor.replace(year=cursor.year + 1, month=1)
            if cursor.month == 12
            else cursor.replace(month=cursor.month + 1)
        )

    return months


# --------------------------------------------------------------------------
# Rankings
# --------------------------------------------------------------------------


def most_borrowed(period, limit=TOP_N):
    """The books that left the building most often in the window.

    `distinct=True` because a book reaches its loans through volumes and
    copies: without it a title with three copies would count each loan
    three times. That is Task 15's `popular()` pattern, and this is
    deliberately the same shape - what differs is the window, the limit and
    the second figure beside it.

    Archived books are included: see the module docstring.

    Ties break by title then id, so the same fortnight ranks the same way
    every time it is looked at.
    """

    return _rank_books(
        Book.objects.filter(period.in_category()), period
    ).filter(
        loans__gt=0
    ).order_by("-loans", "title", "id")[:limit]


def underused(period, limit=TOP_N):
    """Books the library owns that are barely moving.

    Only books with a copy on the premises: a title with nothing on the
    shelf is not underused, it is unstocked, and putting it here would fill
    the list with records nobody can act on.

    Only books still in the catalogue, because this is a list of decisions
    to make and an archived book is a decision already made.

    Includes books with no loans at all in the window - a nought is the
    strongest form of underused, and hiding it would make the list say less
    the worse things got. The section beside this one is about a stronger
    claim: never borrowed *ever*.
    """

    return _rank_books(
        period.books(), period
    ).filter(
        has_copies()
    ).order_by("loans", "title", "id")[:limit]


def never_borrowed_books(period):
    """Books with copies that no loan has ever touched.

    Not "not borrowed lately" - `ever_loaned` has no date in it at all, so
    a book that went out once in 2019 is not here however quiet this term
    was. Two EXISTS subqueries and no join, so counting them and listing
    them cost the same one query each.
    """

    return period.books().filter(has_copies()).exclude(ever_loaned())


def never_borrowed_list(period, limit=TOP_N):
    """The first few never-borrowed books, with their copy counts."""

    return never_borrowed_books(period).annotate(
        copies=models.Count("bookvolume__bookcopy", distinct=True)
    ).select_related(
        "author", "category"
    ).order_by("title", "id")[:limit]


def _rank_books(books, period):
    """Annotate `books` with loans in the window and copies out right now.

    Both aggregates share the one join to loans, so a ranked page is one
    query - the count that ranks and the count beside it are not two trips.
    """

    return books.annotate(
        loans=models.Count(
            "bookvolume__bookcopy__loan",
            filter=period.issued_in_period("bookvolume__bookcopy__loan__"),
            distinct=True,
        ),
        out_now=models.Count(
            "bookvolume__bookcopy__loan",
            filter=models.Q(
                bookvolume__bookcopy__loan__return_date__isnull=True
            ),
            distinct=True,
        ),
    ).select_related("author", "category")


def most_active_borrowers(period, limit=TOP_N):
    """Who borrowed most in the window, and where they stand today.

    `loans` is the period; `out_now` and `overdue_now` are the position
    today, for the same reason the summary splits them - what somebody is
    holding is not a fact about a date range.

    Inactive borrowers are not filtered out. Somebody who borrowed twelve
    books last term and has since been deactivated is exactly who a
    librarian looking at last term wants to see, and hiding them would make
    the history depend on a flag set afterwards.
    """

    return Borrower.objects.annotate(
        loans=models.Count(
            "loan", filter=period.issued_in_period("loan__"), distinct=True
        ),
        out_now=models.Count(
            "loan",
            filter=models.Q(loan__return_date__isnull=True),
            distinct=True,
        ),
        overdue_now=models.Count(
            "loan",
            filter=models.Q(
                loan__return_date__isnull=True,
                loan__due_date__lt=period.today,
            ),
            distinct=True,
        ),
    ).filter(
        loans__gt=0
    ).order_by("-loans", "name", "id")[:limit]


def category_usage(period, limit=TOP_N):
    """Which parts of the collection are being read.

    Two figures that answer different questions: `loans` is how much
    borrowing the category saw, `books` is how much of it was involved.
    A category with two hundred loans spread over four titles is a
    different finding from two hundred spread over ninety.

    Both are `distinct=True` over the same join - loans by loan id, books
    by book id - so one query gives both without either multiplying the
    other. Categories nobody borrowed from are left out: a page of
    noughts is not a ranking.
    """

    categories = Category.objects.all()

    if period.category_id:
        categories = categories.filter(id=period.category_id)

    loan_path = "book__bookvolume__bookcopy__loan__"

    return categories.annotate(
        loans=models.Count(
            "book__bookvolume__bookcopy__loan",
            filter=period.issued_in_period(loan_path),
            distinct=True,
        ),
        books=models.Count(
            "book",
            filter=period.issued_in_period(loan_path),
            distinct=True,
        ),
    ).filter(
        loans__gt=0
    ).order_by("-loans", "name")[:limit]


# --------------------------------------------------------------------------
# How long a loan lasts
# --------------------------------------------------------------------------


def loan_duration(period):
    """Shortest, longest and average, over the loans that actually ended.

    Only returned loans, and only those returned inside the window. A loan
    still out has no duration - it has an age - and averaging today's date
    into it would make the figure drift every time the page was refreshed
    and shorten every time somebody returned something late.

    `return_date - issue_date` is computed and averaged in the database.
    Postgres subtracts two dates to an interval, which Django hands back as
    a `timedelta`; a same-day return is a real nought rather than a
    missing value, so it is counted.

    Returns `None` for each figure when nothing was returned, which the
    template shows as an em dash - "no returns to measure" is an answer,
    and printing 0 days would be a different and untrue one.
    """

    loans = Loan.objects.filter(
        period.returned_in_period(), return_date__isnull=False
    )

    if period.category_id:
        loans = loans.filter(
            copy__volume__book__category_id=period.category_id
        )

    tallies = loans.annotate(
        held=models.ExpressionWrapper(
            models.F("return_date") - models.F("issue_date"),
            output_field=models.DurationField(),
        )
    ).aggregate(
        returned=models.Count("id"),
        average=models.Avg("held"),
        shortest=models.Min("held"),
        longest=models.Max("held"),
    )

    return {
        "returned": tallies["returned"],
        "average": as_days(tallies["average"]),
        "shortest": as_days(tallies["shortest"]),
        "longest": as_days(tallies["longest"]),
    }


def as_days(value):
    """A `timedelta` as a number of days, or None.

    One decimal place: an average of six and a half days is worth knowing
    and six-point-four-eight-three is not.
    """

    if value is None:
        return None

    return round(value.total_seconds() / 86400.0, 1)


# --------------------------------------------------------------------------
# What is wrong with the stock
# --------------------------------------------------------------------------


def collection_attention(period):
    """Copies that are not on the shelf in usable condition, and why.

    The four condition counts come straight from Task 15's
    `inventory_counts` with its `CONDITION_STATES` - the same four
    conditions the copy list filters by and the condition report shows, in
    one query, so a count here and the list it links to agree.

    Nothing here is a score. Four numbers and a link to the records behind
    each; ranking them against one another would be inventing a judgement
    the library did not make.

    Not scoped to the period. A copy that has been lost since March is
    still lost in a page about the last thirty days, and hiding it because
    it was lost too long ago would be the opposite of the point.
    """

    copies = BookCopy.objects.all()

    if period.category_id:
        copies = copies.filter(
            volume__book__category_id=period.category_id
        )

    return inventory_counts(copies, period.today, states=CONDITION_STATES)


def recent_stock_check_findings(limit=RECENT_SESSIONS):
    """What the last few finished stock checks failed to find.

    Task 13's own `missing_copies` for each one, which is that session's
    expected set minus what it found. Two things follow from reusing it
    rather than writing the query again: the expected set is already scoped
    to the session, so a scan of something from another shelf was never in
    it and cannot appear here; and nothing about a copy's status is read or
    written, because a stock check reports and the librarian decides.

    Completed sessions only. A count from a stock check still being walked
    would say most of the library was missing.

    Counted per session and labelled per session, so a copy that went
    unfound in three checks is three findings - which is what "this check
    did not find it" means three times over. The page says so; it does not
    claim to count distinct copies.

    `limit` is fixed, so this costs the same after the fiftieth stock check
    as after the fifth.
    """

    sessions = list(
        InventorySession.objects.filter(
            status=InventorySession.STATUS_COMPLETED
        ).select_related("location", "shelf__location")
        .order_by("-completed_at", "-id")[:limit]
    )

    findings = []

    for session in sessions:
        findings.append({
            "session": session,
            "missing": inventory.missing_copies(session).count(),
        })

    return findings
