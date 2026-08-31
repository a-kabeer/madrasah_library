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
           Author / Category / Publisher on the Add & Edit Book forms.

           The visible text box only searches; the committed selection lives
           in the sibling hidden input, which is what the form posts. HTMX
           fills the menu from the matching list view, and the "add new"
           option posts to the matching add view, which answers with a
           `comboboxItemCreated` event naming the record to select.
           ========================= */

        (function () {

            var comboboxes = document.querySelectorAll("[data-combobox]");

            if (!comboboxes.length) {
                return;
            }


            function setup(root) {

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
                }


                function clearSelection(focus) {
                    valueInput.value = "";
                    textInput.value = "";
                    committedText = "";

                    menu.innerHTML = "";

                    syncFilled();
                    close();

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

            }


            comboboxes.forEach(setup);

        })();


        /* =========================
           BOOK COVER HOVER PREVIEW

           The book table lives inside a `.table-responsive` wrapper, whose
           `overflow-x: auto` would clip any popover positioned inside a row.
           The preview is therefore a single `position: fixed` element on
           <body>, moved to follow the pointer.
           ========================= */

        (function () {

            var rows = document.querySelectorAll("[data-book-row]");

            if (!rows.length) {
                return;
            }

            /* Skip entirely for touch and narrow screens: there is no hover
               to speak of, and the row thumbnails already show the cover. */
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
                preview.classList.remove("show");
            }


            rows.forEach(function (row) {

                var url = row.getAttribute("data-cover-url");

                /* No cover: no preview at all, which is the graceful case. */
                if (!url) {
                    return;
                }

                row.addEventListener("mouseenter", function (e) {

                    if (image.getAttribute("src") !== url) {
                        image.setAttribute("src", url);
                        image.setAttribute(
                            "alt",
                            "Cover of " + (row.getAttribute("data-cover-title") || "")
                        );
                    }

                    position(e);
                    preview.classList.add("show");
                });

                row.addEventListener("mousemove", position);
                row.addEventListener("mouseleave", hide);
            });


            /* Any scroll or resize invalidates a pointer-anchored position. */
            window.addEventListener("scroll", hide, true);
            window.addEventListener("resize", hide);

        })();

    }
);
