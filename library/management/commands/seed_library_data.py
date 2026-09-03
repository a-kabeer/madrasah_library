# -*- coding: utf-8 -*-
"""Development-only seeding of realistic, interconnected demo data.

    python manage.py seed_library_data

What it is for
--------------
Manual testing needs a catalogue with shape: multi-volume books next to
single-volume ones, books with no copies at all, shelves that are empty,
borrowers who have never taken anything out beside borrowers holding three
overdue items. This command builds that, on top of whatever is already in
the database.

What it will not do
-------------------
It never deletes, resets or rewrites real records. Every demo record is
created through the same code paths and the same rules the application
uses, so nothing it leaves behind is a state the app could not have
reached on its own:

  * copy codes come from `library.views.create_book_copies`, which is what
    Add Book uses — so they are real accession numbers, not invented strings
  * issuing and returning follow the loan views' rules, and the database's
    own `unique_active_loan_per_copy` partial unique index is what actually
    guarantees a copy is never out twice
  * statuses only ever take values the `check_copy_status` CHECK allows

Re-running
----------
Safe. Lookup rows are matched on their natural keys, copies are topped up
per volume rather than duplicated, and every demo loan carries a stable
slot number in its notes. A second run creates only what is missing and
refreshes the loan dates so "overdue" and "due today" still mean today.
See `--dry-run` to see what a run would do without keeping it.

Demo records are recognisable by DEMO_TAG in a free-text field (copy,
loan and borrower notes; location and shelf descriptions) and, for
borrowers, by the DM- registration-number series. Books, authors,
categories and publishers have no such field: for those the catalogue
below *is* the manifest, matched by name.
"""

import random
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from library.models import (
    ActivityLog,
    Author,
    Book,
    BookCopy,
    BookVolume,
    Borrower,
    Category,
    Loan,
    Location,
    Publisher,
    Shelf,
    User,
)
from library.views import DEFAULT_LOAN_PERIOD_DAYS, create_book_copies


# Written into a free-text field on every demo record that has one. Kept
# short and bracketed so it is easy to eyeball in the UI and easy to match
# with `notes__contains`.
DEMO_TAG = "[demo-data]"

DEMO_NOTE = "%s seeded by manage.py seed_library_data" % DEMO_TAG

# The whole dataset is planned from this one seed, so two runs agree with
# each other about which book gets which shelf and which borrower is
# holding what.
RANDOM_SEED = 20260903


class Rollback(Exception):
    """Raised at the end of a --dry-run to undo the transaction."""


# --------------------------------------------------------------------------
# Catalogue. Fictional, but shaped like a real madrasah library: Arabic,
# Urdu and English side by side, because the search has to be tested in all
# three.
# --------------------------------------------------------------------------

AUTHORS = [
    # Classical figures, paired below with classical works.
    "علامہ ابن حزم اندلسی",
    "امام ابن خزیمہ",
    "امام ابو جعفر طحاوی",
    "علامہ ابن رجب حنبلی",
    "علامہ عز الدین ابن عبد السلام",
    "امام ابو اسحاق شاطبی",
    "امام ابن الصلاح",
    "علامہ محمود آلوسی",
    "امام عبد الرزاق صنعانی",
    "امام ابن ابی شیبہ",
    "امام ابن حبان",
    "امام دار قطنی",
    "علامہ ابن الہمام",
    "علامہ ابن نجیم مصری",
    "علامہ خطیب بغدادی",
    "علامہ جاحظ",
    "علامہ ابن جنی",
    "علامہ ابو البقاء عکبری",
    "علامہ ابن قدامہ مقدسی",
    "امام ابو عبید قاسم بن سلام",
    # Invented modern authors.
    "ڈاکٹر ادریس کریم",
    "پروفیسر حلیمہ صادق",
    "ڈاکٹر نادر شفیق",
    "پروفیسر طاہر منور",
    "ڈاکٹر سلیم فاروقی",
    "پروفیسر زینب قریشی",
    "ڈاکٹر باسل حداد",
    "پروفیسر مریم زبیر",
    "ڈاکٹر سلیمان اشرف",
    "پروفیسر لیث الانصاری",
    "Dr. Hafsa Rahmani",
    "Prof. Junaid Baloch",
    "Dr. Ayesha Naqvi",
    "Prof. Waleed Siddiqui",
    "Dr. Imran Chishti",
    "Prof. Sadia Kamal",
    "Dr. Yaqub Meherally",
    "Prof. Hasan Barlas",
    "Dr. Nusrat Jahangir",
    "Prof. Bilqis Tanveer",
]

CATEGORIES = [
    "اصول فقہ",
    "اصول حدیث",
    "رجال و طبقات",
    "فتاوی",
    "ادب عربی",
    "ادب اردو",
    "منطق و فلسفہ",
    "تجوید و قراءت",
    "English Language & Literature",
    "Science & Mathematics",
    "بچوں کا ادب",
]

