"""What this installation is, and the rules it lends by.

One page, three things: how the library names and colours itself, the
institution it belongs to, and the borrowing policy - how many books at
once, for how long, and what happens when something is overdue. Admin only.
"""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import EmailValidator, URLValidator
from django.db import transaction
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.translation import gettext

from ..models import (
    OrganizationSettings,
    RoleFeature,
    validate_hex_color,
)

from .. import features
from .. import policy

from ..context_processors import clear_branding_cache
from ..permissions import feature_required, role_required

from .common import (
    FAVICON_EXTENSIONS,
    create_activity_log,
    validate_image_upload,
)


def read_policy_form(request, branding):
    """Apply the posted borrowing rules to `branding`, or say what is wrong.

    Blank means "not configured", which is stored as NULL and read back as
    the default - so clearing a field is how a library goes back to the
    behaviour it had before it set one, and there is no magic number to
    remember.
    """

    def whole_number(field, label, low, high):

        raw = (request.POST.get(field) or "").strip()

        if not raw:
            return None, None

        if not raw.isdigit():
            return None, "%s has to be a whole number." % label

        value = int(raw)

        if not low <= value <= high:
            return None, "%s has to be between %d and %d." % (
                label, low, high
            )

        return value, None

    fields = (
        (
            "loan_period_days",
            "Loan period",
            policy.LOAN_PERIOD_MIN,
            policy.LOAN_PERIOD_MAX,
        ),
        (
            "max_active_loans",
            "Maximum books per borrower",
            policy.LIMIT_MIN,
            policy.LIMIT_MAX,
        ),
        (
            "max_renewals",
            "Renewal limit",
            policy.LIMIT_MIN,
            policy.LIMIT_MAX,
        ),
    )

    values = {}

    for field, label, low, high in fields:

        value, problem = whole_number(field, label, low, high)

        if problem:
            return problem

        values[field] = value

    for field, value in values.items():
        setattr(branding, field, value)

    branding.block_when_overdue = (
        request.POST.get("block_when_overdue") == "on"
    )

    return None


# Institution types offered in the form as a datalist, never enforced. The
# column is free text on purpose (see the model), so these are a shortcut
# for the common answers rather than the set of permitted ones - an
# institution that is none of them types its own.
INSTITUTION_TYPE_SUGGESTIONS = (
    "Madrasah",
    "Jamia",
    "Maktab",
    "School",
    "College",
    "University",
    "Public Library",
    "Research Institute",
)


def normalize_website(value):
    """A typed web address, with a scheme if the typist left it out.

    People write "alnoor.edu.pk", not "https://alnoor.edu.pk", and refusing
    that would be the form being right about a standard rather than useful
    about an address. Anything that already names a scheme is left exactly
    as it is - including one this application will not accept, so a typed
    "ftp://..." is still refused by the validator below rather than quietly
    turned into something else.
    """

    value = (value or "").strip()

    if not value or "://" in value:
        return value

    return "https://%s" % value


def validate_institution_fields(email, website):
    """The first complaint about the institution's contact details, or None.

    Django's own validators rather than a pattern written here: an email
    address and a URL are two of the things it already knows, and a second
    opinion about either would eventually disagree with the model field
    that stores it.

    Both are optional, so a blank value is never checked - "nobody has
    said" is not "wrong", and the whole point of these fields is that an
    installation may leave every one of them empty.
    """

    checks = (
        (
            email,
            EmailValidator(
                message=(
                    "Enter a valid email address, e.g. office@example.org."
                )
            ),
        ),
        (
            website,
            URLValidator(
                schemes=["http", "https"],
                message=(
                    "Enter a valid web address, e.g. https://example.org."
                ),
            ),
        ),
    )

    for value, validator in checks:

        if not value:
            continue

        try:
            validator(value)

        except ValidationError as exc:
            return exc.messages[0]

    return None


