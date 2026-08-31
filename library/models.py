from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone

# Create your models here.
class Author(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255, unique=True)

    class Meta:
        managed = False
        db_table = "authors"

    def __str__(self):
        return self.name
    
class Category(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        managed = False
        db_table = "categories"

    def __str__(self):
        return self.name
    

class Publisher(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255, unique=True)
    city = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        managed = False
        db_table = "publishers"

    def __str__(self):
        return self.name
    
class Book(models.Model):
    id = models.AutoField(primary_key=True)
    title = models.CharField(max_length=500)

    author = models.ForeignKey(
        Author,
        on_delete=models.DO_NOTHING,
        db_column="author_id"
    )

    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="category_id"
    )

    publisher = models.ForeignKey(
        Publisher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="publisher_id"
    )

    # Optional: books added before this field existed have no cover, and
    # templates must keep working without one. max_length matches the
    # varchar(255) column added in migration 0002.
    cover_image = models.ImageField(
        upload_to="book_covers/",
        max_length=255,
        null=True,
        blank=True,
    )

    class Meta:
        managed = False
        db_table = "books"

    def __str__(self):
        return self.title
    
class BookVolume(models.Model):
    id = models.AutoField(primary_key=True)

    book = models.ForeignKey(
        Book,
        on_delete=models.CASCADE,
        db_column="book_id"
    )

    volume_number = models.IntegerField()

    title = models.CharField(max_length=255)

    class Meta:
        managed = False
        db_table = "book_volumes"

        constraints = [
            models.UniqueConstraint(
                fields=["book", "volume_number"],
                name="unique_book_volume"
            )
        ]

    def __str__(self):
        if self.title:
            return f"{self.book.title} - جلد {self.volume_number} ({self.title})"
        return f"{self.book.title} - جلد {self.volume_number}"
    
class BookContent(models.Model):
    id = models.AutoField(primary_key=True)

    volume = models.ForeignKey(
        BookVolume,
        on_delete=models.CASCADE,
        db_column="volume_id"
    )

    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_column="parent_id",
        related_name="children"
    )

    title = models.CharField(max_length=500)

    content_type = models.CharField(max_length=50)

    page_number = models.IntegerField(
        null=True,
        blank=True
    )

    sort_order = models.IntegerField()

    class Meta:
        managed = False
        db_table = "book_contents"

    def __str__(self):
        return self.title
    
class Location(models.Model):
    id = models.AutoField(primary_key=True)

    name = models.CharField(
        max_length=255,
        unique=True
    )

    description = models.TextField(
        null=True,
        blank=True
    )

    class Meta:
        managed = False
        db_table = "locations"

    def __str__(self):
        return self.name
    
class Shelf(models.Model):
    id = models.AutoField(primary_key=True)

    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        db_column="location_id"
    )

    shelf_code = models.CharField(max_length=50)

    description = models.TextField(
        null=True,
        blank=True
    )

    class Meta:
        managed = False
        db_table = "shelves"

        constraints = [
            models.UniqueConstraint(
                fields=["location", "shelf_code"],
                name="unique_location_shelf"
            )
        ]

    def __str__(self):
        return f"{self.location.name} - {self.shelf_code}"
    
class BookCopy(models.Model):
    id = models.AutoField(primary_key=True)

    volume = models.ForeignKey(
        BookVolume,
        on_delete=models.DO_NOTHING,
        db_column="volume_id"
    )

    shelf = models.ForeignKey(
        Shelf,
        on_delete=models.DO_NOTHING,
        db_column="shelf_id",
        null=True,
        blank=True
    )

    copy_code = models.CharField(
        max_length=50,
        unique=True
    )

    STATUS_AVAILABLE = "Available"
    STATUS_ISSUED = "Issued"
    STATUS_LOST = "Lost"
    STATUS_DAMAGED = "Damaged"
    STATUS_MISSING = "Missing"
    STATUS_TRANSFERRED = "Transferred"

    # Mirrors the `check_copy_status` CHECK constraint on book_copies in Postgres.
    STATUS_CHOICES = [
        (STATUS_AVAILABLE, "Available"),
        (STATUS_ISSUED, "Issued"),
        (STATUS_LOST, "Lost"),
        (STATUS_DAMAGED, "Damaged"),
        (STATUS_MISSING, "Missing"),
        (STATUS_TRANSFERRED, "Transferred"),
    ]

    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
    )

    acquisition_date = models.DateField(
        null=True,
        blank=True
    )

    notes = models.TextField(
        null=True,
        blank=True
    )

    class Meta:
        managed = False
        db_table = "book_copies"

    def __str__(self):
        return self.copy_code

