from django.urls import path

from . import views


urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.profile_view, name="profile"),
    path("", views.library_home, name="library_home"),
    path("categories/", views.category_list, name="category_list"),
    path("categories/add/", views.category_add, name="category_add"),
    path(
    "categories/<int:category_id>/",
    views.category_detail,
    name="category_detail",
),
    path(
    "categories/<int:category_id>/edit/",
    views.category_edit,
    name="category_edit",
),
    path(
    "categories/<int:category_id>/delete/",
    views.category_delete,
    name="category_delete",
),
    path("authors/", views.author_list, name="author_list"),

    path(
    "authors/add/",
    views.author_add,
    name="author_add",
),

    path(
    "authors/<int:author_id>/",
    views.author_detail,
    name="author_detail",
),

    path(
    "authors/<int:author_id>/edit/",
    views.author_edit,
    name="author_edit",
),

    path(
    "authors/<int:author_id>/delete/",
    views.author_delete,
    name="author_delete",
),
    path(
    "publishers/",
    views.publisher_list,
    name="publisher_list",
),

    path(
    "publishers/add/",
    views.publisher_add,
    name="publisher_add",
),

    path(
    "publishers/<int:publisher_id>/",
    views.publisher_detail,
    name="publisher_detail",
),

    path(
    "publishers/<int:publisher_id>/edit/",
    views.publisher_edit,
    name="publisher_edit",
),

    path(
    "publishers/<int:publisher_id>/delete/",
    views.publisher_delete,
    name="publisher_delete",
),
    path(
    "locations/",
    views.location_list,
    name="location_list",
),

path(
    "locations/<int:location_id>/",
    views.location_detail,
    name="location_detail"
),

path(
    "locations/add/",
    views.location_add,
    name="location_add",
),

path(
    "locations/<int:location_id>/edit/",
    views.location_edit,
    name="location_edit",
),

    path(
    "locations/<int:location_id>/delete/",
    views.location_delete,
    name="location_delete",
),
    path(
    "shelves/",
    views.shelf_list,
    name="shelf_list",
),

path(
    "shelves/<int:shelf_id>/",
    views.shelf_detail,
    name="shelf_detail"
),

    path(
    "shelves/add/",
    views.shelf_add,
    name="shelf_add",
),

    path(
    "shelves/<int:shelf_id>/edit/",
    views.shelf_edit,
    name="shelf_edit",
),

    path(
    "shelves/<int:shelf_id>/delete/",
    views.shelf_delete,
    name="shelf_delete",
),


path(
    "books/",
    views.book_list,
    name="book_list",
),

path(
    "books/add/",
    views.book_add,
    name="book_add",
),

path(
    "books/<int:book_id>/",
    views.book_detail,
    name="book_detail",
),

path(
    "books/<int:book_id>/edit/",
    views.book_edit,
    name="book_edit",
),

path(
    "books/<int:book_id>/delete/",
    views.book_delete,
    name="book_delete",
),

path(
    "book-volumes/",
    views.book_volume_list,
    name="book_volume_list",
),

path(
    "book-volumes/add/",
    views.book_volume_add,
    name="book_volume_add",
),

path(
    "book-volumes/<int:volume_id>/",
    views.book_volume_detail,
    name="book_volume_detail",
),

path(
    "book-volumes/<int:volume_id>/edit/",
    views.book_volume_edit,
    name="book_volume_edit",
),

path(
    "book-volumes/<int:volume_id>/delete/",
    views.book_volume_delete,
    name="book_volume_delete",
),

path(
    "book-contents/",
    views.book_content_list,
    name="book_content_list",
),

path(
    "book-contents/add/",
    views.book_content_add,
    name="book_content_add",
),

path(
    "book-contents/<int:content_id>/",
    views.book_content_detail,
    name="book_content_detail",
),

path(
    "book-contents/<int:content_id>/edit/",
    views.book_content_edit,
    name="book_content_edit",
),

path(
    "book-contents/<int:content_id>/delete/",
    views.book_content_delete,
    name="book_content_delete",
),

path(
    "book-copies/",
    views.book_copy_list,
    name="book_copy_list",
),

path(
    "book-copies/<int:copy_id>/",
    views.book_copy_detail,
    name="book_copy_detail"
),

path(
    "book-copies/add/",
    views.book_copy_add,
    name="book_copy_add",
),

path(
    "book-copies/<int:copy_id>/edit/",
    views.book_copy_edit,
    name="book_copy_edit",
),

path(
    "book-copies/<int:copy_id>/move/",
    views.book_copy_move,
    name="book_copy_move",
),

path(
    "book-copies/<int:copy_id>/delete/",
    views.book_copy_delete,
    name="book_copy_delete",
),

path(
    "loans/",
    views.loan_list,
    name="loan_list",
),

path(
    "loans/add/",
    views.loan_add,
    name="loan_add",
),

path(
    "loans/<int:loan_id>/edit/",
    views.loan_edit,
    name="loan_edit",
),

path(
    "loans/<int:loan_id>/return/",
    views.loan_return,
    name="loan_return"
),

path(
    "loans/<int:loan_id>/delete/",
    views.loan_delete,
    name="loan_delete",
),

path(
    "loans/<int:loan_id>/",
    views.loan_detail,
    name="loan_detail"
),

path(
    "borrowers/",
    views.borrower_list,
    name="borrower_list",
),

path(
    "borrowers/add/",
    views.borrower_add,
    name="borrower_add",
),

path(
    "borrowers/<int:borrower_id>/",
    views.borrower_detail,
    name="borrower_detail",
),

path(
    "borrowers/<int:borrower_id>/edit/",
    views.borrower_edit,
    name="borrower_edit",
),

path(
    "borrowers/<int:borrower_id>/toggle-active/",
    views.borrower_toggle_active,
    name="borrower_toggle_active",
),

path(
    "borrowers/<int:borrower_id>/delete/",
    views.borrower_delete,
    name="borrower_delete",
),

path(
    "users/",
    views.user_list,
    name="user_list",
),

path(
    "users/add/",
    views.user_add,
    name="user_add",
),

path(
    "users/<int:user_id>/edit/",
    views.user_edit,
    name="user_edit",
),

path(
    "users/<int:user_id>/toggle-active/",
    views.user_toggle_active,
    name="user_toggle_active",
),

path(
    "users/<int:user_id>/delete/",
    views.user_delete,
    name="user_delete",
),

path(
    "activity-logs/",
    views.activity_log_list,
    name="activity_log_list",
),

path(
    "dashboard/",
    views.dashboard,
    name="dashboard",
),

]
