from django.test import RequestFactory, TestCase
from django.utils import timezone

from library import features
from library.models import RoleFeature


class RoleFeatureFreshnessTests(TestCase):
    def test_a_new_request_reads_updated_permissions(self):
        factory = RequestFactory()

        first_request = factory.get("/")
        self.assertTrue(features.role_has(
            "Librarian", "authors", features.overrides_for(first_request)
        ))

        RoleFeature.objects.update_or_create(
            role="Librarian",
            feature_key="authors",
            defaults={"allowed": False, "updated_at": timezone.now()},
        )

        second_request = factory.get("/")
        self.assertFalse(features.role_has(
            "Librarian", "authors", features.overrides_for(second_request)
        ))

    def test_one_request_uses_one_database_snapshot(self):
        factory = RequestFactory()
        request = factory.get("/")

        first = features.overrides_for(request)
        second = features.overrides_for(request)

        self.assertIs(first, second)
