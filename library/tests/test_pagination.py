from django.test import TestCase
from django.urls import reverse

from library.views import PAGE_SIZE
from library.tests.helpers import make_user, make_category


class PaginationTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        for i in range(PAGE_SIZE + 5):
            make_category(name=f"Category {i:03d}")

    def test_first_page_shows_page_size_items(self):
        response = self.client.get(reverse("category_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["categories"]), PAGE_SIZE)

    def test_second_page_shows_remainder(self):
        response = self.client.get(reverse("category_list") + "?page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["categories"]), 5)

    def test_out_of_range_page_falls_back_without_crashing(self):
        response = self.client.get(reverse("category_list") + "?page=9999")
        self.assertEqual(response.status_code, 200)

    def test_non_integer_page_falls_back_without_crashing(self):
        response = self.client.get(reverse("category_list") + "?page=abc")
        self.assertEqual(response.status_code, 200)

    def test_total_count_reported_correctly_regardless_of_page_size(self):
        response = self.client.get(reverse("category_list"))
        self.assertEqual(
            response.context["categories"].paginator.count,
            PAGE_SIZE + 5,
        )
