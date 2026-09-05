import re

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def validate_hex_color(value):
    """Accept "#rgb" / "#rrggbb", or an empty value meaning "use the default"."""

    if not value:
        return

    if not HEX_COLOR_RE.match(value):
        raise ValidationError(
            "Enter a colour as a hex code, e.g. #0d6efd."
        )


def hex_to_rgb_triplet(value):
    """"#0d6efd" -> "13, 110, 253", the form Bootstrap's --bs-*-rgb wants.

    Several Bootstrap utilities compose alpha from the -rgb variables (e.g.
    `bg-primary bg-opacity-25`, focus-ring shadows), and CSS cannot derive
    those channels from a hex value — so they are computed here instead.
    """

    if not value or not HEX_COLOR_RE.match(value):
        return ""

    digits = value.lstrip("#")

    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)

    return ", ".join(
        str(int(digits[index:index + 2], 16))
        for index in (0, 2, 4)
    )


def readable_foreground(value):
    """"#fff" or "#000", whichever stays legible on `value`.

    Administrators pick one brand colour; expecting them to also nominate a
    matching text colour would be a poor trade. This uses WCAG relative
    luminance so a pale brand colour gets dark text instead of unreadable
    white-on-yellow.

    The threshold is deliberately well above WCAG's equal-contrast crossover
    (~0.179). At that crossover even Bootstrap's own blue (#0d6efd,
    luminance 0.18) tips to black text, which would both look wrong and
    disagree with the white default in style.css. The failure mode worth
    guarding against is a genuinely pale colour, so only those flip.
    """

    triplet = hex_to_rgb_triplet(value)

    if not triplet:
        return "#fff"

    red, green, blue = (int(part) for part in triplet.split(", "))

    def channel(raw):
        proportion = raw / 255

        if proportion <= 0.03928:
            return proportion / 12.92

        return ((proportion + 0.055) / 1.055) ** 2.4

    luminance = (
        0.2126 * channel(red)
        + 0.7152 * channel(green)
        + 0.0722 * channel(blue)
    )

    return "#000" if luminance > 0.45 else "#fff"


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

    # When this book left the active catalogue, or NULL while it is still
    # in it. Added in migration 0004.
    #
    # A timestamp rather than a boolean, because "when" is the question
    # anyone asking about an archived book actually has, and NULL/not-NULL
    # answers "whether" just as well. Archiving is never destructive:
    # volumes, copies, loans and every other record stay exactly as they
    # were, and restoring is only clearing this one value.
    archived_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        managed = False
        db_table = "books"

    def __str__(self):
        return self.title

    @property
    def is_archived(self):
        """Whether this book has been taken out of the active catalogue."""

        return self.archived_at is not None
    
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

    # Appearance preference. Nullable with no default so the column could be
    # added to the existing `users` table without rewriting a single row:
    # NULL means "follow the system", same as an explicit "system".
    THEME_SYSTEM = "system"
    THEME_LIGHT = "light"
    THEME_DARK = "dark"

    THEME_CHOICES = [
        (THEME_LIGHT, "Light"),
        (THEME_DARK, "Dark"),
        (THEME_SYSTEM, "System"),
    ]

    theme_preference = models.CharField(
        max_length=10,
        choices=THEME_CHOICES,
        null=True,
        blank=True,
    )

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["full_name", "role"]

    objects = UserManager()

    class Meta:
        managed = False
        db_table = "users"

    def __str__(self):
        return self.username

    @property
    def theme(self):
        """The user's appearance choice, treating NULL/unknown as "system"."""

        valid = {choice for choice, _ in self.THEME_CHOICES}

        if self.theme_preference in valid:
            return self.theme_preference

        return self.THEME_SYSTEM
    
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


