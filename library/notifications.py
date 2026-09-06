"""Actionable messages for the people who can act on them.

A notification is not an event log. `activity_logs` already records what
happened and who did it, and every row of it is worth keeping whether or
not anybody reads it. A notification is addressed to somebody, asks them to
do something, and stops mattering once they have. Nothing here reads the
audit trail to decide what to send, and nothing here is written back into
it - the transitions that log already log, and adding a second entry saying
the same thing would leave the library with two accounts of one event.


Who can be a recipient
----------------------

Only a `User`. This schema has no relationship at all between `borrowers`
and `users`: a borrower is a name, a phone number and a type, and there is
no column joining one to an account. Borrowers do not sign in - the whole
application is behind `LoginRequiredMiddleware` and the only accounts are
the three staff roles. Creating accounts for borrowers to receive
notifications would be inventing an authentication story this task does not
have, so it is not done.

What that means for the events that are, on the face of it, about a
borrower: they are sent to the desk instead, phrased as the thing the desk
can do. "Hafsa's reservation is ready" is not a message to Hafsa here; it
is a message to whoever is standing at the counter, telling them a copy is
on the shelf and who it has to go to. The borrower is told the way this
library already tells borrowers things - a member of staff picks up the
phone.

And the events with nothing for the desk to do are not sent at all. A
cancelled reservation is one: the only person who would want to know is the
borrower, who has no account, and the librarian who cancelled it is the one
who did it. It is already on the reservation list and in the activity log,
which is the existing staff workflow for it.


When a notification is created
------------------------------

Only inside an authoritative state transition, never while a page is being
drawn. Five of them, each of which was already the moment the library's
mind changed:

  * a copy is returned          `loan_return`
  * a reservation is fulfilled  `loan_add`, after `reservations.fulfil_for`
  * a reservation is cancelled  `reservation_cancel`
  * a stock check is completed  `inventory_session_complete`
  * a book is suggested         `suggestion_add`

The first three all ask the same question - is somebody now at the front of
a queue with a copy on the shelf? - which is why they all call
`announce_ready`. Two bounded queries, on the transition, not on the
request.

Nothing scans the loans or the reservations on an ordinary page load, and
there is no worker, no schedule and no polling. Overdue loans are
deliberately absent for exactly that reason: nothing in this application
transitions a loan to "overdue", the state is derived from `due_date` on
every read, and manufacturing a transition for it would mean either a
background job (out of scope) or a sweep on every request (forbidden, and
slower every year). The dashboard's overdue card and the overdue report
already answer that question, and they stay the way it is answered.


Idempotency
-----------

`event_key` names the transition, not the message: "reservation_ready:41"
is *the* notification for reservation 41 becoming actionable, and there is
never a second one - not when the queue advances past it, not when it is
fulfilled, not when it is cancelled and not when two copies come back at
the same moment.

`unique_notification_event` over (recipient_id, event_key) is a unique
index in the database, and creation goes through `bulk_create(...,
ignore_conflicts=True)`, which is one INSERT ... ON CONFLICT DO NOTHING.
So the second attempt writes nothing rather than raising, and two
simultaneous attempts leave one row without either of them holding a lock
anybody else could be waiting on.
"""

from django.urls import reverse
from django.utils import timezone

from . import reservations
from .models import BookCopy, Notification, User


# How many notifications the shell's panel shows. Bounded on purpose: the
# panel is a glance, and the list page is where the rest lives.
PANEL_LIMIT = 8

# The unread count stops counting here. A badge saying "99+" is as useful
# as one saying 4,312, and this keeps the query's cost fixed no matter how
# long somebody has gone without reading anything.
UNREAD_CAP = 99


def staff_recipients(roles=None):
    """The active users a notification may be sent to.

    `roles` narrows it, and should whenever the destination is narrower:
    sending an Assistant a link to a page `role_required` will refuse them
    is offering a door that answers 403. Reading a notification never
    grants access to what it points at - the view behind the link is still
    the boundary - so this is about not wasting somebody's attention, and
    about not putting a record in front of a role that does not otherwise
    see it.

    Inactive accounts are excluded: they cannot sign in, so a notification
    for one is one nobody will ever read.
    """

    people = User.objects.filter(is_active=True)

    if roles:
        people = people.filter(role__in=roles)

    return people.only("id")


