(function (root) {
  "use strict";

  const W2A_BASE = "https://www.dramawavew2a.com/ads/0/2049/view";
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

  const FEATURED_DRAMAS = Object.freeze([
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

  function renderFeaturedStories(container) {
    for (const drama of FEATURED_DRAMAS) {
      const card = document.createElement("article");
      card.className = "story";

      const image = document.createElement("img");
      image.src = drama.cover;
      image.alt = `${drama.title} cover`;
      image.loading = "lazy";

      const info = document.createElement("div");
      info.className = "story-info";
      const title = document.createElement("div");
      title.className = "story-title";
      title.textContent = drama.title;
      const tag = document.createElement("div");
      tag.className = "story-tag";
      tag.textContent = drama.language;

      info.append(title, tag);
      card.append(image, info);
      container.appendChild(card);
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
    const continueLink = document.querySelector("#continue-link");
    const continueText = document.querySelector("#continue-text");
    const stories = document.querySelector("#stories");

    if (!contentIdInput || !searchButton || !helper || !trackingSummary || !result ||
        !resultTitle || !resultMeta || !continueLink || !continueText || !stories) {
      return;
    }

    const currentPassthrough = collectPassthroughParams(root.location.search);
    const trackingText = trackingCountText(currentPassthrough);
    if (trackingText) {
      trackingSummary.textContent = trackingText;
      trackingSummary.classList.add("visible");
    }

    function hideResult() {
      result.classList.remove("visible");
      helper.classList.remove("error");
      continueText.textContent = "Open matching story";
    }

    function showContentId(contentId) {
      const target = createTarget(contentId, root.location.search);
      const count = target.passthrough.entries.length;
      const skipped = target.passthrough.skipped;

      resultTitle.textContent = target.contentId;
      if (count > 0) {
        resultMeta.textContent =
          `${count} tracking ${count === 1 ? "parameter will" : "parameters will"} be carried to the DramaWave landing page.` +
          (skipped > 0 ? ` ${skipped} unsupported ${skipped === 1 ? "parameter was" : "parameters were"} ignored.` : "");
      } else {
        resultMeta.textContent =
          "DramaWave will resolve this Content ID and open the corresponding full story." +
          (skipped > 0 ? ` ${skipped} unsupported tracking ${skipped === 1 ? "parameter was" : "parameters were"} ignored.` : "");
      }
      continueLink.href = target.url;
      continueLink.dataset.contentId = target.contentId;
      result.classList.add("visible");
      helper.classList.remove("error");
      helper.textContent = "Content ID prepared. Tap below to open the matching story.";
    }

    function prepareDrama() {
      const original = contentIdInput.value.trim();
      const contentId = normalizeContentId(original);
      contentIdInput.value = contentId;
      hideResult();
      if (contentId !== original || !isValidContentId(contentId)) {
        helper.textContent = "Enter the complete Content ID shown in the video (10–32 letters or numbers).";
        helper.classList.add("error");
        return;
      }
      showContentId(contentId);
    }

    contentIdInput.addEventListener("input", () => {
      contentIdInput.value = normalizeContentId(contentIdInput.value);
      hideResult();
      helper.textContent = "Tap the arrow to prepare this Content ID.";
    });
    contentIdInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        prepareDrama();
      }
    });
    searchButton.addEventListener("click", prepareDrama);
    continueLink.addEventListener("click", () => {
      continueText.textContent = "Opening DramaWave";
    });

    renderFeaturedStories(stories);
  }

  const api = Object.freeze({
    W2A_BASE,
    CORE_PARAMS,
    RESERVED_QUERY_KEYS,
    MAX_PASSTHROUGH_PARAMS,
    MAX_PARAM_KEY_LENGTH,
    MAX_PARAM_VALUE_LENGTH,
    normalizeContentId,
    isValidContentId,
    collectPassthroughParams,
    createTarget,
    buildW2AUrl
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
