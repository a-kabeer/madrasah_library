"""Stock checking: what a session expects, what it found, what is missing.

A stock check is a comparison, not a state. Nothing here writes to a copy:
the shelves are the truth, the session records what a person picked up, and
the difference between the two is the report. Marking something Missing
afterwards is the librarian's decision and the existing copy-management
action - which is why a session that finds nothing changes nothing.

Everything is counted in the database. A library with forty thousand copies
should cost a stock-check dashboard the same as one with four hundred, so
the expected set is a queryset that is only ever counted or excluded
against, never pulled into Python.

The copy identifier is the same one the rest of the application scans:
`copy_code`, resolved through `copy_for_code` in views.py. There is no
inventory barcode and nothing to migrate.
"""

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from .models import BookCopy, InventoryScan, InventorySession


def expected_copies(session):
    """The copies a session should be able to find.

    Scope decides it, and nothing else does:

      * the whole library, which is every copy on record;
      * a location, which is every copy on any of its shelves;
      * a shelf.

    Copies of archived books are included. Archiving takes a book out of
    the catalogue, not out of the building - the physical copy is still on
    the shelf and still has to be accounted for.

    Nor is any status excluded. A copy marked Lost or Missing is expected
    here, because a stock check is exactly the occasion on which one of
    them turns up again; and a copy that is out on loan is expected too,
    for the opposite reason - the report shows it as not found with the
    status that explains why. Filtering either of them out would be the
    report deciding in advance what the count is allowed to say.
    """

    copies = BookCopy.objects.all()

    if session.scope == InventorySession.SCOPE_LOCATION:
        return copies.filter(shelf__location_id=session.location_id)

    if session.scope == InventorySession.SCOPE_SHELF:
        return copies.filter(shelf_id=session.shelf_id)

    return copies


def found_copy_ids(session):
    """The ids this session has accounted for, as a subquery.

    Deliberately not a list: it is used to exclude from the expected set,
    and a list would mean carrying every found id through Python to build
    the query it is about to be put back into.
    """

    return InventoryScan.objects.filter(
        session=session,
        outcome=InventoryScan.OUTCOME_FOUND,
    ).values("copy_id")


def session_counts(session):
    """Expected, found, remaining, and the two kinds of unhelpful scan.

    Two queries whatever the size of the library: one over the copies in
    scope, one over this session's scans with the outcomes counted
    conditionally.
    """

    expected = expected_copies(session).count()

    tallies = InventoryScan.objects.filter(session=session).aggregate(
        found=models.Count(
            "id",
            filter=models.Q(outcome=InventoryScan.OUTCOME_FOUND),
        ),
        duplicates=models.Count(
            "id",
            filter=models.Q(outcome=InventoryScan.OUTCOME_DUPLICATE),
        ),
        outside=models.Count(
            "id",
            filter=models.Q(outcome=InventoryScan.OUTCOME_OUTSIDE),
        ),
        unknown=models.Count(
            "id",
            filter=models.Q(outcome=InventoryScan.OUTCOME_UNKNOWN),
        ),
    )

    found = tallies["found"]

    return {
        "expected": expected,
        "found": found,
        "remaining": max(expected - found, 0),
        "duplicates": tallies["duplicates"],
        "outside": tallies["outside"],
        "unknown": tallies["unknown"],
        # Whole percent, and 100 for an empty scope rather than a division
        # by zero: a shelf with nothing on it has been fully checked.
        "progress": (
            100 if not expected else int(found * 100 / expected)
        ),
    }


def missing_copies(session):
    """Expected, and not found. The report, not a verdict.

    Their status comes with them, because it is usually the explanation: a
    copy that is out on loan is not missing, and one already marked Lost
    was missing before this session started. Nothing here changes any of
    them.
    """

    return expected_copies(session).exclude(
        id__in=found_copy_ids(session)
    ).select_related(
        "volume__book__author",
        "shelf__location",
    ).order_by("copy_code")


def session_scans(session, outcome):
    """This session's scans of one kind, ready to list."""

    return InventoryScan.objects.filter(
        session=session,
        outcome=outcome,
    ).select_related(
        "copy__volume__book__author",
        "copy__shelf__location",
        "scanned_by",
    ).order_by("-scanned_at", "-id")


def in_scope(session, copy):
    """Whether `copy` is one this session expects to find."""

    if session.scope == InventorySession.SCOPE_LOCATION:
        return (
            copy.shelf_id is not None
            and copy.shelf.location_id == session.location_id
        )

    if session.scope == InventorySession.SCOPE_SHELF:
        return copy.shelf_id == session.shelf_id

    return True


def record_scan(session, copy, code, user):
    """Write one read and say what it was.

    `copy` is what `copy_for_code` resolved, or None. Four answers, and the
    caller reports whichever comes back:

      found       - counted, for the first time
      duplicate   - this session already has it
      outside     - a real copy, but not one this session covers, which is
                    a misplacement worth reporting and not a thing to fix
                    here: the copy is left exactly where the record says
      unknown     - no copy carries that code

    The duplicate case is decided by the database, not by the check above
    it. `unique_found_copy_per_session` refuses a second found row, so two
    people scanning the same shelf at the same moment get one Found and one
    duplicate rather than two Founds - which a read-then-write in Python
    could not promise.
    """

    now = timezone.now()

    def write(outcome):
        return InventoryScan.objects.create(
            session=session,
            copy=copy,
            copy_code=code,
            outcome=outcome,
            scanned_by=user,
            scanned_at=now,
        )

    if copy is None:
        return InventoryScan.OUTCOME_UNKNOWN, write(
            InventoryScan.OUTCOME_UNKNOWN
        )

    if not in_scope(session, copy):
        return InventoryScan.OUTCOME_OUTSIDE, write(
            InventoryScan.OUTCOME_OUTSIDE
        )

    try:
        # Its own transaction: a refused insert marks the surrounding one
        # as broken, and the duplicate row written afterwards has to be
        # able to land.
        with transaction.atomic():
            scan = write(InventoryScan.OUTCOME_FOUND)

    except IntegrityError:
        return InventoryScan.OUTCOME_DUPLICATE, write(
            InventoryScan.OUTCOME_DUPLICATE
        )

    return InventoryScan.OUTCOME_FOUND, scan


def last_completed_check(copy):
    """The most recent finished session that covered this copy, and whether
    it turned up.

    For the copy's own page. Scoped to the one copy and to sessions whose
    scope actually included it - a shelf check of a different shelf says
    nothing about this book, and reporting it as "not found" would be a
    lie by omission.

    Returns `(session, found)` or `(None, None)`. Deliberately not offered
    to the copy list: this is two queries for one copy, which is fine on a
    page about that copy and would be a per-row cost on a page about
    twenty-five of them.
    """

    covering = models.Q(scope=InventorySession.SCOPE_LIBRARY)

    if copy.shelf_id is not None:
        covering |= models.Q(
            scope=InventorySession.SCOPE_SHELF,
            shelf_id=copy.shelf_id,
        )
        covering |= models.Q(
            scope=InventorySession.SCOPE_LOCATION,
            location_id=copy.shelf.location_id,
        )

    session = InventorySession.objects.filter(
        covering,
        status=InventorySession.STATUS_COMPLETED,
    ).select_related(
        "started_by", "location", "shelf"
    ).order_by("-completed_at", "-id").first()

    if session is None:
        return None, None

    return session, InventoryScan.objects.filter(
        session=session,
        copy=copy,
        outcome=InventoryScan.OUTCOME_FOUND,
    ).exists()
