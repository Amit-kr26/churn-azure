(() => {
  "use strict";

  const API = (window.API_URL || "").replace(/\/$/, "");
  const SVG_NS = "http://www.w3.org/2000/svg";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const VIEWS = ["score", "batch", "data", "model", "history"];
  const VIEW_TITLES = {
    score: "Score a customer",
    batch: "Score a CSV",
    data: "Training data",
    model: "Model",
    history: "Recent scores",
  };
  const ALGORITHMS = {
    logistic_regression: "Logistic regression",
    random_forest: "Random forest",
    extra_trees: "Extra trees",
    gradient_boosting: "Gradient boosting",
    neural_network: "Neural network",
  };
  const FRIENDLY = {
    CreditScore: "Credit score",
    Geography: "Country",
    Gender: "Gender",
    Age: "Age",
    Tenure: "Years as customer",
    Balance: "Balance",
    NumOfProducts: "Products held",
    HasCrCard: "Has credit card",
    IsActiveMember: "Active member",
    EstimatedSalary: "Estimated salary",
  };
  const PRESETS = {
    steady: {
      CreditScore: 720, Geography: "France", Gender: "Female", Age: 38, Tenure: 6,
      Balance: 50000, NumOfProducts: 2, HasCrCard: 1, IsActiveMember: 1, EstimatedSalary: 70000,
    },
    risk: {
      CreditScore: 580, Geography: "Germany", Gender: "Male", Age: 55, Tenure: 6,
      Balance: 120000, NumOfProducts: 2, HasCrCard: 1, IsActiveMember: 0, EstimatedSalary: 65000,
    },
  };
  const state = {
    loaded: { data: false, model: false },
    scoring: false,
    hasResult: false,
    batchFile: null,
    batchBusy: false,
    batchResult: null,
    batchFilter: "all",
  };

  class ApiError extends Error {
    constructor(detail, status) {
      super("API request failed");
      this.name = "ApiError";
      this.detail = detail;
      this.status = status;
    }
  }

  function make(namespace, tag, attrs, kids) {
    const node = namespace ? document.createElementNS(namespace, tag) : document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") {
        node.setAttribute("class", Array.isArray(value) ? value.filter(Boolean).join(" ") : value);
      } else if (key === "style" && typeof value === "object") {
        for (const [name, setting] of Object.entries(value)) node.style.setProperty(name, setting);
      } else if (key === "text") {
        node.textContent = String(value);
      } else {
        node.setAttribute(key, value === true ? "" : String(value));
      }
    }
    for (const kid of (kids || []).flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }
  const el = (tag, attrs, ...kids) => make(null, tag, attrs, kids);
  const svg = (tag, attrs, ...kids) => make(SVG_NS, tag, attrs, kids);

  function pct(value, digits = 0) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return `${(number * 100).toFixed(digits)}%`;
  }
  function num(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString() : "—";
  }
  function fixed(value, digits = 3) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(digits) : "—";
  }
  function signedPoints(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    const sign = number > 0 ? "+" : number < 0 ? "−" : "";
    return `${sign}${Math.abs(number * 100).toFixed(1)} pts`;
  }
  function bytesLabel(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  function featureLabel(name) {
    return FRIENDLY[name] || name;
  }
  function algorithmLabel(name) {
    return ALGORITHMS[name] || name;
  }
  function formatDate(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    }).format(date);
  }
  function formatSource(endpoint) {
    return endpoint === "batch_predict" ? "CSV" : "Single";
  }
  function profileLine(features) {
    if (!features) return "—";
    const products = Number(features.NumOfProducts);
    return `${features.Geography} · age ${features.Age} · ${products} ${products === 1 ? "product" : "products"}`;
  }
  function validationLine(error) {
    const location = Array.isArray(error.loc) ? error.loc.filter((part) => part !== "body").join(".") : "";
    return `${location ? `${location}: ` : ""}${error.msg || "invalid value"}`;
  }
  function readError(error) {
    if (error instanceof ApiError) {
      const detail = error.detail;
      if (typeof detail === "string") return { message: detail };
      if (Array.isArray(detail)) {
        const message = detail.map(validationLine).filter(Boolean).join("\n");
        return { message: message || `Request failed (${error.status})` };
      }
      if (detail && typeof detail === "object") {
        if (Array.isArray(detail.row_errors)) {
          return { message: detail.message || "Some rows could not be scored.", rows: detail.row_errors };
        }
        if (typeof detail.message === "string") return { message: detail.message };
        if (typeof detail.detail === "string") return { message: detail.detail };
      }
      return { message: `Request failed (${error.status})` };
    }
    return { message: (error && error.message) || "Request failed." };
  }
  function errorState(title, error, retry) {
    const detail = readError(error);
    const box = el("div", { class: "error-state" },
      el("h2", {}, title),
      el("p", { class: "error-message" }, detail.message),
    );
    if (detail.rows && detail.rows.length) {
      box.append(el("ul", {}, detail.rows.slice(0, 25).map((row) => {
        const messages = (row.errors || []).map(validationLine).join("; ");
        return el("li", {}, `Row ${Number(row.row) + 1}: ${messages}`);
      })));
    }
    if (detail.rows && detail.rows.length >= 25) {
      box.append(el("p", {}, "Only the first 25 invalid rows are shown."));
    }
    if (retry) {
      const button = el("button", { class: "button secondary", type: "button" }, "Try again");
      button.addEventListener("click", retry);
      box.append(button);
    }
    return box;
  }

  function sessionId() {
    const fresh = () => {
      const bytes = new Uint8Array(16);
      crypto.getRandomValues(bytes);
      return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    };
    try {
      let id = localStorage.getItem("churn-session");
      if (!/^[0-9a-f]{32}$/.test(id || "")) {
        id = fresh();
        localStorage.setItem("churn-session", id);
      }
      return id;
    } catch {
      return fresh();
    }
  }

  const SESSION = sessionId();

  async function api(path, options = {}) {
    const { timeout = 60000, headers: extraHeaders, ...fetchOptions } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    let response;
    try {
      response = await fetch(`${API}${path}`, {
        ...fetchOptions,
        headers: { ...(extraHeaders || {}), "X-Session": SESSION },
        signal: controller.signal,
      });
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw new Error("The API did not respond in time. If it was idle, the first request can take about 15 seconds; try again.");
      }
      throw new Error("Cannot reach the API. If it was idle, the first request can take about 15 seconds; try again.");
    } finally {
      clearTimeout(timer);
    }
    const raw = await response.text();
    let body = {};
    if (raw) {
      try {
        body = JSON.parse(raw);
      } catch {
        body = { detail: raw.slice(0, 400) };
      }
    }
    if (!response.ok) throw new ApiError(body && body.detail !== undefined ? body.detail : body, response.status);
    return body;
  }

  function setStatus(status, message) {
    const statusEl = $("#apiStatus");
    statusEl.dataset.state = status;
    $("#statusText").textContent = message;
    $("#retryStatus").hidden = status !== "bad";
  }
  function setModelVersion(version) {
    $("#modelVersion").textContent = version && version !== "none" ? version : "—";
  }
  async function checkHealth() {
    setStatus("checking", "Connecting to API");
    try {
      const health = await api("/health", { timeout: 30000 });
      if (health.model_loaded) {
        setStatus("ok", "API online");
        setModelVersion(health.model_version);
      } else {
        setStatus("bad", "API online, model unavailable");
        setModelVersion(health.model_version);
      }
    } catch {
      setStatus("bad", "API unreachable");
      setModelVersion("—");
    }
  }

  const THEME_KEY = "churn-theme";
  const THEME_META = { light: "#e9ede8", dark: "#0d1210" };
  function storedTheme() {
    try {
      const theme = localStorage.getItem(THEME_KEY);
      if (theme === "light" || theme === "dark") return theme;
    } catch {}
    return null;
  }
  function applyTheme(theme, persist = false) {
    const next = theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    for (const button of $$("[data-theme-choice]")) {
      button.setAttribute("aria-pressed", String(button.dataset.themeChoice === next));
    }
    const meta = $("#themeColor");
    if (meta) meta.setAttribute("content", THEME_META[next]);
    if (persist) {
      try { localStorage.setItem(THEME_KEY, next); } catch {}
    }
  }
  function initTheme() {
    const systemDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    const initial = document.documentElement.dataset.theme === "dark" ? "dark" : storedTheme() || (systemDark ? "dark" : "light");
    applyTheme(initial);
    for (const button of $$("[data-theme-choice]")) {
      button.addEventListener("click", () => applyTheme(button.dataset.themeChoice, true));
    }
    const media = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
    if (media) {
      const followSystem = (event) => {
        if (!storedTheme()) applyTheme(event.matches ? "dark" : "light");
      };
      if (media.addEventListener) media.addEventListener("change", followSystem);
      else if (media.addListener) media.addListener(followSystem);
    }
  }

  function currentView() {
    const name = location.hash.replace(/^#/, "");
    return VIEWS.includes(name) ? name : "score";
  }
  function showView(name) {
    const view = VIEWS.includes(name) ? name : "score";
    for (const panel of $$(".view")) panel.hidden = panel.id !== `view-${view}`;
    for (const link of $$(".nav-item")) {
      if (link.dataset.view === view) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    document.title = `${VIEW_TITLES[view]} · Churn predictor`;
    if (view === "data" && !state.loaded.data) loadData();
    if (view === "model" && !state.loaded.model) loadModel();
    if (view === "history") loadHistory();
    window.scrollTo(0, 0);
  }
  function goToView(name) {
    const view = VIEWS.includes(name) ? name : "score";
    if (location.hash !== `#${view}`) window.history.pushState(null, "", `#${view}`);
    showView(view);
  }
  function initNavigation() {
    if (!location.hash || !VIEWS.includes(location.hash.slice(1))) {
      window.history.replaceState(null, "", "#score");
    }
    for (const link of $$(".nav-item")) {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        goToView(link.dataset.view);
      });
    }
    const brand = $(".brand");
    if (brand) {
      brand.addEventListener("click", (event) => {
        event.preventDefault();
        goToView("score");
      });
    }
    window.addEventListener("hashchange", () => showView(currentView()));
    window.addEventListener("popstate", () => showView(currentView()));
    showView(currentView());
    const resetInitialScroll = () => {
      if (location.hash && VIEWS.includes(location.hash.slice(1))) window.scrollTo(0, 0);
    };
    window.addEventListener("load", resetInitialScroll, { once: true });
    if (document.readyState === "complete") requestAnimationFrame(resetInitialScroll);
  }

  function summaryStrip(items) {
    return el("div", { class: "summary-strip" }, items.map((item) =>
      el("div", { class: "summary-item" },
        el("span", { class: "summary-label" }, item.label),
        el("span", { class: "summary-value" }, item.value),
        item.note ? el("span", { class: "summary-note" }, item.note) : null,
      )));
  }

  function loadingState(label) {
    return el("div", { class: "loading-state", "aria-label": label },
      el("div", { class: "skeleton skeleton-strip" }),
      el("div", { class: "skeleton-grid" },
        el("div", { class: "skeleton skeleton-panel" }),
        el("div", { class: "skeleton skeleton-panel" }),
      ));
  }

  const chartPainters = new WeakMap();
  let chartObserver = null;

  function paintChart(host) {
    const painter = chartPainters.get(host);
    if (!painter) return;
    const width = Math.round(host.clientWidth);
    if (width < 120 || width === host._chartWidth) return;
    host._chartWidth = width;
    host.replaceChildren(painter(width));
  }

  function mountChart(host, painter) {
    chartPainters.set(host, painter);
    if (!chartObserver && typeof ResizeObserver !== "undefined") {
      chartObserver = new ResizeObserver((entries) => {
        for (const entry of entries) {
          const width = Math.round(entry.contentRect.width);
          const host = entry.target;
          if (width < 120 || width === host._chartWidth) continue;
          const paint = chartPainters.get(host);
          if (!paint) continue;
          host._chartWidth = width;
          host.replaceChildren(paint(width));
        }
      });
    }
    if (chartObserver) chartObserver.observe(host);
    return host;
  }

  function paintCharts(root = document) {
    for (const host of $$(".chart-host", root)) paintChart(host);
  }

  const scoreForm = $("#scoreForm");
  const resultPanel = $("#result");

  function profileText(features) {
    if (!features) return "Profile unavailable";
    const products = Number(features.NumOfProducts);
    return `${features.Geography} · age ${features.Age} · ${products} ${products === 1 ? "product" : "products"}`;
  }

  function renderScoreEmpty() {
    state.hasResult = false;
    resultPanel.classList.remove("is-filled");
    resultPanel.replaceChildren(
      el("h2", { class: "sr-only" }, "Score result"),
      el("div", { class: "empty-state" },
        el("h2", {}, "No score yet"),
        el("p", {}, "Fill in the profile and press Score customer. You’ll get a probability, the model’s flag line, and the fields that moved it."),
      ),
    );
  }

  function renderScoreLoading() {
    resultPanel.classList.remove("is-filled");
    resultPanel.replaceChildren(
      el("h2", { class: "sr-only" }, "Score result"),
      el("div", { class: "score-skeleton", "aria-label": "Scoring customer" },
        el("div", { class: "skeleton-line", style: { width: "34%" } }),
        el("div", { class: "skeleton-line short" }),
        el("div", { class: "skeleton-score" }),
        el("div", { class: "skeleton-rail" }),
        el("div", { class: "skeleton-line" }),
        el("div", { class: "skeleton-line" }),
      ),
    );
  }

  function setScoreBusy(busy) {
    $("#scoreBtn").disabled = busy;
    $("#scoreBtn").textContent = busy ? "Scoring…" : "Score customer";
    resultPanel.setAttribute("aria-busy", String(busy));
  }

  function markStale() {
    if (!state.hasResult || state.scoring) return;
    if ($(".stale-note", resultPanel)) return;
    resultPanel.prepend(el("div", { class: "stale-note" },
      "Profile changed since this score was run. Score again to update the result."));
  }

  function probabilityRail(probability, threshold) {
    const probabilityPct = Math.min(100, Math.max(0, Number(probability) * 100));
    const thresholdPct = Math.min(100, Math.max(0, Number(threshold) * 100));
    const isRisk = probabilityPct >= thresholdPct;
    const rail = el("div", {
      class: "prob-rail",
      role: "img",
      "aria-label": `Churn probability ${probabilityPct.toFixed(1)} percent. Flag line ${thresholdPct.toFixed(1)} percent.`,
    });
    const track = el("div", { class: "prob-track" });
    const stayBand = el("span", { class: "prob-band prob-band-stay" });
    stayBand.style.width = `${thresholdPct}%`;
    const riskBand = el("span", { class: "prob-band prob-band-risk" });
    riskBand.style.left = `${thresholdPct}%`;
    const fill = el("span", { class: `prob-fill${isRisk ? " risk" : ""}` });
    const marker = el("span", { class: "prob-threshold" });
    marker.style.left = `${thresholdPct}%`;
    const pin = el("span", { class: `prob-pin${isRisk ? " risk" : ""}` });
    track.append(stayBand, riskBand, fill, marker, pin);
    const labels = el("div", { class: "prob-labels" },
      el("span", {}, "0%"),
      el("span", { class: "prob-threshold-label" }, `Flag line ${thresholdPct.toFixed(1)}%`),
      el("span", {}, "100%"),
    );
    const thresholdLabel = $(".prob-threshold-label", labels);
    thresholdLabel.style.left = `${thresholdPct}%`;
    rail.append(track, labels);

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduceMotion) {
      fill.style.width = `${probabilityPct}%`;
      pin.style.left = `${probabilityPct}%`;
    } else {
      fill.style.width = "0%";
      pin.style.left = "0%";
      requestAnimationFrame(() => requestAnimationFrame(() => {
        fill.style.width = `${probabilityPct}%`;
        pin.style.left = `${probabilityPct}%`;
      }));
    }
    return rail;
  }

  function renderScoreResult(prediction, explanation, features) {
    const probability = Number(prediction.probability);
    const threshold = Number(prediction.threshold ?? (explanation && explanation.threshold) ?? 0.5);
    const isRisk = Number(prediction.churn) === 1 || (!Number.isFinite(Number(prediction.churn)) && probability >= threshold);
    const version = prediction.model_version || (explanation && explanation.model_version) || "—";

    const head = el("div", { class: "result-head" },
      el("div", {},
        el("p", { class: "result-label" }, "Scored profile"),
        el("p", { class: "result-profile" }, profileText(features)),
      ),
      el("span", { class: `tag ${isRisk ? "tag-risk" : "tag-stay"}` }, isRisk ? "Likely to churn" : "Likely to stay"),
    );

    const score = el("div", { class: "result-score" },
      el("div", {},
        el("span", { class: "metric-label" }, "Churn probability"),
        el("span", { class: `big-number ${isRisk ? "risk" : "stay"}` }, pct(probability, 1)),
      ),
      el("div", { class: "threshold-copy" }, "Flag line", el("strong", {}, pct(threshold, 1))),
    );

    const decision = el("p", { class: "result-decision" },
      isRisk
        ? "At or above the model’s flag line."
        : "Below the model’s flag line.");

    const section = el("div", { class: "driver-section" });
    section.append(el("h3", {}, "What moved the score"));
    const drivers = explanation
      ? [...(explanation.toward_churn || []), ...(explanation.toward_stay || [])]
        .filter((driver) => Number.isFinite(Number(driver.contribution)) && Number(driver.contribution) !== 0)
        .sort((a, b) => Math.abs(Number(b.contribution)) - Math.abs(Number(a.contribution)))
      : [];

    if (!explanation) {
      section.append(el("div", { class: "result-note" },
        "The model returned a score, but the explanation is unavailable right now."));
    } else if (!drivers.length) {
      section.append(el("div", { class: "result-note" }, "No material drivers were returned for this profile."));
    } else {
      section.append(
        el("p", { class: "driver-note" },
          "Each bar is the change in churn probability when that field is swapped for a typical customer."),
        el("div", { class: "driver-legend" },
          el("span", {}, "Lowers risk"), el("span", {}, "Raises risk")),
      );
      const max = Math.max(...drivers.map((driver) => Math.abs(Number(driver.contribution))), 0.0001);
      const list = el("div", { class: "driver-list" });
      for (const driver of drivers) {
        const contribution = Number(driver.contribution);
        const raises = contribution > 0;
        const width = Math.max(1, (Math.abs(contribution) / max) * 50);
        const bar = el("span", { class: `driver-bar ${raises ? "up" : "down"}` });
        bar.style.width = `${width}%`;
        list.append(el("div", {
          class: "driver-row",
          "aria-label": `${featureLabel(driver.feature)} ${raises ? "raises" : "lowers"} risk by ${Math.abs(contribution * 100).toFixed(1)} percentage points`,
        },
          el("span", { class: "driver-name" }, featureLabel(driver.feature)),
          el("span", { class: "driver-track", "aria-hidden": "true" }, bar),
          el("span", { class: `driver-value ${raises ? "up" : "down"}` }, signedPoints(contribution)),
        ));
      }
      section.append(list);
    }

    resultPanel.classList.add("is-filled");
    resultPanel.replaceChildren(
      el("h2", { class: "sr-only" }, "Score result"),
      head,
      score,
      probabilityRail(probability, threshold),
      decision,
      section,
      el("p", { class: "result-foot" },
        `Model ${version} · probability is the model’s estimate, not a decision.`),
    );

    if (window.matchMedia("(max-width: 1120px)").matches) {
      const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
      resultPanel.scrollIntoView({ behavior, block: "start" });
    }
  }

  function renderScoreError(error) {
    state.hasResult = false;
    resultPanel.classList.remove("is-filled");
    resultPanel.replaceChildren(
      el("h2", { class: "sr-only" }, "Score result"),
      errorState("Could not score this customer", error, () => scoreForm.requestSubmit()),
    );
  }

  function formPayload() {
    const integerFields = new Set(["CreditScore", "Age", "Tenure", "NumOfProducts", "HasCrCard", "IsActiveMember"]);
    const floatFields = new Set(["Balance", "EstimatedSalary"]);
    const payload = {};
    for (const name of Object.keys(FRIENDLY)) {
      const field = scoreForm.elements.namedItem(name);
      if (!field) continue;
      const value = field.value;
      payload[name] = integerFields.has(name) ? parseInt(value, 10) : floatFields.has(name) ? Number(value) : value;
    }
    return payload;
  }

  function fillForm(features) {
    if (!features) return;
    for (const name of Object.keys(FRIENDLY)) {
      const field = scoreForm.elements.namedItem(name);
      if (field && features[name] !== undefined && features[name] !== null) field.value = String(features[name]);
    }
  }

  function applyPreset(name) {
    const preset = PRESETS[name];
    if (!preset) return;
    fillForm(preset);
    markStale();
    const first = scoreForm.elements.namedItem("CreditScore");
    if (first) first.focus();
  }

  function initScore() {
    for (const button of $$("[data-preset]")) {
      button.addEventListener("click", () => applyPreset(button.dataset.preset));
    }
    scoreForm.addEventListener("input", markStale);
    scoreForm.addEventListener("change", markStale);
    $("#resetForm").addEventListener("click", () => {
      scoreForm.reset();
      renderScoreEmpty();
    });

    scoreForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (state.scoring) return;
      if (!scoreForm.checkValidity()) {
        scoreForm.reportValidity();
        return;
      }
      const payload = formPayload();
      state.scoring = true;
      state.hasResult = false;
      setScoreBusy(true);
      renderScoreLoading();
      try {
        const [predictionResult, explanationResult] = await Promise.allSettled([
          api("/predict", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          }),
          api("/explain", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          }),
        ]);
        if (predictionResult.status !== "fulfilled") throw predictionResult.reason;
        const prediction = predictionResult.value;
        const explanation = explanationResult.status === "fulfilled" ? explanationResult.value : null;
        renderScoreResult(prediction, explanation, payload);
        state.hasResult = true;
        setStatus("ok", "API online");
        setModelVersion(prediction.model_version);
      } catch (error) {
        renderScoreError(error);
        if (!(error instanceof ApiError)) setStatus("bad", "API unreachable");
      } finally {
        state.scoring = false;
        setScoreBusy(false);
      }
    });
  }

  function downloadText(filename, content, type = "text/csv") {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const link = el("a", { href: url, download: filename });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  }

  function setBatchHint(message, isError = false) {
    const hint = $("#batchHint");
    hint.textContent = message;
    hint.classList.toggle("is-error", isError);
  }

  function clearBatchFile() {
    state.batchFile = null;
    state.batchResult = null;
    state.batchFilter = "all";
    $("#file").value = "";
    $("#fileRow").hidden = true;
    $("#dropzone").hidden = false;
    $("#fileName").textContent = "";
    $("#fileMeta").textContent = "";
    $("#batchBtn").disabled = true;
    $("#batchOut").replaceChildren();
    setBatchHint("Choose a CSV to score.");
  }

  function setBatchFile(file) {
    if (!file) return;
    const isCsv = file.name.toLowerCase().endsWith(".csv") || String(file.type).includes("csv");
    if (!isCsv) {
      $("#file").value = "";
      setBatchHint("Choose a .csv file.", true);
      return;
    }
    if (file.size > 1_000_000) {
      $("#file").value = "";
      setBatchHint("This file is larger than 1 MB. Split it and try again.", true);
      return;
    }
    state.batchFile = file;
    state.batchResult = null;
    state.batchFilter = "all";
    $("#batchOut").replaceChildren();
    $("#fileName").textContent = file.name;
    $("#fileMeta").textContent = `${bytesLabel(file.size)} · Ready to score`;
    $("#dropzone").hidden = true;
    $("#fileRow").hidden = false;
    $("#batchBtn").disabled = false;
    setBatchHint(`Ready when you are. Up to ${num(500)} rows can be scored.`);
  }

  function histogramSvg(width, bins, threshold) {
    const height = 244;
    const margin = { left: 38, right: 12, top: 26, bottom: 40 };
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const maxCount = Math.max(...bins.map((bin) => bin.count), 1);
    const yMax = Math.max(4, Math.ceil(maxCount / 4) * 4);
    const y = (value) => margin.top + plotH - (value / yMax) * plotH;
    const n = bins.length;
    const step = plotW / n;
    const gap = Math.min(8, step * 0.18);
    const barWidth = step - gap;
    const dense = width < 560;
    const root = svg("svg", {
      class: "chart",
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": `Distribution of scored churn probabilities in ten-point buckets. Flag line ${pct(threshold, 1)}.`,
    });
    for (const tick of [0, yMax / 2, yMax]) {
      const ty = y(tick);
      root.append(svg("line", { x1: margin.left, x2: width - margin.right, y1: ty, y2: ty, class: "chart-grid" }));
      root.append(svg("text", { x: margin.left - 8, y: ty + 3, class: "chart-label", "text-anchor": "end" }, num(tick)));
    }
    root.append(svg("line", {
      x1: margin.left, x2: width - margin.right,
      y1: margin.top + plotH, y2: margin.top + plotH,
      class: "chart-axis",
    }));
    bins.forEach((bin, index) => {
      const x = margin.left + index * step + gap / 2;
      const top = y(bin.count);
      const barHeight = Math.max(0, margin.top + plotH - top);
      const isRisk = bin.hi > threshold + 1e-9;
      const rect = svg("rect", {
        x, y: top, width: barWidth, height: barHeight, rx: 2,
        class: isRisk ? "chart-bar-hot" : "chart-bar-cool",
      });
      rect.append(svg("title", {}, `${bin.lo * 100}–${bin.hi * 100}%: ${num(bin.count)} customers`));
      root.append(rect);
      if (!dense || index % 2 === 0) {
        root.append(svg("text", {
          x: x + barWidth / 2, y: margin.top + plotH + 18,
          class: "chart-label", "text-anchor": "middle",
        }, `${bin.lo * 100}–${bin.hi * 100}`));
      }
    });
    const thresholdX = margin.left + Math.min(1, Math.max(0, threshold)) * plotW;
    root.append(svg("line", {
      x1: thresholdX, x2: thresholdX,
      y1: margin.top, y2: margin.top + plotH,
      class: "chart-threshold",
    }));
    root.append(svg("text", {
      x: thresholdX, y: margin.top - 8,
      class: "chart-label-strong", "text-anchor": threshold > 0.55 ? "end" : "start",
    }, `Flag line ${pct(threshold, 1)}`));
    return root;
  }

  function histogramPanel(result) {
    const bins = Array.from({ length: 10 }, (_, index) => ({
      lo: index / 10,
      hi: (index + 1) / 10,
      count: 0,
    }));
    for (const row of result.rows) {
      const probability = Math.min(1, Math.max(0, Number(row.probability) || 0));
      const index = Math.min(9, Math.floor(probability * 10));
      bins[index].count += 1;
    }
    const threshold = Number(result.threshold);
    const host = el("div", { class: "histogram-wrap chart-host" });
    mountChart(host, (width) => histogramSvg(width, bins, threshold));
    return el("section", { class: "panel chart-panel", "aria-labelledby": "batch-distribution-title" },
      el("div", { class: "panel-head" },
        el("div", {},
          el("h2", { id: "batch-distribution-title" }, "Risk distribution"),
          el("p", {}, "Scored probabilities, in ten-point buckets."),
        ),
      ),
      host,
      el("div", { class: "chart-legend" },
        el("span", {}, el("span", { class: "chart-swatch cool" }), "Below the flag line"),
        el("span", {}, el("span", { class: "chart-swatch" }), "At or above the flag line"),
        el("span", {}, el("span", { class: "chart-swatch line" }), "Flag line"),
      ),
      el("p", { class: "chart-note" }, "Each bar covers ten probability points. Hover a bar for its customer count."),
    );
  }

  function batchRowsTable(result, filter) {
    const rows = filter === "flagged" ? result.rows.filter((row) => row.churn === 1) : result.rows;
    const shown = rows.slice(0, 100);
    if (!shown.length) {
      return el("div", { class: "batch-empty" }, "No customers are above the flag line in this file.");
    }
    const table = el("table", { class: "batch-table" },
      el("caption", { class: "sr-only" }, "Scored customers, sorted by churn probability"),
      el("thead", {}, el("tr", {},
        el("th", { scope: "col", class: "n" }, "CSV row"),
        el("th", { scope: "col" }, "Probability"),
        el("th", { scope: "col" }, "Decision"),
      )),
      el("tbody", {}, shown.map((row) => {
        const probability = Number(row.probability);
        const bar = el("span", { class: "mini-bar", "aria-hidden": "true" },
          el("span", { style: { width: `${Math.min(100, Math.max(0, probability * 100))}%` } }),
        );
        return el("tr", {},
          el("td", { class: "n" }, num(row.row)),
          el("td", {}, el("span", { class: "prob-cell" },
            bar,
            el("span", { class: "prob-value" }, pct(probability, 1)),
          )),
          el("td", {}, el("span", { class: `tag ${row.churn === 1 ? "tag-risk" : "tag-stay"}` },
            row.churn === 1 ? "Flagged" : "Below line")),
        );
      })),
    );
    const wrap = el("div", { class: "table-wrap", role: "region", "aria-label": "Scored customers", tabindex: "0" }, table);
    const note = el("p", { class: "batch-note" },
      `Showing ${num(shown.length)} of ${num(rows.length)} ${rows.length === 1 ? "customer" : "customers"}${filter === "flagged" ? " above the flag line" : ""}. Sorted by probability, highest first.` +
      (rows.length > 100 ? " Download the CSV for every row." : ""),
    );
    return el("div", {}, wrap, note);
  }

  function batchTablePanel(result) {
    const tableHost = el("div", {});
    const filterButtons = new Map();
    const allCount = result.rows.length;
    const flaggedCount = result.rows.filter((row) => row.churn === 1).length;
    const controls = el("div", { class: "segmented", role: "group", "aria-label": "Filter scored customers" });
    for (const [key, label, count] of [["all", "All", allCount], ["flagged", "Flagged", flaggedCount]]) {
      const button = el("button", { type: "button", "aria-pressed": String(state.batchFilter === key) }, `${label} ${num(count)}`);
      button.addEventListener("click", () => {
        state.batchFilter = key;
        for (const [filter, control] of filterButtons) control.setAttribute("aria-pressed", String(filter === key));
        tableHost.replaceChildren(batchRowsTable(result, key));
      });
      filterButtons.set(key, button);
      controls.append(button);
    }
    tableHost.replaceChildren(batchRowsTable(result, state.batchFilter));
    const download = el("button", { class: "button secondary", type: "button" }, "Download CSV");
    download.addEventListener("click", () => {
      const lines = ["row,churn,probability", ...result.rows.map((row) => `${row.row},${row.churn},${row.probability}`)];
      const stamp = new Date().toISOString().slice(0, 10);
      downloadText(`churn-scores-${stamp}.csv`, lines.join("\n"));
    });
    return el("section", { class: "panel", "aria-labelledby": "batch-table-title" },
      el("div", { class: "panel-head" },
        el("div", {},
          el("h2", { id: "batch-table-title" }, "Scored customers"),
          el("p", {}, "Every row from the file, highest risk first."),
        ),
        el("div", { class: "batch-tools" }, controls, download),
      ),
      tableHost,
    );
  }

  function renderBatchResults(result) {
    state.batchResult = result;
    const share = result.n_scored ? result.n_churn / result.n_scored : 0;
    $("#batchOut").replaceChildren(
      summaryStrip([
        { label: "Rows scored", value: num(result.n_scored), note: "In this file" },
        { label: "Flagged", value: num(result.n_churn), note: "At or above the flag line" },
        { label: "Share flagged", value: pct(share, 1), note: "Of scored rows" },
        { label: "Flag line", value: pct(result.threshold, 1), note: "Model cut-off" },
      ]),
      histogramPanel(result),
      batchTablePanel(result),
    );
    paintCharts($("#batchOut"));
  }

  async function scoreBatch() {
    if (!state.batchFile || state.batchBusy) return;
    state.batchBusy = true;
    state.batchResult = null;
    state.batchFilter = "all";
    $("#batchBtn").disabled = true;
    setBatchHint("Scoring the file…");
    $("#batchOut").replaceChildren(loadingState("Scoring CSV"));
    try {
      const result = await api("/batch_predict", {
        method: "POST",
        headers: { "Content-Type": "text/csv" },
        body: state.batchFile,
        timeout: 120000,
      });
      renderBatchResults(result);
      setBatchHint(`Done. ${num(result.n_scored)} rows scored.`);
      setStatus("ok", "API online");
      setModelVersion(result.model_version);
    } catch (error) {
      $("#batchOut").replaceChildren(errorState("Could not score this file", error, () => scoreBatch()));
      setBatchHint("The file could not be scored.", true);
      if (!(error instanceof ApiError)) setStatus("bad", "API unreachable");
    } finally {
      state.batchBusy = false;
      $("#batchBtn").disabled = !state.batchFile;
      $("#batchBtn").textContent = "Score file";
    }
  }

  function initBatch() {
    const input = $("#file");
    const dropzone = $("#dropzone");
    dropzone.addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (file) setBatchFile(file);
    });
    for (const event of ["dragenter", "dragover"]) {
      dropzone.addEventListener(event, (eventObject) => {
        eventObject.preventDefault();
        dropzone.classList.add("is-dragover");
      });
    }
    for (const event of ["dragleave", "drop"]) {
      dropzone.addEventListener(event, (eventObject) => {
        eventObject.preventDefault();
        dropzone.classList.remove("is-dragover");
      });
    }
    dropzone.addEventListener("drop", (eventObject) => {
      const file = eventObject.dataTransfer && eventObject.dataTransfer.files && eventObject.dataTransfer.files[0];
      if (file) setBatchFile(file);
    });
    $("#clearFile").addEventListener("click", clearBatchFile);
    $("#changeFile").addEventListener("click", () => input.click());
    $("#batchBtn").addEventListener("click", scoreBatch);
    $("#tpl").addEventListener("click", () => {
      const header = "CreditScore,Geography,Gender,Age,Tenure,Balance,NumOfProducts,HasCrCard,IsActiveMember,EstimatedSalary";
      const rows = [
        "720,France,Female,38,6,50000,2,1,1,70000",
        "560,Germany,Female,52,4,130000,3,0,0,85000",
      ];
      downloadText("churn-template.csv", [header, ...rows].join("\n"));
    });
  }

  function compactRange(label) {
    return String(label).split("–").map((part) => {
      const number = Number(part);
      if (!Number.isFinite(number) || number < 1000) return part;
      return `${Math.round(number / 1000)}k`;
    }).join("–");
  }

  function columnChartSvg(width, { labels, values, counts, overall, ariaLabel, compact = false }) {
    const height = 244;
    const margin = { left: 40, right: 12, top: 24, bottom: 42 };
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const maxValue = Math.max(...values, overall, 0.1);
    const yMax = Math.min(1, Math.ceil((maxValue + 0.0001) * 10) / 10);
    const y = (value) => margin.top + plotH - (value / yMax) * plotH;
    const n = labels.length || 1;
    const step = plotW / n;
    const gap = Math.min(9, step * 0.18);
    const barWidth = step - gap;
    const dense = width < 470 && n > 5;
    const root = svg("svg", {
      class: "chart",
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": `${ariaLabel}. Overall churn rate ${pct(overall, 1)}.`,
    });
    for (const tick of [0, yMax / 2, yMax]) {
      const tickY = y(tick);
      root.append(svg("line", {
        x1: margin.left, x2: width - margin.right,
        y1: tickY, y2: tickY, class: "chart-grid",
      }));
      root.append(svg("text", {
        x: margin.left - 8, y: tickY + 3,
        class: "chart-label", "text-anchor": "end",
      }, pct(tick, 0)));
    }
    root.append(svg("line", {
      x1: margin.left, x2: width - margin.right,
      y1: margin.top + plotH, y2: margin.top + plotH,
      class: "chart-axis",
    }));
    labels.forEach((label, index) => {
      const value = Number(values[index]) || 0;
      const count = Number(counts[index]) || 0;
      const x = margin.left + index * step + gap / 2;
      const top = y(value);
      const barHeight = Math.max(0, margin.top + plotH - top);
      const rect = svg("rect", {
        x, y: top, width: barWidth, height: barHeight, rx: 2,
        class: value > overall ? "chart-bar-hot" : "chart-bar-cool",
      });
      rect.append(svg("title", {}, `${label}: ${pct(value, 1)} churn, ${num(count)} customers`));
      root.append(rect);
      if (!dense || index % 2 === 0) {
        root.append(svg("text", {
          x: x + barWidth / 2, y: margin.top + plotH + 18,
          class: "chart-label", "text-anchor": "middle",
        }, compact ? compactRange(label) : label));
      }
    });
    const overallY = y(overall);
    root.append(svg("line", {
      x1: margin.left, x2: width - margin.right,
      y1: overallY, y2: overallY, class: "chart-baseline",
    }));
    return root;
  }

  function chartTable(labels, values, counts, valueLabel) {
    return el("div", { class: "table-wrap" },
      el("table", {},
        el("caption", { class: "sr-only" }, "Chart values"),
        el("thead", {}, el("tr", {},
          el("th", { scope: "col" }, "Band"),
          el("th", { scope: "col", class: "n" }, "Customers"),
          el("th", { scope: "col", class: "n" }, valueLabel),
        )),
        el("tbody", {}, labels.map((label, index) => el("tr", {},
          el("td", {}, label),
          el("td", { class: "n" }, num(counts[index])),
          el("td", { class: "n" }, pct(values[index], 1)),
        ))),
      ));
  }

  function columnChartPanel({ id, title, description, labels, values, counts, overall, compact = false }) {
    const host = el("div", { class: "chart-host" });
    mountChart(host, (width) => columnChartSvg(width, {
      labels, values, counts, overall, compact,
      ariaLabel: title,
    }));
    const section = el("section", { class: "panel chart-panel", "aria-labelledby": id },
      el("div", { class: "panel-head" },
        el("div", {},
          el("h2", { id }, title),
          el("p", {}, description),
        ),
      ),
      host,
      el("div", { class: "chart-legend" },
        el("span", {}, el("span", { class: "chart-swatch" }), "Above overall"),
        el("span", {}, el("span", { class: "chart-swatch cool" }), "Below overall"),
        el("span", {}, el("span", { class: "chart-swatch line" }), `Overall ${pct(overall, 1)}`),
      ),
      el("details", { class: "table-toggle" },
        el("summary", {}, "View as table"),
        chartTable(labels, values, counts, title),
      ),
    );
    return section;
  }

  function segmentPanel({ id, title, description, entries, overall }) {
    const maxRate = Math.max(...entries.map((entry) => Number(entry.rate) || 0), 0.0001);
    const table = el("table", {},
      el("caption", { class: "sr-only" }, title),
      el("thead", {}, el("tr", {},
        el("th", { scope: "col" }, "Segment"),
        el("th", { scope: "col", class: "n" }, "Customers"),
        el("th", { scope: "col", class: "n" }, "Churn rate"),
        el("th", { scope: "col" }, el("span", { class: "sr-only" }, "Relative rate")),
      )),
      el("tbody", {}, entries.map((entry) => {
        const rate = Number(entry.rate) || 0;
        const fill = el("span", { class: `bar-fill${rate > overall ? "" : " cool"}` });
        fill.style.width = `${Math.min(100, Math.max(0, (rate / maxRate) * 100))}%`;
        return el("tr", {},
          el("td", { class: "segment-name" }, entry.label),
          el("td", { class: "n" }, num(entry.n)),
          el("td", { class: "n" }, pct(rate, 1)),
          el("td", { class: "bar-cell" },
            el("div", { class: "bar-track", "aria-hidden": "true" }, fill)),
        );
      })),
    );
    return el("section", { class: "panel", "aria-labelledby": id },
      el("div", { class: "panel-head" },
        el("div", {},
          el("h2", { id }, title),
          el("p", {}, description),
        ),
      ),
      el("div", { class: "table-wrap" }, table),
      el("p", { class: "batch-note" }, "Bars are scaled to the highest rate in this panel."),
    );
  }

  async function loadData() {
    const out = $("#dataOut");
    out.setAttribute("aria-busy", "true");
    out.replaceChildren(loadingState("Loading training data"));
    try {
      const stats = await api("/stats");
      renderData(stats);
      state.loaded.data = true;
      setStatus("ok", "API online");
      setModelVersion(stats.model_version);
    } catch (error) {
      out.replaceChildren(el("div", { class: "content-stack" },
        errorState("Could not load training data", error, loadData)));
      state.loaded.data = false;
      if (!(error instanceof ApiError)) setStatus("bad", "API unreachable");
    } finally {
      out.removeAttribute("aria-busy");
    }
  }

  function renderData(stats) {
    const out = $("#dataOut");
    const overall = Number(stats.overall_churn_rate) || 0;
    const age = stats.age_bins || { labels: [], churn_rate: [], n: [] };
    const credit = stats.credit_bins || { labels: [], churn_rate: [], n: [] };
    const balance = stats.balance_bins || { labels: [], churn_rate: [], n: [] };
    const geographies = Object.entries(stats.by_geography || {}).map(([label, data]) => ({
      label, rate: data.rate, n: data.n,
    }));
    const genders = Object.entries(stats.by_gender || {}).map(([label, data]) => ({
      label, rate: data.rate, n: data.n,
    }));
    const products = Object.entries(stats.by_products || {}).map(([label, data]) => ({
      label: `${label} ${label === "1" ? "product" : "products"}`, rate: data.rate, n: data.n,
    }));
    const source = stats.data_source === "real" ? "Bank churn dataset" : "Synthetic dataset";

    out.replaceChildren(
      summaryStrip([
        { label: "Customers in training set", value: num(stats.n_rows), note: source },
        { label: "Overall churn rate", value: pct(overall, 1), note: "Customers who left" },
        {
          label: "Countries",
          value: num(geographies.length),
          note: geographies.map((entry) => entry.label).join(" · "),
        },
      ]),
      el("div", { class: "content-stack" },
        el("div", { class: "grid-2" },
          columnChartPanel({
            id: "age-title", title: "Churn rate by age",
            description: "Share of customers who left, by age band.",
            labels: age.labels, values: age.churn_rate, counts: age.n, overall,
          }),
          columnChartPanel({
            id: "products-title", title: "Churn rate by products held",
            description: "Customers with more products are a smaller but higher-risk group.",
            labels: products.map((entry) => entry.label),
            values: products.map((entry) => entry.rate),
            counts: products.map((entry) => entry.n),
            overall,
          }),
        ),
        el("div", { class: "grid-2" },
          columnChartPanel({
            id: "credit-title", title: "Churn rate by credit score",
            description: "Churn is close to the overall rate in most score bands.",
            labels: credit.labels, values: credit.churn_rate, counts: credit.n, overall,
          }),
          columnChartPanel({
            id: "balance-title", title: "Churn rate by balance",
            description: "The highest balance bands hold very few customers.",
            labels: balance.labels, values: balance.churn_rate, counts: balance.n, overall,
            compact: true,
          }),
        ),
        el("div", { class: "grid-2" },
          segmentPanel({
            id: "geo-title", title: "Churn rate by country",
            description: "Churn differs by the customer’s country of record.",
            entries: geographies, overall,
          }),
          segmentPanel({
            id: "gender-title", title: "Churn rate by gender",
            description: "Churn differs by the gender recorded in the dataset.",
            entries: genders, overall,
          }),
        ),
      ),
    );
    paintCharts(out);
  }

  function matrixOutcome(label) {
    const text = String(label || "").toLowerCase();
    if (text.startsWith("stayed")) return "Actually stayed";
    if (text.startsWith("churned")) return "Actually churned";
    return label || "Actual";
  }

  function confusionPanel(metrics) {
    const confusion = metrics.confusion_matrix || {};
    const matrix = Array.isArray(confusion.matrix) ? confusion.matrix : [[0, 0], [0, 0]];
    const labels = Array.isArray(confusion.labels) ? confusion.labels : ["stayed(0)", "churned(1)"];
    const total = matrix.flat().reduce((sum, value) => sum + (Number(value) || 0), 0) || 1;
    const table = el("table", { class: "matrix" },
      el("caption", { class: "sr-only" }, "Confusion matrix on held-out test rows"),
      el("thead", {}, el("tr", {},
        el("th", { scope: "col" }, el("span", { class: "sr-only" }, "Actual outcome")),
        el("th", { scope: "col" }, "Predicted stayed"),
        el("th", { scope: "col" }, "Predicted churned"),
      )),
      el("tbody", {}, matrix.map((row, rowIndex) => el("tr", {},
        el("th", { scope: "row" }, matrixOutcome(labels[rowIndex])),
        row.map((count, columnIndex) => {
          const value = Number(count) || 0;
          return el("td", { class: rowIndex === columnIndex ? "matrix-good" : "matrix-bad" },
            el("span", { class: "matrix-count" }, num(value)),
            el("span", { class: "matrix-share" }, pct(value / total, 1)),
          );
        }),
      ))),
    );
    const truePositives = Number(matrix[1] && matrix[1][1]) || 0;
    const falseNegatives = Number(matrix[1] && matrix[1][0]) || 0;
    const falsePositives = Number(matrix[0] && matrix[0][1]) || 0;
    return el("section", { class: "panel", "aria-labelledby": "confusion-title" },
      el("div", { class: "panel-head" },
        el("div", {},
          el("h2", { id: "confusion-title" }, "Confusion matrix"),
          el("p", {}, "Held-out test rows at the current flag line."),
        ),
      ),
      table,
      el("p", { class: "batch-note" },
        `${num(truePositives)} churners flagged · ${num(falseNegatives)} missed · ${num(falsePositives)} non-churners flagged.`),
    );
  }

  function importancePanel(metrics) {
    const features = Array.isArray(metrics.feature_importance) ? metrics.feature_importance : [];
    const max = Math.max(...features.map((feature) => Number(feature.importance) || 0), 0.0001);
    const list = el("div", { class: "feature-list" });
    for (const feature of features) {
      const importance = Number(feature.importance) || 0;
      const fill = el("span", { class: "feature-fill" });
      fill.style.width = `${Math.min(100, Math.max(0, (importance / max) * 100))}%`;
      list.append(el("div", { class: "feature-row" },
        el("span", { class: "feature-name" }, featureLabel(feature.feature)),
        el("span", { class: "feature-track", "aria-hidden": "true" }, fill),
        el("span", { class: "feature-value" }, pct(importance, 1)),
      ));
    }
    return el("section", { class: "panel", "aria-labelledby": "importance-title" },
      el("div", { class: "panel-head" },
        el("div", {},
          el("h2", { id: "importance-title" }, "Feature importance"),
          el("p", {}, "Relative contribution reported by the training run."),
        ),
      ),
      features.length ? list : el("p", { class: "batch-note" }, "No feature importance data is available."),
    );
  }

  function candidatesPanel(metrics) {
    const candidates = Array.isArray(metrics.candidates) ? metrics.candidates : [];
    const table = el("table", {},
      el("caption", { class: "sr-only" }, "Candidate model comparison"),
      el("thead", {}, el("tr", {},
        el("th", { scope: "col" }, "Model"),
        el("th", { scope: "col", class: "n" }, "CV F1"),
        el("th", { scope: "col", class: "n" }, "CV AUC"),
        el("th", { scope: "col", class: "n" }, "Test F1"),
        el("th", { scope: "col", class: "n" }, "Test AUC"),
        el("th", { scope: "col", class: "n" }, "Test acc."),
        el("th", { scope: "col", class: "n" }, "Flag line"),
      )),
      el("tbody", {}, candidates.map((candidate) => {
        const selected = candidate.name === metrics.algorithm;
        return el("tr", { class: selected ? "selected-row" : "" },
          el("td", {}, algorithmLabel(candidate.name), selected ? el("span", { class: "row-tag" }, "Selected") : null),
          el("td", { class: "n" }, fixed(candidate.cv_f1)),
          el("td", { class: "n" }, fixed(candidate.cv_auc)),
          el("td", { class: "n" }, fixed(candidate.test_f1)),
          el("td", { class: "n" }, fixed(candidate.test_auc)),
          el("td", { class: "n" }, pct(candidate.test_accuracy, 1)),
          el("td", { class: "n" }, pct(candidate.threshold, 1)),
        );
      })),
    );
    return el("section", { class: "panel", "aria-labelledby": "candidates-title" },
      el("div", { class: "panel-head" },
        el("div", {},
          el("h2", { id: "candidates-title" }, "Candidate comparison"),
          el("p", {}, "Cross-validation and held-out test scores for every trained model."),
        ),
      ),
      el("p", { class: "table-scroll-hint" }, "Scroll sideways to see every column."),
      el("div", { class: "table-wrap", role: "region", "aria-label": "Candidate model comparison", tabindex: "0", "data-wide": "true" }, table),
      el("p", { class: "batch-note" },
        "CV scores come from cross-validation on the training rows; test scores come from the held-out split."),
    );
  }

  function subgroupViz(rows) {
    const max = Math.max(...rows.flatMap((row) => [
      Number(row.values.observed_churn_rate) || 0,
      Number(row.values.mean_predicted) || 0,
    ]), 0.0001);
    const list = el("div", { class: "subgroup-viz-list" });
    for (const row of rows) {
      const observed = Number(row.values.observed_churn_rate) || 0;
      const predicted = Number(row.values.mean_predicted) || 0;
      const observedFill = el("span", { class: "subgroup-viz-fill" });
      observedFill.style.width = `${Math.min(100, Math.max(1, (observed / max) * 100))}%`;
      const predictedFill = el("span", { class: "subgroup-viz-fill predicted" });
      predictedFill.style.width = `${Math.min(100, Math.max(1, (predicted / max) * 100))}%`;
      list.append(el("div", {
        class: "subgroup-viz-row",
        "aria-label": `${row.segment}: observed churn ${pct(observed, 1)}, predicted ${pct(predicted, 1)}`,
      },
        el("span", { class: "subgroup-viz-name" }, row.segment),
        el("span", { class: "subgroup-viz-bars", "aria-hidden": "true" },
          el("span", { class: "subgroup-viz-track" }, observedFill),
          el("span", { class: "subgroup-viz-track" }, predictedFill),
        ),
        el("span", { class: "subgroup-viz-values" }, `${pct(observed, 1)} / ${pct(predicted, 1)}`),
      ));
    }
    return el("div", { class: "subgroup-viz" },
      el("div", { class: "subgroup-viz-head" },
        el("h3", {}, "Observed vs predicted churn"),
        el("div", { class: "subgroup-viz-legend" },
          el("span", {}, el("span", { class: "chart-swatch" }), "Observed"),
          el("span", {}, el("span", { class: "chart-swatch cool" }), "Predicted"),
        ),
      ),
      list,
      el("p", { class: "batch-note" }, "Bars are scaled to the highest rate shown."),
    );
  }

  function subgroupPanel(metrics) {
    const groups = Object.entries(metrics.subgroup_metrics || {});
    const rows = [];
    for (const [group, segments] of groups) {
      for (const [segment, values] of Object.entries(segments || {})) {
        rows.push({ group, segment, values });
      }
    }
    const table = el("table", { class: "subgroup-table" },
      el("caption", { class: "sr-only" }, "Model performance by country and gender"),
      el("thead", {}, el("tr", {},
        el("th", { scope: "col" }, "Segment"),
        el("th", { scope: "col", class: "n" }, "Customers"),
        el("th", { scope: "col", class: "n" }, "Accuracy"),
        el("th", { scope: "col", class: "n" }, "F1"),
        el("th", { scope: "col", class: "n" }, "Recall"),
        el("th", { scope: "col", class: "n" }, "Observed"),
        el("th", { scope: "col", class: "n" }, "Predicted"),
      )),
      el("tbody", {}, rows.map((row) => el("tr", {},
        el("td", {},
          el("span", { class: "segment-name" }, row.segment),
          el("span", { class: "cell-sub" }, featureLabel(row.group)),
        ),
        el("td", { class: "n" }, num(row.values.n)),
        el("td", { class: "n" }, pct(row.values.accuracy, 1)),
        el("td", { class: "n" }, pct(row.values.f1, 1)),
        el("td", { class: "n" }, pct(row.values.recall, 1)),
        el("td", { class: "n" }, pct(row.values.observed_churn_rate, 1)),
        el("td", { class: "n" }, pct(row.values.mean_predicted, 1)),
      ))),
    );
    return el("section", { class: "panel", "aria-labelledby": "subgroups-title" },
      el("div", { class: "panel-head" },
        el("div", {},
          el("h2", { id: "subgroups-title" }, "Performance by group"),
          el("p", {}, "Held-out rows split by country and gender."),
        ),
      ),
      rows.length ? el("p", { class: "table-scroll-hint" }, "Scroll sideways to see every column.") : null,
      rows.length
        ? el("div", { class: "table-wrap", role: "region", "aria-label": "Performance by group", tabindex: "0" }, table)
        : el("p", { class: "batch-note" }, "No subgroup metrics are available."),
      el("p", { class: "batch-note" },
        "Observed is the actual churn rate; predicted is the mean model score. Compare the two within a row."),
      rows.length ? subgroupViz(rows) : null,
    );
  }

  function calibrationSvg(width, calibration) {
    const height = 300;
    const margin = { left: 46, right: 16, top: 14, bottom: 44 };
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const x = (value) => margin.left + value * plotW;
    const y = (value) => margin.top + plotH - value * plotH;
    const maxN = Math.max(...calibration.map((point) => Number(point.n) || 0), 1);
    const root = svg("svg", {
      class: "chart",
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": "Calibration chart comparing mean predicted probability with observed churn rate",
    });
    for (const tick of [0, 0.5, 1]) {
      root.append(svg("line", {
        x1: margin.left, x2: margin.left + plotW,
        y1: y(tick), y2: y(tick), class: "chart-grid",
      }));
      root.append(svg("line", {
        x1: x(tick), x2: x(tick),
        y1: margin.top, y2: margin.top + plotH, class: "chart-grid",
      }));
      root.append(svg("text", {
        x: margin.left - 8, y: y(tick) + 3,
        class: "chart-label", "text-anchor": "end",
      }, pct(tick, 0)));
      root.append(svg("text", {
        x: x(tick), y: margin.top + plotH + 18,
        class: "chart-label", "text-anchor": "middle",
      }, pct(tick, 0)));
    }
    root.append(svg("line", {
      x1: margin.left, x2: margin.left + plotW,
      y1: margin.top + plotH, y2: margin.top, class: "chart-identity",
    }));
    for (const point of calibration) {
      const dot = svg("circle", {
        cx: x(Number(point.mean_predicted) || 0),
        cy: y(Number(point.observed_rate) || 0),
        r: 3.5 + 5 * Math.sqrt((Number(point.n) || 0) / maxN),
        class: "chart-dot",
      });
      dot.append(svg("title", {}, `${num(point.n)} customers · predicted ${pct(point.mean_predicted, 1)} · observed ${pct(point.observed_rate, 1)}`));
      root.append(dot);
    }
    root.append(svg("text", {
      x: x(0.5), y: height - 8,
      class: "chart-label", "text-anchor": "middle",
    }, "Mean predicted churn"));
    root.append(svg("text", {
      x: 12, y: y(0.5), transform: `rotate(-90 12 ${y(0.5)})`,
      class: "chart-label", "text-anchor": "middle",
    }, "Observed churn"));
    return root;
  }

  function calibrationPanel(metrics) {
    const calibration = Array.isArray(metrics.calibration) ? metrics.calibration : [];
    const host = el("div", { class: "chart-host" });
    if (calibration.length) mountChart(host, (width) => calibrationSvg(width, calibration));
    return el("section", { class: "panel chart-panel", "aria-labelledby": "calibration-title" },
      el("div", { class: "panel-head" },
        el("div", {},
          el("h2", { id: "calibration-title" }, "Calibration"),
          el("p", {}, "Predicted probability compared with observed churn in the test set."),
        ),
      ),
      calibration.length ? host : el("p", { class: "batch-note" }, "No calibration data is available."),
      el("p", { class: "chart-note" },
        "Points near the dashed line are well calibrated. Dot size follows the number of customers in the bin."),
    );
  }

  function renderModel(metrics) {
    const out = $("#modelOut");
    const meta = el("p", { class: "model-meta" },
      `${algorithmLabel(metrics.algorithm)} · version ${metrics.model_version || "—"} · ${num(metrics.n_train)} train / ${num(metrics.n_test)} test rows · Brier ${fixed(metrics.brier, 3)}`);
    const content = el("div", { class: "content-stack" },
      meta,
      metrics.model_loaded === false
        ? el("div", { class: "result-note" }, "The API has no model loaded. These are the last saved metrics.")
        : null,
      el("div", { class: "grid-5-7" },
        confusionPanel(metrics),
        importancePanel(metrics),
      ),
      candidatesPanel(metrics),
      el("div", { class: "grid-7-5" },
        subgroupPanel(metrics),
        calibrationPanel(metrics),
      ),
    );
    out.replaceChildren(
      summaryStrip([
        { label: "Accuracy", value: pct(metrics.accuracy, 1), note: "Held-out test rows" },
        { label: "F1", value: fixed(metrics.f1), note: "Positive-class F1" },
        { label: "ROC AUC", value: fixed(metrics.roc_auc), note: "Ranking quality" },
        { label: "Flag line", value: pct(metrics.threshold, 1), note: "Scores at or above are flagged" },
      ]),
      content,
    );
    paintCharts(out);
  }

  async function loadModel() {
    const out = $("#modelOut");
    out.setAttribute("aria-busy", "true");
    out.replaceChildren(loadingState("Loading model metrics"));
    try {
      const metrics = await api("/metrics");
      renderModel(metrics);
      state.loaded.model = true;
      setStatus("ok", "API online");
      setModelVersion(metrics.model_version);
    } catch (error) {
      out.replaceChildren(el("div", { class: "content-stack" },
        errorState("Could not load model metrics", error, loadModel)));
      state.loaded.model = false;
      if (!(error instanceof ApiError)) setStatus("bad", "API unreachable");
    } finally {
      out.removeAttribute("aria-busy");
    }
  }

  function scoreAgain(features) {
    fillForm(features);
    renderScoreEmpty();
    goToView("score");
    setTimeout(() => $("#scoreBtn").focus(), 80);
  }

  function renderHistory(items) {
    const out = $("#historyOut");
    if (!items.length) {
      const button = el("button", { class: "button primary", type: "button" }, "Score a customer");
      button.addEventListener("click", () => goToView("score"));
      out.replaceChildren(
        el("div", { class: "panel" },
          el("div", { class: "empty-panel" },
            el("h2", {}, "No scores yet"),
            el("p", {}, "Scores you run from this browser will appear here. Batch scores are listed too."),
            button,
          ),
        ),
      );
      return;
    }
    const table = el("table", { class: "history-table" },
      el("caption", { class: "sr-only" }, "Recent scores from this browser"),
      el("thead", {}, el("tr", {},
        el("th", { scope: "col" }, "When"),
        el("th", { scope: "col" }, "Source"),
        el("th", { scope: "col" }, "Profile"),
        el("th", { scope: "col" }, "Result"),
        el("th", { scope: "col" }, el("span", { class: "sr-only" }, "Action")),
      )),
      el("tbody", {}, items.map((item) => {
        const isBatch = item.endpoint === "batch_predict";
        const when = el("td", { class: "history-when", "data-label": "When" },
          el("time", { datetime: item.ts || "" }, formatDate(item.ts)));
        const source = el("td", { class: "history-source", "data-label": "Source" }, formatSource(item.endpoint));
        const profile = el("td", { class: "history-profile", "data-label": "Profile" },
          isBatch ? `${num(item.n_scored)} customers` : profileLine(item.features));
        const result = isBatch
          ? el("td", { class: "history-result", "data-label": "Result" },
            el("span", { class: "batch-note" }, `${num(item.n_churn)} flagged of ${num(item.n_scored)}`))
          : el("td", { class: "history-result", "data-label": "Result" },
            el("span", { class: `tag ${item.churn === 1 ? "tag-risk" : "tag-stay"}` },
              item.churn === 1 ? "Flagged" : "Below line"),
            el("span", { class: "batch-note" }, pct(item.probability, 1)));
        const action = el("td", { class: "history-action", "data-label": "Action" });
        if (!isBatch && item.features) {
          const button = el("button", { class: "text-button", type: "button" }, "Score again");
          button.addEventListener("click", () => scoreAgain(item.features));
          action.append(button);
        } else {
          action.append(el("span", { class: "batch-note" }, "—"));
        }
        return el("tr", {}, when, source, profile, result, action);
      })),
    );
    out.replaceChildren(
      el("div", { class: "panel" },
        el("div", { class: "table-wrap", role: "region", "aria-label": "Recent scores", tabindex: "0" }, table),
        el("p", { class: "batch-note" },
          `Showing the latest ${num(items.length)} ${items.length === 1 ? "score" : "scores"}. Stored for 30 days.`),
      ),
    );
  }

  async function loadHistory() {
    if (state.historyBusy) return;
    state.historyBusy = true;
    const out = $("#historyOut");
    const refresh = $("#refreshHistory");
    refresh.disabled = true;
    out.setAttribute("aria-busy", "true");
    out.replaceChildren(loadingState("Loading recent scores"));
    try {
      const response = await api("/history?limit=50");
      renderHistory(Array.isArray(response.items) ? response.items : []);
      setStatus("ok", "API online");
    } catch (error) {
      out.replaceChildren(el("div", { class: "content-stack" },
        errorState("Could not load recent scores", error, loadHistory)));
      if (!(error instanceof ApiError)) setStatus("bad", "API unreachable");
    } finally {
      state.historyBusy = false;
      refresh.disabled = false;
      out.removeAttribute("aria-busy");
    }
  }

  function initHistory() {
    $("#refreshHistory").addEventListener("click", loadHistory);
  }

  function init() {
    initTheme();
    initNavigation();
    initScore();
    initBatch();
    initHistory();
    checkHealth();
  }

  init();
})();
