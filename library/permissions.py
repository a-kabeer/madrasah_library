from functools import wraps

from django.core.exceptions import PermissionDenied


def role_required(*allowed_roles):
    """Restrict a view to users whose `role` is one of `allowed_roles`.

    Must run after the auth middleware (request.user is always set by then,
    since LoginRequiredMiddleware already blocks anonymous access globally).
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if request.user.role not in allowed_roles:
                raise PermissionDenied(
                    "Your role does not have permission to perform this action."
                )
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator
