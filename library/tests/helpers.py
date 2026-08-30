from datetime import date, timedelta

from django.utils import timezone

from library.models import (
    Author,
    Category,
    Publisher,
    Book,
    BookVolume,
    BookContent,
    Location,
    Shelf,
    BookCopy,
    Borrower,
    User,
    Loan,
)


def make_author(name="Test Author"):
    return Author.objects.create(name=name)


def make_category(name="Test Category"):
    return Category.objects.create(name=name)


def make_publisher(name="Test Publisher", city="Test City"):
    return Publisher.objects.create(name=name, city=city)


def make_book(title="Test Book", author=None, category=None, publisher=None):
    return Book.objects.create(
        title=title,
        author=author or make_author(),
        category=category,
        publisher=publisher,
    )


def make_volume(book=None, volume_number=1, title="Volume One"):
    return BookVolume.objects.create(
        book=book or make_book(),
        volume_number=volume_number,
        title=title,
    )


def make_content(volume=None, parent=None, title="Chapter One",
                  content_type="Chapter", page_number=1, sort_order=1):
    return BookContent.objects.create(
        volume=volume or make_volume(),
        parent=parent,
        title=title,
        content_type=content_type,
        page_number=page_number,
        sort_order=sort_order,
    )


def make_location(name="Test Location", description=""):
    return Location.objects.create(name=name, description=description)


def make_shelf(location=None, shelf_code="A1", description=""):
    return Shelf.objects.create(
        location=location or make_location(),
        shelf_code=shelf_code,
        description=description,
    )


def make_copy(volume=None, shelf=None, copy_code="COPY-0001", status="Available"):
    return BookCopy.objects.create(
        volume=volume or make_volume(),
        shelf=shelf,
        copy_code=copy_code,
        status=status,
    )


def make_borrower(name="Test Borrower", phone="1234567890",
                   borrower_type="Student", is_active=True):
    return Borrower.objects.create(
        name=name,
        phone=phone,
        borrower_type=borrower_type,
        is_active=is_active,
        created_at=timezone.now(),
    )


def make_user(username="testuser", password="TestPass123", role="Admin", is_active=True):
    return User.objects.create_user(
        username=username,
        password=password,
        full_name=username.title(),
        role=role,
        is_active=is_active,
        created_at=timezone.now(),
    )


def make_loan(copy=None, borrower=None, issue_date=None, due_date=None,
              issued_by=None, return_date=None):
    issue_date = issue_date or date.today()
    due_date = due_date or (issue_date + timedelta(days=14))
    copy = copy or make_copy()

    loan = Loan.objects.create(
        copy=copy,
        borrower=borrower or make_borrower(),
        issue_date=issue_date,
        due_date=due_date,
        issued_by=issued_by,
        return_date=return_date,
    )

    # Mirror what the real issue/return views do, so test data stays
    # internally consistent with the rest of the app's assumptions.
    copy.status = "Available" if return_date else "Issued"
    copy.save()

    return loan
