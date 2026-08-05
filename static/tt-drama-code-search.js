(function (root) {
  "use strict";

  const CODE_RESOLVER_PATH = "/api/public/tt-code/resolve";
  const FEATURED_PATH = "/api/public/tt-drama/featured";
  const TARGET_ORIGIN = "https://www.dramawavew2a.com";
  const TARGET_PATH = "/ads/101/2250/view";
  const SEARCH_SOURCE = "Search";
  const FEATURED_SOURCE = "Featured";
  const REQUEST_TIMEOUT_MS = 8000;
  const FEATURED_TIMEOUT_MS = 2000;
  const FEATURED_MAX_STALE_MS = 72 * 60 * 60 * 1000;
  const FEATURED_MAX_FUTURE_SKEW_MS = 24 * 60 * 60 * 1000;
  const DRAG_THRESHOLD_PX = 7;
  const CODE_PATTERN = /^[A-Z0-9]{4}$/;
  const CONTENT_ID_PATTERN = /^[A-Za-z0-9_-]{10,32}$/;
  const SAFE_TOKEN_PATTERN = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/;
  const TARGET_PARAM_KEYS = Object.freeze([
    "af_dp",
    "c",
    "af_adset",
    "af_adset_id",
    "af_ad",
    "af_ad_id",
    "af_channel",
    "af_c_id"
  ]);
  const TARGET_PARAM_KEY_SET = new Set(TARGET_PARAM_KEYS);
  const FEATURED_COVER_HOSTS = new Set([
    "ads-cdn.yingliang.tech",
    "cdn.usrgrow.com",
    "static.mydramawave.com",
    "static-v1.mydramawave.com",
    "static-v2.mydramawave.com"
  ]);

  const FALLBACK_FEATURED_DRAMAS = Object.freeze([
    Object.freeze({
      title: "Countdown King",
      language: "English",
      cover_url: "https://ads-cdn.yingliang.tech/custom/source/20260712/cover_5609376.jpg"
    }),
    Object.freeze({
      title: "Fake Relationship With My Puppy Rival",
      language: "English",
      cover_url: "https://ads-cdn.yingliang.tech/custom/source/20260712/cover_5608143.jpg"
    }),
    Object.freeze({
      title: "Замуж за брата моего жениха",
      language: "Русский",
      cover_url: "https://ads-cdn.yingliang.tech/custom/source/20260713/cover_5614704.jpg"
    }),
    Object.freeze({
      title: "희생 후, 가족들이 후회한다",
      language: "한국어",
      cover_url: "https://ads-cdn.yingliang.tech/custom/source/20260712/cover_5601464.jpg"
    }),
    Object.freeze({
      title: "Der Dämonenpakt der Elfe",
      language: "Deutsch",
      cover_url: "https://ads-cdn.yingliang.tech/custom/source/20260712/cover_5601268.jpg"
    })
  ]);

  function normalizeQuery(value) {
    const query = String(value == null ? "" : value).trim();
    const upper = query.toUpperCase();
    if (CODE_PATTERN.test(upper)) {
      return Object.freeze({
        query: upper,
        queryType: "code"
      });
    }
    if (CONTENT_ID_PATTERN.test(query)) {
      return Object.freeze({
        query,
        queryType: "content_id"
      });
    }
    throw new TypeError("Enter a four-character code or a complete Content ID");
  }

  function requireContentId(value) {
    const contentId = String(value == null ? "" : value);
    if (!CONTENT_ID_PATTERN.test(contentId)) {
      throw new TypeError("Invalid DramaWave content_id");
    }
    return contentId;
  }

  function normalizeSource(value) {
    const source = String(value || "");
    if (source !== SEARCH_SOURCE && source !== FEATURED_SOURCE) {
      throw new TypeError("Invalid TT code resolver source");
    }
    return source;
  }

  function normalizeOrigin(value) {
    const parsed = new URL(String(value || ""));
    if (
      (parsed.protocol !== "https:" && parsed.protocol !== "http:") ||
      parsed.username ||
      parsed.password ||
      parsed.hash
    ) {
      throw new TypeError("Invalid resolver origin");
    }
    return parsed.origin;
  }

  function buildCodeResolverUrl(query, source, origin) {
    const normalized = normalizeQuery(query);
    const url = new URL(CODE_RESOLVER_PATH, normalizeOrigin(origin));
    url.searchParams.set("query", normalized.query);
    url.searchParams.set("source", normalizeSource(source));
    return url.href;
  }

  function buildFeaturedUrl(origin) {
    return new URL(FEATURED_PATH, normalizeOrigin(origin)).href;
  }

  function normalizeSafeToken(value, label) {
    const token = String(value || "");
    if (!SAFE_TOKEN_PATTERN.test(token)) {
      throw new TypeError("Invalid " + label);
    }
    return token;
  }

  function validateTargetUrl(value, contentId) {
    const expectedContentId = requireContentId(contentId);
    const raw = String(value || "");
    if (!raw || raw.length > 8192 || raw.trim() !== raw) {
      throw new TypeError("Invalid TT target URL");
    }

    let target;
    try {
      target = new URL(raw);
    } catch (_error) {
      throw new TypeError("Invalid TT target URL");
    }

    if (
      target.protocol !== "https:" ||
      target.origin !== TARGET_ORIGIN ||
      target.pathname !== TARGET_PATH ||
      target.username ||
      target.password ||
      target.port ||
      target.hash
    ) {
      throw new TypeError("Untrusted TT target URL");
    }

    const seen = new Set();
    for (const [key] of target.searchParams.entries()) {
      if (!TARGET_PARAM_KEY_SET.has(key) || seen.has(key)) {
        throw new TypeError("Untrusted TT target parameters");
      }
      seen.add(key);
    }

    for (const requiredKey of ["af_dp", "c", "af_c_id"]) {
      const values = target.searchParams.getAll(requiredKey);
      if (values.length !== 1 || !values[0]) {
        throw new TypeError("Incomplete TT target parameters");
      }
    }
    if (target.searchParams.get("af_dp") !== expectedContentId) {
      throw new TypeError("TT target content_id mismatch");
    }
    const channel = target.searchParams.get("af_channel");
    if (
      channel &&
      channel !== "TT" &&
      channel !== "IG" &&
      channel !== SEARCH_SOURCE &&
      channel !== FEATURED_SOURCE
    ) {
      throw new TypeError("Invalid TT target channel");
    }
    return target.href;
  }

  function normalizeCodeResolvePayload(payload, expectedQuery, expectedSource) {
    if (!payload || payload.found !== true || !payload.item ||
        typeof payload.item !== "object" || Array.isArray(payload.item)) {
      throw new TypeError("Invalid TT code resolver payload");
    }
    const normalizedQuery = normalizeQuery(expectedQuery);
    const contentId = requireContentId(payload.item.content_id);
    const queryType = normalizeSafeToken(payload.item.query_type, "query_type");
    const routeMode = normalizeSafeToken(payload.item.route_mode, "route_mode");
    const source = normalizeSource(expectedSource);
    if (queryType !== normalizedQuery.queryType) {
      throw new TypeError("TT code resolver query_type mismatch");
    }
    if (queryType === "code" && routeMode !== "code_exact") {
      throw new TypeError("Code result must use the frozen TT target");
    }
    if (
      queryType === "content_id" &&
      routeMode !== "published_clone" &&
      routeMode !== "generic_fallback"
    ) {
      throw new TypeError("Content ID result has an invalid route_mode");
    }
    const targetUrl = validateTargetUrl(payload.item.target_url, contentId);
    const channel = new URL(targetUrl).searchParams.get("af_channel");
    if (queryType === "code" && channel !== "TT") {
      throw new TypeError("Code result must preserve the TT channel");
    }
    if (queryType === "content_id" && channel !== source) {
      throw new TypeError("Content ID result source mismatch");
    }
    const drama = normalizeDramaPayload({
      found: true,
      data: payload.item
    }, contentId);
    return Object.freeze({
      content_id: contentId,
      target_url: targetUrl,
      query_type: queryType,
      route_mode: routeMode,
      title: drama.title,
      description: drama.description,
      cover_url: drama.cover_url,
      language: drama.language,
      episode_count: drama.episode_count
    });
  }

  function normalizeDramaPayload(payload, expectedContentId) {
    const contentId = requireContentId(expectedContentId);
    if (!payload || payload.found !== true || !payload.data ||
        typeof payload.data !== "object" || Array.isArray(payload.data) ||
        payload.data.content_id !== contentId) {
      throw new TypeError("Invalid DramaWave resolver payload");
    }
    return Object.freeze({
      content_id: contentId,
      title: String(payload.data.title || contentId).trim().slice(0, 240),
      description: String(payload.data.description || "").trim().slice(0, 1600),
      cover_url: String(payload.data.cover_url || "").trim(),
      language: String(payload.data.language || "").trim().slice(0, 32),
      episode_count: Math.max(0, Number(payload.data.episode_count) || 0)
    });
  }

  function isSafeFeaturedCover(value) {
    try {
      const url = new URL(String(value || ""));
      return (
        url.protocol === "https:" &&
        !url.username &&
        !url.password &&
        !url.port &&
        !url.hash &&
        FEATURED_COVER_HOSTS.has(url.hostname)
      );
    } catch (_error) {
      return false;
    }
  }

  function shanghaiYesterday(nowMs) {
    const currentMs = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
    const shifted = new Date(currentMs + (8 * 60 * 60 * 1000));
    const midnight = Date.UTC(
      shifted.getUTCFullYear(),
      shifted.getUTCMonth(),
      shifted.getUTCDate()
    );
    const previous = new Date(midnight - (24 * 60 * 60 * 1000));
    const year = previous.getUTCFullYear();
    const month = String(previous.getUTCMonth() + 1).padStart(2, "0");
    const day = String(previous.getUTCDate()).padStart(2, "0");
    return year + "-" + month + "-" + day;
  }

  function normalizeFeaturedPayload(payload, nowMs) {
    if (
      !payload ||
      Number(payload.schema_version) !== 1 ||
      !/^\d{4}-\d{2}-\d{2}$/.test(String(payload.source_date || "")) ||
      !Array.isArray(payload.items) ||
      payload.items.length !== 5
    ) {
      throw new TypeError("Invalid featured stories payload");
    }

    const generatedAtMs = Date.parse(String(payload.generated_at || ""));
    const currentMs = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
    const sourceDateMs = Date.parse(String(payload.source_date) + "T00:00:00Z");
    const yesterdayMs = Date.parse(shanghaiYesterday(currentMs) + "T00:00:00Z");
    if (
      !Number.isFinite(generatedAtMs) ||
      generatedAtMs - currentMs > FEATURED_MAX_FUTURE_SKEW_MS ||
      currentMs - generatedAtMs > FEATURED_MAX_STALE_MS ||
      !Number.isFinite(sourceDateMs) ||
      sourceDateMs > yesterdayMs ||
      yesterdayMs - sourceDateMs > FEATURED_MAX_STALE_MS
    ) {
      throw new TypeError("Featured stories payload is stale");
    }

    const items = [];
    const seen = new Set();
    for (const source of payload.items) {
      const contentId = String(source && source.content_id || "");
      const title = String(source && source.title || "").trim().slice(0, 240);
      const coverUrl = String(source && source.cover_url || "").trim();
      if (
        !source ||
        Object.prototype.hasOwnProperty.call(source, "spend") ||
        Object.prototype.hasOwnProperty.call(source, "spend_n") ||
        !CONTENT_ID_PATTERN.test(contentId) ||
        seen.has(contentId) ||
        !title ||
        !isSafeFeaturedCover(coverUrl)
      ) {
        throw new TypeError("Featured stories payload is incomplete");
      }
      seen.add(contentId);
      items.push(Object.freeze({
        content_id: contentId,
        title,
        cover_url: coverUrl,
        language: String(source.language || "").trim().slice(0, 32),
        episode_count: Math.max(0, Number(source.episode_count) || 0)
      }));
    }

    return Object.freeze({
      source_date: String(payload.source_date),
      generated_at: String(payload.generated_at),
      items: Object.freeze(items)
    });
  }

  function createDragTracker(thresholdPx) {
    const threshold = Math.max(1, Number(thresholdPx) || DRAG_THRESHOLD_PX);
    let active = false;
    let startX = 0;
    let startScrollLeft = 0;
    let dragged = false;
    let suppressClick = false;

    return Object.freeze({
      begin(clientX, scrollLeft) {
        active = true;
        startX = Number(clientX) || 0;
        startScrollLeft = Math.max(0, Number(scrollLeft) || 0);
        dragged = false;
        suppressClick = false;
      },
      move(clientX) {
        if (!active) {
          return null;
        }
        const delta = (Number(clientX) || 0) - startX;
        if (Math.abs(delta) >= threshold) {
          dragged = true;
        }
        return Object.freeze({
          dragged,
          scrollLeft: dragged
            ? Math.max(0, startScrollLeft - delta)
            : startScrollLeft
        });
      },
      end() {
        const wasDragged = active && dragged;
        active = false;
        dragged = false;
        suppressClick = wasDragged;
        return wasDragged;
      },
      cancel() {
        active = false;
        dragged = false;
        suppressClick = false;
      },
      consumeSuppressedClick() {
        if (!suppressClick) {
          return false;
        }
        suppressClick = false;
        return true;
      },
      isActive() {
        return active;
      }
    });
  }

  function getCarouselStep(clientWidth) {
    return Math.max(129, Math.round(Math.max(1, Number(clientWidth) || 1) * 0.78));
  }

  function prefersReducedMotion() {
    return Boolean(
      root.matchMedia &&
      root.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function nextFrame(callback) {
    if (typeof root.requestAnimationFrame === "function") {
      root.requestAnimationFrame(callback);
    } else {
      root.setTimeout(callback, 0);
    }
  }

  function attachCarousel(container, previousButton, nextButton) {
    const tracker = createDragTracker(DRAG_THRESHOLD_PX);
    let activePointerId = null;
    let updatePending = false;

    function updateButtons() {
      updatePending = false;
      const maxScroll = Math.max(0, container.scrollWidth - container.clientWidth);
      previousButton.disabled = container.scrollLeft <= 2;
      nextButton.disabled = container.scrollLeft >= maxScroll - 2;
    }

    function queueButtonUpdate() {
      if (updatePending) {
        return;
      }
      updatePending = true;
      nextFrame(updateButtons);
    }

    function scrollByDirection(direction) {
      container.scrollBy({
        left: direction * getCarouselStep(container.clientWidth),
        behavior: prefersReducedMotion() ? "auto" : "smooth"
      });
    }

    function snapToNearestCard() {
      const cards = Array.from(container.querySelectorAll(".story"));
      if (!cards.length) {
        return;
      }
      const firstOffset = cards[0].offsetLeft;
      let closest = 0;
      let distance = Number.POSITIVE_INFINITY;
      for (const card of cards) {
        const candidate = Math.max(0, card.offsetLeft - firstOffset);
        const candidateDistance = Math.abs(candidate - container.scrollLeft);
        if (candidateDistance < distance) {
          distance = candidateDistance;
          closest = candidate;
        }
      }
      container.scrollTo({
        left: closest,
        behavior: prefersReducedMotion() ? "auto" : "smooth"
      });
    }

    function finishPointer(event, cancelled) {
      if (activePointerId === null || event.pointerId !== activePointerId) {
        return;
      }
      const wasDragged = cancelled ? false : tracker.end();
      if (cancelled) {
        tracker.cancel();
      }
      activePointerId = null;
      try {
        if (container.hasPointerCapture(event.pointerId)) {
          container.releasePointerCapture(event.pointerId);
        }
      } catch (_error) {
        // Pointer capture may already have been released by the browser.
      }
      container.classList.remove("is-dragging");
      if (wasDragged) {
        snapToNearestCard();
      }
      queueButtonUpdate();
    }

    previousButton.addEventListener("click", function () {
      scrollByDirection(-1);
    });
    nextButton.addEventListener("click", function () {
      scrollByDirection(1);
    });
    container.addEventListener("scroll", queueButtonUpdate, { passive: true });
    container.addEventListener("dragstart", function (event) {
      event.preventDefault();
    });
    container.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
        return;
      }
      event.preventDefault();
      scrollByDirection(event.key === "ArrowLeft" ? -1 : 1);
    });
    container.addEventListener("pointerdown", function (event) {
      if (
        event.isPrimary === false ||
        (event.pointerType !== "mouse" && event.pointerType !== "pen") ||
        (event.pointerType === "mouse" && event.button !== 0)
      ) {
        return;
      }
      activePointerId = event.pointerId;
      tracker.begin(event.clientX, container.scrollLeft);
    });
    root.addEventListener("pointermove", function (event) {
      if (activePointerId === null || event.pointerId !== activePointerId) {
        return;
      }
      if (event.buttons === 0) {
        finishPointer(event, true);
        return;
      }
      const movement = tracker.move(event.clientX);
      if (!movement) {
        return;
      }
      if (movement.dragged) {
        try {
          if (!container.hasPointerCapture(event.pointerId)) {
            container.setPointerCapture(event.pointerId);
          }
        } catch (_error) {
          // Window-level pointer events still keep the drag state bounded.
        }
        container.classList.add("is-dragging");
        event.preventDefault();
      }
      container.scrollLeft = movement.scrollLeft;
      queueButtonUpdate();
    }, { passive: false });
    root.addEventListener("pointerup", function (event) {
      finishPointer(event, false);
    });
    root.addEventListener("pointercancel", function (event) {
      finishPointer(event, true);
    });
    container.addEventListener("lostpointercapture", function (event) {
      finishPointer(event, false);
    });
    container.addEventListener("click", function (event) {
      if (!tracker.consumeSuppressedClick()) {
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
    }, true);

    if (typeof root.ResizeObserver === "function") {
      const resizeObserver = new root.ResizeObserver(queueButtonUpdate);
      resizeObserver.observe(container);
    } else {
      root.addEventListener("resize", queueButtonUpdate, { passive: true });
    }

    updateButtons();
    return Object.freeze({
      refresh: queueButtonUpdate,
      consumeSuppressedClick: tracker.consumeSuppressedClick
    });
  }

  function responseError(response, payload, fallback) {
    const error = new Error(
      String(payload && payload.message || fallback || "Request failed")
    );
    error.status = response.status;
    error.code = String(
      payload && (payload.code || payload.error) || "resolver_unavailable"
    );
    return error;
  }

  async function fetchPayload(url, signal, cacheMode) {
    const response = await root.fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "omit",
      cache: cacheMode || "no-store",
      signal
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch (_error) {
      payload = {};
    }
    return { response, payload };
  }

  async function resolveCodeQuery(query, source, signal) {
    const normalized = normalizeQuery(query);
    const result = await fetchPayload(
      buildCodeResolverUrl(normalized.query, source, root.location.origin),
      signal,
      "no-store"
    );
    if (!result.response.ok || result.payload.found !== true) {
      throw responseError(
        result.response,
        result.payload,
        "Code or Content ID was not found"
      );
    }
    return normalizeCodeResolvePayload(
      result.payload,
      normalized.query,
      source
    );
  }

  async function resolveAndVerify(query, source, signal) {
    const resolved = await resolveCodeQuery(query, source, signal);
    const route = Object.freeze({
      content_id: resolved.content_id,
      target_url: resolved.target_url,
      query_type: resolved.query_type,
      route_mode: resolved.route_mode
    });
    const drama = Object.freeze({
      content_id: resolved.content_id,
      title: resolved.title,
      description: resolved.description,
      cover_url: resolved.cover_url,
      language: resolved.language,
      episode_count: resolved.episode_count
    });
    return Object.freeze({ route, drama });
  }

  function renderFeaturedStories(container, dramas) {
    container.replaceChildren();
    for (const drama of dramas) {
      const isLinked = Boolean(drama.content_id);
      const card = document.createElement(isLinked ? "a" : "article");
      card.className = isLinked ? "story story-link" : "story";
      card.setAttribute("role", "listitem");
      if (isLinked) {
        card.href = "#story-" + drama.content_id;
        card.rel = "noreferrer";
        card.dataset.contentId = drama.content_id;
        card.setAttribute(
          "aria-label",
          "Open " + drama.title + " in DramaWave"
        );
      }

      const placeholder = document.createElement("div");
      placeholder.className = "story-cover-placeholder";
      placeholder.textContent =
        String(drama.title || "D").trim().slice(0, 1) || "D";
      placeholder.setAttribute("aria-hidden", "true");

      const image = document.createElement("img");
      image.src = drama.cover_url;
      image.alt = drama.title + " cover";
      image.loading = "lazy";
      image.decoding = "async";
      image.draggable = false;
      image.addEventListener("error", function () {
        image.hidden = true;
      }, { once: true });

      const info = document.createElement("div");
      info.className = "story-info";
      const title = document.createElement("div");
      title.className = "story-title";
      title.textContent = drama.title;
      const tag = document.createElement("div");
      tag.className = "story-tag";
      tag.textContent = drama.language;
      info.append(title, tag);
      card.append(placeholder, image, info);
      container.appendChild(card);
    }
  }

  async function loadFeaturedStories(container, title, note, carousel) {
    const controller = typeof root.AbortController === "function"
      ? new root.AbortController()
      : { signal: undefined, abort() {} };
    let timeoutId = null;
    let timedOut = false;
    timeoutId = root.setTimeout(function () {
      timedOut = true;
      controller.abort();
    }, FEATURED_TIMEOUT_MS);
    try {
      const result = await fetchPayload(
        buildFeaturedUrl(root.location.origin),
        controller.signal,
        "default"
      );
      if (!result.response.ok) {
        throw responseError(
          result.response,
          result.payload,
          "Featured stories are unavailable"
        );
      }
      const featured = normalizeFeaturedPayload(result.payload);
      renderFeaturedStories(container, featured.items);
      title.textContent = featured.source_date === shanghaiYesterday()
        ? "Yesterday's top stories"
        : "Featured stories";
      note.textContent = "Swipe, drag, or use the arrows";
      container.dataset.sourceDate = featured.source_date;
      container.dataset.cacheState = "dynamic";
      carousel.refresh();
      return true;
    } catch (error) {
      container.dataset.cacheState = "fallback";
      note.textContent = timedOut
        ? "Featured stories took too long"
        : "Swipe, drag, or use the arrows";
      carousel.refresh();
      return false;
    } finally {
      root.clearTimeout(timeoutId);
    }
  }

  function initPage() {
    const searchForm = document.querySelector("#search-form");
    const queryInput = document.querySelector("#drama-query");
    const searchButton = document.querySelector("#search-button");
    const helper = document.querySelector("#search-helper");
    const result = document.querySelector("#result");
    const resultTitle = document.querySelector("#result-title");
    const resultMeta = document.querySelector("#result-meta");
    const resultDescription = document.querySelector("#result-description");
    const resultCover = document.querySelector("#result-cover");
    const resultCoverPlaceholder =
      document.querySelector("#result-cover-placeholder");
    const continueLink = document.querySelector("#continue-link");
    const continueText = document.querySelector("#continue-text");
    const stories = document.querySelector("#stories");
    const featuredTitle = document.querySelector("#recent-title");
    const featuredNote = document.querySelector("#recent-note");
    const previousButton = document.querySelector("#stories-previous");
    const nextButton = document.querySelector("#stories-next");

    if (
      !searchForm ||
      !queryInput ||
      !searchButton ||
      !helper ||
      !result ||
      !resultTitle ||
      !resultMeta ||
      !resultDescription ||
      !resultCover ||
      !resultCoverPlaceholder ||
      !continueLink ||
      !continueText ||
      !stories ||
      !featuredTitle ||
      !featuredNote ||
      !previousButton ||
      !nextButton
    ) {
      return;
    }

    const carousel = attachCarousel(stories, previousButton, nextButton);
    let activeController = null;
    let activeRequest = 0;
    let featuredController = null;

    function setLoading(loading) {
      searchButton.disabled = loading;
      queryInput.setAttribute("aria-busy", loading ? "true" : "false");
    }

    function hideResult() {
      result.classList.remove("visible");
      result.removeAttribute("aria-busy");
      continueLink.removeAttribute("href");
      continueLink.removeAttribute("data-content-id");
      continueLink.removeAttribute("data-query-type");
      resultCover.hidden = true;
      resultCover.removeAttribute("src");
      resultCover.alt = "";
    }

    function showCover(coverUrl, title) {
      resultCoverPlaceholder.textContent =
        String(title || "D").trim().slice(0, 1) || "D";
      resultCover.hidden = true;
      resultCover.removeAttribute("src");
      resultCover.alt = "";
      if (!isSafeFeaturedCover(coverUrl)) {
        return;
      }
      resultCover.onload = function () {
        resultCover.hidden = false;
      };
      resultCover.onerror = function () {
        resultCover.hidden = true;
        resultCover.removeAttribute("src");
      };
      resultCover.alt = title + " cover";
      resultCover.src = coverUrl;
    }

    function showDrama(resolved, normalizedQuery) {
      const route = resolved.route;
      const drama = resolved.drama;
      const facts = [];
      if (drama.language) {
        facts.push(drama.language.toUpperCase());
      }
      if (drama.episode_count > 0) {
        facts.push(String(drama.episode_count) + " episodes");
      }
      facts.push("ID " + route.content_id);
      if (route.query_type === "code") {
        facts.push("CODE " + normalizedQuery.query);
      }

      resultTitle.textContent = drama.title || route.content_id;
      resultMeta.textContent = facts.join(" · ");
      resultDescription.textContent =
        drama.description || "Story description is not available yet.";
      continueLink.href = route.target_url;
      continueLink.dataset.contentId = route.content_id;
      continueLink.dataset.queryType = route.query_type;
      continueText.textContent = "Open matching story";
      result.dataset.routeMode = route.route_mode;
      result.dataset.queryType = route.query_type;
      result.classList.add("visible");
      helper.classList.remove("error");
      helper.textContent =
        "Match confirmed. Tap below to continue in DramaWave.";
      showCover(drama.cover_url, drama.title || route.content_id);
    }

    async function prepareDrama() {
      let normalized;
      try {
        normalized = normalizeQuery(queryInput.value);
      } catch (_error) {
        hideResult();
        helper.textContent =
          "Enter a four-character code or the complete 10–32 character Content ID.";
        helper.classList.add("error");
        return;
      }
      queryInput.value = normalized.query;
      hideResult();
      helper.classList.remove("error");

      if (activeController) {
        activeController.abort();
      }
      activeRequest += 1;
      const requestNumber = activeRequest;
      const controller = typeof root.AbortController === "function"
        ? new root.AbortController()
        : { signal: undefined, abort() {} };
      activeController = controller;
      let timedOut = false;
      const timeoutId = root.setTimeout(function () {
        timedOut = true;
        controller.abort();
      }, REQUEST_TIMEOUT_MS);

      setLoading(true);
      result.setAttribute("aria-busy", "true");
      helper.textContent = "Finding and verifying your story…";
      try {
        const resolved = await resolveAndVerify(
          normalized.query,
          SEARCH_SOURCE,
          controller.signal
        );
        if (requestNumber !== activeRequest) {
          return;
        }
        showDrama(resolved, normalized);
      } catch (error) {
        if (requestNumber !== activeRequest) {
          return;
        }
        hideResult();
        helper.classList.add("error");
        if (error && error.status === 404) {
          helper.textContent =
            "We couldn’t find that code or Content ID. Check it and try again.";
        } else if (error && error.status === 429) {
          helper.textContent =
            "Too many searches. Wait a moment and try again.";
        } else if (timedOut) {
          helper.textContent =
            "Story search took too long. Please try again.";
        } else if (error && error.name === "AbortError") {
          return;
        } else {
          helper.textContent =
            "Story search is temporarily unavailable. Please try again.";
        }
      } finally {
        root.clearTimeout(timeoutId);
        if (requestNumber === activeRequest) {
          setLoading(false);
          result.removeAttribute("aria-busy");
          activeController = null;
        }
      }
    }

    searchForm.addEventListener("submit", function (event) {
      event.preventDefault();
      prepareDrama();
    });
    queryInput.addEventListener("input", function () {
      activeRequest += 1;
      if (activeController) {
        activeController.abort();
        activeController = null;
      }
      setLoading(false);
      hideResult();
      helper.classList.remove("error");
      helper.textContent =
        "Enter a four-character code or the complete Content ID.";
    });
    queryInput.addEventListener("blur", function () {
      const raw = queryInput.value.trim();
      if (/^[A-Za-z0-9]{4}$/.test(raw)) {
        queryInput.value = raw.toUpperCase();
      }
    });
    continueLink.addEventListener("click", function () {
      if (continueLink.hasAttribute("href")) {
        continueText.textContent = "Opening DramaWave";
      }
    });

    stories.addEventListener("click", async function (event) {
      const card = event.target.closest
        ? event.target.closest("a.story-link[data-content-id]")
        : null;
      if (!card || !stories.contains(card)) {
        return;
      }
      event.preventDefault();
      if (card.dataset.opening === "true") {
        return;
      }
      if (featuredController) {
        featuredController.abort();
      }
      const controller = typeof root.AbortController === "function"
        ? new root.AbortController()
        : { signal: undefined, abort() {} };
      featuredController = controller;
      card.dataset.opening = "true";
      card.setAttribute("aria-busy", "true");
      featuredNote.textContent = "Checking story…";
      let timedOut = false;
      const timeoutId = root.setTimeout(function () {
        timedOut = true;
        controller.abort();
      }, REQUEST_TIMEOUT_MS);
      try {
        const resolved = await resolveAndVerify(
          card.dataset.contentId,
          FEATURED_SOURCE,
          controller.signal
        );
        root.location.assign(resolved.route.target_url);
      } catch (error) {
        if (
          error &&
          error.name === "AbortError" &&
          featuredController !== controller
        ) {
          return;
        }
        featuredNote.textContent = error && error.status === 404
          ? "Story unavailable"
          : timedOut
            ? "Story check took too long"
            : "Please try again";
      } finally {
        root.clearTimeout(timeoutId);
        card.removeAttribute("aria-busy");
        delete card.dataset.opening;
        if (featuredController === controller) {
          featuredController = null;
        }
      }
    });

    renderFeaturedStories(stories, FALLBACK_FEATURED_DRAMAS);
    carousel.refresh();
    loadFeaturedStories(stories, featuredTitle, featuredNote, carousel);
  }

  const api = Object.freeze({
    CODE_RESOLVER_PATH,
    FEATURED_PATH,
    TARGET_ORIGIN,
    TARGET_PATH,
    SEARCH_SOURCE,
    FEATURED_SOURCE,
    REQUEST_TIMEOUT_MS,
    FEATURED_TIMEOUT_MS,
    FEATURED_MAX_STALE_MS,
    FEATURED_MAX_FUTURE_SKEW_MS,
    DRAG_THRESHOLD_PX,
    TARGET_PARAM_KEYS,
    normalizeQuery,
    requireContentId,
    normalizeSource,
    buildCodeResolverUrl,
    buildFeaturedUrl,
    validateTargetUrl,
    normalizeCodeResolvePayload,
    normalizeDramaPayload,
    isSafeFeaturedCover,
    shanghaiYesterday,
    normalizeFeaturedPayload,
    createDragTracker,
    getCarouselStep
  });

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.TTDramaCodeBridge = api;

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initPage, { once: true });
    } else {
      initPage();
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
