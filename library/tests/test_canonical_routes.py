from django.test import TestCase
from django.urls import reverse

from .helpers import make_user


class CanonicalRouteTests(TestCase):
    """Legacy loan entry points redirect instead of duplicating views."""

    def setUp(self):
        make_user(username="route_admin", password="pass12345", role="Admin")
        self.client.login(username="route_admin", password="pass12345")

    def test_loan_add_alias_redirects_to_circulation_issue(self):
        response = self.client.get(reverse("loan_add"))

        self.assertRedirects(
            response,
            reverse("circulation_issue"),
            status_code=301,
        )

    def test_active_loans_alias_redirects_to_filtered_loan_list(self):
        response = self.client.get(reverse("circulation_active_loans"))

        self.assertRedirects(
            response,
            "%s?status=active" % reverse("loan_list"),
            status_code=301,
        )
