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

from ..features import SUPER_ADMIN
from ..permissions import feature_required, role_required

from .common import (
    DASHBOARD_CACHE_KEY,
    PAGE_SIZE,
    USER_CACHE_KEY,
    USER_ROLES,
    create_activity_log,
    safe_redirect_target,
)


@login_not_required
def login_view(request):

    error = None

    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)

            return redirect(safe_redirect_target(request, "dashboard"))

        error = "Invalid username or password."

    return render(
        request,
        "library/login.html",
        {
            "error": error,
        }
    )


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


def refuse_user_change(actor, target, new_role=None, ending=False):
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
        }
    )


@feature_required("users", "Admin")
def user_add(request):

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        full_name = request.POST.get("full_name", "").strip()
        password_hash = request.POST.get("password_hash", "").strip()
        role = request.POST.get("role", "").strip()

        if username and full_name and password_hash and role in USER_ROLES:
            user = User.objects.create(
                username=username,
                full_name=full_name,
                password_hash=password_hash,
                role=role,
                is_active=True,
                created_at=timezone.now(),
            )

            cache.delete(USER_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="User",
                entity_id=user.id,
                description=f"{user.username} شامل کیا گیا",
            )

            return redirect("user_list")

    return render(
        request,
        "library/user_add.html"
    )


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
            user=None,
            action="UPDATE",
            entity_type="User",
            entity_id=user.id,
            description=(
                f"{user.username} "
                f"{'activated' if user.is_active else 'deactivated'}"
            ),
        )

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
                user=None,
                action="UPDATE",
                entity_type="User",
                entity_id=target_user.id,
                description=f"{target_user.username} updated",
            )

            return redirect(
                "user_list"
            )

    return render(
        request,
        "library/user_edit.html",
        {
            "target_user": target_user,
            "error": error,
        }
    )


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

    if request.method == "POST":

        refusal = refuse_user_change(request.user, target_user, ending=True)

        if refusal:
            messages.error(request, refusal)
            return redirect("user_list")

        if has_related_records:

            return render(
                request,
                "library/user_delete.html",
                {
                    "target_user": target_user,
                    "has_related_records": True,
                }
            )

        deleted_user_id = target_user.id
        deleted_username = target_user.username

        target_user.delete()

        cache.delete(USER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="User",
            entity_id=deleted_user_id,
            description=f"{deleted_username} deleted",
        )

        return redirect("user_list")

    return render(
        request,
        "library/user_delete.html",
        {
            "target_user": target_user,
            "has_related_records": has_related_records,
        }
    )
