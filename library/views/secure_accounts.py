"""Security-sensitive account creation helpers.

Account creation must always pass a plaintext password through Django's
password hasher.  This small module keeps that invariant explicit and is
re-exported by library.views for the staff URL layer.
"""

from django.contrib import messages
from django.db import IntegrityError
from django.shortcuts import render, redirect
from django.utils import timezone

from ..models import User
from ..permissions import feature_required
from .common import DASHBOARD_CACHE_KEY, USER_CACHE_KEY, USER_ROLES, create_activity_log
from django.core.cache import cache


@feature_required("users", "Admin")
def user_add(request):
    """Create a staff account with a properly hashed password.

    The old implementation accepted a field named ``password_hash`` from
    the browser and wrote it directly to the password column. That bypassed
    Django's password hasher and could make a newly-created account
    unusable with normal authentication (or store a caller-controlled hash).
    This view accepts a normal plaintext password field and delegates hashing
    to ``User.set_password`` through ``User.objects.create_user``.
    """

    error = None
    form_data = {
        "username": "",
        "full_name": "",
        "role": "Assistant",
    }

    if request.method == "POST":
        form_data["username"] = request.POST.get("username", "").strip()
        form_data["full_name"] = request.POST.get("full_name", "").strip()
        password = request.POST.get("password", "")
        confirm_password = request.POST.get("confirm_password", "")
        form_data["role"] = request.POST.get("role", "").strip()

        if not form_data["username"] or not form_data["full_name"] or not password:
            error = "Please fill in all required fields."
        elif form_data["role"] not in USER_ROLES:
            error = "Please select a valid role."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm_password:
            error = "Password and confirmation do not match."
        elif User.objects.filter(username__iexact=form_data["username"]).exists():
            error = "A user with this username already exists."
        else:
            try:
                user = User.objects.create_user(
                    username=form_data["username"],
                    password=password,
                    full_name=form_data["full_name"],
                    role=form_data["role"],
                    is_active=True,
                    created_at=timezone.now(),
                )
            except IntegrityError:
                error = "A user with this username already exists."
            else:
                cache.delete(USER_CACHE_KEY)
                cache.delete(DASHBOARD_CACHE_KEY)

                create_activity_log(
                    user=request.user,
                    action="CREATE",
                    entity_type="User",
                    entity_id=user.id,
                    description=f"{user.username} created",
                )

                messages.success(request, f"User {user.username} created successfully.")
                return redirect("user_list")

    return render(
        request,
        "library/user_add.html",
        {
            "error": error,
            "form_data": form_data,
            "roles": User.ROLE_CHOICES,
        },
    )