def notify(recipients, *, event_type, event_key, title, message="", url=""):
    """Send one notification to each of `recipients`, at most once ever.

    Returns how many recipients this transition was newly announced to, and
    0 when it had already been announced.

    The writing is one INSERT ... ON CONFLICT DO NOTHING, whatever the
    number of recipients. That statement, and the unique index behind it,
    are what guarantee a single row per recipient per transition: two
    requests arriving together both attempt the insert and the database
    decides, with no lock either of them could be waiting on.

    The `already` read above it decides only what to *return*. It is not
    the rule and is not relied on as one: in the race where both callers
    read "not announced", both are told they announced it, and there is
    still exactly one row. Nothing in this application acts on the return
    value except to phrase a sentence, so that is the right trade against
    a lock.
    """

    people = list(recipients)

    if not people:
        return 0

    # Scoped to these recipients as well as to the key, so it is an index
    # probe on `unique_notification_event` rather than a scan of a column
    # that is only ever indexed alongside the recipient.
    already = Notification.objects.filter(
        event_key=event_key,
        recipient__in=people,
    ).exists()

    # One timestamp for the whole fan-out: these rows are one event, and
    # giving them microseconds apart would order them arbitrarily against
    # each other for no reason anybody could see.
    now = timezone.now()

    Notification.objects.bulk_create(
        [
            Notification(
                recipient=person,
                event_type=event_type,
                event_key=event_key,
                title=title,
                message=message,
                url=url,
                created_at=now,
            )
            for person in people
        ],
        ignore_conflicts=True,
    )

    return 0 if already else len(people)


def has_available_copy(book):
    """Whether a copy of `book` is on the shelf right now.

    An existence check against the index rather than a count: the question
    is "any", and the answer stops at the first row.
    """

    return BookCopy.objects.filter(
        volume__book_id=book.id,
        status=BookCopy.STATUS_AVAILABLE,
    ).exists()


def announce_ready(book):
    """Tell the desk if the front of `book`'s queue can be served now.

    "Actionable" is both halves: somebody is waiting *and* there is a copy
    to give them. Either alone is not - a queue for a book that is entirely
    out is a queue, and a copy on the shelf that nobody is waiting for is
    just a copy.

    Called from the three transitions that can make the pair true: a copy
    coming back, and the two ways the front of a queue can leave it. Not
    from any page render, and not from a sweep - each call is two bounded
    queries about one book.

    The reservation's own id keys it, so the same reservation is announced
    once whatever happens afterwards. Somebody behind it in the queue gets
    their own announcement, with their own key, if they ever reach the
    front while a copy is in.

    Returns the reservation it announced, or None.
    """

    front = reservations.queue_front(book)

    if front is None:
        return None

    if not has_available_copy(book):
        return None

    written = notify(
        # Everybody at the desk. Issuing a book is open to all three roles
        # (`loan_add` has no `role_required`), and so is the book page this
        # links to, so there is no role here that could not act on it.
        staff_recipients(),
        event_type=Notification.EVENT_RESERVATION_READY,
        event_key="%s:%d" % (Notification.EVENT_RESERVATION_READY, front.id),
        title="Reserved book is available",
        # The borrower's name, because that is the whole point of the
        # message - and nothing else about them. Their phone number,
        # registration and address are on their own page, behind the link,
        # which is where they belong.
        message=(
            "%s is first in the queue for %s, and a copy is on the shelf."
            % (front.borrower.name, front.book.title)
        ),
        url="%s?tab=reservations" % reverse("book_detail", args=[book.id]),
    )

    return front if written else None


def announce_stock_check(session, missing_count):
    """Tell the librarians a finished stock check has copies unaccounted for.

    Only when something is actually missing. A stock check that found
    everything is good news, and good news that needs no decision is not a
    notification.

    Admin and Librarian only, because `inventory_session_detail` is
    `@role_required("Admin", "Librarian")` - the report this points at is
    not a page an Assistant may open.
    """

    if missing_count <= 0:
        return 0

    return notify(
        staff_recipients(roles=("Admin", "Librarian")),
        event_type=Notification.EVENT_STOCK_CHECK_MISSING,
        event_key=(
            "%s:%d" % (Notification.EVENT_STOCK_CHECK_MISSING, session.id)
        ),
        title="Stock check finished with copies not found",
        message=(
            "%s: %d cop%s expected and not found. Nothing has been marked "
            "Missing."
            % (session.name, missing_count, "y" if missing_count == 1 else "ies")
        ),
        url=reverse("inventory_session_detail", args=[session.id]),
    )


