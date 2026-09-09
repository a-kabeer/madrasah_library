"""Signing in, the signed-in person, and the people who may sign in.

Two things that look separate but are not: the account you are using
(sign in, sign out, your profile, your theme) and the accounts an Admin
manages. They share the User model and the same rules about who may change
what, so they share a file.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import (
    authenticate,
    login,
    logout,
    update_session_auth_hash,
)
from django.contrib.auth.decorators import login_not_required
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import translation
from django.utils.translation import gettext
from django.urls import reverse
from django.core.cache import cache
from django.utils import timezone
from django.db import models

from ..models import (
    ActivityLog,
    Loan,
    User,
)

from ..features import ADMIN, SUPER_ADMIN
from ..permissions import feature_required
from ..security import login_rate_limiter

from .common import (
    DASHBOARD_CACHE_KEY,
    PAGE_SIZE,
    USER_CACHE_KEY,
    USER_ROLES,
    create_activity_log,
    is_form_modal_request,
    lookup_deleted_response,
    lookup_saved_response,
    query_with,
    safe_redirect_target,
)


@login_not_required
def login_view(request):

    error = None
    rate_limited = False

    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        if login_rate_limiter.is_blocked(request, username):
            rate_limited = True
            error = "Too many failed login attempts. Please try again later."
        else:
            user = authenticate(request, username=username, password=password)

            if user is not None:
                login_rate_limiter.clear_username_failures(username)
                login(request, user)

                return redirect(safe_redirect_target(request, "dashboard"))

            login_rate_limiter.record_failure(request, username)
            error = "Invalid username or password."

    response = render(
        request,
        "library/login.html",
        {
            "error": error,
            "rate_limited": rate_limited,
        }
    )

    if rate_limited:
        response.status_code = 429
        response["Retry-After"] = str(settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS)

    return response


def logout_view(request):
    logout(request)
    return redirect("login")


def profile_view(request):

    error = None
    success = None

    if request.method == "POST":

        current_password = request.POST.get("current_password", "")
        new_password = request.POST.get("new_password", "")
        confirm_password = request.POST.get("confirm_password", "")

        if not request.user.check_password(current_password):

            error = "Current password is incorrect."

        elif not new_password:

            error = "New password is required."

        elif new_password != confirm_password:

            error = "New password and confirmation do not match."

        elif len(new_password) < 8:

            error = "New password must be at least 8 characters."

        else:

            request.user.set_password(new_password)
            request.user.save()

            # Keep the current session valid after changing our own password.
            update_session_auth_hash(request, request.user)

            success = "Password updated successfully."

    return render(
        request,
        "library/profile.html",
        {
            "error": error,
            "success": success,
            "theme_choices": User.THEME_CHOICES,
        }
    )


#Appearance (Light / Dark / System)
def theme_set(request):
    """Save the signed-in user's appearance choice.

    Shared by the topbar selector (which posts with HTMX and wants nothing
    swapped back) and the profile page (a plain form post that should land
    back where it came from).
    """

    if request.method != "POST":

        return HttpResponseBadRequest("POST required.")

    theme = request.POST.get("theme", "").strip()

    valid = {choice for choice, _ in User.THEME_CHOICES}

    if theme not in valid:

        return HttpResponseBadRequest("Unknown theme.")

    request.user.theme_preference = theme
    request.user.save(update_fields=["theme_preference"])

    if request.headers.get("HX-Request") == "true":

        # Nothing to swap: the page already applied the change locally.
        return HttpResponse(status=204)

    messages.success(request, gettext("Appearance updated."))

    return redirect(
        safe_redirect_target(request, "profile")
    )


def language_set(request):
    """Save the signed-in user's language choice, and apply it now.

    `theme_set`'s twin, with one difference that matters: a theme is applied
    by the browser, so that view can answer 204 and let the page repaint
    itself. A language is applied by the server, so the page in front of the
    user is still in the old one and has to be re-fetched. The HTMX caller
    therefore gets a redirect rather than "no content".

    Written to the session as well as the row, so the change takes effect on
    this request rather than the next: `LocaleMiddleware` has already run by
    the time this view is reached.
    """

    if request.method != "POST":

        return HttpResponseBadRequest("POST required.")

    language = request.POST.get("language", "").strip()

    offered = {code for code, _ in settings.LANGUAGES}

    # "" is a real choice: it clears the stored preference and goes back to
    # following the browser.
    if language and language not in offered:

        return HttpResponseBadRequest("Unknown language.")

    request.user.language_preference = language or None
    request.user.save(update_fields=["language_preference"])

    if language:
        translation.activate(language)
        request.session["_language"] = language
    else:
        request.session.pop("_language", None)

    target = safe_redirect_target(request, "profile")

    if request.headers.get("HX-Request") == "true":

        # The whole page has to come back in the new language, and it is not
        # this view's business which page that was - so the browser is told
        # to go there itself rather than a fragment being swapped in.
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target
        return response

    messages.success(request, gettext("Language updated."))

    return redirect(target)


def refuse_user_change(
    actor, target, new_role=None, ending=False, deleting=False
):
    """Why `actor` may not make this change to `target`, or "".

    Four rules, and each of them is about a way an installation could lose
    the ability to govern itself:

      * A SuperAdmin row is the developer's, and an Admin may not touch it.
        The row is still listed - an account that exists but cannot be
        accounted for is its own kind of confusion - but every write
        against it stops here.

      * Nobody changes their own role. An Admin who demoted themselves
        would be locked out of the page that could put them back.

      * The last active SuperAdmin cannot be removed, deactivated or
        demoted. There would then be no account that could restore one.

      * `role` is checked against USER_ROLES by the callers, which is what
        stops anyone creating a SuperAdmin through the form - that account
        is made by somebody with database access, deliberately.

    `ending` covers deletion and deactivation together: both take the
    account out of use, and the last SuperAdmin must survive either.
    """

    actor_role = getattr(actor, "role", None)

    if target.role == SUPER_ADMIN and actor_role != SUPER_ADMIN:
        return (
            "Super Admin accounts are managed by the developer and cannot "
            "be changed from here."
        )

    if actor is not None and actor.pk == target.pk and new_role:
        if new_role != target.role:
            return "You cannot change your own role."

    if target.role == SUPER_ADMIN:

        losing_super_admin = ending or (new_role and new_role != SUPER_ADMIN)

        if losing_super_admin and last_active_super_admin(target):
            return (
                "This is the only active Super Admin. Make another one "
                "first, or the installation would have none."
            )

    # Only a SuperAdmin may delete an Admin. Deactivating one is still
    # allowed - that is reversible from the same page, and deletion is not.
    # Without this, one Admin could remove the others and leave the
    # installation held by a single person.
    if (
        deleting
        and target.role == ADMIN
        and getattr(actor, "role", None) != SUPER_ADMIN
    ):
        return (
            "Only a Super Admin can delete an Admin account. You can "
            "deactivate it instead."
        )

    return ""


def last_active_super_admin(target):
    """True when `target` is the only active SuperAdmin left."""

    return not User.objects.filter(
        role=SUPER_ADMIN,
        is_active=True,
    ).exclude(pk=target.pk).exists()


@feature_required("users", "Admin")
def user_list(request):

    search = request.GET.get("search", "").strip()
    role = request.GET.get("role", "").strip()
    active_status = request.GET.get("status", "").strip()

    if search or role or active_status:
        users = User.objects.all()

        if search:
            users = users.filter(
                models.Q(username__icontains=search)
                | models.Q(full_name__icontains=search)
                | models.Q(role__icontains=search)
            )

        if role:
            users = users.filter(role=role)

        if active_status == "active":
            users = users.filter(is_active=True)
        elif active_status == "inactive":
            users = users.filter(is_active=False)

        users = list(users)

    else:
        users = cache.get(USER_CACHE_KEY)

        if users is None:
            users = list(
                User.objects.all()
            )

            cache.set(
                USER_CACHE_KEY,
                users,
                timeout=300
            )

    paginator = Paginator(users, PAGE_SIZE)
    users = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/user_list.html",
        {
            "role": role,
            "active_status": active_status,
            "roles": User.ROLE_CHOICES,
            "users": users,
            "search": search,
            "paginator": paginator,
            # Everything except `page`, so the search and both filters
            # survive being paged through.
            "pagination_query": query_with(request, page=None),
            "elided_page_range": list(
                paginator.get_elided_page_range(
                    users.number,
                    on_each_side=1,
                    on_ends=1,
                )
            ),
            "page_ellipsis": Paginator.ELLIPSIS,
        }
    )


@feature_required("users", "Admin")
def user_add(request):
    """Create an account, from the dialog or from a plain POST.

    This used to pass `password_hash=` to `User.objects.create()`. The
    model field is `password` with `db_column="password_hash"`, so that
    keyword was not a field at all and every submission raised TypeError -
    Add User has been broken since the model was refactored. It now goes
    through `set_password`, which hashes it; writing the raw value into the
    column would have left `authenticate()` on the login page unable to
    verify it anyway.
    """

    modal = is_form_modal_request(request)
    error = None
    username = ""
    full_name = ""
    role = ""

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        full_name = request.POST.get("full_name", "").strip()
        password = request.POST.get("password", "").strip()
        role = request.POST.get("role", "").strip()

        if not (username and full_name and password and role):

            error = "Please fill in all required fields."

        elif role not in USER_ROLES:

            # USER_ROLES excludes SuperAdmin on purpose: that account is
            # made by somebody with database access, deliberately.
            error = "Choose a role from the list."

        elif len(password) < 8:

            error = "Password must be at least 8 characters."

        elif User.objects.filter(username__iexact=username).exists():

            # Checked rather than left to the unique index, so the dialog
            # can say so instead of the database returning a 500.
            error = "A user with this username already exists."

        else:
            user = User(
                username=username,
                full_name=full_name,
                role=role,
                is_active=True,
                created_at=timezone.now(),
            )
            user.set_password(password)
            user.save()

            cache.delete(USER_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=request.user,
                action="CREATE",
                entity_type="User",
                entity_id=user.id,
                description=f"{user.username} added",
            )

            if modal:
                return lookup_saved_response(user.username)

            return redirect("user_list")

    if modal:
        return user_form_modal(
            request,
            reverse("user_add"),
            error=error,
            username=username,
            full_name=full_name,
            role=role,
        )

    if error:
        messages.error(request, error)

    return redirect("user_list")


@feature_required("users", "Admin")
def user_toggle_active(request, user_id):

    user = get_object_or_404(User, id=user_id)

    if request.method == "POST":

        # Deactivating is `ending` for the purposes of the last-SuperAdmin
        # rule; reactivating one is always allowed.
        refusal = refuse_user_change(
            request.user, user, ending=user.is_active
        )

        if refusal:
            messages.error(request, refusal)
            return redirect(safe_redirect_target(request, reverse("user_list")))

        user.is_active = not user.is_active
        user.save()

        cache.delete(USER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="UPDATE",
            entity_type="User",
            entity_id=user.id,
            description=(
                f"{user.username} "
                f"{'activated' if user.is_active else 'deactivated'}"
            ),
        )

    # The Status switch posts with HTMX and swaps nothing: the list
    # re-requests itself off the event, which keeps the row's badge and the
    # switch in step without this view knowing how either is drawn.
    if request.headers.get("HX-Request") == "true":
        return lookup_saved_response(user.username)

    fallback = reverse("user_list")
    return redirect(safe_redirect_target(request, fallback))


@feature_required("users", "Admin")
def user_edit(request, user_id):

    target_user = get_object_or_404(
        User,
        id=user_id
    )

    error = None

    if request.method == "POST":

        username = request.POST.get(
            "username",
            ""
        ).strip()

        full_name = request.POST.get(
            "full_name",
            ""
        ).strip()

        new_password = request.POST.get(
            "new_password",
            ""
        ).strip()

        role = request.POST.get(
            "role",
            ""
        ).strip()

        refusal = refuse_user_change(
            request.user,
            target_user,
            new_role=role,
            ending=request.POST.get("is_active") != "on",
        )

        if refusal:

            error = refusal

        elif not (username and full_name and role in USER_ROLES):

            error = "Please fill in all required fields."

        elif new_password and len(new_password) < 8:

            error = "New password must be at least 8 characters."

        else:

            target_user.username = username
            target_user.full_name = full_name

            if new_password:

                target_user.set_password(new_password)

            target_user.role = role

            target_user.is_active = (
                request.POST.get("is_active")
                == "on"
            )

            target_user.save()

            cache.delete(
                USER_CACHE_KEY
            )

            cache.delete(
                DASHBOARD_CACHE_KEY
            )

            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="User",
                entity_id=target_user.id,
                description=f"{target_user.username} updated",
            )

            if is_form_modal_request(request):
                return lookup_saved_response(target_user.username)

            return redirect(
                "user_list"
            )

    if is_form_modal_request(request):
        return user_form_modal(
            request,
            reverse("user_edit", args=[target_user.id]),
            error=error,
            username=target_user.username,
            full_name=target_user.full_name or "",
            role=target_user.role,
            editing=True,
            target_user=target_user,
        )

    if error:
        messages.error(request, error)

    return redirect("user_list")


@feature_required("users", "Admin")
def user_delete(request, user_id):

    target_user = get_object_or_404(User, id=user_id)

    # users.id is referenced by loans.issued_by/returned_to and
    # activity_logs.user_id, all NO ACTION FKs — deleting a user who has
    # ever issued/returned a loan (or been logged doing something) would
    # otherwise crash with an unhandled IntegrityError.
    has_related_records = (
        Loan.objects.filter(
            models.Q(issued_by_id=target_user.id)
            | models.Q(returned_to_id=target_user.id)
        ).exists()
        or ActivityLog.objects.filter(user_id=target_user.id).exists()
    )

    modal = is_form_modal_request(request)

    # Why this account cannot go, if it cannot: either a rule refuses it or
    # the records point at it. Both are shown in the dialog rather than
    # discovered on submit.
    blocker = refuse_user_change(
        request.user, target_user, ending=True, deleting=True
    )

    if not blocker and has_related_records:
        blocker = (
            "This account has issued or returned loans, or appears in the "
            "activity log. Deactivate it instead - deleting it would take "
            "that history with it."
        )

    if request.method == "POST":

        if blocker:

            if modal:
                return user_delete_modal(request, target_user, blocker)

            messages.error(request, blocker)
            return redirect("user_list")

        deleted_user_id = target_user.id
        deleted_username = target_user.username

        target_user.delete()

        cache.delete(USER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="DELETE",
            entity_type="User",
            entity_id=deleted_user_id,
            description=f"{deleted_username} deleted",
        )

        if modal:
            return lookup_deleted_response(deleted_username)

        return redirect("user_list")

    if modal:
        return user_delete_modal(request, target_user, blocker)

    if blocker:
        messages.error(request, blocker)

    return redirect("user_list")


def user_form_modal(
    request,
    action,
    error=None,
    username="",
    full_name="",
    role="",
    editing=False,
    target_user=None,
):
    """The Add or Edit User dialog.

    One template for both, the same way one template serves all six lookup
    dialogs. `editing` decides the title, the button and whether the
    password field is required - on an edit it is optional, and blank means
    leave the current one alone.

    Only the roles in USER_ROLES are offered, so SuperAdmin cannot be
    granted through the form however the request is shaped.
    """

    return render(
        request,
        "library/partials/user_form_modal.html",
        {
            "action": action,
            "editing": editing,
            "error": error,
            "username": username,
            "full_name": full_name,
            "role": role,
            "roles": USER_ROLES,
            "target_user": target_user,
        },
    )


def user_delete_modal(request, target_user, blocker):
    """The Delete User confirmation, or the reason there is no button."""

    return render(
        request,
        "library/partials/user_delete_modal.html",
        {
            "target_user": target_user,
            "blocker": blocker,
        },
    )
