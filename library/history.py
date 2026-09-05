"""One copy's history, assembled from the records that already hold it.

The copy's own page says where a book is now. This says how it got there.

Nothing here is stored. Every event is read from whichever table already
owns it - the loans, the activity log, the inventory scans - and merged for
display. Copying a `Loan` row into a movement table would give the library
two accounts of the same lending, and the day they disagreed the copy would
be one that had been both returned and not returned.

  Issued, Returned    `Loan`. The dates are on the row, and so are the
                      borrower and the member of staff who handled it.
  Renewed             `ActivityLog`, written by `loan_renew` inside the
                      same transaction that moves the due date - which is
                      what makes it the count the renewal limit trusts.
  Withdrawn           `ActivityLog`, written by `book_copy_withdraw`.
  Moved, status       `ActivityLog`, written by the move and edit views.
  Counted             Task 13's `InventoryScan`.

Nothing is reconstructed. A shelf change made before the edit view started
recording the previous shelf simply has no event, and the timeline says
what it knows rather than guessing what it does not.
"""

from datetime import datetime, time

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .models import ActivityLog, InventoryScan, Loan


class Event:
    """One thing that happened to a copy.

    A plain object rather than a model: there is no table behind it, and
    the whole point is that there is not one.
    """

    def __init__(self, when, kind, label, *, user=None, borrower=None,
                 detail="", url="", tone="secondary", order=0, key=0):
        self.when = when
        self.kind = kind
        self.label = label
        self.user = user
        self.borrower = borrower
        self.detail = detail
        self.url = url
        self.tone = tone

        # Two events at the same instant need a deterministic order, or a
        # page reload can shuffle them. `order` separates kinds that share
        # a timestamp - an issue and its return on the same day - and `key`
        # is the row id, which never repeats within a kind.
        self.order = order
        self.key = key

    @property
    def sort_key(self):
        return (self.when, self.order, self.key)


def _at(day):
    """A date as a datetime, so it can be ordered against a timestamp.

    `issue_date` and `return_date` are dates: the time of day was never
    recorded. Midnight is the honest reading of that, and it puts a loan
    event at the start of its day rather than inventing an hour for it.
    """

    moment = datetime.combine(day, time.min)

    return timezone.make_aware(moment) if settings.USE_TZ else moment


def loan_events(copy):
    """Issued and Returned, from the loans themselves."""

    events = []

    loans = Loan.objects.filter(copy=copy).select_related(
        "borrower", "issued_by", "returned_to"
    )

    for loan in loans:

        events.append(
            Event(
                _at(loan.issue_date),
                "issued",
                "Issued",
                user=loan.issued_by,
                borrower=loan.borrower,
                detail="Due %s" % loan.due_date,
                url=reverse("loan_detail", args=[loan.id]),
                tone="info",
                # After a return on the same day: a copy returned and
                # re-issued on one afternoon reads in that order.
                order=1,
                key=loan.id,
            )
        )

        if loan.return_date is not None:
            events.append(
                Event(
                    _at(loan.return_date),
                    "returned",
                    "Returned",
                    user=loan.returned_to,
                    borrower=loan.borrower,
                    url=reverse("loan_detail", args=[loan.id]),
                    tone="success",
                    order=0,
                    key=loan.id,
                )
            )

    return events, [loan.id for loan in loans]


# What an activity-log action means on a copy's timeline, and how to show
# it. Anything not named here is a copy edit that said nothing specific.
COPY_LOG_KINDS = {
    "WITHDRAW": ("withdrawn", "Withdrawn", "warning"),
    "CREATE": ("added", "Added to the library", "secondary"),
    "UPDATE": ("updated", "Updated", "secondary"),
}


def log_events(copy, loan_ids):
    """Renewals, withdrawals, moves and status changes.

    Two reads of the activity log: the entries against this copy, and the
    renewals, which are recorded against the loan they extended rather than
    against the copy - so they are found through the copy's own loans.
    """

    events = []

    for log in ActivityLog.objects.filter(
        entity_type="BookCopy", entity_id=copy.id
    ).select_related("user"):

        kind, label, tone = COPY_LOG_KINDS.get(
            log.action, ("updated", log.action.title(), "secondary")
        )

        events.append(
            Event(
                log.created_at,
                kind,
                label,
                user=log.user,
                detail=log.description or "",
                tone=tone,
                order=2,
                key=log.id,
            )
        )

    if loan_ids:

        for log in ActivityLog.objects.filter(
            action="RENEW",
            entity_type="Loan",
            entity_id__in=loan_ids,
        ).select_related("user"):

            events.append(
                Event(
                    log.created_at,
                    "renewed",
                    "Renewed",
                    user=log.user,
                    detail=log.description or "",
                    url=reverse("loan_detail", args=[log.entity_id]),
                    tone="info",
                    order=2,
                    key=log.id,
                )
            )

    return events


def inventory_events(copy):
    """Every stock check that accounted for this copy."""

    return [
        Event(
            scan.scanned_at,
            "counted",
            "Counted in a stock check",
            user=scan.scanned_by,
            detail=scan.session.name,
            url=reverse("inventory_session_detail", args=[scan.session_id]),
            tone="success",
            order=3,
            key=scan.id,
        )
        for scan in InventoryScan.objects.filter(
            copy=copy,
            outcome=InventoryScan.OUTCOME_FOUND,
        ).select_related("session", "scanned_by")
    ]


def copy_timeline(copy):
    """Everything on record for this copy, newest first.

    Four queries whatever the length of the history: the loans, the copy's
    log entries, the renewals of those loans, and the stock checks. None of
    them grows with the size of the library, and none of them is per-event.
    """

    events, loan_ids = loan_events(copy)
    events += log_events(copy, loan_ids)
    events += inventory_events(copy)

    return sorted(events, key=lambda event: event.sort_key, reverse=True)