def announce_suggestion(suggestion, *, submitted_by=None):
    """Tell the people who may review it that a book has been suggested.

    Admin and Librarian only, because they are exactly the roles that may
    approve, reject or mark a suggestion acquired - an Assistant may write
    one and read the list, but there is nothing here for them to do, and a
    notification with no action in it is a notification not worth having.

    The person who wrote it is left out. They know: they have just been
    redirected to the suggestion they made, and telling somebody their own
    news is how a notification list stops being read.

    Keyed to the suggestion, so the same one is announced once - a retried
    POST that somehow wrote a second row would still announce only the
    first, and nothing announces it again afterwards.
    """

    recipients = staff_recipients(roles=("Admin", "Librarian"))

    if submitted_by is not None:
        recipients = recipients.exclude(id=submitted_by.id)

    described = suggestion.title

    if suggestion.author_name:
        described = "%s — %s" % (described, suggestion.author_name)

    return notify(
        recipients,
        event_type=Notification.EVENT_SUGGESTION_SUBMITTED,
        event_key=(
            "%s:%d"
            % (Notification.EVENT_SUGGESTION_SUBMITTED, suggestion.id)
        ),
        title="A book has been suggested",
        message=(
            "%s. Suggested by %s."
            % (
                described,
                submitted_by.full_name if submitted_by else "a member of staff",
            )
        ),
        url=reverse("suggestion_detail", args=[suggestion.id]),
    )


# --------------------------------------------------------------------------
# Reading
#
# Every query below starts from the signed-in user. There is no function
# here that takes a notification id without also taking the recipient, so a
# forged id cannot address somebody else's row by accident - the filter is
# not something a caller has to remember.
# --------------------------------------------------------------------------


def for_user(user):
    """This user's notifications, newest first, deterministically.

    `-created_at, -id` everywhere. The timestamp alone would leave two
    written in the same statement - which is what a fan-out to several
    recipients is - in whatever order the database felt like, and a list
    that reshuffles between two page loads is not a list.
    """

    return Notification.objects.filter(recipient=user).order_by(
        "-created_at", "-id"
    )


def unread_count(user):
    """How many unread, counted in the database and capped.

    The cap is what keeps this cheap for somebody who has never opened the
    panel: the subquery stops after UNREAD_CAP rows, so the shell's badge
    costs the same on a fresh install and after ten years.
    """

    return len(
        Notification.objects.filter(
            recipient=user,
            read_at__isnull=True,
        ).order_by().values_list("id", flat=True)[:UNREAD_CAP]
    )


def recent(user, limit=PANEL_LIMIT):
    """The newest few, for the shell's panel. Never the whole history."""

    return list(for_user(user)[:limit])


def mark_read(user, notification_id):
    """Mark one of this user's notifications read. Returns whether it moved.

    A conditional UPDATE rather than a read, a check and a write: the
    `read_at__isnull=True` is in the statement, so a second click - or a
    retried request, or two tabs - matches nothing and changes nothing
    rather than moving the timestamp.

    Somebody else's id matches nothing here for the same reason a
    non-existent one does: `recipient` is part of the filter. The caller
    cannot tell the two apart, and that is deliberate.
    """

    return Notification.objects.filter(
        id=notification_id,
        recipient=user,
        read_at__isnull=True,
    ).update(read_at=timezone.now()) > 0


def mark_all_read(user):
    """Mark this user's unread notifications read. Returns how many moved.

    One UPDATE over the partial index, scoped to this recipient and to what
    is still unread - so it touches nobody else's rows, and running it
    twice does nothing the second time.
    """

    return Notification.objects.filter(
        recipient=user,
        read_at__isnull=True,
    ).update(read_at=timezone.now())


def unread_filter(queryset, unread_only):
    """Narrow a list to the unread ones, in the database."""

    if not unread_only:
        return queryset

    return queryset.filter(read_at__isnull=True)


__all__ = [
    "PANEL_LIMIT",
    "UNREAD_CAP",
    "announce_ready",
    "announce_stock_check",
    "announce_suggestion",
    "for_user",
    "has_available_copy",
    "mark_all_read",
    "mark_read",
    "notify",
    "recent",
    "staff_recipients",
    "unread_count",
    "unread_filter",
]
