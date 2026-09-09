import re

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
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


# The darkest surface in the dark theme (--slate-950 in style.css). A link
# has to be legible against this one, which is the sidebar and the page
# ground both.
DARK_GROUND = (11, 17, 32)

# WCAG AA for body text is 4.5. The target is a little above it because a
# lifted colour is read on several dark surfaces, some of them a translucent
# panel over another - a card cap over a card - and a value that only just
# clears the line on the darkest ground falls under it on the lightest.
MINIMUM_CONTRAST = 5.2


def _relative_luminance(red, green, blue):
    """WCAG relative luminance for one 0-255 triplet."""

    def channel(raw):
        proportion = raw / 255

        if proportion <= 0.03928:
            return proportion / 12.92

        return ((proportion + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(red)
        + 0.7152 * channel(green)
        + 0.0722 * channel(blue)
    )


def _contrast(first, second):
    lighter = max(_relative_luminance(*first), _relative_luminance(*second))
    darker = min(_relative_luminance(*first), _relative_luminance(*second))

    return (lighter + 0.05) / (darker + 0.05)


def lifted_for_dark(value, ground=DARK_GROUND, minimum=MINIMUM_CONTRAST):
    """`value` mixed towards white until it is legible on a dark ground.

    The companion to `readable_foreground`, and the same bargain: an
    administrator picks one brand colour, against the light page they are
    looking at while they pick it. On the dark theme's near-black ground
    that same colour is often unreadable - the default indigo #4f46e5 comes
    to 2.99:1, where WCAG AA wants 4.5 - so links, and the outline buttons
    that borrow the link colour, need a lifted version of it rather than a
    different colour.

    Mixing towards white rather than choosing a new hue is what keeps it
    recognisably the same brand. Steps of 5% are fine enough that the
    result is never noticeably paler than it needs to be, and white itself
    is the guaranteed terminus, so this always returns something.

    Returns the "r, g, b" form, because that is what Bootstrap's
    --bs-link-color-rgb wants and CSS cannot compute it.
    """

    triplet = hex_to_rgb_triplet(value)

    if not triplet:
        return ""

    red, green, blue = (int(part) for part in triplet.split(", "))

    for step in range(0, 21):
        weight = step / 20
        mixed = tuple(
            round(part + (255 - part) * weight)
            for part in (red, green, blue)
        )

        if _contrast(mixed, ground) >= minimum:
            return ", ".join(str(part) for part in mixed)

    return "255, 255, 255"


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

    # DO_NOTHING, not SET_NULL: the column is NO ACTION in Postgres (checked
    # against information_schema), and `lookup_delete_blocker` refuses the
    # delete outright while any book is filed here - so SET_NULL described a
    # path nothing takes and the database does not implement.
    category = models.ForeignKey(
        Category,
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        db_column="category_id"
    )

    publisher = models.ForeignKey(
        Publisher,
        on_delete=models.DO_NOTHING,
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
        (STATUS_AVAILABLE, _("Available")),
        (STATUS_ISSUED, _("Issued")),
        (STATUS_LOST, _("Lost")),
        (STATUS_DAMAGED, _("Damaged")),
        (STATUS_MISSING, _("Missing")),
        (STATUS_TRANSFERRED, _("Transferred")),
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
        ("Student", _("Student")),
        ("Teacher", _("Teacher")),
        ("Staff", _("Staff")),
        ("Other", _("Other")),
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
    #
    # SuperAdmin is the developer's account and sits outside the library's
    # own hierarchy: it holds every feature unconditionally and is the only
    # role that can change what an Admin sees. It is never offered in the
    # Add/Edit User form - see library/views/accounts.py - so the only way
    # one exists is for somebody with database access to make it.
    ROLE_CHOICES = [
        ("SuperAdmin", _("Super Admin")),
        ("Admin", _("Admin")),
        ("Librarian", _("Librarian")),
        ("Assistant", _("Assistant")),
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
        (THEME_LIGHT, _("Light")),
        (THEME_DARK, _("Dark")),
        (THEME_SYSTEM, _("System")),
    ]

    theme_preference = models.CharField(
        max_length=10,
        choices=THEME_CHOICES,
        null=True,
        blank=True,
    )

    # Language preference, exactly the shape `theme_preference` above has
    # and for the same reason: nullable with no default, so the column
    # could be added to an existing `users` table without rewriting a row.
    # NULL means "no choice stored", and the request then falls back to the
    # browser's Accept-Language the way an anonymous visitor's does.
    #
    # The codes are the ones in settings.LANGUAGES; they are not a CHECK
    # constraint because adding a language should not need a migration, and
    # library/middleware.py ignores a code that is no longer offered.
    LANGUAGE_CHOICES = [
        ("en", "English"),
        ("ur", "اردو"),
        ("ar", "العربية"),
    ]

    language_preference = models.CharField(
        max_length=5,
        choices=LANGUAGE_CHOICES,
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

    @property
    def language(self):
        """The user's language choice, or "" when they have made none.

        Empty rather than a default, because "no choice" and "chose
        English" are different: the first should follow the browser, the
        second should not. The switcher shows this as no option selected.
        """

        valid = {choice for choice, _ in self.LANGUAGE_CHOICES}

        if self.language_preference in valid:
            return self.language_preference

        return ""
    
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

    # --- Institution identity ---
    #
    # Who this installation belongs to, for the places that have to say so:
    # the sidebar, a printed report, a sheet of labels.
    #
    # Deliberately only what was actually missing. The institution's *name*
    # is `name` above - the field the sidebar, the page titles, the login
    # page, the report headers and the label sheets have always read - and
    # its email and phone are `contact_email` and `contact_phone`. Adding
    # `institution_name`, `institution_email` and `institution_phone`
    # beside them would give the library two answers to each of three
    # questions, and the day they disagreed there would be no way to say
    # which was right. So there are three new columns and no more.
    #
    # All optional, all `blank`/`default=""` rather than nullable: an
    # install that predates them reads as "nothing said" without a single
    # row being rewritten, and every display below is written to show
    # nothing at all rather than an empty label.
    #
    # This is one institution, not a hierarchy. There is no branch, no
    # campus and no second row - `organization_settings` is pinned to one
    # by a CHECK, which is what makes it a source of truth rather than a
    # list.

    name_arabic = models.CharField(
        max_length=255,
        blank=True,
        default="",
    )

    # Free text rather than a choice list with a CHECK behind it, unlike
    # every status column in this schema. Nothing branches on this value -
    # it is printed and never tested - so an enumeration would buy no
    # correctness and would need a migration the first time an institution
    # described itself in a way the list did not anticipate. The form
    # offers the common answers as suggestions instead.
    institution_type = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    address = models.TextField(
        blank=True,
        default="",
    )

    website = models.URLField(
        max_length=255,
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
    def display_primary_rgb_on_dark(self):
        """The brand colour, lifted enough to read on the dark theme.

        `display_primary_rgb` is what Bootstrap's --bs-link-color-rgb and
        the -rgb utilities read on the light theme. On the dark one the
        same channels give a link 2.99:1 against the ground for the default
        indigo, so the dark block in style.css reads this instead.
        """

        return lifted_for_dark(self.display_primary_color)

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

    # The institution's identity, in the shapes the pages that show it
    # actually want. Properties rather than template logic, so the sidebar
    # and the two printed outputs cannot drift apart in how they read the
    # same row - and so a blank field is decided once, here, rather than in
    # three `{% if %}`s.

    @property
    def display_address(self):
        """The postal address on one line, or "".

        Stored as a textarea, so it arrives with the line breaks whoever
        typed it used. A printed footer has one line to give it, and a
        run of blank lines in the middle of a letterhead is worse than a
        comma.
        """

        parts = [
            line.strip()
            for line in self.address.splitlines()
            if line.strip()
        ]

        return ", ".join(parts)

    @property
    def print_contact_line(self):
        """Address, phone and website for a printed footer, or "".

        Only what has been filled in, in the order a letterhead reads. The
        email is deliberately absent: it is already in the on-screen page
        footer, and a sheet of labels is not something anybody replies to.
        """

        parts = [
            self.display_address,
            self.contact_phone.strip(),
            self.website.strip(),
        ]

        return " · ".join(part for part in parts if part)

    @property
    def has_institution_details(self):
        """Whether anything beyond the name has been configured.

        Lets a template skip a whole block rather than render an empty one.
        """

        return bool(
            self.name_arabic
            or self.institution_type
            or self.address
            or self.website
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
        (SCOPE_LIBRARY, _("Entire library")),
        (SCOPE_LOCATION, _("One location")),
        (SCOPE_SHELF, _("One shelf")),
    ]

    STATUS_IN_PROGRESS = "In Progress"
    STATUS_COMPLETED = "Completed"

    # Mirrors `check_inventory_status`.
    STATUS_CHOICES = [
        (STATUS_IN_PROGRESS, _("In Progress")),
        (STATUS_COMPLETED, _("Completed")),
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
        (OUTCOME_FOUND, _("Found")),
        (OUTCOME_DUPLICATE, _("Already scanned")),
        (OUTCOME_OUTSIDE, _("Outside this session")),
        (OUTCOME_UNKNOWN, _("Unknown code")),
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


class Reservation(models.Model):
    """A borrower waiting for a book, in the order they asked.

    A hold on the *book*, never on a particular copy. Which physical copy
    someone ends up with is decided when a librarian hands one over, and
    tying a reservation to a copy would mean the queue could be blocked by
    one volume sitting on a trolley while three others were on the shelf.

    Deliberately thin. There is no expiry, no priority, no notification and
    no shelf reserved: a queue, a position in it, and a record of how each
    one ended. Everything else a library might want from holds is a
    decision someone at the desk makes, and the point of this is to tell
    them who asked first.

    `unique_active_reservation` in the database is what stops one borrower
    holding two places in the same queue - not a check in Python, which two
    simultaneous requests could both pass.
    """

    STATUS_ACTIVE = "Active"
    STATUS_FULFILLED = "Fulfilled"
    STATUS_CANCELLED = "Cancelled"

    # Mirrors the `check_reservation_status` CHECK constraint in Postgres.
    STATUS_CHOICES = [
        (STATUS_ACTIVE, _("Waiting")),
        (STATUS_FULFILLED, _("Fulfilled")),
        (STATUS_CANCELLED, _("Cancelled")),
    ]

    id = models.AutoField(primary_key=True)

    borrower = models.ForeignKey(
        Borrower,
        on_delete=models.DO_NOTHING,
        db_column="borrower_id",
        related_name="reservations",
    )

    book = models.ForeignKey(
        Book,
        on_delete=models.DO_NOTHING,
        db_column="book_id",
        related_name="reservations",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
    )

    created_at = models.DateTimeField()

    # When it stopped being active, whichever way it ended. One column
    # rather than two, because a reservation ends once and the status says
    # how; two nullable dates would allow a row claiming both.
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "reservations"

    def __str__(self):
        return "%s waiting for %s" % (self.borrower.name, self.book.title)

    @property
    def is_active(self):
        return self.status == self.STATUS_ACTIVE


class Notification(models.Model):
    """One actionable message for one member of staff.

    Not an event log. `activity_logs` already records what happened and who
    did it, and every row of it is worth keeping whether or not anybody
    ever reads it. A notification is the opposite: it is addressed to
    somebody, it asks them to do something, and it stops mattering once
    they have. The two are kept apart deliberately - nothing here is
    generated by reading the audit trail, and nothing here is written back
    into it.

    Every recipient is a `User`. There is no relationship between
    `borrowers` and `users` in this schema and borrowers do not sign in, so
    a borrower cannot be a recipient - see library/notifications.py for
    what that means for events that are, on the face of it, about a
    borrower.

    The title, message and destination are written out at creation time
    rather than derived from the record that caused them. A notification is
    a historical fact: it said what it said on the day it was sent, and it
    has to keep saying it after the reservation is fulfilled, the book is
    archived or the stock check is deleted. It also means reading a
    hundred notifications is one query and no joins.

    `event_key` is what makes creation idempotent. It names the one-time
    transition the notification is about ("reservation_ready:41"), and
    `unique_notification_event` over (recipient_id, event_key) is a unique
    index in the database rather than a check in Python - so a refresh, a
    retried POST, two simultaneous returns of the same book, or a queue
    that advances twice all leave exactly one row.
    """

    # A reservation reached the front of its queue while a copy of the
    # book was on the shelf: somebody at the desk can act on it now.
    EVENT_RESERVATION_READY = "reservation_ready"

    # A stock check was declared finished and some copies were never
    # found. The session's own page is the report.
    EVENT_STOCK_CHECK_MISSING = "stock_check_missing"

    # Somebody suggested a book for the library to look for. Only the
    # people who may decide about it are told.
    EVENT_SUGGESTION_SUBMITTED = "suggestion_submitted"

    # Mirrors the `check_notification_event_type` CHECK constraint in
    # Postgres. Explicit values, not a free-text kind: a notification whose
    # type nothing recognises is one no page can render properly.
    EVENT_CHOICES = [
        (EVENT_RESERVATION_READY, _("Reserved book available")),
        (EVENT_STOCK_CHECK_MISSING, _("Stock check found copies missing")),
        (EVENT_SUGGESTION_SUBMITTED, _("Book suggested for acquisition")),
    ]

    id = models.AutoField(primary_key=True)

    recipient = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        db_column="recipient_id",
        related_name="notifications",
    )

    event_type = models.CharField(max_length=50, choices=EVENT_CHOICES)

    event_key = models.CharField(max_length=200)

    title = models.CharField(max_length=255)

    message = models.TextField(blank=True, default="")

    # Where to go to act on it, as a path within this application built by
    # `reverse()` at creation time. Never anything a user typed, and never
    # an absolute URL - see `safe_destination` on why it is checked again
    # on the way out.
    url = models.CharField(max_length=500, blank=True, default="")

    created_at = models.DateTimeField()

    # NULL while unread. A timestamp rather than a boolean because "when"
    # is the question worth answering, and NULL/not-NULL answers "whether"
    # just as well - the same reasoning as `books.archived_at`.
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "notifications"

    def __str__(self):
        return self.title

    @property
    def is_read(self):
        return self.read_at is not None

    @property
    def safe_destination(self):
        """The link to offer, or "" when there is nothing safe to offer.

        Only ever a path inside this application. Everything stored here is
        built by `reverse()`, so this should never reject anything - it is
        the second half of the rule, kept because a stored string is data
        and data outlives the code that wrote it.
        """

        target = self.url or ""

        if not target.startswith("/") or target.startswith("//"):
            return ""

        return target


class AcquisitionSuggestion(models.Model):
    """A book somebody thinks the library should have, and what came of it.

    Not a Book, and never allowed to become one by itself. A suggestion is
    a note in somebody's own words - a title, perhaps an author, perhaps an
    ISBN off the back of a copy they saw - and none of that is catalogue
    data. `author_name` and `publisher_name` are free text precisely
    because turning "ibn kathir" into an `Author` row on the strength of a
    suggestion would put a record in the catalogue that nobody checked.
    Nothing here creates an Author, a Publisher, a Category, a Book or a
    BookCopy; the catalogue is still entered through `book_add`, with its
    own validation and its own duplicate check.

    Not a purchase order either. There is no supplier, no quotation, no
    price, no invoice and no receiving step: those are an accounting system,
    and this is a list of books worth looking for.

    The four states are the whole workflow, and they only ever move one
    way:

        Pending ──▶ Approved ──▶ Acquired
            └────▶ Rejected

    `check_acquisition_status` pins the set in the database, and the
    transitions are enforced by conditional UPDATEs in library/acquisitions
    .py rather than by a check in Python - so a second click, a retried
    POST or a URL typed by hand cannot reopen something already decided.

    `suggested_by` and `reviewed_by` are nullable for the same reason every
    other user reference in this schema is: an account can be removed, and
    a suggestion outliving the person who made it is better than a delete
    that fails. The view always writes the signed-in user; NULL only ever
    means "that account is gone".
    """

    STATUS_PENDING = "Pending"
    STATUS_APPROVED = "Approved"
    STATUS_REJECTED = "Rejected"
    STATUS_ACQUIRED = "Acquired"

    # Mirrors the `check_acquisition_status` CHECK constraint in Postgres.
    # Title case, like every other status column in this schema
    # (`book_copies.status`, `reservations.status`,
    # `inventory_sessions.status`) rather than the SCREAMING_CASE a fresh
    # project might pick - there is one convention here and this follows it.
    STATUS_CHOICES = [
        (STATUS_PENDING, _("Pending")),
        (STATUS_APPROVED, _("Approved")),
        (STATUS_REJECTED, _("Rejected")),
        (STATUS_ACQUIRED, _("Acquired")),
    ]

    # Which states may follow which. The one place that says so:
    # `acquisitions.advance` reads it to build the WHERE clause that
    # enforces it, and the properties below read it to decide what a page
    # should offer. Nothing repeats the rule.
    TRANSITIONS = {
        STATUS_PENDING: (STATUS_APPROVED, STATUS_REJECTED),
        STATUS_APPROVED: (STATUS_ACQUIRED,),
        STATUS_REJECTED: (),
        STATUS_ACQUIRED: (),
    }

    id = models.AutoField(primary_key=True)

    # Matches `books.title`, so a suggestion can hold anything the
    # catalogue could.
    title = models.CharField(max_length=500)

    author_name = models.CharField(max_length=255, blank=True, default="")

    publisher_name = models.CharField(max_length=255, blank=True, default="")

    # Free text, and deliberately not validated as an ISBN. It is copied
    # off a cover by somebody who is not buying the book, and refusing a
    # mistyped check digit would lose the one piece of information that
    # makes the title findable.
    isbn = models.CharField(max_length=32, blank=True, default="")

    notes = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )

    suggested_by = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        db_column="suggested_by",
        null=True,
        blank=True,
        related_name="acquisition_suggestions",
    )

    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        db_column="reviewed_by",
        null=True,
        blank=True,
        related_name="acquisition_reviews",
    )

    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField()

    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "acquisition_suggestions"

    def __str__(self):
        return self.title

    @property
    def is_pending(self):
        return self.status == self.STATUS_PENDING

    @property
    def is_approved(self):
        return self.status == self.STATUS_APPROVED

    @property
    def is_closed(self):
        """Whether this suggestion has finished moving.

        Rejected and Acquired both have no transition out of them, so the
        detail page shows them as a record rather than as a decision.
        """

        return not self.TRANSITIONS[self.status]

    @property
    def was_reviewed(self):
        return self.reviewed_at is not None

    def may_become(self, status):
        """Whether `status` is a legal next state for this suggestion.

        The one readable form of the table above, for callers that have a
        target state in hand. Never the enforcement: library/acquisitions
        .py is, in the database, in one statement - so neither this nor a
        button drawn from `is_pending`/`is_approved` above is the rule.
        """

        return status in self.TRANSITIONS[self.status]


class RoleFeature(models.Model):
    """One override: this role does, or does not, get this part of the menu.

    Overrides only. A missing row means "whatever library/features.py says
    by default", which is why an installation that has never opened the
    permissions page has an empty table and behaves exactly as it did
    before the table existed.

    Nothing here can widen a role beyond the ceiling in features.py - the
    resolver checks the ceiling before it reads the table, so a row saying
    an Assistant may manage users is stored, read, and ignored. The table
    is a record of a person's choice, not the authority on what is allowed.

    `role` is deliberately plain text with no foreign key: roles are a fixed
    list in code, not rows. The unique constraint is what keeps one
    (role, feature) pair from having two answers.
    """

    id = models.AutoField(primary_key=True)

    role = models.CharField(max_length=30)

    feature_key = models.CharField(max_length=50)

    allowed = models.BooleanField()

    updated_at = models.DateTimeField()

    updated_by = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        db_column="updated_by",
        null=True,
        blank=True,
        related_name="feature_changes",
    )

    class Meta:
        managed = False
        db_table = "role_features"

    def __str__(self):
        return "%s %s %s" % (
            self.role,
            "may" if self.allowed else "may not",
            self.feature_key,
        )
