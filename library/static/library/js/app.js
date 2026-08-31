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


        function openSidebar() {

            sidebar.classList.add(
                "show"
            );

            sidebarOverlay.classList.add(
                "show"
            );

        }


        function closeSidebar() {

            sidebar.classList.remove(
                "show"
            );

            sidebarOverlay.classList.remove(
                "show"
            );

        }


        if (menuToggle) {

            menuToggle.addEventListener(
                "click",
                openSidebar
            );

        }


        if (sidebarOverlay) {

            sidebarOverlay.addEventListener(
                "click",
                closeSidebar
            );

        }

    }
);