PUBLISHERS = [
    ("مکتبۃ البشری", "کراچی"),
    ("زمزم پبلشرز", "کراچی"),
    ("دار الکتب العلمیۃ", "بیروت"),
    ("مؤسسۃ الرسالۃ", "بیروت"),
    ("مکتبہ رحمانیہ", "لاہور"),
    ("ادارہ اسلامیات", "لاہور"),
    ("دار القلم", "دمشق"),
    ("مکتبۃ المعارف", "ریاض"),
    ("بیت الحکمۃ", "استنبول"),
    ("Crescent Academic Press", "Islamabad"),
    ("Minaret Educational Books", "Birmingham"),
    ("Nur Publications", "Toronto"),
    ("Al-Andalus Press", "Granada"),
    ("Iqra Learning House", "Karachi"),
]

# (name, description). The last one is deliberately a place with almost
# nothing in it, so "a location with no inventory" can be looked at.
LOCATIONS = [
    ("شعبہ تفسیر", "تفسیر و علوم القرآن کا مطالعہ گاہ"),
    ("شعبہ فقہ و فتاوی", "دار الافتاء سے متصل الماریاں"),
    ("طالبات کتب خانہ", "طالبات کے شعبہ کا الگ کتب خانہ"),
    ("مخزن", "اضافی کتب کا ذخیرہ"),
    ("دار الاقامہ مطالعہ گاہ", "ہاسٹل کی مطالعہ گاہ"),
    ("نمائشی الماری", "داخلی دروازے کے سامنے نمائش کے لیے"),
]

# (location name, shelf code, description).
SHELVES = (
    [("شعبہ تفسیر", "T-%02d" % n, "تفسیر سیکشن") for n in range(1, 9)]
    + [("شعبہ فقہ و فتاوی", "F-%02d" % n, "فقہ و فتاوی سیکشن") for n in range(1, 9)]
    + [("طالبات کتب خانہ", "G-%02d" % n, "طالبات سیکشن") for n in range(1, 7)]
    + [("مخزن", "S-%02d" % n, "ذخیرہ") for n in range(1, 8)]
    + [("دار الاقامہ مطالعہ گاہ", "R-%02d" % n, "مطالعہ گاہ") for n in range(1, 5)]
    + [("نمائشی الماری", "X-01", "نمائشی کتب")]
)

# Shelves that are meant to stay empty, so the shelf list has some. The
# display cabinet is left out of the stocking rotation entirely, which is
# what makes its location one with no inventory.
EMPTY_SHELVES = {
    ("مخزن", "S-06"),
    ("مخزن", "S-07"),
    ("دار الاقامہ مطالعہ گاہ", "R-04"),
    ("نمائشی الماری", "X-01"),
    ("طالبات کتب خانہ", "G-06"),
}

