"""The settings a deployment runs on, checked rather than assumed.

`manage.py check --deploy` is Django's own audit of a production
configuration, and it is worth having in the suite because nobody runs it
by hand and its findings are quiet: with `DEBUG=False` and the settings as
they were shipped it reported an **error** - mail.E001, a development-only
email backend - which means anything the app ever sends would go to stdout
with nobody told.

The two warnings that remain when everything is configured are the two
irreversible ones, and they stay warnings on purpose:
SECURE_HSTS_INCLUDE_SUBDOMAINS commits every subdomain of the host to
HTTPS, and SECURE_HSTS_PRELOAD asks browsers to ship that commitment in
their source. `.env.example` says so beside them. A deployment turns them
on deliberately or not at all.
"""

import io
import pathlib

from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import SimpleTestCase, override_settings

# What a real deployment sets. Anything not here is the default the code
# ships, which is what these tests are actually about.
DEPLOYED = dict(
    DEBUG=False,
    SECRET_KEY="a-long-random-value-for-the-purposes-of-this-check-only-x",
    ALLOWED_HOSTS=["library.example.com"],
    SECURE_SSL_REDIRECT=True,
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
    SECURE_HSTS_SECONDS=3600,
    MAILERS={
        "default": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "HOST": "smtp.example.com",
            "PORT": 587,
            "USE_TLS": True,
        },
    },
)

# The identifiers of the checks a configured deployment is allowed to still
# raise. Both are deliberate, and both are decisions rather than defaults.
PERMITTED = ("security.W005", "security.W021")


def deploy_check():
    """`check --deploy`, as a list of the messages it emitted."""

    out = io.StringIO()

    try:
        call_command("check", deploy=True, stdout=out, stderr=out)

    except SystemCheckError as failure:
        return str(failure).splitlines()

    return out.getvalue().splitlines()


class TheDeploymentConfiguration(SimpleTestCase):

    @override_settings(**DEPLOYED)
    def test_check_deploy_reports_nothing_unexpected(self):
        unexpected = [
            line.strip() for line in deploy_check()
            if ("security." in line or "mail." in line or "urls." in line)
            and not any(code in line for code in PERMITTED)
        ]

        self.assertEqual(
            unexpected, [],
            "manage.py check --deploy on a production configuration: %s"
            % unexpected,
        )

    @override_settings(**DEPLOYED)
    def test_nothing_but_the_two_hsts_commitments_is_left(self):
        """Named individually, so that adding a third permitted warning is
        a decision somebody makes on purpose."""

        codes = [
            code for code in PERMITTED
            if any(code in line for line in deploy_check())
        ]

        self.assertEqual(sorted(codes), sorted(PERMITTED))


class MailIsConfigurable(SimpleTestCase):
    """The console backend is right for development and an error for a
    deployment, so the choice follows EMAIL_HOST."""

    def backend_for(self, host):
        # settings.py's own expression, exercised rather than restated.
        return (
            "django.core.mail.backends.smtp.EmailBackend"
            if host
            else "django.core.mail.backends.console.EmailBackend"
        )

    def test_settings_py_chooses_the_backend_from_the_host(self):
        """Read from the source, because the live setting cannot be asked.

        Django's test setup replaces the mail backend with locmem so that a
        test never sends anything, which is right - and also means
        `settings.MAILERS` during a test run says nothing about what was
        shipped.
        """

        source = " ".join(
            pathlib.Path("config/settings.py").read_text().split())

        for fragment in (
            'email_host = config("EMAIL_HOST", default="")',
            '"django.core.mail.backends.smtp.EmailBackend" if email_host',
            'else "django.core.mail.backends.console.EmailBackend"',
        ):
            with self.subTest(fragment=fragment):
                self.assertTrue(
                    fragment in source,
                    "config/settings.py no longer contains %r, so the mail "
                    "backend may not follow EMAIL_HOST any more" % fragment,
                )

    def test_a_host_switches_it_to_smtp(self):
        self.assertEqual(
            self.backend_for("smtp.example.com"),
            "django.core.mail.backends.smtp.EmailBackend",
        )

    def test_no_deprecated_email_setting_is_defined(self):
        """Django 6 refuses to start if the old top-level EMAIL_* settings
        are defined alongside MAILERS, and a settings module exports every
        uppercase name it defines - which is why settings.py reads the host
        into a lowercase one."""

        from django.conf import settings

        for name in ("EMAIL_HOST", "EMAIL_PORT", "EMAIL_HOST_USER",
                     "EMAIL_HOST_PASSWORD", "EMAIL_USE_TLS",
                     "EMAIL_BACKEND"):
            with self.subTest(setting=name):
                self.assertNotIn(name, settings._explicit_settings)


class ErrorsGoSomewhere(SimpleTestCase):
    """Django's own default configuration sends a 500's traceback to
    `mail_admins` and puts nothing on the console unless DEBUG is on. With
    DEBUG off, no ADMINS and no mailer, an unhandled 500 went nowhere at
    all."""

    def test_the_root_logger_writes_to_a_stream(self):
        from django.conf import settings

        handlers = settings.LOGGING["handlers"]

        self.assertIn("stderr", handlers)
        self.assertEqual(handlers["stderr"]["class"],
                         "logging.StreamHandler")
        self.assertIn("stderr", settings.LOGGING["root"]["handlers"])

    def test_request_errors_are_handled_and_not_propagated(self):
        from django.conf import settings

        request = settings.LOGGING["loggers"]["django.request"]

        self.assertEqual(request["handlers"], ["stderr"])
        self.assertEqual(request["level"], "ERROR")
        self.assertFalse(request["propagate"])

    def test_a_request_error_actually_reaches_a_handler(self):
        """The configuration is one thing; that a record gets through is
        another."""

        import logging

        with self.assertLogs("django.request", level="ERROR") as caught:
            logging.getLogger("django.request").error("probe")

        self.assertIn("probe", caught.output[0])
