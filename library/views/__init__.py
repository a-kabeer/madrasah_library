"""The web layer, one module per part of the library.

Everything each module defines is re-exported here, so `views.author_list`
and `from library.views import PAGE_SIZE` mean what they always did and
urls.py did not have to change.
"""

from .common import (  # noqa: F401
    ACTIVITY_LOG_CACHE_KEY, ACTIVITY_LOG_DETAIL_ROUTES, ACTIVITY_LOG_LOOKUP_KINDS,
    AUTHOR_CACHE_KEY, BOOK_AVAILABILITY_FILTERS, BOOK_AVAILABILITY_LABELS,
    BOOK_CACHE_KEY, BOOK_CONTENT_CACHE_KEY, BOOK_COPY_CACHE_KEY, BOOK_DETAIL_TABS,
    BOOK_LIST_FRAGMENTS, BOOK_LIST_MODES, BOOK_LIST_MODE_DEFAULT, BOOK_SORT_DEFAULT,
    BOOK_SORT_FIELDS, BOOK_VOLUME_CACHE_KEY, BORROWER_CACHE_KEY, BORROWER_TYPES,
    CATEGORY_CACHE_KEY, COMBOBOX_LIMIT, COPY_CODE_DIGITS, COPY_CODE_PREFIX,
    COPY_SORT_DEFAULT, COPY_SORT_FIELDS, COPY_STATE_ALWAYS_SHOWN, COPY_STATE_FILTERS,
    COPY_STATE_LABELS, COPY_STATE_ORDER, COPY_STATE_TONES, COPY_STORED_STATES,
    DASHBOARD_CACHE_KEY, DEFAULT_LOAN_PERIOD_DAYS, FAVICON_EXTENSIONS, IMAGE_EXTENSIONS,
    INTERNAL_PARAMS, LOAN_CACHE_KEY, LOCATION_CACHE_KEY, LOOKUP_BOOK_QUERIES,
    LOOKUP_LABELS, LOOKUP_NAME_LABELS, LOOKUP_SORT_DEFAULT, LOOKUP_SORT_FIELDS,
    MAX_COPIES_PER_VOLUME, MAX_TOTAL_COPIES, MAX_VOLUMES, PAGE_SIZE, PAGE_SIZE_CHOICES,
    PUBLISHER_CACHE_KEY, PolicyRefused, SHELF_CACHE_KEY, USER_CACHE_KEY, USER_ROLES,
    activity_log_target, allocate_copy_code, book_list_fragment, book_saved_response,
    combobox_created_response, combobox_options_response, copy_for_code, copy_state,
    create_activity_log, create_book_copies, describe_copies, describe_loans,
    describe_size_limit, filter_copies_by_state, is_combobox_request, is_form_modal_request,
    is_modal_request, is_options_request, lookup_book_counts, lookup_books_url,
    lookup_delete_blocker, lookup_delete_modal, lookup_deleted_response, lookup_form_modal,
    lookup_options, lookup_saved_response, lookup_table, numeric_param, page_size_options,
    query_with, read_copy_plan, read_volume_rows, resolve_page_size, resolve_sort,
    safe_redirect_target, selected_name, shelf_options_for, sort_ordering, sortable_columns,
    validate_cover_image, validate_image_upload, volume_label,
)

from .catalog import (  # noqa: F401
    COPY_WITHDRAWN_STATUS, author_add, author_delete, author_edit, author_list, book_add,
    book_archive, book_archive_blocker, book_content_add, book_content_delete,
    book_content_detail, book_content_edit, book_content_list, book_copy_withdraw,
    book_delete, book_delete_blocker, book_deleted_response, book_detail, book_edit,
    book_restore, book_volume_add, book_volume_delete, book_volume_detail, book_volume_edit,
    book_volume_list, category_add, category_delete, category_edit, category_list,
    duplicate_book_error, find_duplicate_books, isolated, normalize_title,
    normalized_title_expression, publisher_add, publisher_delete, publisher_edit,
    publisher_list, read_volume_form, suggestion_matches, suggestion_prefill,
)

from .copies import (  # noqa: F401
    COPY_LIST_FRAGMENTS, COPY_LOOKUP_LIMIT, CopyFilters, LABEL_LIMIT, book_copy_add,
    book_copy_bulk_move, book_copy_delete, book_copy_detail, book_copy_edit,
    book_copy_labels, book_copy_list, book_copy_move, book_list, copies_moved_response,
    copy_list_fragment, filtered_copies, labelled, location_add, location_delete,
    location_detail, location_edit, location_list, location_options_response, render_options,
    shelf_add, shelf_delete, shelf_detail, shelf_edit, shelf_list, shelf_options_response,
)
from .location_shelf_modals import (  # noqa: F401
    location_add_modal, location_delete_modal, location_edit_modal,
    shelf_add_modal, shelf_delete_modal, shelf_edit_modal,
)
from .volume_content_modals import (  # noqa: F401
    book_content_add_modal, book_content_delete_modal, book_content_edit_modal,
    book_volume_add_modal, book_volume_delete_modal, book_volume_edit_modal,
)
from .circulation import (  # noqa: F401
    RETURN_LOOKUP_LIMIT, ScanOutcome, active_loan_for_code, active_loans_matching,
    circulation_dashboard, copy_selection_url, issuable_copies, loan_add, loan_delete,
    loan_detail, loan_edit, loan_list, loan_renew, loan_return, loan_return_lookup,
    scan_issue_outcome,
)
from .borrowers import (  # noqa: F401
    BORROWER_ACTIVITY_FILTERS, borrower_add, borrower_delete, borrower_detail,
    borrower_edit, borrower_list, borrower_toggle_active,
)
from .inventory import (  # noqa: F401
    inventory_session_complete, inventory_session_detail, inventory_session_list,
    inventory_session_scan, inventory_session_start,
)
from .reservations import reservation_add, reservation_cancel, reservation_list  # noqa: F401
from .acquisitions import (  # noqa: F401
    can_review_suggestions, suggestion_add, suggestion_detail, suggestion_list, suggestion_review,
)
from .imports import (  # noqa: F401
    IMPORT_FAILURE_KEY, IMPORT_KEEP_SECONDS, IMPORT_SESSION_KEY, _volume_numbers_by_book,
    book_export, book_import, book_import_errors, book_import_template, clear_import,
    get_or_create_named, import_failure_payload, import_limits, import_lookups, import_state,
    import_storage, mapping_rows, read_stored_upload, run_book_import, sweep_stale_imports,
    workbook_response,
)
from .reports import (  # noqa: F401
    csv_response, inventory_report, report_borrowers, report_circulation, report_condition,
    report_context, report_inventory, report_overdue, report_popular, reports_home, wants_csv,
)
from .dashboard import activity_log_list, analytics, library_home  # noqa: F401
from .notifications import (  # noqa: F401
    notification_list, notification_panel, notification_panel_response,
    notification_read, notification_read_all,
)
from .accounts import (  # noqa: F401
    language_set, login_view, logout_view, profile_view, theme_set,
    user_delete, user_edit, user_list, user_toggle_active,
)
from .secure_accounts import user_add  # noqa: F401,E402
from .organization import (  # noqa: F401
    permissions_matrix, INSTITUTION_TYPE_SUGGESTIONS, branding_settings,
    normalize_website, read_policy_form, validate_institution_fields,
)
