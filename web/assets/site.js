// Scroll reveal + OS-aware download links from the latest GitHub release.
(() => {
  "use strict";
  const doc = document.documentElement;
  doc.classList.add("js");

  // Belt and braces for the reload position: once fonts and images have
  // settled, make sure a reload without a #section link is at the top.
  if (!location.hash) {
    addEventListener("load", () => { if (scrollY < 200) scrollTo(0, 0); }, { once: true });
  }

  // Mobile menu.
  const menu = document.querySelector(".menu");
  const nav = document.getElementById("site-nav");
  if (menu && nav) {
    const setOpen = (open) => {
      menu.setAttribute("aria-expanded", String(open));
      nav.classList.toggle("open", open);
    };
    menu.addEventListener("click", () => setOpen(menu.getAttribute("aria-expanded") !== "true"));
    nav.querySelectorAll("a").forEach((a) => a.addEventListener("click", () => setOpen(false)));
  }

  // Reveal on scroll.
  const items = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          io.unobserve(e.target);
        }
      }
    }, { rootMargin: "0px 0px -8% 0px" });
    items.forEach((el) => io.observe(el));
  } else {
    items.forEach((el) => el.classList.add("in"));
  }

  // Download detection.
  const ua = navigator.userAgent;
  const os = /Windows/i.test(ua) ? "windows" : /Linux/i.test(ua) && !/Android/i.test(ua) ? "linux" : "other";
  const matchers = {
    setup: (n) => /^Vanta-Setup-.*\.exe$/.test(n),
    portable: (n) => /-portable\.exe$/.test(n),
    linux: (n) => /^Vanta-linux-x86_64\.tar\.gz$/.test(n),
    appimage: (n) => /\.AppImage$/.test(n),
    deb: (n) => /_amd64\.deb$/.test(n),
    arch: (n) => /\.pkg\.tar\.zst$/.test(n),
    tar: (n) => /-linux\.tar\.gz$/.test(n),
  };
  const preferred = os === "windows" ? "setup" : os === "linux" ? "linux" : null;
  const primary = document.getElementById("download-primary");
  const label = document.getElementById("download-label");
  const hint = document.getElementById("download-hint");

  // Phones can't run Vanta: offer to send the link to a computer instead.
  // iPads report themselves as a Mac; touch support gives them away.
  const mobile = /Android|iPhone|iPad|iPod|Mobile/i.test(ua) || (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1);
  const mac = !mobile && /Macintosh|Mac OS X/i.test(ua);
  if (mobile) {
    label.textContent = "Send to my computer";
    const secondary = document.getElementById("secondary-cta");
    secondary.textContent = "All downloads";
    secondary.href = "#download";
    secondary.removeAttribute("rel");
    hint.textContent = "Vanta runs on Windows and Linux.";
    primary.addEventListener("click", async (e) => {
      e.preventDefault();
      const url = "https://getvanta.xyz/";
      try {
        if (navigator.share) {
          await navigator.share({ title: "Vanta", text: "Vanta — a fast Minecraft launcher", url });
          return;
        }
        await navigator.clipboard.writeText(url);
        hint.textContent = "Link copied — open it on your PC.";
      } catch {
        /* share sheet dismissed */
      }
    });
  }

  // No macOS build yet: say so instead of offering a file that won't run.
  if (mac) {
    label.textContent = "Not available on macOS";
    primary.classList.add("is-disabled");
    primary.setAttribute("aria-disabled", "true");
    primary.removeAttribute("href");
    hint.textContent = "Vanta runs on Windows and Linux for now.";
  }

  // Hotbar: the selected slot steps along, like scrolling through blocks.
  const slots = [...document.querySelectorAll(".hotbar .slot")];
  if (slots.length && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    let sel = 0;
    setInterval(() => {
      slots[sel].classList.remove("on");
      sel = (sel + 1) % slots.length;
      slots[sel].classList.add("on");
    }, 1400);
  }

  // Swipeable gallery (phones): the centred shot is fully lit and the others
  // fade with their distance from the middle, in both directions.
  const gallery = document.querySelector(".gallery");
  if (gallery && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const shots = [...gallery.children];
    let queued = false;
    const update = () => {
      queued = false;
      const swipeable = gallery.scrollWidth > gallery.clientWidth + 1;
      const mid = gallery.scrollLeft + gallery.clientWidth / 2;
      for (const s of shots) {
        if (!swipeable) { s.style.opacity = ""; continue; }
        const d = Math.min(1, Math.abs(s.offsetLeft + s.offsetWidth / 2 - mid) / s.offsetWidth);
        s.style.opacity = String(1 - 0.6 * d);
      }
    };
    const queue = () => { if (!queued) { queued = true; requestAnimationFrame(update); } };
    gallery.addEventListener("scroll", queue, { passive: true });
    addEventListener("resize", queue);
    update();
  }

  if (preferred && !mobile) {
    document.querySelector(`[data-asset="${preferred}"]`)?.classList.add("recommended");
    label.textContent = os === "windows" ? "Download for Windows" : "Download for Linux";
  }

  fetch("https://api.github.com/repos/Inpriv/vanta/releases/latest", { headers: { Accept: "application/vnd.github+json" } })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((release) => {
      const version = String(release.tag_name || "").replace(/^v/, "");
      document.getElementById("release-line").textContent = `Version ${version} · released ${new Date(release.published_at).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })}.`;
      for (const [key, test] of Object.entries(matchers)) {
        const asset = (release.assets || []).find((a) => test(a.name));
        const link = document.querySelector(`[data-asset="${key}"]`);
        if (asset && link) link.href = asset.browser_download_url;
        if (asset && key === preferred && !mobile) {
          primary.href = asset.browser_download_url;
          hint.textContent = `Version ${version} · ${(asset.size / 1e6).toFixed(1)} MB · Free forever, GPL-3.0.`;
        }
      }
    })
    .catch(() => { /* Links already point at the releases page. */ });
})();
