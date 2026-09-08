from django.test import TestCase
from django.utils import timezone

from library.models import Book

from .helpers import make_book, make_user


class DashboardCatalogueCountTests(TestCase):
    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_total_books_matches_active_catalogue(self):
        active = make_book(title="Active Book")
        archived = make_book(title="Archived Book")
        archived.archived_at = timezone.now()
        archived.save(update_fields=["archived_at"])

        response = self.client.get("/library/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_books"], 1)
        self.assertTrue(
            Book.objects.filter(
                id=active.id,
                archived_at__isnull=True,
            ).exists()
        )
        self.assertTrue(
            Book.objects.filter(
                id=archived.id,
                archived_at__isnull=False,
            ).exists()
        )
