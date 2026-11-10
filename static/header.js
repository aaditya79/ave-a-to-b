// The shared header: the phone menu, and the current page's link marked. Pages add their own link behavior.
(() => {
    const menu = document.getElementById("signMenu"), links = document.getElementById("signLinks");
    const close = () => { links.classList.remove("open"); menu.setAttribute("aria-expanded", "false"); };
    menu.addEventListener("click", (e) => {
        e.stopPropagation();
        menu.setAttribute("aria-expanded", links.classList.toggle("open"));
    });
    document.addEventListener("pointerdown", (e) => !e.target.closest(".signbar") && close());
    document.addEventListener("keydown", (e) => e.key === "Escape" && close());
    links.addEventListener("click", close);
    window.markHeader = (page) => {
        for (const a of links.querySelectorAll("a")) {
            if (a.dataset.page === page) a.setAttribute("aria-current", "page");
            else a.removeAttribute("aria-current");
        }
    };
    if (location.pathname.startsWith("/how-we-calculate-risk")) window.markHeader("method");
})();
