// Scroll reveal + OS-aware download links from the latest GitHub release.
(() => {
  "use strict";
  const doc = document.documentElement;
  doc.classList.add("js");

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
  const os = /Windows/i.test(ua) ? "windows" : /Linux|X11/i.test(ua) && !/Android/i.test(ua) ? "linux" : "other";
  const matchers = {
    setup: (n) => /^Vanta-Setup-.*\.exe$/.test(n),
    portable: (n) => /-portable\.exe$/.test(n),
    appimage: (n) => /\.AppImage$/.test(n),
    deb: (n) => /_amd64\.deb$/.test(n),
    arch: (n) => /\.pkg\.tar\.zst$/.test(n),
    tar: (n) => /-linux\.tar\.gz$/.test(n),
  };
  const preferred = os === "windows" ? "setup" : os === "linux" ? (/Ubuntu|Debian/i.test(ua) ? "deb" : "appimage") : null;
  const primary = document.getElementById("download-primary");
  const label = document.getElementById("download-label");
  const hint = document.getElementById("download-hint");

  if (preferred) {
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
        if (asset && key === preferred) {
          primary.href = asset.browser_download_url;
          hint.textContent = `Version ${version} · ${(asset.size / 1e6).toFixed(1)} MB · Free forever, GPL-3.0.`;
        }
      }
    })
    .catch(() => { /* Links already point at the releases page. */ });
})();
