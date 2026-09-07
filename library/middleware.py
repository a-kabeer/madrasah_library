"""A signed-in person's stored language wins.

`LocaleMiddleware` decides the language from the session, then a cookie,
then the browser's `Accept-Language` header. That is right for a visitor to
the public catalogue, and wrong for a librarian: the language they chose in
this application is a property of their account, not of the browser they
happen to be sitting at. Somebody who set Urdu should get Urdu on the desk
machine, on their phone, and on a colleague's browser.

So this runs immediately after `LocaleMiddleware` and, for a signed-in user
with a stored preference, activates it instead - and writes it into the
session, so the templates' `{% get_current_language %}` and Django's own
`set_language` view agree with it afterwards.

Anonymous requests are left entirely alone: the login page and the public
catalogue keep Django's ordinary session/cookie/header behaviour, which is
what a visitor with no account should get.
"""

from django.conf import settings
from django.utils import translation


class UserLanguageMiddleware:
    """Activate the signed-in user's stored language, if they have one."""

    def __init__(self, get_response):
        self.get_response = get_response

        # The codes actually on offer. A stored value that is no longer
        # offered - a language removed from settings - is ignored rather
        # than activated, which would raise deep inside Django's catalogue
        # loading.
        self.offered = {code for code, _ in settings.LANGUAGES}

    def __call__(self, request):
        stored = self.stored_language(request)

        if stored:
            translation.activate(stored)
            request.LANGUAGE_CODE = stored

            # So the rest of the request - and the next one - agree with it.
            if request.session.get(
                translation.LANGUAGE_SESSION_KEY
                if hasattr(translation, "LANGUAGE_SESSION_KEY")
                else "_language"
            ) != stored:
                request.session["_language"] = stored

        return self.get_response(request)

    def stored_language(self, request):
        """The user's own choice, or None to leave the decision alone."""

        user = getattr(request, "user", None)

        if user is None or not user.is_authenticated:
            return None

        chosen = getattr(user, "language_preference", None)

        if chosen in self.offered:
            return chosen

        return None
