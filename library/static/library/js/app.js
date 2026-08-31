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
           QUICK-ADD MODAL
           (Author / Category / Publisher from Add Book page)
           ========================= */

        (function () {

            var quickAddButtons = document.querySelectorAll(".quick-add-btn");

            if (!quickAddButtons.length) {
                return;
            }

            var modalEl = document.getElementById("globalModal");

            if (!modalEl) {
                return;
            }

            var modalTitle = document.getElementById("globalModalLabel");
            var activeSelect = null;

            quickAddButtons.forEach(function (btn) {
                btn.addEventListener("click", function () {
                    activeSelect = document.querySelector(
                        btn.getAttribute("data-select")
                    );

                    if (modalTitle) {
                        modalTitle.textContent =
                            btn.getAttribute("data-title") || "Add New";
                    }
                });
            });

            document.body.addEventListener("quickAddSuccess", function (e) {
                if (!activeSelect) {
                    return;
                }

                var detail = e.detail || {};
                var value = String(detail.id);
                var exists = false;

                for (var i = 0; i < activeSelect.options.length; i++) {
                    if (activeSelect.options[i].value === value) {
                        exists = true;
                        break;
                    }
                }

                if (!exists) {
                    var option = document.createElement("option");
                    option.value = value;
                    option.textContent = detail.name;
                    activeSelect.appendChild(option);
                }

                activeSelect.value = value;
                activeSelect = null;

                var modal = bootstrap.Modal.getInstance(modalEl);

                if (modal) {
                    modal.hide();
                }
            });

        })();

    }
);