# (title, author, category, publisher) — 100 books.
BOOKS = [
    # اصول فقہ
    ("المستصفى في أصول الفقه", "علامہ ابن حزم اندلسی", "اصول فقہ", "دار الکتب العلمیۃ"),
    ("الموافقات في أصول الشريعة", "امام ابو اسحاق شاطبی", "اصول فقہ", "مؤسسۃ الرسالۃ"),
    ("قواعد الأحكام في مصالح الأنام", "علامہ عز الدین ابن عبد السلام", "اصول فقہ", "دار القلم"),
    ("التقرير والتحبير على تحرير الأصول", "علامہ ابن الہمام", "اصول فقہ", "دار الکتب العلمیۃ"),
    ("الأشباه والنظائر في الفروع", "علامہ ابن نجیم مصری", "اصول فقہ", "مکتبۃ البشری"),
    ("اصول فقہ کا تعارف", "ڈاکٹر ادریس کریم", "اصول فقہ", "ادارہ اسلامیات"),
    ("مقاصد شریعت اور عصر حاضر", "پروفیسر طاہر منور", "اصول فقہ", "زمزم پبلشرز"),
    ("الاجتہاد و التقلید: ایک تحقیقی جائزہ", "ڈاکٹر سلیم فاروقی", "اصول فقہ", "مکتبہ رحمانیہ"),
    ("Principles of Islamic Legal Theory", "Prof. Waleed Siddiqui", "اصول فقہ", "Crescent Academic Press"),
    ("قواعد الترجيح عند الفقهاء", "علامہ ابن قدامہ مقدسی", "اصول فقہ", "مکتبۃ المعارف"),
    # اصول حدیث
    ("علوم الحديث ومصطلحه", "امام ابن الصلاح", "اصول حدیث", "دار الکتب العلمیۃ"),
    ("الكفاية في علم الرواية", "علامہ خطیب بغدادی", "اصول حدیث", "مؤسسۃ الرسالۃ"),
    ("شرح مشكل الآثار", "امام ابو جعفر طحاوی", "اصول حدیث", "دار القلم"),
    ("المصنف في الأحاديث والآثار", "امام ابن ابی شیبہ", "اصول حدیث", "دار الکتب العلمیۃ"),
    ("المصنف للصنعاني", "امام عبد الرزاق صنعانی", "اصول حدیث", "مکتبۃ المعارف"),
    ("اصول حدیث: مبادیات", "ڈاکٹر نادر شفیق", "اصول حدیث", "ادارہ اسلامیات"),
    ("تخریج حدیث کا فن", "پروفیسر حلیمہ صادق", "اصول حدیث", "مکتبہ رحمانیہ"),
    ("An Introduction to Hadith Criticism", "Dr. Hafsa Rahmani", "اصول حدیث", "Minaret Educational Books"),
    ("علل الحديث ومناهج المحدثين", "امام دار قطنی", "اصول حدیث", "بیت الحکمۃ"),
    # رجال و طبقات
    ("الثقات", "امام ابن حبان", "رجال و طبقات", "دار الکتب العلمیۃ"),
    ("طبقات الفقهاء والمحدثين", "علامہ ابن رجب حنبلی", "رجال و طبقات", "مؤسسۃ الرسالۃ"),
    ("معجم شيوخ المحدثين", "امام ابن خزیمہ", "رجال و طبقات", "مکتبۃ المعارف"),
    ("تاريخ رواة الحديث", "علامہ ابن حزم اندلسی", "رجال و طبقات", "بیت الحکمۃ"),
    ("اسماء الرجال کا علمی جائزہ", "ڈاکٹر سلیمان اشرف", "رجال و طبقات", "زمزم پبلشرز"),
    ("برصغیر کے محدثین", "پروفیسر زینب قریشی", "رجال و طبقات", "ادارہ اسلامیات"),
    ("Biographical Dictionaries in Islamic Scholarship", "Prof. Hasan Barlas", "رجال و طبقات", "Crescent Academic Press"),
    ("طبقات المفسرين", "علامہ محمود آلوسی", "رجال و طبقات", "دار القلم"),
    ("تراجم علماء الأندلس", "علامہ ابن حزم اندلسی", "رجال و طبقات", "Al-Andalus Press"),
    # فتاوی
    ("الفتاوى في المعاملات المعاصرة", "ڈاکٹر باسل حداد", "فتاوی", "مکتبۃ البشری"),
    ("فتاوى الطهارة والصلاة", "علامہ ابن نجیم مصری", "فتاوی", "دار الکتب العلمیۃ"),
    ("جامع الفتاوى الحنفية", "علامہ ابن الہمام", "فتاوی", "مکتبۃ البشری"),
    ("فتاوی جدیدہ", "ڈاکٹر ادریس کریم", "فتاوی", "مکتبہ رحمانیہ"),
    ("عصری مسائل اور ان کا حل", "پروفیسر مریم زبیر", "فتاوی", "زمزم پبلشرز"),
    ("طبی مسائل کے شرعی احکام", "ڈاکٹر سلیم فاروقی", "فتاوی", "ادارہ اسلامیات"),
    ("Contemporary Fatwa and Public Interest", "Dr. Ayesha Naqvi", "فتاوی", "Nur Publications"),
    ("فتاوى المواريث والوصايا", "علامہ ابن قدامہ مقدسی", "فتاوی", "مؤسسۃ الرسالۃ"),
    ("احکام زکوٰۃ و عشر", "پروفیسر لیث الانصاری", "فتاوی", "مکتبہ رحمانیہ"),
    # ادب عربی
    ("البيان والتبيين", "علامہ جاحظ", "ادب عربی", "دار الکتب العلمیۃ"),
    ("الخصائص في فقه اللغة", "علامہ ابن جنی", "ادب عربی", "مؤسسۃ الرسالۃ"),
    ("شرح ديوان المتنبي", "علامہ ابو البقاء عکبری", "ادب عربی", "دار القلم"),
    ("غريب القرآن ومعانيه", "امام ابو عبید قاسم بن سلام", "ادب عربی", "مکتبۃ المعارف"),
    ("نصوص من الأدب العربي الحديث", "پروفیسر لیث الانصاری", "ادب عربی", "بیت الحکمۃ"),
    ("البلاغة العربية: دراسة تطبيقية", "ڈاکٹر باسل حداد", "ادب عربی", "دار القلم"),
    ("عربی زبان و ادب کا ارتقا", "ڈاکٹر نادر شفیق", "ادب عربی", "ادارہ اسلامیات"),
    ("قواعد اللغة العربية للمبتدئين", "پروفیسر حلیمہ صادق", "ادب عربی", "مکتبۃ البشری"),
    ("Readings in Classical Arabic Prose", "Prof. Junaid Baloch", "ادب عربی", "Minaret Educational Books"),
    # ادب اردو
    ("اردو نثر کی تاریخ", "پروفیسر مریم زبیر", "ادب اردو", "ادارہ اسلامیات"),
    ("غزل کا سفر", "ڈاکٹر سلیمان اشرف", "ادب اردو", "زمزم پبلشرز"),
    ("اقبال: فکر و فن", "پروفیسر طاہر منور", "ادب اردو", "مکتبہ رحمانیہ"),
    ("اردو افسانہ: انتخاب", "Dr. Nusrat Jahangir", "ادب اردو", "ادارہ اسلامیات"),
    ("دیارِ ادب کے چراغ", "پروفیسر زینب قریشی", "ادب اردو", "زمزم پبلشرز"),
    ("اردو تنقید کے اصول", "Dr. Imran Chishti", "ادب اردو", "مکتبہ رحمانیہ"),
    ("سفرنامہ: راہِ حرم", "Prof. Bilqis Tanveer", "ادب اردو", "ادارہ اسلامیات"),
    ("اردو مرثیہ اور اس کا پس منظر", "Dr. Yaqub Meherally", "ادب اردو", "زمزم پبلشرز"),
    ("خطوط و مکاتیب کا فن", "Prof. Sadia Kamal", "ادب اردو", "مکتبہ رحمانیہ"),
    # منطق و فلسفہ
    ("الإشارات إلى علم المنطق", "علامہ ابن حزم اندلسی", "منطق و فلسفہ", "دار الکتب العلمیۃ"),
    ("مدخل إلى الفلسفة الإسلامية", "ڈاکٹر باسل حداد", "منطق و فلسفہ", "بیت الحکمۃ"),
    ("منطق کی ابتدائی کتاب", "ڈاکٹر ادریس کریم", "منطق و فلسفہ", "ادارہ اسلامیات"),
    ("علم الكلام ومسائله", "امام ابو جعفر طحاوی", "منطق و فلسفہ", "دار القلم"),
    ("فلسفہ اخلاق", "پروفیسر لیث الانصاری", "منطق و فلسفہ", "زمزم پبلشرز"),
    ("Reason and Revelation in Islamic Thought", "Prof. Hasan Barlas", "منطق و فلسفہ", "Crescent Academic Press"),
    ("المنطق التطبيقي", "پروفیسر مریم زبیر", "منطق و فلسفہ", "مکتبۃ البشری"),
    ("تصور علم اور جدید فکر", "Dr. Imran Chishti", "منطق و فلسفہ", "مکتبہ رحمانیہ"),
    ("الحكمة المشرقية: قراءة نقدية", "علامہ ابو البقاء عکبری", "منطق و فلسفہ", "Al-Andalus Press"),
    # تجوید و قراءت
    ("التمهيد في علم التجويد", "امام ابو عبید قاسم بن سلام", "تجوید و قراءت", "مکتبۃ المعارف"),
    ("القراءات السبع وحججها", "امام ابن الصلاح", "تجوید و قراءت", "دار الکتب العلمیۃ"),
    ("تجوید القرآن آسان اسلوب میں", "پروفیسر حلیمہ صادق", "تجوید و قراءت", "زمزم پبلشرز"),
    ("مخارج الحروف و صفات", "ڈاکٹر سلیم فاروقی", "تجوید و قراءت", "مکتبہ رحمانیہ"),
    ("وقف و ابتدا کے قواعد", "Prof. Sadia Kamal", "تجوید و قراءت", "ادارہ اسلامیات"),
    ("أحكام النون الساكنة والتنوين", "علامہ ابن جنی", "تجوید و قراءت", "دار القلم"),
    ("Tajwid for English Speakers", "Dr. Hafsa Rahmani", "تجوید و قراءت", "Minaret Educational Books"),
    ("الرسم العثماني وضبط المصحف", "علامہ محمود آلوسی", "تجوید و قراءت", "مکتبۃ المعارف"),
    ("حفظ قرآن کے تربیتی اصول", "ڈاکٹر نادر شفیق", "تجوید و قراءت", "زمزم پبلشرز"),
    # English Language & Literature
    ("Foundations of English Grammar", "Prof. Junaid Baloch", "English Language & Literature", "Minaret Educational Books"),
    ("Academic Writing for Madrasah Students", "Dr. Ayesha Naqvi", "English Language & Literature", "Crescent Academic Press"),
    ("An Anthology of Muslim Poets in English", "Prof. Bilqis Tanveer", "English Language & Literature", "Nur Publications"),
    ("English Vocabulary Builder", "Dr. Nusrat Jahangir", "English Language & Literature", "Iqra Learning House"),
    ("Comparative Literature: East and West", "Prof. Hasan Barlas", "English Language & Literature", "Nur Publications"),
    ("Reading Comprehension Practice", "Dr. Imran Chishti", "English Language & Literature", "Iqra Learning House"),
    ("Public Speaking and Debate", "Prof. Waleed Siddiqui", "English Language & Literature", "Crescent Academic Press"),
    ("A Short History of the English Novel", "Dr. Yaqub Meherally", "English Language & Literature", "Minaret Educational Books"),
    ("Translation Studies: Arabic to English", "Dr. Hafsa Rahmani", "English Language & Literature", "Nur Publications"),
    # Science & Mathematics
    ("Elementary Algebra and Geometry", "Prof. Waleed Siddiqui", "Science & Mathematics", "Iqra Learning House"),
    ("General Science for Secondary Classes", "Dr. Nusrat Jahangir", "Science & Mathematics", "Iqra Learning House"),
    ("حساب و ہندسہ کی بنیادی کتاب", "Dr. Yaqub Meherally", "Science & Mathematics", "ادارہ اسلامیات"),
    ("مبادئ الحساب والجبر", "علامہ ابو البقاء عکبری", "Science & Mathematics", "بیت الحکمۃ"),
    ("Astronomy and the Islamic Calendar", "Prof. Junaid Baloch", "Science & Mathematics", "Crescent Academic Press"),
    ("علم الفلک اور اوقات نماز", "ڈاکٹر سلیمان اشرف", "Science & Mathematics", "مکتبہ رحمانیہ"),
    ("Biology: An Introduction", "Dr. Ayesha Naqvi", "Science & Mathematics", "Iqra Learning House"),
    ("طبیعیات کے بنیادی اصول", "پروفیسر طاہر منور", "Science & Mathematics", "زمزم پبلشرز"),
    ("History of Mathematics in Muslim Lands", "Prof. Sadia Kamal", "Science & Mathematics", "Al-Andalus Press"),
    # بچوں کا ادب
    ("بچوں کی پہلی عربی کتاب", "پروفیسر حلیمہ صادق", "بچوں کا ادب", "Iqra Learning House"),
    ("نبیوں کی کہانیاں", "ڈاکٹر ادریس کریم", "بچوں کا ادب", "زمزم پبلشرز"),
    ("چاند اور ستارے", "Prof. Bilqis Tanveer", "بچوں کا ادب", "Iqra Learning House"),
    ("صحابہ کرام کے واقعات", "ڈاکٹر سلیم فاروقی", "بچوں کا ادب", "مکتبہ رحمانیہ"),
    ("Stories of the Prophets for Children", "Dr. Nusrat Jahangir", "بچوں کا ادب", "Minaret Educational Books"),
    ("اخلاق کی باتیں", "پروفیسر زینب قریشی", "بچوں کا ادب", "ادارہ اسلامیات"),
    ("My First Book of Duas", "Prof. Bilqis Tanveer", "بچوں کا ادب", "Nur Publications"),
    ("حروف تہجی اور تصویریں", "Prof. Sadia Kamal", "بچوں کا ادب", "Iqra Learning House"),
    ("پیارے نبی کی پیاری باتیں", "Dr. Imran Chishti", "بچوں کا ادب", "زمزم پبلشرز"),
]

