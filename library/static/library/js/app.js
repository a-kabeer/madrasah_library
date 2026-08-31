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
           THEME TOGGLE
           ========================= */

        var themeToggle = document.getElementById("themeToggle");
        var themeIcon = document.getElementById("themeIcon");

        function getTheme() {
            return document.documentElement.getAttribute("data-bs-theme") || "light";
        }

        function setTheme(theme) {
            document.documentElement.setAttribute("data-bs-theme", theme);
            try {
                localStorage.setItem("madrasah_theme", theme);
            } catch (e) {
                /* localStorage unavailable */
            }
            updateIcon(theme);
        }

        function updateIcon(theme) {
            if (!themeIcon) return;
            if (theme === "dark") {
                themeIcon.className = "bi bi-sun-fill";
            } else {
                themeIcon.className = "bi bi-moon-stars-fill";
            }
        }

        /* Initialize icon on page load */
        updateIcon(getTheme());

        if (themeToggle) {
            themeToggle.addEventListener(
                "click",
                function () {
                    var current = getTheme();
                    setTheme(current === "dark" ? "light" : "dark");
                }
            );
        }


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

    }
);
