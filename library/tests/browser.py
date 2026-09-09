"""Driving the real app in a real browser.

Most of this project is testable with the Django client, and most of it is
tested that way. What the client cannot see is the half of the app that only
exists once JavaScript runs: `hx-boost` turning every sidebar link into a
swap of `#mainContent`, the shared dialog that every add/edit/delete goes
through, the clickable rows, the mobile sidebar, and whether any of it
throws. A view can answer 200 with a body that is broken on screen.

So these tests open Chromium and use the app.

Skipped, not failed, where Playwright or the browser is missing: a
contributor without them still gets a green suite, and the checks run
wherever they are installed.
"""

import importlib.util
import os
import unittest

# Playwright's sync API runs an asyncio loop in this thread, which trips
# Django's SynchronousOnlyOperation guard on the ORM calls beside it. The
# guard is there to stop blocking an event loop that is serving requests;
# here the loop only drives the browser, and the test is the only thing
# running.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connection
from django.test import override_settings
from django.urls import reverse

CHROMIUM = "/opt/pw-browsers/chromium"

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# `{% static %}` has to give a name the live server can serve. The
# production manifest storage returns a content-hashed name that only
# exists under STATIC_ROOT, which the test server does not serve from.
PLAIN_STATIC = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

PASSWORD = "pass12345"


def browser_available():
    """Whether there is a Playwright and a Chromium to drive."""

    return (
        importlib.util.find_spec("playwright") is not None
        and os.path.exists(CHROMIUM)
    )


@unittest.skipUnless(browser_available(),
                     "Playwright or Chromium is not installed")
@override_settings(CACHES=LOCMEM, DEBUG=False, STORAGES=PLAIN_STATIC)
class BrowserTestCase(StaticLiveServerTestCase):
    """A live server, a browser, and a way to clean up after both."""

    viewport = {"width": 1440, "height": 900}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        from playwright.sync_api import sync_playwright

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=CHROMIUM,
            # No user namespaces in the container this runs in.
            args=["--no-sandbox"],
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

        super().tearDownClass()

    def _fixture_teardown(self):
        """Empty the tables Django's own flush will not touch.

        A live-server test is a `TransactionTestCase`: it commits, and
        cleans up afterwards by flushing. But Django builds its TRUNCATE
        list from `django_table_names()`, which lists only *managed*
        models - and every model in this project is `managed = False`,
        because the PostgreSQL schema is the source of truth. So the
        standard flush truncates nothing here, and rows would survive into
        the next test and into the next run of the suite, which reuses the
        database with --keepdb.

        Reading the table list back from the database rather than from the
        models keeps this correct when a table is added. `django_migrations`
        and `django_cache` are Django's own bookkeeping and must survive.

        Only ever reached against the test database: Django refuses to run
        a test against anything else.
        """

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public'
                AND tablename NOT IN ('django_migrations', 'django_cache')
                """
            )
            tables = [row[0] for row in cursor.fetchall()]

        if not tables:
            return

        with connection.cursor() as cursor:
            cursor.execute(
                "TRUNCATE %s RESTART IDENTITY CASCADE"
                % ", ".join('"%s"' % table for table in tables)
            )

    # --- helpers ---

    def watched_page(self, **context):
        """A page that records anything the browser complains about.

        Returns `(page, problems)`. `problems` is a list that fills up as
        the page is used - a console error or warning, an uncaught
        exception, a request that failed, or any response of 400 or worse.
        A test asserts it is still empty.
        """

        options = {"viewport": self.viewport}
        options.update(context)

        ctx = self.browser.new_context(**options)
        page = ctx.new_page()
        problems = []

        page.on("console", lambda message: (
            problems.append("console %s: %s" % (message.type,
                                                message.text[:160]))
            if message.type in ("error", "warning") else None
        ))
        page.on("pageerror",
                lambda error: problems.append("uncaught: %s" % str(error)[:200]))
        page.on("requestfailed", lambda request: problems.append(
            "request failed: %s (%s)" % (request.url, request.failure)
        ))
        page.on("response", lambda response: (
            problems.append("HTTP %d: %s" % (response.status, response.url))
            if response.status >= 400 else None
        ))

        return page, problems

    def sign_in(self, page, username):
        page.goto(self.live_server_url + reverse("login"))
        page.fill("input[name=username]", username)
        page.fill("input[name=password]", PASSWORD)
        # The sign-in page also carries the language switcher, whose entries
        # are submit buttons too, so pressing Enter in the field is less
        # brittle than picking one of several submits.
        page.press("input[name=password]", "Enter")
        page.wait_for_load_state("networkidle")

    def at(self, page, name, *args, **query):
        url = reverse(name, args=args)

        if query:
            url += "?" + "&".join("%s=%s" % pair for pair in query.items())

        page.goto(self.live_server_url + url)
        page.wait_for_load_state("networkidle")

        return url