# How many volumes each book gets. Rotated across the catalogue below so the
# shapes are spread through the alphabet rather than bunched at the front.
VOLUME_PLAN = (
    [0] * 8      # books with nothing under them at all
    + [1] * 50   # the ordinary case
    + [2] * 20
    + [3] * 12
    + [4] * 5
    + [8] * 3    # a set
    + [12] * 2   # a long set
)

VOLUME_TITLES = [
    "جلد اول", "جلد دوم", "جلد سوم", "جلد چہارم", "جلد پنجم", "جلد ششم",
    "جلد ہفتم", "جلد ہشتم", "جلد نہم", "جلد دہم", "جلد یازدہم", "جلد دوازدہم",
]

# Copies per volume, cycled. The zeroes matter: a volume with no copies is
# a real thing a librarian has to be able to see.
COPY_PATTERN = (1, 2, 0, 3, 1, 2, 6, 1, 3, 2, 1, 2, 3, 0, 2, 1, 6, 3, 2, 1)

# Every ninth copy is left off a shelf, so the "unshelved" state has
# something in it.
UNSHELVED_EVERY = 9
UNSHELVED_OFFSET = 4

# A handful of copies in a stored state other than Available/Issued, so the
# copy filters have something to find. All values the CHECK constraint
# allows; none of these copies is ever issued.
SPECIAL_STATES = (
    BookCopy.STATUS_LOST,
    BookCopy.STATUS_DAMAGED,
    BookCopy.STATUS_MISSING,
    BookCopy.STATUS_TRANSFERRED,
)
SPECIAL_EVERY = 43
SPECIAL_OFFSET = 11