class Borrower(models.Model):
    id = models.AutoField(primary_key=True)

    name = models.CharField(max_length=255)

    phone = models.CharField(max_length=30)

    # Mirrors the `check_borrower_type` CHECK constraint on borrowers in Postgres.
    BORROWER_TYPE_CHOICES = [
        ("Student", "Student"),
        ("Teacher", "Teacher"),
        ("Staff", "Staff"),
        ("Other", "Other"),
    ]

    borrower_type = models.CharField(
        max_length=30,
        choices=BORROWER_TYPE_CHOICES,
    )

    registration_no = models.CharField(
        max_length=100,
        null=True,
        blank=True
    )

    department = models.CharField(
        max_length=255,
        null=True,
        blank=True
    )

    address = models.TextField(
        null=True,
        blank=True
    )

    notes = models.TextField(
        null=True,
        blank=True
    )

    is_active = models.BooleanField()

    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "borrowers"

    def __str__(self):
        return f"{self.name} - {self.phone}"
    
class UserManager(BaseUserManager):
    def create_user(self, username, password=None, **extra_fields):
        if not username:
            raise ValueError("Username is required")

        extra_fields.setdefault("role", "Assistant")
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("created_at", timezone.now())
        extra_fields.setdefault("full_name", username)

        user = self.model(username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, password=None, **extra_fields):
        extra_fields["role"] = "Admin"
        return self.create_user(username, password, **extra_fields)


class User(AbstractBaseUser):
    id = models.AutoField(primary_key=True)

    username = models.CharField(
        max_length=100,
        unique=True
    )

    full_name = models.CharField(
        max_length=255
    )

    # Django's AbstractBaseUser expects an attribute literally named
    # `password` (used by set_password/check_password/login()); the real
    # column is `password_hash`, so map it via db_column.
    password = models.CharField(
        max_length=128,
        db_column="password_hash",
    )

    # Mirrors the `check_user_role` CHECK constraint on users in Postgres.
    ROLE_CHOICES = [
        ("Admin", "Admin"),
        ("Librarian", "Librarian"),
        ("Assistant", "Assistant"),
    ]

    role = models.CharField(
        max_length=30,
        choices=ROLE_CHOICES,
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["full_name", "role"]

    objects = UserManager()

    class Meta:
        managed = False
        db_table = "users"

    def __str__(self):
        return self.username
    
class Loan(models.Model):
    id = models.AutoField(primary_key=True)

    copy = models.ForeignKey(
        BookCopy,
        on_delete=models.DO_NOTHING,
        db_column="copy_id"
    )

    borrower = models.ForeignKey(
        Borrower,
        on_delete=models.DO_NOTHING,
        db_column="borrower_id"
    )

    issue_date = models.DateField()

    due_date = models.DateField()

    return_date = models.DateField(
        null=True,
        blank=True
    )

    issued_by = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        db_column="issued_by",
        null=True,
        blank=True,
        related_name="loans_issued"
    )

    returned_to = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        db_column="returned_to",
        null=True,
        blank=True,
        related_name="loans_returned"
    )

    notes = models.TextField(
        null=True,
        blank=True
    )

    class Meta:
        managed = False
        db_table = "loans"

    def __str__(self):
        return f"{self.copy.copy_code} - {self.borrower.name}"
    
class ActivityLog(models.Model):
    id = models.AutoField(primary_key=True)

    user = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        db_column="user_id",
        null=True,
        blank=True
    )

    action = models.CharField(
        max_length=100
    )

    entity_type = models.CharField(
        max_length=100,
        null=True,
        blank=True
    )

    entity_id = models.IntegerField(
        null=True,
        blank=True
    )

    description = models.TextField(
        null=True,
        blank=True
    )

    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "activity_logs"

    def __str__(self):
        return f"{self.action} - {self.created_at}"
    