class OrganizationSettings(models.Model):
    """Branding for the one organisation this installation serves.

    A single-row table rather than a key-value store, so colours, logo and
    favicon get real field types and real validation. Every field is
    optional: `load()` returns an unsaved instance carrying the defaults
    below when nothing has been configured yet, so the whole application
    (including the login page) renders correctly on a fresh install.
    """

    # Every row is forced to this id, which is what makes the table a
    # singleton — see save() and load().
    SINGLETON_ID = 1

    DEFAULT_NAME = "Madrasah Library"
    DEFAULT_PRIMARY_COLOR = "#0d6efd"
    DEFAULT_SECONDARY_COLOR = "#6c757d"
    DEFAULT_ACCENT_COLOR = "#198754"

    id = models.AutoField(primary_key=True)

    name = models.CharField(
        max_length=255,
        blank=True,
        default="",
    )

    logo = models.ImageField(
        upload_to="branding/",
        max_length=255,
        null=True,
        blank=True,
    )

    favicon = models.ImageField(
        upload_to="branding/",
        max_length=255,
        null=True,
        blank=True,
    )

    primary_color = models.CharField(
        max_length=7,
        blank=True,
        default="",
        validators=[validate_hex_color],
    )

    secondary_color = models.CharField(
        max_length=7,
        blank=True,
        default="",
        validators=[validate_hex_color],
    )

    accent_color = models.CharField(
        max_length=7,
        blank=True,
        default="",
        validators=[validate_hex_color],
    )

    contact_email = models.EmailField(
        max_length=255,
        blank=True,
        default="",
    )

    contact_phone = models.CharField(
        max_length=50,
        blank=True,
        default="",
    )

    footer_text = models.TextField(
        blank=True,
        default="",
    )

    # --- Borrowing policy ---
    #
    # The lending rules, on the row this installation already configures
    # itself through. All four are nullable and NULL means "nobody has
    # said", which reads as the default in library/policy.py rather than as
    # zero - so an install that predates these columns lends exactly as it
    # did. That module is the only thing that should read them; everything
    # else asks it.

    loan_period_days = models.IntegerField(
        null=True,
        blank=True,
    )

    max_active_loans = models.IntegerField(
        null=True,
        blank=True,
    )

    max_renewals = models.IntegerField(
        null=True,
        blank=True,
    )

    block_when_overdue = models.BooleanField(
        null=True,
        blank=True,
    )

    updated_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        managed = False
        db_table = "organization_settings"

    def __str__(self):
        return self.display_name

    def save(self, *args, **kwargs):
        # Pin the primary key so a second row can never be created, whatever
        # the caller does.
        self.id = self.SINGLETON_ID
        self.updated_at = timezone.now()

        return super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """The settings row, or an unsaved instance holding the defaults.

        Never returns None, so templates and the context processor don't
        need to guard against an unconfigured install.
        """

        existing = cls.objects.filter(id=cls.SINGLETON_ID).first()

        if existing is not None:
            return existing

        return cls()

    # The `display_*` properties are what templates should use: they fall
    # back to the defaults so a half-filled row still renders sensibly.

    @property
    def display_name(self):
        return self.name or self.DEFAULT_NAME

    @property
    def display_primary_color(self):
        return self.primary_color or self.DEFAULT_PRIMARY_COLOR

    @property
    def display_secondary_color(self):
        return self.secondary_color or self.DEFAULT_SECONDARY_COLOR

    @property
    def display_accent_color(self):
        return self.accent_color or self.DEFAULT_ACCENT_COLOR

    @property
    def display_primary_rgb(self):
        return hex_to_rgb_triplet(self.display_primary_color)

    @property
    def display_on_primary(self):
        """Text colour that stays readable on the primary colour."""

        return readable_foreground(self.display_primary_color)

    @property
    def has_custom_colors(self):
        return bool(
            self.primary_color
            or self.secondary_color
            or self.accent_color
        )



