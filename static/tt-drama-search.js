(function (root) {
  "use strict";

  const W2A_BASE = "https://www.dramawavew2a.com/ads/0/2049/view";
  const RESOLVER_PATH = "/api/public/tt-drama/resolve";
  const FEATURED_PATH = "/api/public/tt-drama/featured";
  const REQUEST_TIMEOUT_MS = 6000;
  const FEATURED_TIMEOUT_MS = 2000;
  const FEATURED_MAX_STALE_MS = 72 * 60 * 60 * 1000;
  const FEATURED_MAX_FUTURE_SKEW_MS = 24 * 60 * 60 * 1000;
  const CORE_PARAMS = Object.freeze({
    c: "TTpost",
    af_c_id: "0001"
  });
  const RESERVED_QUERY_KEYS = new Set([
    "af_dp",
    "c",
    "af_c_id",
    "content_id",
    "cid",
    "auto",
    "preview"
  ]);
  const PARAM_KEY_PATTERN = /^[A-Za-z][A-Za-z0-9_.-]*$/;
  const MAX_PASSTHROUGH_PARAMS = 40;
  const MAX_PARAM_KEY_LENGTH = 100;
  const MAX_PARAM_VALUE_LENGTH = 1024;

  const FALLBACK_FEATURED_DRAMAS = Object.freeze([
    {
      title: "Countdown King",
      language: "English",
      cover: "https://ads-cdn.yingliang.tech/custom/source/20260712/cover_5609376.jpg"
    },
    {
      title: "Fake Relationship With My Puppy Rival",
      language: "English",
      cover: "https://ads-cdn.yingliang.tech/custom/source/20260712/cover_5608143.jpg"
    },
    {
      title: "Замуж за брата моего жениха",
      language: "Русский",
      cover: "https://ads-cdn.yingliang.tech/custom/source/20260713/cover_5614704.jpg"
    },
    {
      title: "희생 후, 가족들이 후회한다",
      language: "한국어",
      cover: "https://ads-cdn.yingliang.tech/custom/source/20260712/cover_5601464.jpg"
    },
    {
      title: "Der Dämonenpakt der Elfe",
      language: "Deutsch",
      cover: "https://ads-cdn.yingliang.tech/custom/source/20260712/cover_5601268.jpg"
    }
  ]);

  function normalizeContentId(value) {
    return String(value || "")
      .trim()
      .replace(/[^A-Za-z0-9_-]/g, "")
      .slice(0, 32);
  }

  function isValidContentId(value) {
    return /^[A-Za-z0-9_-]{10,32}$/.test(value);
  }

  function collectPassthroughParams(search) {
    const query = String(search || "").replace(/^\?/, "");
    const entries = [];
    let skipped = 0;
    let total = 0;

    for (const [key, value] of new URLSearchParams(query).entries()) {
      total += 1;
      const normalizedKey = key.toLowerCase();
      const invalidKey =
        !key ||
        key.length > MAX_PARAM_KEY_LENGTH ||
        !PARAM_KEY_PATTERN.test(key);
      const invalidValue = value.length > MAX_PARAM_VALUE_LENGTH;
      const reserved = RESERVED_QUERY_KEYS.has(normalizedKey);
      const overLimit = entries.length >= MAX_PASSTHROUGH_PARAMS;

      if (invalidKey || invalidValue || reserved || overLimit) {
        skipped += 1;
        continue;
      }
      entries.push([key, value]);
    }

    return Object.freeze({
      entries: Object.freeze(entries.map((entry) => Object.freeze(entry.slice()))),
      skipped,
      total
    });
  }

  function createTarget(contentId, search) {
    const sourceContentId = String(contentId || "").trim();
    const normalizedContentId = normalizeContentId(sourceContentId);
    if (normalizedContentId !== sourceContentId || !isValidContentId(normalizedContentId)) {
      throw new TypeError("Invalid DramaWave content_id");
    }

    const passthrough = collectPassthroughParams(search);
    const url = new URL(W2A_BASE);
    url.searchParams.set("af_dp", normalizedContentId);
    url.searchParams.set("c", CORE_PARAMS.c);
    url.searchParams.set("af_c_id", CORE_PARAMS.af_c_id);
    for (const [key, value] of passthrough.entries) {
      url.searchParams.append(key, value);
    }

    return Object.freeze({
      url: url.toString(),
      contentId: normalizedContentId,
      passthrough
    });
  }

  function buildW2AUrl(contentId, search) {
    return createTarget(contentId, search).url;
  }

  function buildResolverUrl(contentId, origin) {
    const sourceContentId = String(contentId || "").trim();
    const normalizedContentId = normalizeContentId(sourceContentId);
    if (normalizedContentId !== sourceContentId || !isValidContentId(normalizedContentId)) {
      throw new TypeError("Invalid DramaWave content_id");
    }
    const url = new URL(RESOLVER_PATH, origin || "https://ai.yingliangads.com");
    url.searchParams.set("content_id", normalizedContentId);
    return url.toString();
  }

  function buildFeaturedUrl(origin) {
    return new URL(
      FEATURED_PATH,
      origin || "https://ai.yingliangads.com"
    ).toString();
  }

  function trackingCountText(passthrough) {
    const count = passthrough.entries.length;
    if (count === 0 && passthrough.skipped === 0) {
      return "";
    }
    if (count === 0) {
      return `${passthrough.skipped} unsupported tracking ${passthrough.skipped === 1 ? "parameter was" : "parameters were"} ignored`;
    }
    const base = `${count} tracking ${count === 1 ? "parameter" : "parameters"} ready`;
    return passthrough.skipped > 0
      ? `${base} · ${passthrough.skipped} unsupported ${passthrough.skipped === 1 ? "one" : "ones"} ignored`
      : base;
  }

  function isSafeFeaturedCover(value) {
    const allowedHosts = new Set([
      "ads-cdn.yingliang.tech",
      "cdn.usrgrow.com",
      "static.mydramawave.com",
      "static-v1.mydramawave.com",
      "static-v2.mydramawave.com"
    ]);
    try {
      const url = new URL(String(value || ""));
      return (
        url.protocol === "https:" &&
        allowedHosts.has(url.hostname.toLowerCase()) &&
        !url.username &&
        !url.password &&
        (!url.port || url.port === "443")
      );
    } catch (_error) {
      return false;
    }
  }

  function normalizeFeaturedPayload(payload, nowMs) {
    if (!payload || Number(payload.schema_version) !== 1 ||
        !/^\d{4}-\d{2}-\d{2}$/.test(String(payload.source_date || "")) ||
        !Array.isArray(payload.items) ||
        payload.items.length !== 5) {
      throw new TypeError("Invalid featured stories payload");
    }
    const generatedAtMs = Date.parse(String(payload.generated_at || ""));
    const currentMs = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
    const sourceDateMs = Date.parse(`${payload.source_date}T00:00:00Z`);
    const yesterdayMs = Date.parse(`${shanghaiYesterday(currentMs)}T00:00:00Z`);
    if (!Number.isFinite(generatedAtMs) ||
        generatedAtMs - currentMs > FEATURED_MAX_FUTURE_SKEW_MS ||
        currentMs - generatedAtMs > FEATURED_MAX_STALE_MS ||
        !Number.isFinite(sourceDateMs) ||
        sourceDateMs > yesterdayMs ||
        yesterdayMs - sourceDateMs > FEATURED_MAX_STALE_MS) {
      throw new TypeError("Featured stories payload is stale");
    }

    const items = [];
    const seen = new Set();
    for (const source of payload.items.slice(0, 5)) {
      const rawContentId = String(source && source.content_id || "");
      const contentId = normalizeContentId(rawContentId);
      const title = String(source && source.title || "").trim().slice(0, 240);
      const coverUrl = String(source && source.cover_url || "").trim();
      if (
        !source ||
        Object.prototype.hasOwnProperty.call(source, "spend") ||
        Object.prototype.hasOwnProperty.call(source, "spend_n") ||
        contentId !== rawContentId ||
        !isValidContentId(contentId) ||
        seen.has(contentId) ||
        !title ||
        !isSafeFeaturedCover(coverUrl)
      ) {
        continue;
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
    if (items.length !== 5) {
      throw new TypeError("Featured stories payload is incomplete");
    }
    return Object.freeze({
      source_date: String(payload.source_date),
      generated_at: String(payload.generated_at || ""),
      items: Object.freeze(items)
    });
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
    return `${year}-${month}-${day}`;
  }

  function renderFeaturedStories(container, dramas, search) {
    container.replaceChildren();
    for (const drama of dramas) {
      const isLinked = Boolean(drama.content_id);
      const card = document.createElement(isLinked ? "a" : "article");
      card.className = isLinked ? "story story-link" : "story";
      if (isLinked) {
        const target = createTarget(drama.content_id, search);
        card.href = `#story-${target.contentId}`;
        card.rel = "noreferrer";
        card.dataset.contentId = target.contentId;
        card.dataset.targetUrl = target.url;
        card.setAttribute(
          "aria-label",
          `Open ${drama.title} in DramaWave`
        );
      }

      const placeholder = document.createElement("div");
      placeholder.className = "story-cover-placeholder";
      placeholder.textContent = String(drama.title || "D").trim().slice(0, 1) || "D";
      placeholder.setAttribute("aria-hidden", "true");

      const image = document.createElement("img");
      image.src = drama.cover_url || drama.cover;
      image.alt = `${drama.title} cover`;
      image.loading = "lazy";
      image.decoding = "async";
      image.addEventListener("error", () => {
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

  async function loadFeaturedStories(container, title, note, search, origin) {
    const controller = typeof root.AbortController === "function"
      ? new root.AbortController()
      : { signal: undefined, abort() {} };
    let timeoutId = null;
    const timeoutPromise = new Promise((_resolve, reject) => {
      timeoutId = root.setTimeout(() => {
        controller.abort();
        reject(new Error("Featured stories request timed out"));
      }, FEATURED_TIMEOUT_MS);
    });
    try {
      const response = await Promise.race([
        root.fetch(buildFeaturedUrl(origin), {
          method: "GET",
          headers: { Accept: "application/json" },
          credentials: "omit",
          cache: "default",
          signal: controller.signal
        }),
        timeoutPromise
      ]);
      if (!response.ok) {
        throw new Error("Featured stories are unavailable");
      }
      const featured = normalizeFeaturedPayload(await response.json());
      renderFeaturedStories(container, featured.items, search);
      title.textContent = featured.source_date === shanghaiYesterday()
        ? "Yesterday's top stories"
        : "Featured stories";
      note.textContent = "Swipe & tap";
      container.dataset.sourceDate = featured.source_date;
      container.dataset.cacheState = "dynamic";
      return true;
    } catch (_error) {
      container.dataset.cacheState = "fallback";
      return false;
    } finally {
      root.clearTimeout(timeoutId);
    }
  }

  function initPage() {
    const contentIdInput = document.querySelector("#content-id");
    const searchButton = document.querySelector("#search-button");
    const helper = document.querySelector("#search-helper");
    const trackingSummary = document.querySelector("#tracking-summary");
    const result = document.querySelector("#result");
    const resultTitle = document.querySelector("#result-title");
    const resultMeta = document.querySelector("#result-meta");
    const resultDescription = document.querySelector("#result-description");
    const resultCover = document.querySelector("#result-cover");
    const resultCoverPlaceholder = document.querySelector("#result-cover-placeholder");
    const continueLink = document.querySelector("#continue-link");
    const continueText = document.querySelector("#continue-text");
    const stories = document.querySelector("#stories");
    const featuredTitle = document.querySelector("#recent-title");
    const featuredNote = document.querySelector("#recent-note");

    if (!contentIdInput || !searchButton || !helper || !trackingSummary || !result ||
        !resultTitle || !resultMeta || !resultDescription || !resultCover ||
        !resultCoverPlaceholder || !continueLink || !continueText || !stories ||
        !featuredTitle || !featuredNote) {
      return;
    }

    let activeController = null;
    let activeRequest = 0;
    let featuredOpenController = null;

    function clockNow() {
      return root.performance && typeof root.performance.now === "function"
        ? root.performance.now()
        : Date.now();
    }

    const currentPassthrough = collectPassthroughParams(root.location.search);
    const trackingText = trackingCountText(currentPassthrough);
    if (trackingText) {
      trackingSummary.textContent = trackingText;
      trackingSummary.classList.add("visible");
    }

    function hideResult() {
      result.classList.remove("visible");
      result.removeAttribute("data-resolve-ms");
      result.removeAttribute("data-cache-state");
      result.removeAttribute("data-cover-ms");
      result.setAttribute("aria-busy", "false");
      helper.classList.remove("error");
      continueText.textContent = "Open matching story";
      continueLink.removeAttribute("href");
      delete continueLink.dataset.contentId;
      resultTitle.textContent = "";
      resultMeta.textContent = "";
      resultDescription.textContent = "";
      resultCover.onload = null;
      resultCover.onerror = null;
      resultCover.hidden = true;
      resultCover.removeAttribute("src");
      resultCover.alt = "";
      resultCoverPlaceholder.hidden = false;
    }

    function setLoading(loading) {
      searchButton.disabled = loading;
      searchButton.classList.toggle("loading", loading);
      searchButton.setAttribute("aria-busy", loading ? "true" : "false");
    }

    function showCover(coverUrl, title) {
      resultCover.hidden = true;
      resultCoverPlaceholder.hidden = false;
      resultCover.alt = title ? `${title} cover` : "Drama cover";
      if (!coverUrl) {
        return;
      }

      const startedAt = clockNow();
      let triedAlternate = false;
      resultCover.onload = () => {
        result.dataset.coverMs = String(Math.max(0, Math.round(clockNow() - startedAt)));
        resultCover.hidden = false;
        resultCoverPlaceholder.hidden = true;
      };
      resultCover.onerror = () => {
        if (!triedAlternate) {
          try {
            const alternate = new URL(resultCover.src);
            if (alternate.hostname === "static-v1.mydramawave.com") {
              triedAlternate = true;
              alternate.hostname = "static-v2.mydramawave.com";
              resultCover.src = alternate.toString();
              return;
            }
          } catch (_error) {
            // Fall through to the safe placeholder.
          }
        }
        resultCover.hidden = true;
        resultCoverPlaceholder.hidden = false;
      };
      resultCover.src = coverUrl;
    }

    async function resolveDrama(contentId, signal) {
      const startedAt = clockNow();
      const response = await root.fetch(
        buildResolverUrl(contentId, root.location.origin),
        {
          method: "GET",
          headers: { Accept: "application/json" },
          credentials: "omit",
          cache: "no-store",
          signal
        }
      );
      let payload = {};
      try {
        payload = await response.json();
      } catch (_error) {
        payload = {};
      }
      const durationMs = Math.max(0, Math.round(clockNow() - startedAt));
      if (!response.ok || !payload.found || !payload.data) {
        const error = new Error(payload.message || "Story search failed");
        error.status = response.status;
        error.code = payload.error || "resolver_unavailable";
        throw error;
      }
      return {
        item: payload.data,
        durationMs,
        cacheState: response.headers.get("X-TT-Drama-Cache") || ""
      };
    }

    function showDrama(contentId, resolved) {
      const target = createTarget(contentId, root.location.search);
      const item = resolved.item || {};
      const count = target.passthrough.entries.length;
      const skipped = target.passthrough.skipped;

      const title = String(item.title || target.contentId);
      const facts = [];
      if (item.language) {
        facts.push(String(item.language).toUpperCase());
      }
      if (Number(item.episode_count) > 0) {
        facts.push(`${Number(item.episode_count)} episodes`);
      }
      facts.push(`ID ${target.contentId}`);

      resultTitle.textContent = title;
      resultMeta.textContent = facts.join(" · ");
      resultDescription.textContent =
        String(item.description || "").trim() || "Story description is not available yet.";
      continueLink.href = target.url;
      continueLink.dataset.contentId = target.contentId;
      result.dataset.resolveMs = String(resolved.durationMs);
      result.dataset.cacheState = resolved.cacheState;
      result.classList.add("visible");
      helper.classList.remove("error");
      helper.textContent =
        "Match confirmed. Tap below to continue in DramaWave." +
        (count > 0 ? ` ${count} tracking ${count === 1 ? "parameter is" : "parameters are"} ready.` : "") +
        (skipped > 0 ? ` ${skipped} unsupported ${skipped === 1 ? "parameter was" : "parameters were"} ignored.` : "");
      showCover(String(item.cover_url || ""), title);
    }

    async function prepareDrama() {
      const original = contentIdInput.value.trim();
      const contentId = normalizeContentId(original);
      hideResult();
      if (contentId !== original || !isValidContentId(contentId)) {
        helper.textContent = "Enter the complete Content ID shown in the video (10–32 letters or numbers).";
        helper.classList.add("error");
        return;
      }
      contentIdInput.value = contentId;

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
      let timeoutId = null;
      const timeoutPromise = new Promise((_resolve, reject) => {
        timeoutId = root.setTimeout(() => {
          timedOut = true;
          controller.abort();
          const timeoutError = new Error("Story search timed out");
          timeoutError.name = "AbortError";
          reject(timeoutError);
        }, REQUEST_TIMEOUT_MS);
      });

      setLoading(true);
      result.setAttribute("aria-busy", "true");
      helper.textContent = "Finding your story…";
      try {
        const resolved = await Promise.race([
          resolveDrama(contentId, controller.signal),
          timeoutPromise
        ]);
        if (requestNumber !== activeRequest) {
          return;
        }
        showDrama(contentId, resolved);
      } catch (error) {
        if (requestNumber !== activeRequest) {
          return;
        }
        hideResult();
        helper.classList.add("error");
        if (error && error.status === 404) {
          helper.textContent = "We couldn’t find that Content ID. Check the final screen and try again.";
        } else if (error && error.status === 429) {
          helper.textContent = "Too many searches. Wait a moment and try again.";
        } else if (timedOut) {
          helper.textContent = "Story search took too long. Please try again.";
        } else if (error && error.name === "AbortError") {
          return;
        } else {
          helper.textContent = "Story search is temporarily unavailable. Please try again.";
        }
      } finally {
        root.clearTimeout(timeoutId);
        if (requestNumber === activeRequest) {
          activeController = null;
          setLoading(false);
          result.setAttribute("aria-busy", "false");
        }
      }
    }

    contentIdInput.addEventListener("input", () => {
      activeRequest += 1;
      if (activeController) {
        activeController.abort();
        activeController = null;
      }
      setLoading(false);
      hideResult();
      helper.textContent = "Tap the arrow to find this Content ID.";
    });
    contentIdInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        prepareDrama();
      }
    });
    searchButton.addEventListener("click", prepareDrama);
    continueLink.addEventListener("click", () => {
      if (continueLink.hasAttribute("href")) {
        continueText.textContent = "Opening DramaWave";
      }
    });
    stories.addEventListener("click", async (event) => {
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
      if (featuredOpenController) {
        featuredOpenController.abort();
      }
      const controller = typeof root.AbortController === "function"
        ? new root.AbortController()
        : { signal: undefined, abort() {} };
      featuredOpenController = controller;
      card.dataset.opening = "true";
      card.setAttribute("aria-busy", "true");
      featuredNote.textContent = "Checking story…";
      let timeoutId = null;
      const timeoutPromise = new Promise((_resolve, reject) => {
        timeoutId = root.setTimeout(() => {
          controller.abort();
          reject(new Error("Featured story check timed out"));
        }, REQUEST_TIMEOUT_MS);
      });
      try {
        await Promise.race([
          resolveDrama(card.dataset.contentId, controller.signal),
          timeoutPromise
        ]);
        root.location.assign(card.dataset.targetUrl);
      } catch (error) {
        if (error && error.name === "AbortError" &&
            featuredOpenController !== controller) {
          return;
        }
        featuredNote.textContent = error && error.status === 404
          ? "Story unavailable"
          : "Please try again";
      } finally {
        root.clearTimeout(timeoutId);
        card.removeAttribute("aria-busy");
        delete card.dataset.opening;
        if (featuredOpenController === controller) {
          featuredOpenController = null;
        }
      }
    });

    renderFeaturedStories(
      stories,
      FALLBACK_FEATURED_DRAMAS,
      root.location.search
    );
    loadFeaturedStories(
      stories,
      featuredTitle,
      featuredNote,
      root.location.search,
      root.location.origin
    );
  }

  const api = Object.freeze({
    W2A_BASE,
    RESOLVER_PATH,
    FEATURED_PATH,
    REQUEST_TIMEOUT_MS,
    FEATURED_TIMEOUT_MS,
    FEATURED_MAX_STALE_MS,
    FEATURED_MAX_FUTURE_SKEW_MS,
    CORE_PARAMS,
    RESERVED_QUERY_KEYS,
    MAX_PASSTHROUGH_PARAMS,
    MAX_PARAM_KEY_LENGTH,
    MAX_PARAM_VALUE_LENGTH,
    normalizeContentId,
    isValidContentId,
    collectPassthroughParams,
    createTarget,
    buildW2AUrl,
    buildResolverUrl,
    buildFeaturedUrl,
    isSafeFeaturedCover,
    normalizeFeaturedPayload,
    shanghaiYesterday
  });

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.TTDramaBridge = api;

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initPage, { once: true });
    } else {
      initPage();
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