#Organization branding and borrowing policy
@feature_required("branding", "Admin")
def branding_settings(request):

    branding = OrganizationSettings.load()

    error = None
    policy_error = None

    # Two forms on one page, told apart by a hidden field. Separate because
    # they are separate state: saving the colours must not blank a lending
    # rule, and the branding form posts files while this one does not.
    if request.method == "POST" and request.POST.get("section") == "policy":

        policy_error = read_policy_form(request, branding)

        if policy_error is None:

            branding.save()

            clear_branding_cache()

            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="OrganizationSettings",
                entity_id=branding.id,
                description="Borrowing policy updated",
            )

            messages.success(request, gettext("Borrowing policy updated."))

            return redirect("branding_settings")

    elif request.method == "POST":

        name = request.POST.get("name", "").strip()
        primary_color = request.POST.get("primary_color", "").strip()
        secondary_color = request.POST.get("secondary_color", "").strip()
        accent_color = request.POST.get("accent_color", "").strip()
        contact_email = request.POST.get("contact_email", "").strip()
        contact_phone = request.POST.get("contact_phone", "").strip()
        footer_text = request.POST.get("footer_text", "").strip()

        # The institution's own details. On this form rather than a third
        # one, because they are the same state as the name, the logo and
        # the contact details already here - all of it is one answer to
        # "who is this installation?", saved and shown together. The
        # borrowing policy is a separate form because lending rules are a
        # genuinely different kind of state; this is not.
        name_arabic = request.POST.get("name_arabic", "").strip()
        institution_type = request.POST.get("institution_type", "").strip()
        address = request.POST.get("address", "").strip()
        website = normalize_website(request.POST.get("website", ""))

        logo = request.FILES.get("logo")
        favicon = request.FILES.get("favicon")

        remove_logo = request.POST.get("remove_logo") == "on"
        remove_favicon = request.POST.get("remove_favicon") == "on"

        # Colours first: cheapest to check, and a bad one shouldn't leave a
        # freshly uploaded file behind.
        for value in (primary_color, secondary_color, accent_color):

            try:
                validate_hex_color(value)

            except ValidationError as exc:

                error = exc.messages[0]
                break

        # Then the two fields Django can check for us, for the same reason
        # the colours come first: text is cheaper than a file, and a
        # rejected form should not have written an upload to storage.
        if error is None:

            error = validate_institution_fields(contact_email, website)

        if error is None and logo:

            error = validate_image_upload(
                logo,
                max_bytes=settings.LOGO_MAX_BYTES,
                label="Logo",
            )

        if error is None and favicon:

            error = validate_image_upload(
                favicon,
                max_bytes=settings.FAVICON_MAX_BYTES,
                extensions=FAVICON_EXTENSIONS,
                label="Favicon",
            )

        if error is None:

            branding.name = name
            branding.primary_color = primary_color
            branding.secondary_color = secondary_color
            branding.accent_color = accent_color
            branding.contact_email = contact_email
            branding.contact_phone = contact_phone
            branding.footer_text = footer_text
            branding.name_arabic = name_arabic
            branding.institution_type = institution_type
            branding.address = address
            branding.website = website

            # Remember the previous files so their storage can be cleaned up
            # once the new state is safely saved. An upload wins over the
            # remove checkbox, since choosing a file is the more specific
            # intent — same rule as replacing a book cover.
            previous_logo = branding.logo.name
            previous_favicon = branding.favicon.name

            if logo:
                branding.logo = logo

            elif remove_logo:
                branding.logo = None

            if favicon:
                branding.favicon = favicon

            elif remove_favicon:
                branding.favicon = None

            branding.save()

            for previous, current in (
                (previous_logo, branding.logo),
                (previous_favicon, branding.favicon),
            ):

                if previous and current.name != previous:
                    current.storage.delete(previous)

            clear_branding_cache()

            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="OrganizationSettings",
                entity_id=branding.id,
                description="Organisation branding updated",
            )

            messages.success(
                request,
                gettext("Branding updated."),
            )

            return redirect("branding_settings")

        # Fell through with an error: show what was typed rather than
        # silently discarding it.
        branding.name = name
        branding.primary_color = primary_color
        branding.secondary_color = secondary_color
        branding.accent_color = accent_color
        branding.contact_email = contact_email
        branding.contact_phone = contact_phone
        branding.footer_text = footer_text
        branding.name_arabic = name_arabic
        branding.institution_type = institution_type
        branding.address = address
        branding.website = website

    return render(
        request,
        "library/branding_settings.html",
        {
            "settings_obj": branding,
            "error": error,
            "policy_error": policy_error,
            "institution_types": INSTITUTION_TYPE_SUGGESTIONS,
            "policy_defaults": {
                "loan_period_days": policy.DEFAULT_LOAN_PERIOD_DAYS,
                "loan_period_min": policy.LOAN_PERIOD_MIN,
                "loan_period_max": policy.LOAN_PERIOD_MAX,
                "limit_min": policy.LIMIT_MIN,
                "limit_max": policy.LIMIT_MAX,
            },
        }
    )


@feature_required("permissions", "Admin")
def permissions_matrix(request):
    """Which roles see which parts of the menu, as a grid you can tick.

    A SuperAdmin configures all three roles. An Admin configures the two
    below them and sees their own column read-only - nobody edits the
    permissions of the role they are signed in as, which is what stops an
    Admin from switching off the page that would let them switch it back.

    The whole grid is one POST. A checkbox that is absent means "off", so
    the form has to be read against the list of pairs this person is
    allowed to set rather than against what arrived - otherwise a
    hand-written POST could clear a column the page never showed them, and
    a missing checkbox for somebody else's row would read as "switch it
    off".

    Nothing here can widen a role past the ceiling in library/features.py:
    `editable_pairs` never offers a pair outside it, and `role_has` checks
    the ceiling before it reads the table anyway. The worst a crafted POST
    achieves is a stored row that is then ignored.
    """

    editor_role = request.user.role

    if request.method == "POST":

        allowed_pairs = features.editable_pairs(editor_role)

        if not allowed_pairs:
            raise PermissionDenied(
                "Your role cannot change menu permissions."
            )

        submitted = set(request.POST.getlist("feature"))

        now = timezone.now()
        changed = []

        with transaction.atomic():

            for role, key in sorted(allowed_pairs):

                wanted = ("%s:%s" % (role, key)) in submitted
                current = features.role_has(role, key)

                if wanted == current:
                    continue

                RoleFeature.objects.update_or_create(
                    role=role,
                    feature_key=key,
                    defaults={
                        "allowed": wanted,
                        "updated_at": now,
                        "updated_by": request.user,
                    },
                )

                changed.append(
                    "%s %s %s" % (
                        role,
                        "gained" if wanted else "lost",
                        features.BY_KEY[key].label,
                    )
                )

        features.clear_cache()

        if changed:
            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="RoleFeature",
                entity_id=None,
                description="Menu permissions: " + "; ".join(changed),
            )

            messages.success(
                request,
                gettext("Saved. %d change%s.")
                % (len(changed), "" if len(changed) == 1 else "s"),
            )

        else:
            messages.info(request, gettext("Nothing changed."))

        return redirect("permissions_matrix")

    return render(
        request,
        "library/permissions_matrix.html",
        {
            "sections": features.matrix_for(editor_role),
            "roles": features.MANAGED_ROLES,
            "editor_role": editor_role,
            "is_super_admin": editor_role == features.SUPER_ADMIN,
        },
    )