GIVEN_NAMES = [
    "محمد بلال", "عبد الرحمٰن", "حمزہ", "طلحہ", "زید", "عمر", "صہیب", "انس",
    "معاذ", "ابوبکر", "حسان", "یاسر", "فہد", "ندیم", "ارشد", "عائشہ",
    "خدیجہ", "فاطمہ", "حفصہ", "مریم", "زینب", "صفیہ", "رقیہ", "ام کلثوم",
    "آمنہ",
]

FAMILY_NAMES = [
    "قریشی", "انصاری", "فاروقی", "صدیقی", "عثمانی", "ہاشمی",
    "چشتی", "بلوچ", "کاکڑ", "سندھی", "کشمیری", "لودھی",
]

DEPARTMENTS = [
    "درجہ اولیٰ", "درجہ ثانیہ", "درجہ ثالثہ", "درجہ رابعہ",
    "تخصص فی الفقہ", "شعبہ حفظ", "شعبہ تجوید",
    "اسٹاف", "Teaching Faculty", "Administration",
]

MOHALLAS = ["محلہ عیدگاہ", "محلہ قاسم آباد", "گلی نمبر 4", "نزد جامع مسجد", "کالج روڈ"]
TOWNS = ["کراچی", "لاہور", "پشاور", "کوئٹہ", "ملتان", "حیدرآباد"]

BORROWER_COUNT = 100

# Every tenth borrower is inactive — 10 of the 100. Because the offset lands
# on several different loan scenarios, the inactive set ends up including
# borrowers with no history, borrowers with returned history, and borrowers
# still holding something, which is what makes the status filter worth
# testing.
INACTIVE_EVERY = 10
INACTIVE_OFFSET = 7


def borrower_scenarios():
    """The loan shapes each demo borrower should end up with.

    Index-driven rather than random so the same borrower is the same kind of
    borrower on every run, which is what makes a second run able to leave
    them alone.
    """

    plan = []

    for index in range(BORROWER_COUNT):

        if index < 25:
            # Never taken anything out.
            shapes = []

        elif index < 60:
            # History, but nothing in hand.
            shapes = ["returned_old"] * (1 + index % 2)

            if index % 3 == 0:
                shapes.append("returned_recent")

        elif index < 80:
            shapes = ["active"]

        elif index < 90:
            # Several things out at once, one of them late.
            shapes = ["active"] * (1 + index % 2) + ["overdue"]

        elif index < 95:
            shapes = ["overdue"]

        elif index < 97:
            shapes = ["due_today"]

        else:
            shapes = ["returned_old", "returned_recent", "overdue"]

        plan.append(shapes)

    return plan


