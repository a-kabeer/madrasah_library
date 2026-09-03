document.addEventListener(
    "DOMContentLoaded",
    function () {

        const menuToggle =
            document.getElementById(
                "menuToggle"
            );

        const sidebar =
            document.getElementById(
                "sidebar"
            );

        const sidebarOverlay =
            document.getElementById(
                "sidebarOverlay"
            );


        /* =========================
           MOBILE SIDEBAR
           ========================= */

        function openSidebar() {

            sidebar.classList.add(
                "show"
            );

            sidebarOverlay.classList.add(
                "show"
            );

            if (menuToggle) {
                menuToggle.setAttribute("aria-expanded", "true");
            }

        }


        function closeSidebar() {

            sidebar.classList.remove(
                "show"
            );

            sidebarOverlay.classList.remove(
                "show"
            );

            if (menuToggle) {
                menuToggle.setAttribute("aria-expanded", "false");
            }

        }


        if (menuToggle) {

            menuToggle.addEventListener(
                "click",
                function () {

                    if (sidebar.classList.contains("show")) {
                        closeSidebar();
                    } else {
                        openSidebar();
                    }

                }
            );

        }


        if (sidebarOverlay) {

            sidebarOverlay.addEventListener(
                "click",
                closeSidebar
            );

        }


        /* Close sidebar on Escape key */
        document.addEventListener(
            "keydown",
            function (e) {

                if (e.key === "Escape" && sidebar.classList.contains("show")) {
                    closeSidebar();
                }

            }
        );


        /* =========================
           APPEARANCE: LIGHT / DARK / SYSTEM

           The pre-paint script in base.html has already resolved and applied
           the theme by the time this runs. This section owns changes made
           after load: it applies a new choice, mirrors it to localStorage,
           persists it for signed-in users, and keeps "system" tracking the
           OS while the page stays open.

           Two attributes are in play, and the distinction matters:
             data-bs-theme      the resolved value, only ever light|dark
             data-theme-choice  what the user actually picked, incl. system
           ========================= */

        (function () {

            var THEME_KEY = "madrasah_theme";
            var VALID = ["light", "dark", "system"];

            var root = document.documentElement;
            var themeIcon = document.getElementById("themeIcon");
            var options = document.querySelectorAll("[data-theme-value]");

            var systemQuery = window.matchMedia
                ? window.matchMedia("(prefers-color-scheme: dark)")
                : null;


            function getChoice() {
                var choice = root.getAttribute("data-theme-choice");

                return VALID.indexOf(choice) === -1 ? "system" : choice;
            }


            function resolve(choice) {
                if (choice === "system") {
                    return systemQuery && systemQuery.matches ? "dark" : "light";
                }

                return choice;
            }


            function updateIcon(choice) {
                if (!themeIcon) {
                    return;
                }

                var icon = "bi-circle-half";

                if (choice === "light") {
                    icon = "bi-sun-fill";
                } else if (choice === "dark") {
                    icon = "bi-moon-stars-fill";
                }

                /* Swap only the glyph class, so anything else on the element
                   (sizing, colour utilities) survives. */
                themeIcon.classList.remove(
                    "bi-sun-fill",
                    "bi-moon-stars-fill",
                    "bi-circle-half"
                );
                themeIcon.classList.add(icon);
            }


            function updateOptions(choice) {
                options.forEach(function (option) {
                    var selected = option.getAttribute("data-theme-value") === choice;

                    option.classList.toggle("active", selected);
                    option.setAttribute("aria-checked", selected ? "true" : "false");
                });
            }


            function apply(choice, resolved) {
                root.setAttribute("data-bs-theme", resolved);
                root.setAttribute("data-theme-choice", choice);

                updateIcon(choice);
                updateOptions(choice);

                /* Let anything added later (charts, embeds) react. */
                document.dispatchEvent(
                    new CustomEvent("themechange", {
                        detail: { choice: choice, theme: resolved }
                    })
                );
            }


            function persist(choice) {
                var url = root.getAttribute("data-theme-save-url");

                /* Absent for anonymous visitors — there is no user to save
                   a preference for, so localStorage is the whole story. */
                if (!url) {
                    return;
                }

                var body = new FormData();
                body.append("theme", choice);

                fetch(url, {
                    method: "POST",
                    headers: { "X-CSRFToken": getCookie("csrftoken") },
                    body: body,
                    credentials: "same-origin"
                }).catch(function () {
                    /* Offline or server error: the choice still applies for
                       this browser via localStorage. */
                });
            }


            function getCookie(name) {
                var match = document.cookie.match(
                    new RegExp("(^|; )" + name + "=([^;]*)")
                );

                return match ? decodeURIComponent(match[2]) : "";
            }


            function choose(choice) {
                if (VALID.indexOf(choice) === -1) {
                    return;
                }

                try {
                    localStorage.setItem(THEME_KEY, choice);
                } catch (e) {
                    /* localStorage unavailable; the attributes still apply */
                }

                apply(choice, resolve(choice));
                persist(choice);
            }


            options.forEach(function (option) {
                option.addEventListener("click", function () {
                    choose(option.getAttribute("data-theme-value"));
                });
            });


            /* Keep "system" honest: follow the OS while the page is open. */
            if (systemQuery) {
                var onSystemChange = function () {
                    if (getChoice() === "system") {
                        apply("system", resolve("system"));
                    }
                };

                if (systemQuery.addEventListener) {
                    systemQuery.addEventListener("change", onSystemChange);
                } else if (systemQuery.addListener) {
                    /* Safari < 14 */
                    systemQuery.addListener(onSystemChange);
                }
            }


            /* Sync the icon and menu with whatever the pre-paint script chose. */
            updateIcon(getChoice());
            updateOptions(getChoice());

        })();


        /* =========================
           BRANDING COLOUR FIELDS

           Keeps the native colour picker and its hex text field in step.
           Only the text field is named, so it is the value that posts and
           the field still works without JavaScript.
           ========================= */

        (function () {

            var fields = document.querySelectorAll("[data-color-field]");

            if (!fields.length) {
                return;
            }

            var HEX = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

            fields.forEach(function (field) {

                var picker = field.querySelector("[data-color-picker]");
                var text = field.querySelector("[data-color-text]");

                if (!picker || !text) {
                    return;
                }

                picker.addEventListener("input", function () {
                    text.value = picker.value;
                });

                text.addEventListener("input", function () {
                    /* Ignore half-typed values; the picker cannot hold them. */
                    if (HEX.test(text.value)) {
                        picker.value = text.value;
                    }
                });
            });

        })();


        /* =========================
           ACTIVE SIDEBAR LINK
           ========================= */

        (function () {
            var currentPath = window.location.pathname;
            if (currentPath === "/" || currentPath === "") return;

            var links = document.querySelectorAll(".nav-link-custom");
            var bestLink = null;
            var bestLength = 0;

            links.forEach(
                function (link) {
                    var href = link.getAttribute("href");
                    if (!href) return;

                    /* Normalize: ensure trailing slash for comparison */
                    var normalizedHref = href.endsWith("/") ? href : href + "/";
                    var normalizedPath = currentPath.endsWith("/") ? currentPath : currentPath + "/";

                    /* The dashboard link is a prefix of every other URL, so
                       prefix-matching it would light it up on any page with
                       no nav entry of its own (e.g. the profile page). Such
                       links match exactly instead; the attribute may list
                       extra paths that count as the same page, since the
                       dashboard is routed at both / and /dashboard/. */
                    if (link.hasAttribute("data-nav-exact")) {

                        var accepted = [normalizedHref];

                        (link.getAttribute("data-nav-exact") || "")
                            .split(",")
                            .forEach(function (alias) {
                                var trimmed = alias.trim();

                                if (trimmed) {
                                    accepted.push(
                                        trimmed.endsWith("/") ? trimmed : trimmed + "/"
                                    );
                                }
                            });

                        if (accepted.indexOf(normalizedPath) !== -1) {
                            bestLink = link;
                            bestLength = Infinity;
                        }

                        return;
                    }

                    if (normalizedPath === normalizedHref || normalizedPath.startsWith(normalizedHref)) {
                        if (normalizedHref.length > bestLength) {
                            bestLink = link;
                            bestLength = normalizedHref.length;
                        }
                    }
                }
            );

            if (bestLink) {
                bestLink.classList.add("active");
            }
        })();


        /* =========================
           SEARCHABLE DROPDOWNS (COMBOBOX)
           Author / Category / Publisher on the Add & Edit Book forms, and
           the Author / Category filters on the book list.

           The visible text box only searches; the committed selection lives
           in the sibling hidden input, which is what the form posts. HTMX
           fills the menu from the matching list view, and the "add new"
           option posts to the matching add view, which answers with a
           `comboboxItemCreated` event naming the record to select.
           ========================= */

        (function () {

            function setup(root) {

                /* Set up once per element. The book list re-renders its
                   filter card when the browsing mode changes, so `setup`
                   runs again over whatever HTMX brought in — without this
                   marker the dropdowns already on the page would collect a
                   second set of listeners each time. */
                if (root.hasAttribute("data-combobox-ready")) {
                    return;
                }

                root.setAttribute("data-combobox-ready", "");

                var valueInput = root.querySelector("[data-combobox-value]");
                var textInput = root.querySelector("[data-combobox-input]");
                var menu = root.querySelector("[data-combobox-menu]");
                var clearButton = root.querySelector("[data-combobox-clear]");

                if (!valueInput || !textInput || !menu) {
                    return;
                }

                /* The label of the committed selection, so we can restore it
                   if the user types a partial search and then walks away. */
                var committedText = textInput.value;

                /* Opt-in: when set, choosing an option submits the enclosing
                   form, which is how the book list filters without an Apply
                   button. Absent on the Add/Edit Book forms, so those only
                   fill the input. Submitting the real form (rather than a
                   URL assembled here) means the filter also serialises
                   whatever else the user has changed on the form. */
                var submitOnSelect = root.hasAttribute("data-combobox-submit");


                function isOpen() {
                    return root.classList.contains("combobox-open");
                }


                /* Drives whether the inline "clear" button is offered. */
                function syncFilled() {
                    root.classList.toggle(
                        "combobox-filled",
                        textInput.value.length > 0
                    );
                }


                function open() {
                    root.classList.add("combobox-open");
                    textInput.setAttribute("aria-expanded", "true");
                }


                function close() {
                    root.classList.remove("combobox-open");
                    textInput.setAttribute("aria-expanded", "false");
                    clearActive();
                }


                function options() {
                    return Array.prototype.slice.call(
                        menu.querySelectorAll("[data-combobox-option]")
                    );
                }


                function clearActive() {
                    options().forEach(function (option) {
                        option.classList.remove("combobox-active");
                        option.setAttribute("aria-selected", "false");
                    });
                }


                function setActive(option) {
                    clearActive();

                    if (!option) {
                        return;
                    }

                    option.classList.add("combobox-active");
                    option.setAttribute("aria-selected", "true");

                    /* Keep the highlighted row inside the scroll area. */
                    if (option.scrollIntoView) {
                        option.scrollIntoView({ block: "nearest" });
                    }
                }


                function activeOption() {
                    return menu.querySelector(".combobox-active");
                }


                function moveActive(step) {

                    var all = options();

                    if (!all.length) {
                        return;
                    }

                    var current = all.indexOf(activeOption());
                    var next = current + step;

                    if (next < 0) {
                        next = all.length - 1;
                    } else if (next >= all.length) {
                        next = 0;
                    }

                    setActive(all[next]);
                }


                /* Commit a selection: what the form will actually post. */
                function select(id, name) {
                    valueInput.value = id;
                    textInput.value = name;
                    committedText = name;

                    root.classList.remove("combobox-invalid");

                    syncFilled();
                    close();

                    submitForm();
                }


                /* Hand the selection to the server. requestSubmit() rather
                   than submit(), so the required-field guard below still
                   runs — submit() would bypass it. */
                function submitForm() {

                    if (!submitOnSelect) {
                        return;
                    }

                    var form = root.closest("form");

                    if (!form) {
                        return;
                    }

                    if (form.requestSubmit) {
                        form.requestSubmit();
                    } else {
                        form.submit();
                    }
                }


                function clearSelection(focus) {
                    valueInput.value = "";
                    textInput.value = "";
                    committedText = "";

                    menu.innerHTML = "";

                    syncFilled();
                    close();

                    /* Clearing is a filter change too: submit so the list
                       goes back to showing everything. */
                    if (submitOnSelect) {
                        submitForm();
                        return;
                    }

                    if (focus) {
                        textInput.focus();
                    }
                }


                /* Set when we hand focus back programmatically after a
                   selection, so the menu doesn't spring open again. */
                var skipNextFocusOpen = false;

                textInput.addEventListener("focus", function () {

                    if (skipNextFocusOpen) {
                        skipNextFocusOpen = false;
                        return;
                    }

                    open();
                });

                textInput.addEventListener("input", function () {

                    /* Typing abandons the previous selection: the hidden
                       value must never disagree with what is on screen. */
                    valueInput.value = "";

                    syncFilled();
                    open();
                });


                textInput.addEventListener("keydown", function (e) {

                    if (e.key === "ArrowDown") {
                        e.preventDefault();

                        if (!isOpen()) {
                            open();
                        }

                        moveActive(1);
                        return;
                    }

                    if (e.key === "ArrowUp") {
                        e.preventDefault();
                        moveActive(-1);
                        return;
                    }

                    if (e.key === "Enter") {

                        var active = activeOption();

                        /* Only swallow Enter when it is acting on the menu,
                           so the form still submits normally otherwise. */
                        if (isOpen() && active) {
                            e.preventDefault();
                            active.click();
                        }

                        return;
                    }

                    if (e.key === "Escape") {

                        if (isOpen()) {
                            e.stopPropagation();
                            textInput.value = committedText;

                            syncFilled();
                            close();
                        }

                        return;
                    }

                    if (e.key === "Tab") {
                        close();
                    }

                });


                /* Clicking an option. Delegated, because HTMX replaces the
                   menu contents on every search. */
                menu.addEventListener("click", function (e) {

                    var option = e.target.closest("[data-combobox-option]");

                    if (!option || option.hasAttribute("data-combobox-create")) {
                        return;
                    }

                    select(
                        option.getAttribute("data-id"),
                        option.getAttribute("data-name")
                    );
                });


                menu.addEventListener("mousemove", function (e) {

                    var option = e.target.closest("[data-combobox-option]");

                    if (option) {
                        setActive(option);
                    }
                });


                /* The "add new" option's HTMX request succeeded; the response
                   named the record to select (freshly created, or the
                   existing one that matched the typed name). */
                root.addEventListener("comboboxItemCreated", function (e) {

                    var detail = e.detail || {};

                    if (!detail.id) {
                        return;
                    }

                    select(String(detail.id), detail.name);

                    skipNextFocusOpen = true;
                    textInput.focus();
                });


                if (clearButton) {
                    clearButton.addEventListener("click", function () {
                        clearSelection(true);
                    });
                }


                /* Clicking away discards an uncommitted search term rather
                   than leaving stale text above an unrelated hidden value. */
                document.addEventListener("click", function (e) {

                    if (root.contains(e.target)) {
                        return;
                    }

                    if (isOpen()) {
                        textInput.value = committedText;

                        syncFilled();
                        close();
                    }
                });


                /* A hidden input can't use HTML5 `required`, so enforce the
                   mandatory fields here. The view validates independently. */
                var form = root.closest("form");

                if (form && root.hasAttribute("data-combobox-required")) {

                    form.addEventListener("submit", function (e) {

                        if (valueInput.value) {
                            root.classList.remove("combobox-invalid");
                            return;
                        }

                        e.preventDefault();

                        root.classList.add("combobox-invalid");
                        textInput.focus();
                    });
                }


                syncFilled();

                /* Selection now submits on its own, so the manual Apply
                   button is redundant. It is only there for the case where
                   this script never ran. */
                if (submitOnSelect) {

                    var owningForm = root.closest("form");
                    var apply = owningForm
                        && owningForm.querySelector("[data-filter-apply]");

                    if (apply) {
                        apply.hidden = true;
                    }
                }

            }


            function setupAll(scope) {

                if (scope.matches && scope.matches("[data-combobox]")) {
                    setup(scope);
                }

                if (scope.querySelectorAll) {
                    scope.querySelectorAll("[data-combobox]").forEach(setup);
                }
            }


            setupAll(document);

            /* Wires up a dropdown that arrived in a swap — the book list
               re-renders its filter card whenever the browsing mode
               changes. `htmx:afterSwap` rather than `htmx:load`, which does
               not reach here in htmx 2.0.8; and the whole document rather
               than the swapped node, because that is the same walk and
               costs one attribute check per dropdown already set up. */
            document.body.addEventListener("htmx:afterSwap", function () {
                setupAll(document);
            });

        })();


        /* =========================
           BOOK COVER HOVER PREVIEW

           The book table lives inside a `.table-responsive` wrapper, whose
           `overflow-x: auto` would clip any popover positioned inside a row.
           The preview is therefore a single `position: fixed` element on
           <body>, moved to follow the pointer.

           Delegated from the document, like the row clicks above, because
           the table is re-rendered in place whenever the search, a filter,
           the sorting or the page changes.
           ========================= */

        (function () {

            /* Nothing to preview here: no cover on the page and no results
               container that a swap could bring one into. Checked before the
               document-wide `mousemove` below is attached, so pages with no
               covers at all pay nothing for this. */
            if (!document.querySelector("[data-cover-url], #bookResults")) {
                return;
            }

            /* Skip entirely for touch and narrow screens: there is no hover
               to speak of. The cover is still reachable there by tapping the
               row, which opens the details modal. */
            var canHover = window.matchMedia(
                "(hover: hover) and (min-width: 768px)"
            );

            if (!canHover.matches) {
                return;
            }

            var preview = document.createElement("div");
            preview.className = "cover-preview";
            preview.setAttribute("aria-hidden", "true");

            var image = document.createElement("img");
            preview.appendChild(image);

            document.body.appendChild(preview);

            var GAP = 16;

            /* Which element the preview is currently following, so a move
               inside it repositions but a move into a different row starts
               over with the right cover. */
            var current = null;


            function position(e) {

                var width = preview.offsetWidth;
                var height = preview.offsetHeight;

                var left = e.clientX + GAP;
                var top = e.clientY + GAP;

                /* Flip to the other side of the pointer rather than letting
                   the preview run off screen. */
                if (left + width > window.innerWidth) {
                    left = e.clientX - width - GAP;
                }

                if (top + height > window.innerHeight) {
                    top = e.clientY - height - GAP;
                }

                preview.style.left = Math.max(GAP, left) + "px";
                preview.style.top = Math.max(GAP, top) + "px";
            }


            function hide() {
                current = null;
                preview.classList.remove("show");
            }


            function show(source, e) {

                var url = source.getAttribute("data-cover-url");

                /* No cover: no preview at all, which is the graceful case.
                   Rows without one carry no attribute and never match. */
                if (!url) {
                    return;
                }

                if (image.getAttribute("src") !== url) {
                    image.setAttribute("src", url);
                    image.setAttribute(
                        "alt",
                        "Cover of " + (source.getAttribute("data-cover-title") || "")
                    );
                }

                current = source;

                position(e);
                preview.classList.add("show");
            }


            /* `mousemove` rather than `mouseover`, so the preview both
               appears and tracks the pointer from one listener. */
            document.addEventListener("mousemove", function (e) {

                var source = e.target && e.target.closest
                    ? e.target.closest("[data-cover-url]")
                    : null;

                if (!source) {

                    if (current) {
                        hide();
                    }

                    return;
                }

                if (source !== current) {
                    show(source, e);
                    return;
                }

                position(e);
            });


            /* Any scroll or resize invalidates a pointer-anchored position. */
            window.addEventListener("scroll", hide, true);
            window.addEventListener("resize", hide);

            /* Clicking a row opens a dialog without the pointer moving, so
               nothing else would hide the preview and it would hang around
               over the page. Hide it explicitly. */
            document.addEventListener("click", hide, true);

            var sharedModal = document.getElementById("globalModal");

            if (sharedModal) {
                sharedModal.addEventListener("show.bs.modal", hide);
            }

        })();


        /* =========================
           CLICKABLE ROWS

           The whole row opens that record, so a click on the title, on a
           count, or on empty space all do the same thing. Three lists use
           it: books and copies name a URL to pull into the shared dialog,
           while locations and shelves name one to navigate to — those are
           whole pages, not popups.

           Delegated from the document rather than bound per row: both
           lists re-render their table in place when the search, a filter,
           the sorting or the page changes, and rows bound at load would
           come back dead after the first of those.

           The action buttons are excluded: they sit inside
           [data-row-actions] and must keep doing their own job. On the
           copy list that also covers the selection checkbox.
           ========================= */

        (function () {

            var modalEl = document.getElementById("globalModal");

            if (!window.bootstrap || !window.htmx) {
                return;
            }

            var body = modalEl ? modalEl.querySelector(".modal-body") : null;

            var placeholder =
                '<div class="text-center text-body-secondary py-4">' +
                '<span class="spinner-border spinner-border-sm" role="status"></span>' +
                '<span class="visually-hidden">Loading</span>' +
                "</div>";


            function open(row) {

                /* A plain page to go to: locations and shelves are places
                   to be in, not things to glance at in a dialog. */
                var page = row.getAttribute("data-row-url");

                if (page) {
                    window.location.assign(page);
                    return;
                }

                var url = row.getAttribute("data-book-url")
                    || row.getAttribute("data-copy-url");

                if (!url || !modalEl) {
                    return;
                }

                /* Clear, then fetch, then show. Bootstrap's own data-api
                   would open the dialog before the request landed, and
                   clearing from `show.bs.modal` instead would race the
                   reply and sometimes wipe it. */
                modalEl.dataset.modalWanted = "1";

                if (body) {
                    body.innerHTML = placeholder;
                }

                var label = document.getElementById("globalModalLabel");

                if (label) {
                    label.textContent = "Loading…";
                }

                window.htmx.ajax("GET", url, { target: body, swap: "innerHTML" });

                window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
            }


            /* The row this event happened in, or null if it happened on
               something that is its own control — a link, a button, or the
               Edit/Delete group — which keeps its own behaviour. */
            function rowFor(target) {

                if (!target || !target.closest) {
                    return null;
                }

                var row = target.closest(
                    "[data-book-row][data-book-url],"
                    + "[data-copy-row][data-copy-url],"
                    + "[data-row-url]"
                );

                if (!row) {
                    return null;
                }

                if (
                    target.closest("[data-row-actions]")
                    || target.closest("a")
                    || target.closest("button")
                ) {
                    return null;
                }

                return row;
            }


            document.addEventListener("click", function (e) {

                var row = rowFor(e.target);

                if (row) {
                    open(row);
                }

            });


            /* Rows carry role="button" and tabindex, so honour the keys a
               button would. */
            document.addEventListener("keydown", function (e) {

                if (e.key !== "Enter" && e.key !== " ") {
                    return;
                }

                var row = rowFor(e.target);

                if (!row) {
                    return;
                }

                e.preventDefault();
                open(row);
            });

        })();


        /* =========================
           FORM DIALOG (ADD / EDIT / DELETE BOOK)

           The list's Add, Edit and Delete controls are real links to real
           pages; these handlers intercept them so the work happens in
           #formModal instead, and the list keeps its search, filters,
           sorting and page.

           Delegated from the document, because the Edit and Delete buttons
           live inside the results table, which the list re-renders in
           place whenever anything changes.
           ========================= */

        (function () {

            var modalEl = document.getElementById("formModal");

            if (!modalEl || !window.bootstrap || !window.htmx) {
                return;
            }

            var body = modalEl.querySelector(".modal-body");
            var label = document.getElementById("formModalLabel");

            var placeholder =
                '<div class="text-center text-body-secondary py-5">' +
                '<span class="spinner-border spinner-border-sm" role="status"></span>' +
                '<span class="visually-hidden">Loading</span>' +
                "</div>";


            /* Tidy-up after a dialog closes — but only if it is still
               closed. Bootstrap fires `hidden.bs.modal` from a transition
               callback, which here arrives a second or more after the
               dismissal: long enough for the next dialog to have been
               opened and filled, and clearing it then would leave a
               spinner where the form should be.

               The check is our own flag rather than Bootstrap's `show`
               class, because that class is added on the same stretched
               timetable and is not reliably set yet. Every path that opens
               the dialog clears it explicitly first, so skipping this is
               always safe. */
            function reset() {

                if (modalEl.dataset.modalWanted === "1") {
                    return;
                }

                body.innerHTML = placeholder;

                if (label) {
                    label.textContent = "Loading…";
                }
            }


            /* HTMX does the fetching (the trigger element carries hx-get),
               so this only has to open the dialog. Opening it on click
               rather than on the response keeps the spinner visible while
               the fragment is on its way. */
            document.addEventListener("click", function (e) {

                if (!e.target || !e.target.closest) {
                    return;
                }

                var trigger = e.target.closest("[data-form-modal]");

                if (!trigger) {
                    return;
                }

                /* Let a modified click do what the browser would: these are
                   genuine links to genuine pages. */
                if (
                    e.metaKey || e.ctrlKey || e.shiftKey || e.altKey
                    || e.button !== 0
                ) {
                    return;
                }

                e.preventDefault();

                /* Cleared here, before the dialog opens, rather than from
                   `show.bs.modal`. HTMX starts fetching the moment the
                   click lands, and on a fast reply the fragment can arrive
                   before that event fires — a reset there would then wipe
                   the content it was meant to be waiting for. */
                modalEl.dataset.modalWanted = "1";
                reset();

                window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
            });


            /* Dismissal is known immediately; the matching `hidden` may be
               a long time coming. */
            modalEl.addEventListener("hide.bs.modal", function () {
                modalEl.dataset.modalWanted = "";
            });

            modalEl.addEventListener("hidden.bs.modal", reset);

        })();


        /* =========================
           GUIDED ADD BOOK

           One form, revealed a step at a time. There is no wizard on the
           server: the whole thing posts once, so a book, its volumes and
           its copies are created together or not at all.

           Everything here is an enhancement. With the script absent the
           steps are all on screen at once and the form still saves — which
           is why the step buttons and the indicator start hidden in the
           markup and are shown from here.
           ========================= */

        (function () {

            function setup(form) {

                if (form.hasAttribute("data-wizard-ready")) {
                    return;
                }

                form.setAttribute("data-wizard-ready", "");

                var steps = Array.prototype.slice.call(
                    form.querySelectorAll("[data-book-step]")
                );

                if (!steps.length) {
                    return;
                }

                var stepList = form.querySelector("[data-step-list]");
                var backButton = form.querySelector("[data-step-back]");
                var nextButton = form.querySelector("[data-step-next]");
                var submitButton = form.querySelector("[data-step-submit]");

                var volumeRows = form.querySelector("[data-volume-rows]");
                var quantityRows = form.querySelector("[data-copy-quantities]");
                var manualCodes = form.querySelector("[data-manual-codes]");

                var current = 0;

                if (stepList) {
                    stepList.hidden = false;
                }


                function multipleVolumes() {
                    var choice = form.querySelector(
                        "[data-volume-mode]:checked"
                    );

                    return !!choice && choice.value === "multiple";
                }


                function addingCopies() {
                    var choice = form.querySelector(
                        "[data-copies-mode]:checked"
                    );

                    return !!choice && choice.value === "add";
                }


                function manualCodeMode() {
                    var choice = form.querySelector("[data-code-mode]:checked");

                    return !!choice && choice.value === "manual";
                }


                /* Step 4 only exists when copies are being added: with none
                   to place, asking where to put them is a dead end. */
                function visibleSteps() {
                    return steps.filter(function (step) {
                        return step.getAttribute("data-book-step") !== "4"
                            || addingCopies();
                    });
                }


                function render() {

                    var shown = visibleSteps();

                    if (current >= shown.length) {
                        current = shown.length - 1;
                    }

                    steps.forEach(function (step) {
                        step.hidden = step !== shown[current];
                    });

                    if (stepList) {
                        Array.prototype.forEach.call(
                            stepList.children,
                            function (item) {
                                var number = item.getAttribute("data-step-for");
                                var step = form.querySelector(
                                    '[data-book-step="' + number + '"]'
                                );

                                item.hidden = shown.indexOf(step) === -1;
                                item.classList.toggle(
                                    "current",
                                    step === shown[current]
                                );
                            }
                        );
                    }

                    var last = current === shown.length - 1;

                    if (backButton) {
                        backButton.hidden = current === 0;
                    }

                    if (nextButton) {
                        nextButton.hidden = last;
                    }

                    if (submitButton) {
                        submitButton.hidden = !last;
                    }
                }


                function newVolumeRow(number) {
                    var row = document.createElement("div");

                    row.className = "row g-2 align-items-end mb-2";
                    row.setAttribute("data-volume-row", "");

                    row.innerHTML =
                        '<div class="col-4 col-sm-3">' +
                        '<label class="form-label small mb-1">Volume no.</label>' +
                        '<input type="number" min="1" class="form-control form-control-sm"' +
                        ' name="volume_number" value="' + number + '">' +
                        "</div>" +
                        '<div class="col">' +
                        '<label class="form-label small mb-1">Volume title (optional)</label>' +
                        '<input type="text" class="form-control form-control-sm"' +
                        ' name="volume_title">' +
                        "</div>" +
                        '<div class="col-auto">' +
                        '<button type="button" class="btn btn-sm btn-outline-danger"' +
                        ' data-remove-volume aria-label="Remove this volume">' +
                        '<i class="bi bi-x-lg" aria-hidden="true"></i>' +
                        "</button>" +
                        "</div>";

                    return row;
                }


                function volumeRowCount() {
                    return volumeRows
                        ? volumeRows.querySelectorAll("[data-volume-row]").length
                        : 0;
                }


                function addVolume() {
                    if (!volumeRows) {
                        return;
                    }

                    volumeRows.appendChild(
                        newVolumeRow(volumeRowCount() + 1)
                    );

                    syncQuantities();
                }


                /* The copy counts are rebuilt from the volume rows rather
                   than kept in step by hand, so the two lists are always
                   the same length and in the same order — which is how the
                   server pairs them. */
                function syncQuantities() {

                    if (!quantityRows) {
                        return;
                    }

                    var existing = {};

                    Array.prototype.forEach.call(
                        quantityRows.querySelectorAll('[name="copy_qty"]'),
                        function (input, index) {
                            existing[index] = input.value;
                        }
                    );

                    if (!multipleVolumes()) {

                        quantityRows.innerHTML =
                            '<div class="row g-2 align-items-center" data-copy-qty-row>' +
                            '<div class="col"><label class="form-label small mb-0">' +
                            "Number of copies</label></div>" +
                            '<div class="col-4 col-sm-3">' +
                            '<input type="number" min="0" class="form-control form-control-sm"' +
                            ' name="copy_qty" value="' +
                            (existing[0] || "1") +
                            '" aria-label="Number of copies">' +
                            "</div></div>";

                        syncManualCodes();
                        return;
                    }

                    var rows = volumeRows
                        ? volumeRows.querySelectorAll("[data-volume-row]")
                        : [];

                    var html = "";

                    Array.prototype.forEach.call(rows, function (row, index) {

                        var number = row.querySelector('[name="volume_number"]');
                        var title = row.querySelector('[name="volume_title"]');

                        var name = "Volume " + (
                            number && number.value ? number.value : index + 1
                        );

                        if (title && title.value) {
                            name += " — " + title.value;
                        }

                        html +=
                            '<div class="row g-2 align-items-center mb-2" data-copy-qty-row>' +
                            '<div class="col"><span class="small text-body-secondary">' +
                            escapeHtml(name) +
                            "</span></div>" +
                            '<div class="col-4 col-sm-3">' +
                            '<input type="number" min="0" class="form-control form-control-sm"' +
                            ' name="copy_qty" value="' +
                            (existing[index] || "0") +
                            '" aria-label="Number of copies">' +
                            "</div></div>";
                    });

                    quantityRows.innerHTML = html;

                    syncManualCodes();
                }


                /* One box per copy, in the same order the server will
                   create them, so a typed-in label lands on the copy the
                   librarian meant. */
                function syncManualCodes() {

                    if (!manualCodes) {
                        return;
                    }

                    if (!manualCodeMode()) {
                        manualCodes.innerHTML = "";
                        return;
                    }

                    var total = 0;

                    Array.prototype.forEach.call(
                        form.querySelectorAll('[name="copy_qty"]'),
                        function (input) {
                            total += parseInt(input.value, 10) || 0;
                        }
                    );

                    var kept = Array.prototype.map.call(
                        manualCodes.querySelectorAll('[name="copy_code"]'),
                        function (input) {
                            return input.value;
                        }
                    );

                    var html = "";

                    for (var index = 0; index < total; index += 1) {
                        html +=
                            '<input type="text" class="form-control form-control-sm mb-2"' +
                            ' name="copy_code" placeholder="Copy code ' +
                            (index + 1) +
                            '" aria-label="Copy code ' + (index + 1) + '"' +
                            ' value="' + escapeHtml(kept[index] || "") + '">';
                    }

                    manualCodes.innerHTML = html;
                }


                function escapeHtml(value) {
                    var holder = document.createElement("div");

                    holder.textContent = value;

                    return holder.innerHTML.replace(/"/g, "&quot;");
                }


                function toggleNotes() {

                    var singleNote = form.querySelector("[data-volume-single-note]");
                    var skipNote = form.querySelector("[data-copies-skip-note]");
                    var copiesPanel = form.querySelector("[data-copies-panel]");
                    var autoNote = form.querySelector("[data-code-auto-note]");
                    var addVolumeButton = form.querySelector("[data-add-volume]");

                    if (singleNote) {
                        singleNote.hidden = multipleVolumes();
                    }

                    if (volumeRows) {
                        volumeRows.hidden = !multipleVolumes();
                    }

                    if (addVolumeButton) {
                        addVolumeButton.hidden = !multipleVolumes();
                    }

                    if (skipNote) {
                        skipNote.hidden = addingCopies();
                    }

                    if (copiesPanel) {
                        copiesPanel.hidden = !addingCopies();
                    }

                    if (autoNote) {
                        autoNote.hidden = manualCodeMode();
                    }
                }


                form.addEventListener("click", function (e) {

                    if (e.target.closest("[data-add-volume]")) {
                        e.preventDefault();
                        addVolume();
                        return;
                    }

                    if (e.target.closest("[data-remove-volume]")) {
                        e.preventDefault();

                        var row = e.target.closest("[data-volume-row]");

                        if (row) {
                            row.remove();
                            syncQuantities();
                        }

                        return;
                    }

                    if (e.target.closest("[data-step-next]")) {
                        e.preventDefault();
                        current += 1;
                        render();
                        return;
                    }

                    if (e.target.closest("[data-step-back]")) {
                        e.preventDefault();
                        current -= 1;
                        render();
                    }
                });


                form.addEventListener("change", function (e) {

                    if (e.target.matches("[data-volume-mode]")) {

                        /* Switching to multiple with nothing there yet
                           gives them a row to type in rather than an empty
                           panel. */
                        if (multipleVolumes() && !volumeRowCount()) {
                            addVolume();
                        }

                        toggleNotes();
                        syncQuantities();
                        render();
                        return;
                    }

                    if (
                        e.target.matches("[data-copies-mode]")
                        || e.target.matches("[data-code-mode]")
                    ) {
                        toggleNotes();
                        syncManualCodes();
                        render();
                        return;
                    }

                    if (e.target.matches('[name="copy_qty"]')) {
                        syncManualCodes();
                        return;
                    }

                    if (
                        e.target.matches('[name="volume_number"]')
                        || e.target.matches('[name="volume_title"]')
                    ) {
                        syncQuantities();
                    }
                });


                toggleNotes();
                syncQuantities();
                render();
            }


            function setupAll() {
                Array.prototype.forEach.call(
                    document.querySelectorAll("[data-book-wizard]"),
                    setup
                );
            }


            setupAll();

            /* The dialog's form arrives in a swap, and comes back as a new
               element every time validation rejects it. */
            document.body.addEventListener("htmx:afterSwap", setupAll);

        })();


        /* =========================
           QUICK ADD (LOCATION / SHELF)

           Small inline boxes in the Add Book dialog. HTMX posts to the
           existing location_add / shelf_add views and swaps back the
           refreshed option list with the new record selected; this closes
           the box once that happened, which the `quickAddDone` event says.
           ========================= */

        (function () {

            document.addEventListener("click", function (e) {

                if (!e.target || !e.target.closest) {
                    return;
                }

                var toggle = e.target.closest("[data-quick-add-toggle]");

                if (!toggle) {
                    return;
                }

                e.preventDefault();

                var which = toggle.getAttribute("data-quick-add-toggle");
                var box = document.querySelector(
                    '[data-quick-add="' + which + '"]'
                );

                if (!box) {
                    return;
                }

                box.hidden = !box.hidden;

                if (!box.hidden) {
                    var input = box.querySelector("[data-quick-add-name]");

                    if (input) {
                        input.focus();
                    }
                }
            });


            function errorBox(entity) {
                var box = document.querySelector(
                    '[data-quick-add="' + entity + '"]'
                );

                return box
                    ? box.querySelector("[data-quick-add-error]")
                    : null;
            }


            /* Nothing was created — say why and leave the box open, since
               the response itself can only carry <option> elements. */
            document.body.addEventListener("quickAddFailed", function (e) {

                var detail = (e.detail && e.detail.value) || e.detail || {};
                var target = errorBox(detail.entity);

                if (target) {
                    target.textContent = detail.message || "";
                }
            });


            document.body.addEventListener("quickAddDone", function (e) {

                var detail = (e.detail && e.detail.value) || e.detail || {};

                Array.prototype.forEach.call(
                    document.querySelectorAll("[data-quick-add]"),
                    function (box) {
                        box.hidden = true;

                        var input = box.querySelector("[data-quick-add-name]");

                        if (input) {
                            input.value = "";
                        }

                        var problem = box.querySelector("[data-quick-add-error]");

                        if (problem) {
                            problem.textContent = "";
                        }
                    }
                );

                /* A new location means the Shelf list belongs to the
                   wrong one, so ask it to reload. Not after a new shelf:
                   that response already is the refreshed list, with the
                   new shelf selected, and reloading would drop it. */
                if (detail.entity !== "location") {
                    return;
                }

                var location = document.querySelector("[data-location-select]");

                if (location && window.htmx) {
                    window.htmx.trigger(location, "change");
                }
            });

        })();


        /* =========================
           TOASTS

           The dialogs answer a successful save with "no content" and an
           event, so there is no page load for a Django message to arrive
           on. These turn the event into a confirmation instead, and ask the
           book list to redraw itself.

           `window.showToast` is published here so the copy list's move
           confirmations use the same one.
           ========================= */

        (function () {

            var area = document.getElementById("toastArea");

            if (!area || !window.bootstrap) {
                return;
            }


            /* Published, so anything that needs to confirm something can
               use the one implementation rather than its own. */
            window.showToast = function (message, tone) {

                var toast = document.createElement("div");

                toast.className =
                    "toast align-items-center text-bg-" + (tone || "success")
                    + " border-0";
                toast.setAttribute("role", "status");
                toast.setAttribute("aria-live", "polite");
                toast.setAttribute("aria-atomic", "true");

                var holder = document.createElement("div");
                holder.className = "d-flex";

                var text = document.createElement("div");
                text.className = "toast-body";
                text.textContent = message;

                var close = document.createElement("button");
                close.type = "button";
                close.className = "btn-close btn-close-white me-2 m-auto";
                close.setAttribute("data-bs-dismiss", "toast");
                close.setAttribute("aria-label", "Close");

                holder.appendChild(text);
                holder.appendChild(close);
                toast.appendChild(holder);
                area.appendChild(toast);

                toast.addEventListener("hidden.bs.toast", function () {
                    toast.remove();
                });

                window.bootstrap.Toast.getOrCreateInstance(toast, {
                    delay: 5000
                }).show();
            };


            function closeFormModal() {

                var modalEl = document.getElementById("formModal");

                if (!modalEl) {
                    return;
                }

                var instance = window.bootstrap.Modal.getInstance(modalEl);

                if (instance) {
                    instance.hide();
                }
            }


            function refreshList() {
                /* #bookListRefresh listens for this and re-requests the
                   results for whatever the list is currently showing. */
                document.body.dispatchEvent(
                    new CustomEvent("bookListChanged", { bubbles: true })
                );
            }


            document.body.addEventListener("bookSaved", function (e) {

                var detail = (e.detail && e.detail.value) || e.detail || {};

                var message = detail.title
                    ? '"' + detail.title + '" saved'
                    : "Book saved";

                if (detail.copies) {
                    message += " with " + detail.copies + " cop"
                        + (detail.copies === 1 ? "y" : "ies");
                }

                closeFormModal();
                refreshList();
                window.showToast(message + ".");
            });


            document.body.addEventListener("bookDeleted", function (e) {

                var detail = (e.detail && e.detail.value) || e.detail || {};

                closeFormModal();
                refreshList();
                window.showToast(
                    (detail.title ? '"' + detail.title + '"' : "The book")
                    + " was deleted.",
                    "danger"
                );
            });

        })();


        /* =========================
           SELECTING COPIES TO MOVE

           The move bar is hidden until something is ticked, so the copy
           list is not carrying a form the librarian has no use for yet.

           Delegated, because the table is re-rendered in place — and the
           tick boxes go with it, which is deliberate: a selection that
           survived a change of filter would move copies that are no longer
           on screen.
           ========================= */

        (function () {

            var form = document.getElementById("copyMoveForm");

            if (!form) {
                return;
            }

            var counter = form.querySelector("[data-copy-selected-count]");


            function checkboxes() {
                return Array.prototype.slice.call(
                    document.querySelectorAll("[data-copy-checkbox]")
                );
            }


            function selected() {
                return checkboxes().filter(function (box) {
                    return box.checked;
                });
            }


            function render() {

                var count = selected().length;

                form.hidden = count === 0;

                if (counter) {
                    counter.textContent = count;
                }

                var all = document.querySelector("[data-copy-select-all]");

                if (all) {
                    var boxes = checkboxes();

                    all.checked = boxes.length > 0 && count === boxes.length;
                    all.indeterminate = count > 0 && count < boxes.length;
                }
            }


            document.addEventListener("change", function (e) {

                if (e.target.matches("[data-copy-select-all]")) {

                    checkboxes().forEach(function (box) {
                        box.checked = e.target.checked;
                    });

                    render();
                    return;
                }

                if (e.target.matches("[data-copy-checkbox]")) {
                    render();
                }
            });


            document.addEventListener("click", function (e) {

                if (!e.target || !e.target.closest) {
                    return;
                }

                if (!e.target.closest("[data-copy-clear-selection]")) {
                    return;
                }

                e.preventDefault();

                checkboxes().forEach(function (box) {
                    box.checked = false;
                });

                render();
            });


            /* A swap brings a fresh, unticked table with it. */
            document.body.addEventListener("htmx:afterSwap", render);

            render();

        })();


        /* =========================
           COPY MOVE CONFIRMATIONS

           The move is answered with "no content" and an event, so there is
           no page load for a Django message to arrive on. These turn the
           event into a confirmation and ask the copy list to redraw.
           ========================= */

        (function () {

            var area = document.getElementById("toastArea");

            if (!area || !window.bootstrap) {
                return;
            }


            function refreshCopies() {
                document.body.dispatchEvent(
                    new CustomEvent("copyListChanged", { bubbles: true })
                );
            }


            document.body.addEventListener("copiesMoved", function (e) {

                var detail = (e.detail && e.detail.value) || e.detail || {};
                var count = detail.count || 0;

                refreshCopies();

                if (!count) {
                    window.showToast(
                        "Those copies are already on that shelf.",
                        "secondary"
                    );
                    return;
                }

                window.showToast(
                    count + " cop" + (count === 1 ? "y" : "ies")
                    + " moved to " + detail.shelf + "."
                );
            });


            document.body.addEventListener("copiesMoveFailed", function (e) {

                var detail = (e.detail && e.detail.value) || e.detail || {};

                window.showToast(
                    detail.message || "Those copies could not be moved.",
                    "danger"
                );
            });

        })();


        /* =========================
           SHARED MODAL

           #globalModal is filled by HTMX (currently the book list's details
           popup). Reset it on close so opening a different record never
           flashes the previous one while the new fetch is in flight.
           ========================= */

        (function () {

            var modal = document.getElementById("globalModal");

            if (!modal) {
                return;
            }

            var body = modal.querySelector(".modal-body");

            if (!body) {
                return;
            }

            var placeholder =
                '<div class="text-center text-body-secondary py-4">' +
                '<span class="spinner-border spinner-border-sm" role="status"></span>' +
                '<span class="visually-hidden">Loading</span>' +
                "</div>";

            /* The title is swapped in by the response, so it has to be
               cleared too — otherwise opening a second record briefly shows
               the first one's name above a loading spinner.

               Guarded like the form dialog's, and for the same reason:
               `hidden.bs.modal` comes from a transition callback and can
               land after the dialog has been reopened, so it must not
               clear what is now on screen. */
            function reset() {

                if (modal.dataset.modalWanted === "1") {
                    return;
                }

                body.innerHTML = placeholder;

                var label = document.getElementById("globalModalLabel");

                if (label) {
                    label.textContent = "Loading…";
                }
            }

            /* Only on close. Whatever opens this dialog clears it first,
               just before it starts fetching, because a reset driven by
               `show.bs.modal` races the reply and can erase it. */
            modal.addEventListener("hide.bs.modal", function () {
                modal.dataset.modalWanted = "";
            });

            modal.addEventListener("hidden.bs.modal", reset);

        })();

    }
);
