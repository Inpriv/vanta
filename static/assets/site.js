
  document.body.classList.remove('no-js');
  const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');

  /* ===== Linux commands and distro tabs ===== */
  (() => {
    const root = document.getElementById('linux');
    const tabs = [...root.querySelectorAll('[role="tab"]')];
    const tablist = root.querySelector('.linux-tabs');
    const placeIndicator = () => {
      const selected = tabs.find(tab => tab.getAttribute('aria-selected') === 'true');
      tablist.style.setProperty('--tab-x', selected.offsetLeft + 'px');
      tablist.style.setProperty('--tab-w', selected.offsetWidth + 'px');
    };
    const selectTab = (selected) => {
      const changed = selected.getAttribute('aria-selected') !== 'true';
      tabs.forEach(tab => {
        const active = tab === selected;
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
        const panel = document.getElementById(tab.getAttribute('aria-controls'));
        panel.hidden = !active;
        if (changed && active && !motionPreference.matches && panel.animate) {
          panel.animate([{opacity: 0, transform: 'translateY(5px)'}, {opacity: 1, transform: 'translateY(0)'}],
            {duration: 280, easing: 'cubic-bezier(.16,1,.3,1)'});
        }
      });
      placeIndicator();
    };
    selectTab(tabs[0]);
    tablist.setAttribute('data-enhanced', '');
    if ('ResizeObserver' in window) new ResizeObserver(placeIndicator).observe(tablist);
    document.fonts?.ready.then(placeIndicator);
    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => selectTab(tab));
      tab.addEventListener('keydown', event => {
        let next;
        if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') next = tabs[(index + 1) % tabs.length];
        if (event.key === 'Home') next = tabs[0];
        if (event.key === 'End') next = tabs[tabs.length - 1];
        if (!next) return;
        event.preventDefault();
        selectTab(next);
        next.focus();
      });
    });
    root.querySelectorAll('[data-copy-command]').forEach(button => {
      const original = button.innerHTML;
      let reset;
      button.addEventListener('click', async () => {
        const code = button.closest('.linux-command').querySelector('code');
        const status = document.getElementById('wineCopyStatus');
        try {
          await navigator.clipboard.writeText(code.textContent.trim());
          clearTimeout(reset);
          button.textContent = 'Copied';
          button.setAttribute('data-copied', '');
          status.textContent = 'Copied: ' + code.textContent.trim();
          reset = setTimeout(() => {
            button.innerHTML = original;
            button.removeAttribute('data-copied');
          }, 1800);
        } catch {
          const selection = window.getSelection();
          const range = document.createRange();
          range.selectNodeContents(code);
          selection.removeAllRanges();
          selection.addRange(range);
          status.textContent = 'Command selected. Copy it with Ctrl+C or your browser menu.';
        }
      });
    });
  })();

  /* ===== Header: border on scroll + mobile menu ===== */
  (function () {
    const header = document.getElementById('header');
    const onScroll = () => header.classList.toggle('scrolled', window.scrollY > 4);
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });

    const btn = document.getElementById('menuBtn');
    const menu = document.getElementById('mobileMenu');
    const icon = document.getElementById('menuIcon');
    const set = (open) => {
      menu.classList.toggle('open', open);
      header.classList.toggle('menu-open', open);
      btn.setAttribute('aria-expanded', open);
      btn.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
      icon.setAttribute('d', open ? 'M6 6l12 12M18 6 6 18' : 'M4 7h16M4 12h16M4 17h16');
    };
    btn.addEventListener('click', () => set(!menu.classList.contains('open')));
    menu.querySelectorAll('a').forEach(a => a.addEventListener('click', () => set(false)));
    document.addEventListener('keydown', e => { if (e.key === 'Escape') set(false); });
    document.addEventListener('click', e => {
      if (menu.classList.contains('open') && !header.contains(e.target)) set(false);
    });
    window.matchMedia('(min-width: 860px)').addEventListener('change', e => { if (e.matches) set(false); });
  })();

  /* ===== Latest version + direct download links from version.json ===== */
  (function () {
    const FALLBACK = 'https://github.com/inpriv/vanta/releases/latest';
    document.querySelectorAll('.vanta-download-link').forEach(el => el.href = FALLBACK);
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 4000);
    fetch('https://raw.githubusercontent.com/inpriv/vanta/refs/heads/main/version.json', { signal: ctrl.signal })
      .then(r => { clearTimeout(timer); if (!r.ok) throw 0; return r.json(); })
      .then(data => {
        if (!data || !data.latest) return;
        const tag = data.latest;
        const num = tag.replace(/^v/i, '');
        const exe = data.download_url ? data.download_url.replace('{version}', tag) : FALLBACK;
        document.querySelectorAll('.vanta-version-tag').forEach(el => el.textContent = tag);
        document.querySelectorAll('.vanta-download-link').forEach(el => el.href = exe);
        document.querySelectorAll('.vanta-zip-link').forEach(el =>
          el.href = `https://github.com/inpriv/vanta/releases/download/${tag}/Vanta-${num}-standalone.zip`);
        const ld = document.querySelector('script[type="application/ld+json"]');
        if (ld) {
          try { const s = JSON.parse(ld.textContent); s.softwareVersion = num; ld.textContent = JSON.stringify(s, null, 2); } catch (e) {}
        }
      })
      .catch(() => {});
  })();

  /* ===== Launcher mockup ===== */
  (function () {
    const input = document.getElementById('lcUsername');
    const avatar = document.getElementById('lcAvatar');
    const version = document.getElementById('lcVersion');
    const meta = document.getElementById('lcMeta');
    const play = document.getElementById('lcPlay');
    const fill = document.getElementById('lcFill');
    const label = document.getElementById('lcLabel');
    let timer, busy = false;

    function loadAvatar() {
      const name = input.value.trim();
      avatar.innerHTML = '';
      if (name.length < 3) return;
      const img = new Image();
      img.alt = '';
      img.onload = () => { avatar.innerHTML = ''; avatar.appendChild(img); };
      img.src = `https://minotar.net/helm/${encodeURIComponent(name)}/64.png`;
    }
    input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(loadAvatar, 400); });
    input.addEventListener('keydown', e => { if (e.key === 'Enter') play.click(); });
    loadAvatar();

    function javaFor(v) {
      const p = v.split('.').map(Number);
      if (p[0] > 1) return 25;
      if (p[1] > 20 || (p[1] === 20 && (p[2] || 0) >= 5)) return 21;
      if (p[1] >= 18) return 17;
      if (p[1] === 17) return 16;
      return 8;
    }
    version.addEventListener('change', () => {
      const v = version.value;
      meta.textContent = `Java ${javaFor(v)} · ${javaFor(v) === 25 ? 'Vulkan ready' : 'OpenGL only'} · 16 GB system RAM`;
    });

    play.addEventListener('click', () => {
      if (busy) return;
      if (!input.value.trim()) {
        input.classList.add('error'); input.focus();
        setTimeout(() => input.classList.remove('error'), 1200);
        return;
      }
      busy = true;
      play.classList.add('busy');
      let p = 0;
      label.textContent = 'Preparing…';
      const iv = setInterval(() => {
        p = Math.min(100, p + 3 + Math.random() * 4);
        fill.style.width = p + '%';
        label.textContent = `Installing: ${Math.round(p)}% · v2.2`;
        if (p >= 100) {
          clearInterval(iv);
          label.textContent = 'Starting Minecraft…';
          setTimeout(() => {
            busy = false;
            play.classList.remove('busy');
            fill.style.width = '0';
            label.textContent = 'Play';
          }, 1100);
        }
      }, 60);
    });
  })();

  /* ===== Mods mockup: Install -> progress -> Installed ===== */
  (function () {
    const status = document.getElementById('drawerStatus');
    const base = status.textContent;
    document.querySelectorAll('#modList [data-install]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (!btn.hasAttribute('data-install')) return;
        btn.removeAttribute('data-install');
        const fill = btn.querySelector('.fill');
        const text = btn.querySelector('span:last-child');
        const title = btn.closest('.mod').querySelector('.mod-title').firstChild.textContent;
        btn.classList.add('installing');
        status.textContent = `Installing ${title}…`;
        status.style.color = '#0A84FF';
        let p = 0;
        const iv = setInterval(() => {
          p = Math.min(100, p + 6 + Math.random() * 8);
          fill.style.width = p + '%';
          text.textContent = Math.round(p) + '%';
          if (p >= 100) {
            clearInterval(iv);
            btn.classList.remove('installing');
            btn.classList.add('installed');
            fill.remove();
            text.innerHTML = '<b>✓ Installed</b>';
            btn.closest('.mod').classList.add('flash');
            status.textContent = `✓ Installed ${title}`;
            status.style.color = '#30D158';
            setTimeout(() => { status.textContent = base; status.style.color = ''; }, 2400);
          }
        }, 80);
      });
    });
  })();

  /* ===== Performance comparison ===== */
  (function () {
    const card = document.getElementById('compare');
    const toggle = document.getElementById('toggle');
    const buttons = toggle.querySelectorAll('button');
    const bars = card.querySelectorAll('.bar i');
    const vals = card.querySelectorAll('.val');
    const data = {
      vanilla: { w: ['33%', '90%', '85%', '85%'], v: ['60 FPS', '4.5 GB', '12.0 s', '85%'] },
      vanta: { w: ['100%', '40%', '30%', '40%'], v: ['180 FPS', '1.8 GB', '4.5 s', '40%'] },
    };
    const frames = new Map();
    function setMetric(element, text) {
      if (frames.has(element)) cancelAnimationFrame(frames.get(element));
      const startValue = parseFloat(element.textContent);
      const endValue = parseFloat(text);
      if (motionPreference.matches || !Number.isFinite(startValue)) { element.textContent = text; return; }
      const unit = text.replace(/^[\d.]+/, '');
      const decimals = text.split(' ')[0].includes('.') ? 1 : 0;
      const start = performance.now();
      const update = now => {
        const t = Math.min(1, (now - start) / 600);
        const eased = 1 - Math.pow(1 - t, 3);
        element.textContent = (startValue + (endValue - startValue) * eased).toFixed(decimals) + unit;
        if (t < 1) frames.set(element, requestAnimationFrame(update));
        else { element.textContent = text; frames.delete(element); }
      };
      frames.set(element, requestAnimationFrame(update));
    }
    function apply(mode) {
      card.classList.toggle('vanta', mode === 'vanta');
      toggle.classList.toggle('vanta', mode === 'vanta');
      buttons.forEach(button => {
        const active = button.dataset.mode === mode;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
      });
      bars.forEach((bar, index) => bar.style.width = data[mode].w[index]);
      vals.forEach((value, index) => setMetric(value, data[mode].v[index]));
    }
    buttons.forEach(button => button.addEventListener('click', () => apply(button.dataset.mode)));
  })();

  /* ===== Copy SHA-256 ===== */
  (function () {
    const btn = document.getElementById('copyHashBtn');
    const rawHash = '1D31C80DC906233AB67597223B27293C9959FBA9946F99A52A49451A610AF949';
    const label = document.getElementById('hashText');
    btn.addEventListener('click', () => {
      navigator.clipboard.writeText(rawHash).then(() => {
        const prev = label.textContent;
        label.textContent = 'Copied to clipboard';
        btn.classList.add('copied');
        setTimeout(() => { label.textContent = prev; btn.classList.remove('copied'); }, 1600);
      }).catch(() => {});
    });
  })();

  /* ===== Staged entrances ===== */
  (() => {
    const elements = [...document.querySelectorAll('.reveal')];
    if (!('IntersectionObserver' in window) || motionPreference.matches) return;
    document.body.classList.add('motion-ready');
    document.querySelectorAll('.hero-grid > :first-child, .features, .steps, .split, .linux-grid').forEach(group => {
      [...group.children].filter(child => child.classList.contains('reveal')).forEach((child, index) => {
        child.style.setProperty('--reveal-delay', Math.min(index * 75, 225) + 'ms');
      });
    });
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('in');
      observer.unobserve(entry.target);
    }), {threshold: 0.08, rootMargin: '0px 0px -24px 0px'});
    elements.forEach(element => observer.observe(element));
    motionPreference.addEventListener('change', event => {
      if (event.matches) { elements.forEach(element => element.classList.add('in')); observer.disconnect(); }
    });
  })();

  /* ===== Scroll progress and active navigation; no permanent render loop ===== */
  (() => {
    const progress = document.querySelector('.scroll-progress');
    let frame = 0;
    const update = () => {
      frame = 0;
      const distance = document.documentElement.scrollHeight - window.innerHeight;
      progress.style.transform = `scaleX(${distance > 0 ? Math.min(1, window.scrollY / distance) : 0})`;
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
    window.addEventListener('scroll', schedule, {passive: true});
    window.addEventListener('resize', schedule, {passive: true});
    update();
    const links = [...document.querySelectorAll('.nav-links a')];
    if ('IntersectionObserver' in window) {
      const navObserver = new IntersectionObserver(entries => {
        entries.filter(entry => entry.isIntersecting).forEach(entry => {
          links.forEach(link => {
            if (link.hash === '#' + entry.target.id) link.setAttribute('aria-current', 'location');
            else link.removeAttribute('aria-current');
          });
        });
      }, {rootMargin: '-20% 0px -55% 0px'});
      links.forEach(link => { const section = document.querySelector(link.hash); if (section) navObserver.observe(section); });
    }
  })();

  /* ===== Ambient motion pauses when offscreen or in a background tab ===== */
  (() => {
    const scene = document.querySelector('.preview-scene');
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(entries => entries.forEach(entry => {
        scene.toggleAttribute('data-visible', entry.isIntersecting);
      })).observe(scene);
    }
    document.addEventListener('visibilitychange', () => document.body.classList.toggle('page-idle', document.hidden));
  })();

  /* ===== Card highlights and button ripples ===== */
  (() => {
    const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)');
    document.querySelectorAll('.feature').forEach(card => {
      let frame = 0, point;
      card.addEventListener('pointermove', event => {
        if (!finePointer.matches || motionPreference.matches) return;
        point = {x: event.clientX, y: event.clientY};
        if (frame) return;
        frame = requestAnimationFrame(() => {
          frame = 0;
          const bounds = card.getBoundingClientRect();
          card.style.setProperty('--spot-x', point.x - bounds.left + 'px');
          card.style.setProperty('--spot-y', point.y - bounds.top + 'px');
        });
      }, {passive: true});
      card.addEventListener('pointerleave', () => { cancelAnimationFrame(frame); frame = 0; });
    });
    document.querySelectorAll('.btn').forEach(button => {
      button.addEventListener('pointerdown', event => {
        if (motionPreference.matches || !button.animate) return;
        const bounds = button.getBoundingClientRect();
        const ripple = document.createElement('span');
        ripple.className = 'button-ripple';
        ripple.setAttribute('aria-hidden', 'true');
        const size = Math.max(bounds.width, bounds.height) * 2;
        Object.assign(ripple.style, {width: size + 'px', height: size + 'px',
          left: event.clientX - bounds.left - size / 2 + 'px', top: event.clientY - bounds.top - size / 2 + 'px'});
        button.appendChild(ripple);
        const animation = ripple.animate([{transform: 'scale(0)', opacity: .24}, {transform: 'scale(1)', opacity: 0}],
          {duration: 650, easing: 'cubic-bezier(.16,1,.3,1)'});
        animation.onfinish = () => ripple.remove();
        animation.oncancel = () => ripple.remove();
      }, {passive: true});
    });
  })();

  /* ===== Interruptible FAQ expansion with native details semantics ===== */
  document.querySelectorAll('.faq details').forEach(details => {
    const summary = details.querySelector('summary');
    const answer = details.querySelector('.answer');
    let animation = null, expanded = details.open;
    summary.addEventListener('click', event => {
      if (!details.animate || motionPreference.matches) return;
      event.preventDefault();
      expanded = !expanded;
      details.toggleAttribute('data-closing', !expanded);
      const startHeight = details.getBoundingClientRect().height;
      details.style.height = startHeight + 'px';
      if (animation) animation.cancel();
      details.open = true;
      details.style.overflow = 'hidden';
      const targetHeight = summary.offsetHeight + (expanded ? answer.scrollHeight : 0) + 2;
      animation = details.animate([{height: startHeight + 'px'}, {height: targetHeight + 'px'}],
        {duration: 380, easing: 'cubic-bezier(.16,1,.3,1)'});
      const targetOpen = expanded;
      animation.onfinish = () => {
        details.open = targetOpen;
        details.removeAttribute('data-closing');
        details.style.height = '';
        details.style.overflow = '';
        animation = null;
      };
    });
    details.addEventListener('toggle', () => { if (!animation) expanded = details.open; });
  });