# How many copies get a long-past returned loan as well as the loan they are
# out on now, so "this copy has been borrowed before" is visible on a copy
# that is currently issued. The old loan is hundreds of days back, so the
# two windows cannot overlap.
REISSUED_COPIES = 10


class Command(BaseCommand):
    help = "Seed realistic demo data for manual testing (development only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created, then roll everything back.",
        )

    # ------------------------------------------------------------------ run

    def handle(self, *args, **options):

        if not settings.DEBUG:
            raise CommandError(
                "seed_library_data only runs with DEBUG on. This is demo "
                "data; it has no business in a production database."
            )

        self.rng = random.Random(RANDOM_SEED)
        self.today = timezone.now().date()
        self.made = {}

        try:
            with transaction.atomic():

                authors = self.ensure_authors()
                categories = self.ensure_categories()
                publishers = self.ensure_publishers()
                locations = self.ensure_locations()
                shelves = self.ensure_shelves(locations)
                books = self.ensure_books(authors, categories, publishers)
                volumes = self.ensure_volumes(books)
                copies = self.ensure_copies(volumes, shelves)
                borrowers = self.ensure_borrowers()

                self.ensure_loans(borrowers, copies)
                self.reconcile_copy_status(copies)
                self.log_run()

                if options["dry_run"]:
                    raise Rollback

        except Rollback:
            self.report(dry_run=True)
            return

        self.report(dry_run=False)

    # -------------------------------------------------------------- lookups

    def ensure_authors(self):
        """Authors by name. `authors.name` is UNIQUE, so this is the key."""

        found = {}

        for name in AUTHORS:
            author, created = Author.objects.get_or_create(name=name)
            found[name] = author
            self.count("authors", created)

        return found

    def ensure_categories(self):

        found = {}

        for name in CATEGORIES:
            category, created = Category.objects.get_or_create(name=name)
            found[name] = category
            self.count("categories", created)

        return found

    def ensure_publishers(self):

        found = {}

        for name, city in PUBLISHERS:
            publisher, created = Publisher.objects.get_or_create(
                name=name,
                defaults={"city": city},
            )
            found[name] = publisher
            self.count("publishers", created)

        return found

    def ensure_locations(self):

        found = {}

        for name, description in LOCATIONS:
            location, created = Location.objects.get_or_create(
                name=name,
                defaults={"description": "%s — %s" % (description, DEMO_TAG)},
            )
            found[name] = location
            self.count("locations", created)

        return found

    def ensure_shelves(self, locations):
        """Shelves keyed by (location, code) — the real UNIQUE constraint."""

        found = {}

        for location_name, code, description in SHELVES:
            shelf, created = Shelf.objects.get_or_create(
                location=locations[location_name],
                shelf_code=code,
                defaults={"description": "%s — %s" % (description, DEMO_TAG)},
            )
            found[(location_name, code)] = shelf
            self.count("shelves", created)

        return found

    # ---------------------------------------------------------------- books

    def ensure_books(self, authors, categories, publishers):
        """Books keyed by (title, author).

        `books.title` is not unique — two publishers' editions of the same
        work are a real thing — so the author is part of the key.
        """

        unknown = sorted(
            ({row[1] for row in BOOKS} - set(authors))
            | ({row[2] for row in BOOKS} - set(categories))
            | ({row[3] for row in BOOKS} - set(publishers))
        )

        if unknown:
            raise CommandError(
                "catalogue refers to names that are not in the pools: %s"
                % ", ".join(unknown)
            )

        made = []

        for title, author_name, category_name, publisher_name in BOOKS:

            book, created = Book.objects.get_or_create(
                title=title,
                author=authors[author_name],
                defaults={
                    "category": categories[category_name],
                    "publisher": publishers[publisher_name],
                },
            )

            made.append(book)
            self.count("books", created)

        return made

    def ensure_volumes(self, books):
        """One row per volume, keyed by (book, volume_number).

        Books whose plan is zero volumes get nothing — deliberately, so the
        catalogue contains books with no volumes and therefore no copies.
        """

        made = []

        for index, book in enumerate(books):

            wanted = VOLUME_PLAN[(index * 37) % len(VOLUME_PLAN)]

            for number in range(1, wanted + 1):

                # A single-volume book has no volume title: the app already
                # renders that case as just the book.
                title = VOLUME_TITLES[number - 1] if wanted > 1 else ""

                volume, created = BookVolume.objects.get_or_create(
                    book=book,
                    volume_number=number,
                    defaults={"title": title},
                )

                made.append(volume)
                self.count("volumes", created)

        return made

    def ensure_copies(self, volumes, shelves):
        """Top up each volume to its planned number of demo copies.

        Codes come from `create_book_copies`, the same helper Add Book uses,
        so they are allocated from the copy's own id exactly as a copy added
        through the UI would be. Nothing here invents a code.
        """

        stock = [
            shelf
            for key, shelf in shelves.items()
            if key not in EMPTY_SHELVES
        ]

        made = []
        ordinal = 0
        fresh = []

        for index, volume in enumerate(volumes):

            wanted = COPY_PATTERN[index % len(COPY_PATTERN)]

            existing = list(
                BookCopy.objects.filter(
                    volume=volume,
                    notes__contains=DEMO_TAG,
                ).order_by("id")
            )

            made.extend(existing)

            for position in range(wanted):

                # Held whether or not the copy is created, so the shelving
                # pattern stays put across runs.
                slot = ordinal
                ordinal += 1

                if position < len(existing):
                    continue

                unshelved = slot % UNSHELVED_EVERY == UNSHELVED_OFFSET
                shelf = None if unshelved else stock[slot % len(stock)]

                copy = create_book_copies(volume, shelf, 1)[0]

                made.append(copy)
                fresh.append(copy.id)
                self.count("copies", True)

        if fresh:
            # One statement rather than a save per row. Only the tag and an
            # acquisition date; the code and status were set by the helper.
            BookCopy.objects.filter(id__in=fresh).update(
                notes=DEMO_NOTE,
                acquisition_date=self.today - timedelta(days=400),
            )

        return sorted(made, key=lambda copy: copy.id)

    # ------------------------------------------------------------ borrowers

    def ensure_borrowers(self):
        """Borrowers keyed by their DM- registration number.

        `unique_borrower_registration_no` is a partial UNIQUE index over the
        non-blank values, so this is a real key and a re-run cannot double
        anyone up.
        """

        names = self.borrower_names()
        made = []

        for index in range(BORROWER_COUNT):

            registration_no = "DM-%04d" % (1001 + index)
            active = index % INACTIVE_EVERY != INACTIVE_OFFSET

            borrower, created = Borrower.objects.get_or_create(
                registration_no=registration_no,
                defaults={
                    "name": names[index],
                    "phone": "0345%07d" % (2100000 + index * 37),
                    "borrower_type": self.borrower_type(index),
                    "department": DEPARTMENTS[index % len(DEPARTMENTS)],
                    "address": "%s، %s" % (
                        MOHALLAS[index % len(MOHALLAS)],
                        TOWNS[index % len(TOWNS)],
                    ),
                    "notes": DEMO_NOTE,
                    "is_active": active,
                    "created_at": timezone.now() - timedelta(
                        days=30 + index * 3
                    ),
                },
            )

            made.append(borrower)
            self.count("borrowers", created)

        return made

    def borrower_names(self):
        """Distinct fictional names, the same 100 on every run.

        Given name and family name are paired by walking the given names
        against a rotating family-name offset. Every pair a round produces
        is distinct, and so is every pair between rounds, so the count is
        reached without having to test for collisions.
        """

        names = []

        for offset in range(len(FAMILY_NAMES)):

            for position, given in enumerate(GIVEN_NAMES):

                names.append("%s %s" % (
                    given,
                    FAMILY_NAMES[(position + offset) % len(FAMILY_NAMES)],
                ))

                if len(names) == BORROWER_COUNT:
                    return names

        raise CommandError(
            "the name pools only make %d distinct names, %d are needed"
            % (len(names), BORROWER_COUNT)
        )

    def borrower_type(self, index):

        if index % 10 == 4:
            return "Teacher"

        if index % 10 == 9:
            return "Staff"

        if index % 25 == 13:
            return "Other"

        return "Student"

    # ---------------------------------------------------------------- loans

    def ensure_loans(self, borrowers, copies):
        """Create the missing demo loans, and refresh the dates of the rest.

        Each planned loan owns a slot number, written into its notes. The
        slot is what makes a re-run able to tell "this one is already here"
        from "this one is new" — the dates cannot do that job, because they
        are relative to the day the command runs.
        """

        eligible = [
            copy for index, copy in enumerate(copies)
            if index % SPECIAL_EVERY != SPECIAL_OFFSET
        ]

        if not eligible:
            return

        # Disjoint pools. Nothing in the history pool is ever issued, so a
        # returned loan can never sit on top of an active one.
        split = min(120, len(eligible) // 2)
        active_pool = eligible[:split]
        history_pool = eligible[split:] or eligible

        plan = self.plan_loans(borrowers, active_pool, history_pool)

        existing = {}

        for loan in Loan.objects.filter(notes__contains=DEMO_TAG):
            slot = self.slot_of(loan)

            if slot is not None:
                existing[slot] = loan

        staff = list(User.objects.filter(is_active=True).order_by("id"))

        for entry in plan:

            slot = entry["slot"]
            issue, due, returned = self.dates_for(entry["shape"])

            loan = existing.get(slot)

            if loan is None:

                Loan.objects.create(
                    copy=entry["copy"],
                    borrower=entry["borrower"],
                    issue_date=issue,
                    due_date=due,
                    return_date=returned,
                    issued_by=self.pick(staff, slot),
                    returned_to=(
                        self.pick(staff, slot + 1) if returned else None
                    ),
                    notes=self.slot_note(slot),
                )

                self.count("loans", True)

            else:

                # Already here from an earlier run. Keep its copy and its
                # borrower — moving those could collide with the active-loan
                # index — and only move the dates, so "overdue" and "due
                # today" still describe today.
                loan.issue_date = issue
                loan.due_date = due
                loan.return_date = returned
                loan.returned_to = (
                    loan.returned_to or self.pick(staff, slot + 1)
                ) if returned else None

                loan.save(
                    update_fields=[
                        "issue_date", "due_date", "return_date", "returned_to",
                    ]
                )

                self.count("loans_refreshed", True)

    def plan_loans(self, borrowers, active_pool, history_pool):
        """The full list of planned loans, in slot order."""

        plan = []
        active_cursor = 0
        history_cursor = 0
        reissued = 0

        def add(shape, borrower, copy):
            plan.append({
                "slot": len(plan) + 1,
                "shape": shape,
                "borrower": borrower,
                "copy": copy,
            })

        for index, shapes in enumerate(borrower_scenarios()):

            borrower = borrowers[index]

            for shape in shapes:

                if shape in ("active", "overdue", "due_today"):

                    if active_cursor >= len(active_pool):
                        continue

                    copy = active_pool[active_cursor]
                    active_cursor += 1

                    add(shape, borrower, copy)

                    if reissued < REISSUED_COPIES:
                        # The same copy, borrowed and brought back long
                        # before the loan it is out on now.
                        add("returned_old", borrowers[index - 30], copy)
                        reissued += 1

                else:

                    copy = history_pool[history_cursor % len(history_pool)]
                    history_cursor += 1

                    add(shape, borrower, copy)

        return plan

    def dates_for(self, shape):
        """(issue, due, return) for a shape, relative to today.

        Both loan CHECK constraints hold by construction: `due` is never
        before `issue`, and a `return` is never before `issue`.
        """

        today = self.today
        period = DEFAULT_LOAN_PERIOD_DAYS

        if shape == "active":
            issue = today - timedelta(days=self.rng.randint(1, 12))
            return issue, issue + timedelta(days=period), None

        if shape == "overdue":
            issue = today - timedelta(days=self.rng.randint(21, 95))
            return issue, issue + timedelta(days=period), None

        if shape == "due_today":
            issue = today - timedelta(days=period)
            return issue, today, None

        if shape == "returned_recent":
            issue = today - timedelta(days=self.rng.randint(18, 45))
            due = issue + timedelta(days=period)
            return issue, due, today - timedelta(days=self.rng.randint(0, 6))

        # returned_old: real history, including some that came back late.
        issue = today - timedelta(days=self.rng.randint(200, 700))
        due = issue + timedelta(days=period + 7)

        if self.rng.random() < 0.25:
            returned = due + timedelta(days=self.rng.randint(1, 15))
        else:
            returned = due - timedelta(days=self.rng.randint(0, 10))

        return issue, due, returned

    def slot_note(self, slot):
        return "%s loan slot %04d" % (DEMO_TAG, slot)

    def slot_of(self, loan):
        """The slot number in a demo loan's notes, or None."""

        marker = "%s loan slot " % DEMO_TAG
        text = loan.notes or ""

        if marker not in text:
            return None

        try:
            return int(text.split(marker, 1)[1][:4])
        except ValueError:
            return None

    def pick(self, staff, index):
        return staff[index % len(staff)] if staff else None

    # ------------------------------------------------------------ integrity

    def reconcile_copy_status(self, copies):
        """Make every demo copy's status agree with its loans.

        The rule is the application's own: a copy is Issued exactly while it
        has a loan that has not come back. Written as three statements
        rather than a save per row because it is a correction pass, not a
        workflow — and after it, no demo copy can be marked Issued without
        an active loan to justify it.
        """

        ids = [copy.id for copy in copies]

        if not ids:
            return

        special = {}

        for index, copy in enumerate(copies):
            if index % SPECIAL_EVERY == SPECIAL_OFFSET:
                state = SPECIAL_STATES[
                    (index // SPECIAL_EVERY) % len(SPECIAL_STATES)
                ]
                special.setdefault(state, []).append(copy.id)

        out = set(
            Loan.objects.filter(
                copy_id__in=ids,
                return_date__isnull=True,
            ).values_list("copy_id", flat=True)
        )

        BookCopy.objects.filter(id__in=ids).exclude(
            id__in=out
        ).update(status=BookCopy.STATUS_AVAILABLE)

        if out:
            BookCopy.objects.filter(id__in=out).update(
                status=BookCopy.STATUS_ISSUED
            )

        for state, state_ids in special.items():
            # Never applied to a copy that is out: `special` is excluded
            # from the loan pools, so none of these has an active loan.
            BookCopy.objects.filter(
                id__in=state_ids
            ).exclude(id__in=out).update(status=state)

    # ------------------------------------------------------------- plumbing

    def count(self, key, created):
        if created:
            self.made[key] = self.made.get(key, 0) + 1

    def log_run(self):
        """One activity-log line for the run, not one per record."""

        ActivityLog.objects.create(
            user=None,
            action="SEED",
            entity_type=None,
            entity_id=None,
            description="%s seed_library_data: %s" % (
                DEMO_TAG,
                ", ".join(
                    "%s %s" % (value, key)
                    for key, value in sorted(self.made.items())
                ) or "nothing new",
            ),
            created_at=timezone.now(),
        )

    def report(self, dry_run):

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — rolled back."))

        for key, value in sorted(self.made.items()):
            self.stdout.write("  %-18s %d" % (key, value))

        if not self.made:
            self.stdout.write("  nothing to do — the demo data is all here")

        if not dry_run:
            self.stdout.write(self.style.SUCCESS("Seeding complete."))
