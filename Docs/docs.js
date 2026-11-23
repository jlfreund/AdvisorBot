// AdvisorBot Documentation: Collapsible Chapters & TOC Navigation
(function () {
  const STORAGE_KEY = "advisorbot_docs_toc_v1";

  function getStoredState() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      return saved ? JSON.parse(saved) : {};
    } catch (e) {
      return {};
    }
  }

  function saveState(state) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) {}
  }

  window.toggleChapter = function (chNum) {
    const el = document.getElementById("ch-group-" + chNum);
    if (!el) return;
    const isExpanded = el.classList.toggle("expanded");
    const state = getStoredState();
    state["ch" + chNum] = isExpanded;
    saveState(state);
    updateToggleAllButton();
  };

  window.toggleAllChapters = function () {
    const groups = document.querySelectorAll(".toc-chapter-group");
    if (!groups.length) return;
    let anyCollapsed = false;
    groups.forEach((g) => {
      if (!g.classList.contains("expanded")) anyCollapsed = true;
    });

    const nextState = anyCollapsed; // if any collapsed, expand all; otherwise collapse all
    const state = getStoredState();
    groups.forEach((g) => {
      const idMatch = g.id.match(/ch-group-(\d+)/);
      if (nextState) {
        g.classList.add("expanded");
      } else {
        g.classList.remove("expanded");
      }
      if (idMatch) {
        state["ch" + idMatch[1]] = nextState;
      }
    });
    saveState(state);
    updateToggleAllButton();
  };

  function updateToggleAllButton() {
    const btn = document.getElementById("toc-toggle-all");
    if (!btn) return;
    const groups = document.querySelectorAll(".toc-chapter-group");
    let anyCollapsed = false;
    groups.forEach((g) => {
      if (!g.classList.contains("expanded")) anyCollapsed = true;
    });
    btn.textContent = anyCollapsed ? "Expand All" : "Collapse All";
    btn.title = anyCollapsed ? "Expand all chapter sub-sections" : "Collapse all chapter sub-sections";
  }

  function initToc(currentChapter) {
    const state = getStoredState();
    const groups = document.querySelectorAll(".toc-chapter-group");

    groups.forEach((g) => {
      const idMatch = g.id.match(/ch-group-(\d+)/);
      if (!idMatch) return;
      const num = parseInt(idMatch[1], 10);
      
      // If current page is this chapter, always expand it by default
      if (currentChapter === num) {
        g.classList.add("expanded");
      } else if (state["ch" + num] !== undefined) {
        if (state["ch" + num]) {
          g.classList.add("expanded");
        } else {
          g.classList.remove("expanded");
        }
      } else {
        // On overview (currentChapter === 0), expand all by default for full visibility
        if (currentChapter === 0) {
          g.classList.add("expanded");
        } else {
          g.classList.remove("expanded");
        }
      }
    });

    updateToggleAllButton();

    // ScrollSpy for subsection active highlighting
    setupScrollSpy();
  }

  function setupScrollSpy() {
    const links = Array.from(document.querySelectorAll(".toc-subsections a.sub"));
    if (!links.length) return;

    const headingIds = links.map((l) => {
      const hash = l.getAttribute("href").split("#")[1];
      return hash;
    }).filter(Boolean);

    const headings = headingIds.map((id) => document.getElementById(id)).filter(Boolean);
    if (!headings.length) return;

    let ticking = false;
    window.addEventListener("scroll", () => {
      if (!ticking) {
        window.requestAnimationFrame(() => {
          updateActiveHeading();
          ticking = false;
        });
        ticking = true;
      }
    });

    function updateActiveHeading() {
      const scrollPos = window.scrollY + 120;
      let currentId = null;

      for (let i = 0; i < headings.length; i++) {
        const top = headings[i].offsetTop;
        if (scrollPos >= top) {
          currentId = headings[i].id;
        } else {
          break;
        }
      }

      links.forEach((l) => {
        const hash = l.getAttribute("href").split("#")[1];
        if (hash === currentId) {
          l.classList.add("active");
        } else {
          l.classList.remove("active");
        }
      });
    }

    updateActiveHeading();
  }

  // Hash-based auto-redirect for backwards compatibility on index.html
  window.checkIndexHashRedirect = function () {
    const hash = window.location.hash;
    if (!hash) return;

    const routes = {
      // Chapter 1
      "#ch1-portfolio-setup": "ch1-portfolio-setup.html#ch1-portfolio-setup",
      "#portfolio-dir": "ch1-portfolio-setup.html#portfolio-dir",
      "#llm-setup": "ch1-portfolio-setup.html#llm-setup",
      "#statements-drop": "ch1-portfolio-setup.html#statements-drop",
      "#portfolio-settings-audit": "ch1-portfolio-setup.html#portfolio-settings-audit",
      "#agent-plugins": "ch1-portfolio-setup.html#agent-plugins",

      // Chapter 2
      "#ch2-page-walkthrough": "ch2-core-walkthrough.html#ch2-page-walkthrough",
      "#page-positions": "ch2-core-walkthrough.html#page-positions",
      "#page-grants": "ch2-core-walkthrough.html#page-grants",
      "#page-simulator": "ch2-core-walkthrough.html#page-simulator",
      "#page-social-security": "ch2-core-walkthrough.html#page-social-security",
      "#page-scenarios": "ch2-core-walkthrough.html#page-scenarios",
      "#page-settings": "ch2-core-walkthrough.html#page-settings",
      "#holding-forecast-methodology": "ch2-core-walkthrough.html#holding-forecast-methodology",

      // Chapter 3
      "#ch3-security-privacy": "ch3-security-privacy.html#ch3-security-privacy",
      "#sec-local-first": "ch3-security-privacy.html#sec-local-first",
      "#sec-cross-tab": "ch3-security-privacy.html#sec-cross-tab",
      "#sec-profiles": "ch3-security-privacy.html#sec-profiles",
      "#sec-local-llm": "ch3-security-privacy.html#sec-local-llm",
      "#sec-alias-table": "ch3-security-privacy.html#sec-alias-table",
      "#sec-unsupported-banks": "ch3-security-privacy.html#sec-unsupported-banks",

      // Chapter 4
      "#ch4-advanced": "ch4-developer-agent.html#ch4-advanced",
      "#adv-mcp": "ch4-developer-agent.html#adv-mcp",
      "#adv-mcp-security": "ch4-developer-agent.html#adv-mcp-security",
      "#adv-guardrails": "ch4-developer-agent.html#adv-guardrails",
      "#adv-testing": "ch4-developer-agent.html#adv-testing"
    };

    if (routes[hash]) {
      window.location.replace(routes[hash]);
    }
  };

  function initPlatformFilter() {
    const OS_KEY = "advisorbot_docs_os_v1";
    const buttons = Array.from(document.querySelectorAll(".os-filter-btn[data-os-filter]"));
    const variants = Array.from(document.querySelectorAll(".platform-variant[data-os]"));
    if (!buttons.length && !variants.length) return;

    function applyFilter(targetOs) {
      const mode = targetOs || "all";
      buttons.forEach((btn) => {
        btn.classList.toggle("active", btn.getAttribute("data-os-filter") === mode);
      });
      variants.forEach((card) => {
        const cardOs = card.getAttribute("data-os");
        card.classList.toggle("is-hidden", mode !== "all" && cardOs !== mode);
      });
    }

    document.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".os-filter-btn[data-os-filter]");
      if (!btn) return;
      const chosen = btn.getAttribute("data-os-filter") || "all";
      try { localStorage.setItem(OS_KEY, chosen); } catch (_e) {}
      applyFilter(chosen);
    });

    let initialOs = "all";
    try { initialOs = localStorage.getItem(OS_KEY) || "all"; } catch (_e) {}
    applyFilter(initialOs);
  }

  function fallbackCopyText(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } finally {
      document.body.removeChild(ta);
    }
  }

  window.copyPromptCard = async function (btn) {
    const card = btn && btn.closest(".prompt-card");
    const pre = card && card.querySelector("pre");
    if (!pre) return;
    const text = (pre.innerText || pre.textContent || "").trim();
    if (!text) return;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        fallbackCopyText(text);
      }
    } catch (_err) {
      fallbackCopyText(text);
    }
    const lbl = btn.querySelector(".prompt-copy-label");
    const prev = lbl ? lbl.textContent : "Copy";
    btn.classList.add("copied");
    if (lbl) lbl.textContent = "Copied!";
    if (btn._copyTimer) clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(() => {
      btn.classList.remove("copied");
      if (lbl) lbl.textContent = prev || "Copy";
    }, 1800);
  };

  function initGalleryLightbox() {
    const imgs = Array.from(document.querySelectorAll(".gallery-media img"));
    if (!imgs.length) return;

    const overlay = document.createElement("div");
    overlay.className = "gallery-lightbox";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.innerHTML =
      '<button type="button" class="gallery-lightbox-close" aria-label="Close preview">&times;</button>' +
      '<div class="gallery-lightbox-inner">' +
      '  <img src="" alt="" />' +
      '  <div class="gallery-lightbox-caption"></div>' +
      "</div>";
    document.body.appendChild(overlay);

    const previewImg = overlay.querySelector("img");
    const captionEl = overlay.querySelector(".gallery-lightbox-caption");
    const closeBtn = overlay.querySelector(".gallery-lightbox-close");

    function close() {
      overlay.classList.remove("open");
      previewImg.removeAttribute("src");
    }

    imgs.forEach((img) => {
      img.addEventListener("click", () => {
        previewImg.src = img.getAttribute("src") || "";
        previewImg.alt = img.getAttribute("alt") || "";
        captionEl.textContent = img.getAttribute("data-caption") || img.getAttribute("alt") || "";
        overlay.classList.add("open");
      });
    });

    closeBtn.addEventListener("click", close);
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) close();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && overlay.classList.contains("open")) close();
    });
  }

  window.initDocsPage = function (activeChapterNum) {
    if (activeChapterNum === 0) {
      window.checkIndexHashRedirect();
    }
    initToc(activeChapterNum);
    initPlatformFilter();
    initGalleryLightbox();
  };
})();