class InventorySession(models.Model):
    """One physical stock check: a scope, a period, and who did it.

    Counting the shelves is a job with a beginning and an end, and it is
    normal for it to span days - so it is a record rather than a screen
    state. What it holds is deliberately thin: the scope it covers, when it
    started, and when it was declared finished. Everything else about it -
    how much was expected, how much was found, what is missing - is derived
    from `book_copies` and from the scans, so a session can never disagree
    with the shelves it was counting.

    Nothing here changes a copy. A stock check reports; the librarian
    decides. Marking something Missing stays the existing copy-management
    action, which is why this table has no status column for copies in it.
    """

    SCOPE_LIBRARY = "library"
    SCOPE_LOCATION = "location"
    SCOPE_SHELF = "shelf"

    # Mirrors the `check_inventory_scope` CHECK constraint in Postgres.
    SCOPE_CHOICES = [
        (SCOPE_LIBRARY, "Entire library"),
        (SCOPE_LOCATION, "One location"),
        (SCOPE_SHELF, "One shelf"),
    ]

    STATUS_IN_PROGRESS = "In Progress"
    STATUS_COMPLETED = "Completed"

    # Mirrors `check_inventory_status`.
    STATUS_CHOICES = [
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_COMPLETED, "Completed"),
    ]

    id = models.AutoField(primary_key=True)

    name = models.CharField(max_length=255)

    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES)

    # Exactly one of these is set, and only for the matching scope - the
    # database enforces that rather than trusting the form.
    location = models.ForeignKey(
        Location,
        on_delete=models.DO_NOTHING,
        db_column="location_id",
        null=True,
        blank=True,
    )

    shelf = models.ForeignKey(
        Shelf,
        on_delete=models.DO_NOTHING,
        db_column="shelf_id",
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_IN_PROGRESS,
    )

    started_by = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        db_column="started_by",
        null=True,
        blank=True,
        related_name="inventory_sessions",
    )

    started_at = models.DateTimeField()

    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "inventory_sessions"

    def __str__(self):
        return self.name

    @property
    def is_open(self):
        return self.status == self.STATUS_IN_PROGRESS

    @property
    def scope_label(self):
        """What this session covers, in words."""

        if self.scope == self.SCOPE_SHELF:
            return "Shelf %s" % self.shelf.shelf_code

        if self.scope == self.SCOPE_LOCATION:
            return self.location.name

        return "Entire library"


class InventoryScan(models.Model):
    """One code read during a stock check, and what it turned out to be.

    Every read is kept, not just the successful ones: a duplicate says the
    same book was picked up twice, and an outside-scope read says a copy is
    somewhere it does not belong. Both are findings, and neither is
    recoverable from a table of successes.

    `copy_code` is stored alongside the copy it resolved to, so a read of a
    code that matches nothing is still on the record - there is no row in
    `book_copies` to point at, and "we scanned something unrecognisable"
    is worth knowing.

    A copy can be Found at most once per session. That is a partial unique
    index in the database, not a check in Python, so two people scanning
    the same shelf at the same moment cannot both count it.
    """

    OUTCOME_FOUND = "found"
    OUTCOME_DUPLICATE = "duplicate"
    OUTCOME_OUTSIDE = "outside"
    OUTCOME_UNKNOWN = "unknown"

    # Mirrors `check_inventory_scan_outcome`.
    OUTCOME_CHOICES = [
        (OUTCOME_FOUND, "Found"),
        (OUTCOME_DUPLICATE, "Already scanned"),
        (OUTCOME_OUTSIDE, "Outside this session"),
        (OUTCOME_UNKNOWN, "Unknown code"),
    ]

    id = models.AutoField(primary_key=True)

    session = models.ForeignKey(
        InventorySession,
        on_delete=models.CASCADE,
        db_column="session_id",
        related_name="scans",
    )

    copy = models.ForeignKey(
        BookCopy,
        on_delete=models.DO_NOTHING,
        db_column="copy_id",
        null=True,
        blank=True,
        related_name="inventory_scans",
    )

    copy_code = models.CharField(max_length=50)

    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES)

    scanned_by = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        db_column="scanned_by",
        null=True,
        blank=True,
        related_name="inventory_scans",
    )

    scanned_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "inventory_scans"

    def __str__(self):
        return "%s (%s)" % (self.copy_code, self.outcome)
