/**
 * 图片工作台页面逻辑。
 *
 * 页面运行在 AstrBot WebUI 的受限 iframe 中，所有后端调用都必须经过
 * window.AstrBotPluginPage bridge。受限环境拿不到 Dashboard 的登录态和浏览器存储，
 * 因此图库分页、筛选等状态只保存在页面内存中，刷新即重置。
 */
const bridge = window.AstrBotPluginPage;
const PAGE_I18N_KEY = "pages.studio";
const GALLERY_PAGE_SIZE = 24;
const REFERENCE_IMAGE_PREVIEW_URLS = new WeakMap();
// 浮层缩放范围：下限是「适应屏幕」，保证图片永远不会比整屏还小。
const LIGHTBOX_MIN_ZOOM = 1;
const LIGHTBOX_MAX_ZOOM = 8;
const LIGHTBOX_WHEEL_FACTOR = 1.2;
// 适应屏幕时四周留出的呼吸空间，与缩放倍率无关，只影响 fit 尺寸。
const LIGHTBOX_FIT_PADDING = 32;
// 判定为拖拽的最小位移。超过它就不该触发点击关闭。
const LIGHTBOX_DRAG_THRESHOLD = 4;

const state = {
  images: [],
  selected: null,
  activeTab: "create",
  galleryPage: 1,
  totalPages: 1,
  total: 0,
  referenceImageFiles: [],
  createSize: "auto",
  createCount: 1,
  isGenerating: false,
  lastGeneratedImages: [],
};

const $ = (id) => document.getElementById(id);

function t(key, fallback) {
  return bridge.t(`${PAGE_I18N_KEY}.${key}`, fallback);
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 3600);
}

function applyI18n() {
  document.title = t("title", "图片工作台");
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const text = t(node.dataset.i18n, node.textContent.trim());
    if (text) {
      node.textContent = text;
    }
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    const text = t(node.dataset.i18nPlaceholder, node.placeholder);
    if (text) {
      node.placeholder = text;
    }
  });
  if (state.selected) {
    clearPreview();
  }
  renderGallery();
}

function formatBytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatPrompt(value) {
  return String(value || "").trim() || t("notRecorded", "未记录");
}

function formatImageDimensions(width, height) {
  if (!width || !height) return t("notRecorded", "未记录");
  return `${width}×${height}`;
}

function resolveGalleryColumnCount() {
  const galleryWidth = $("gallery").clientWidth || 0;
  if (!galleryWidth) return 1;
  const targetColumnWidth = 210;
  const columnGap = 12;
  return Math.max(
    1,
    Math.floor((galleryWidth + columnGap) / (targetColumnWidth + columnGap)),
  );
}

function distributeImagesByRows(images) {
  // 按行优先分发：第 1-N 张先铺满第一行再进入下一行，每列内部保持纵向自然流。
  const columnCount = resolveGalleryColumnCount();
  const columns = Array.from({ length: columnCount }, () => []);
  images.forEach((image, index) => {
    columns[index % columnCount].push(image);
  });
  return columns;
}

function showTab(tabId) {
  state.activeTab = tabId;
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.id !== `${tabId}Tab`);
  });
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === tabId);
  });
  if (tabId === "gallery") {
    renderGallery();
  } else if (tabId === "create") {
    resizePromptTextarea($("createPrompt"));
  }
}

function resizePromptTextarea(textarea) {
  if (!textarea) return;
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(240, Math.max(52, textarea.scrollHeight))}px`;
}

function resizePromptTextareas() {
  resizePromptTextarea($("createPrompt"));
}

function renderPromptOptimizerSettings(settings) {
  const select = $("promptOptimizerProvider");
  const selectedId = settings.provider_id || "";
  const defaultLabel = settings.default_model
    ? `${t("defaultModel", "使用 AstrBot 默认模型")} (${settings.default_model})`
    : t("defaultModel", "使用 AstrBot 默认模型");

  select.innerHTML = "";
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = defaultLabel;
  select.appendChild(defaultOption);

  const providers = Array.isArray(settings.providers) ? settings.providers : [];
  providers.forEach((prov) => {
    const opt = document.createElement("option");
    opt.value = prov.id;
    const modelName = prov.model || prov.id;
    opt.textContent =
      prov.id !== modelName ? `${modelName} (${prov.id})` : prov.id;
    select.appendChild(opt);
  });

  select.value = selectedId;
}

async function loadPromptOptimizerSettings() {
  renderPromptOptimizerSettings(await bridge.apiGet("prompt-optimizer-settings"));
}

async function savePromptOptimizerSettings() {
  const button = $("savePromptOptimizerSettingsBtn");
  button.disabled = true;
  try {
    const settings = await bridge.apiPost("prompt-optimizer-settings", {
      provider_id: $("promptOptimizerProvider").value,
    });
    renderPromptOptimizerSettings(settings);
    showToast(t("optimizerSaved", "提示词优化设置已保存"));
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function optimizePrompt(button) {
  if (state.isGenerating) return;
  const textarea = $(button.dataset.promptTarget || "createPrompt");
  const prompt = textarea.value.trim();
  if (!prompt) {
    showToast(t("promptRequired", "请先输入提示词"));
    textarea.focus();
    return;
  }

  const hasReferences = state.referenceImageFiles.length > 0;
  const mode = hasReferences ? "edit" : "generate";
  const label = button.querySelector("span");
  const originalLabel = label ? label.textContent : button.textContent;
  button.disabled = true;
  textarea.disabled = true;
  button.classList.add("is-loading");
  if (label) label.textContent = t("optimizing", "优化中");
  try {
    const data = await bridge.apiPost("optimize-prompt", {
      prompt,
      mode,
    });
    textarea.value = data.prompt || "";
    resizePromptTextarea(textarea);
    showToast(t("promptOptimized", "提示词已优化"));
  } catch (error) {
    showToast(error.message);
  } finally {
    if (label) label.textContent = originalLabel;
    button.classList.remove("is-loading");
    textarea.disabled = false;
    button.disabled = false;
    textarea.focus();
    $("createSubmitBtn").disabled =
      !textarea.value.trim() || state.isGenerating;
  }
}

function renderGallery() {
  const gallery = $("gallery");
  if (!state.images.length) {
    gallery.style.setProperty("--gallery-column-count", "1");
    gallery.innerHTML = `<div class="empty-state">${t(
      "emptyGallery",
      "暂无图片，可切换到生图页面创建新图片。",
    )}</div>`;
  } else {
    const columns = distributeImagesByRows(state.images);
    gallery.style.setProperty("--gallery-column-count", String(columns.length));
    gallery.innerHTML = columns
      .map(
        (columnImages) => `
        <div class="gallery-column">
          ${columnImages.map((image) => renderImageCard(image)).join("")}
        </div>`,
      )
      .join("");
  }

  gallery.querySelectorAll(".image-card").forEach((card) => {
    card.addEventListener("click", () => {
      selectImage(card.dataset.name).catch((error) => showToast(error.message));
    });
  });
  gallery.querySelectorAll(".delete-image-action").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteImageByName(button.dataset.name).catch((error) =>
        showToast(error.message),
      );
    });
  });
  updateGalleryPagination();
}

function renderImageCard(image) {
  // 文件名由 ImageCacheStore 生成（时间戳 + 随机 hex + 白名单后缀），不含用户可控字符，
  // 因此可以直接拼进 data-name 和 title；用户提示词只通过 textContent 输出。
  const selectedClass =
    state.selected && state.selected.name === image.name ? " selected" : "";
  return `
    <button class="image-card${selectedClass}" type="button" data-name="${image.name}">
      <span class="image-card-action delete-image-action" data-name="${image.name}" title="${t("delete", "删除")}">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M3 6h18" />
          <path d="M8 6V4h8v2" />
          <path d="M19 6l-1 14H6L5 6" />
          <path d="M10 11v5" />
          <path d="M14 11v5" />
        </svg>
      </span>
      <img class="thumb" src="${image.thumbnail || ""}" alt="${image.name}" loading="lazy" />
      <div class="card-meta">
        <div class="card-name" title="${image.name}">${image.name}</div>
        <div class="card-sub">
          <span>${formatBytes(image.size_bytes)}</span>
          <span>${new Date(image.modified_at * 1000).toLocaleString()}</span>
        </div>
      </div>
    </button>`;
}

function updateGalleryPagination() {
  const pagination = $("galleryPagination");
  $("imageCount").textContent = String(state.total);
  if (state.totalPages <= 1) {
    pagination.classList.add("hidden");
    return;
  }

  pagination.classList.remove("hidden");
  const startIndex = (state.galleryPage - 1) * GALLERY_PAGE_SIZE + 1;
  const endIndex = Math.min(startIndex + state.images.length - 1, state.total);
  $("pageSummary").textContent = t(
    "pageSummary",
    "第 {page} / {total} 页，显示 {start}-{end} 张，共 {count} 张",
  )
    .replace("{page}", String(state.galleryPage))
    .replace("{total}", String(state.totalPages))
    .replace("{start}", String(startIndex))
    .replace("{end}", String(endIndex))
    .replace("{count}", String(state.total));
  $("prevPageBtn").disabled = state.galleryPage <= 1;
  $("nextPageBtn").disabled = state.galleryPage >= state.totalPages;
}

function clearPreview() {
  state.selected = null;
  $("previewBox").textContent = t("selectHint", "请选择一张图片");
  $("detailTitle").textContent = t("noSelection", "暂无选中图片");
  $("detailPrompt").textContent = "-";
  $("detailGenerationSize").textContent = "-";
  $("detailType").textContent = "-";
  $("detailSize").textContent = "-";
  $("detailTime").textContent = "-";
  $("viewOriginalBtn").disabled = true;
  $("deleteImageBtn").disabled = true;
  updateGallerySelection();
}

function updateGallerySelection() {
  $("gallery")
    .querySelectorAll(".image-card")
    .forEach((card) => {
      card.classList.toggle(
        "selected",
        Boolean(state.selected) && card.dataset.name === state.selected.name,
      );
    });
}

async function fetchFullImage(name) {
  const { image } = await bridge.apiGet("image", { name });
  return image;
}

async function selectImage(name) {
  const image = state.images.find((item) => item.name === name);
  if (!image) return;
  state.selected = image;

  const previewBox = $("previewBox");
  previewBox.textContent = t("loading", "图片加载中...");
  $("detailTitle").textContent = image.name;
  $("detailPrompt").textContent = formatPrompt(image.prompt);
  $("detailGenerationSize").textContent = t("loading", "读取中...");
  $("detailType").textContent = image.mime_type;
  $("detailSize").textContent = formatBytes(image.size_bytes);
  $("detailTime").textContent = new Date(image.modified_at * 1000).toLocaleString();
  $("viewOriginalBtn").disabled = true;
  $("deleteImageBtn").disabled = false;
  updateGallerySelection();

  // 图库缩略图长边只有 768px，大图预览需要单独取一次原图 data URL。
  let fullImage;
  try {
    fullImage = await fetchFullImage(name);
  } catch (error) {
    // 取原图失败时必须把详情栏和预览区从“读取中”切走，否则看上去像页面卡死。
    $("detailGenerationSize").textContent = t("loadFailed", "加载失败");
    previewBox.textContent = t("loadFailed", "原图加载失败");
    showToast(error.message);
    return;
  }

  image.data_url = fullImage.data_url;
  $("viewOriginalBtn").disabled = false;
  const previewImage = document.createElement("img");
  previewImage.alt = image.name;
  previewImage.addEventListener(
    "load",
    () => {
      $("detailGenerationSize").textContent = formatImageDimensions(
        previewImage.naturalWidth,
        previewImage.naturalHeight,
      );
    },
    { once: true },
  );
  previewImage.src = image.data_url;
  previewBox.replaceChildren(previewImage);
}

// iframe 的 sandbox 不含 allow-popups，window.open 必定返回 null，
// 所以原图只能在页内全屏浮层里展示，点击任意处或按 Esc 关闭。
// 缩放以「适应屏幕」为 1 倍（下限），读数显示相对原始像素的真实百分比。
const lightbox = {
  zoom: 1,
  fitScale: 1,
  naturalWidth: 0,
  naturalHeight: 0,
  drag: null,
  suppressClick: false,
};

function lightboxFitScale() {
  const stage = $("lightboxStage");
  const availableWidth = stage.clientWidth - LIGHTBOX_FIT_PADDING;
  const availableHeight = stage.clientHeight - LIGHTBOX_FIT_PADDING;
  if (!lightbox.naturalWidth || !lightbox.naturalHeight) return 1;
  // 不放大到超过原始尺寸，避免小图被拉糊。
  return Math.min(
    availableWidth / lightbox.naturalWidth,
    availableHeight / lightbox.naturalHeight,
    1,
  );
}

function applyLightboxZoom() {
  // 尺寸未知时绝不能写样式：0 会被当成合法的 width/height，图片就变成 0x0 不可见。
  if (!lightbox.naturalWidth || !lightbox.naturalHeight) return;
  const scale = lightbox.fitScale * lightbox.zoom;
  $("lightboxImage").style.width = `${Math.round(lightbox.naturalWidth * scale)}px`;
  $("lightboxImage").style.height = `${Math.round(
    lightbox.naturalHeight * scale,
  )}px`;
  $("lightboxZoomLabel").textContent = `${Math.round(scale * 100)}%`;
  if (lightbox.zoom > LIGHTBOX_MIN_ZOOM) {
    $("lightboxImage").classList.add("can-drag");
  } else {
    $("lightboxImage").classList.remove("can-drag");
  }
}

function layoutLightbox() {
  // 必须先把图片的真实像素尺寸写回状态，否则下面的 fit 会读到初始的 0。
  const lightboxImage = $("lightboxImage");
  lightbox.naturalWidth = lightboxImage.naturalWidth;
  lightbox.naturalHeight = lightboxImage.naturalHeight;
  lightbox.fitScale = lightboxFitScale();
  applyLightboxZoom();
}

function showOriginalImage(image) {
  if (!image || !image.data_url) return;
  const lightboxImage = $("lightboxImage");
  // 先显示浮层再设置 src，让图片在有布局盒的状态下加载并解码。
  $("lightbox").classList.remove("hidden");
  lightbox.zoom = LIGHTBOX_MIN_ZOOM;
  lightbox.naturalWidth = 0;
  lightbox.naturalHeight = 0;
  lightboxImage.style.width = "";
  lightboxImage.style.height = "";
  lightboxImage.draggable = false;
  lightboxImage.src = image.data_url;
  lightboxImage.alt = image.name;

  const whenSized = () => {
    layoutLightbox();
    if (lightbox.naturalWidth) return;
    // data URL 也不保证 load 回调里就拿到尺寸，解码完成后再试一次。
    lightboxImage
      .decode()
      .catch(() => {})
      .finally(layoutLightbox);
  };

  if (lightboxImage.complete && lightboxImage.naturalWidth) {
    whenSized();
    return;
  }
  lightboxImage.addEventListener("load", whenSized, { once: true });
}

function hideLightbox() {
  $("lightbox").classList.add("hidden");
  $("lightboxImage").removeAttribute("src");
  $("lightboxImage").alt = "";
  $("lightboxImage").style.width = "";
  $("lightboxImage").style.height = "";
  lightbox.zoom = LIGHTBOX_MIN_ZOOM;
  lightbox.naturalWidth = 0;
  lightbox.naturalHeight = 0;
  lightbox.drag = null;
  lightbox.suppressClick = false;
  $("lightboxImage").classList.remove("is-dragging");
  $("lightboxImage").classList.remove("can-drag");
  $("lightboxStage").scrollTo(0, 0);
}

async function deleteImageByName(name) {
  const image = state.images.find((item) => item.name === name);
  if (!image) return;
  const confirmed = window.confirm(
    t("deleteConfirm", "确定删除图片 {name} 吗？服务端缓存文件也会被删除。").replace(
      "{name}",
      image.name,
    ),
  );
  if (!confirmed) return;

  await bridge.apiPost("image/delete", { name: image.name });
  if (state.selected && state.selected.name === image.name) {
    clearPreview();
  }
  await loadGallery();
  showToast(t("deleted", "图片已删除"));
}

async function loadGallery() {
  const data = await bridge.apiGet("images", {
    keyword: $("searchInput").value.trim(),
    type: $("typeFilter").value,
    sort: $("sortFilter").value,
    page: state.galleryPage,
    page_size: GALLERY_PAGE_SIZE,
  });
  state.images = data.images || [];
  state.total = Number(data.total || 0);
  state.totalPages = Math.max(1, Number(data.total_pages || 1));
  state.galleryPage = Math.max(1, Number(data.page || 1));
  renderGallery();
}

function renderReferenceChips() {
  const container = $("referenceChips");
  if (!container) return;
  container.innerHTML = state.referenceImageFiles
    .map((file, index) => {
      if (!REFERENCE_IMAGE_PREVIEW_URLS.has(file)) {
        REFERENCE_IMAGE_PREVIEW_URLS.set(file, URL.createObjectURL(file));
      }
      return `
        <div class="reference-chip">
          <img src="${REFERENCE_IMAGE_PREVIEW_URLS.get(file)}" alt="参考图片 ${index + 1}" />
          <button class="reference-chip-remove" type="button" data-index="${index}" title="移除参考图片">✕</button>
        </div>`;
    })
    .join("");

  container.querySelectorAll(".reference-chip-remove").forEach((button) => {
    button.addEventListener("click", () => {
      removeReferenceImageFile(Number(button.dataset.index));
    });
  });
}

function removeReferenceImageFile(index) {
  const file = state.referenceImageFiles[index];
  if (!file) return;
  const previewUrl = REFERENCE_IMAGE_PREVIEW_URLS.get(file);
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  REFERENCE_IMAGE_PREVIEW_URLS.delete(file);
  state.referenceImageFiles.splice(index, 1);
  renderReferenceChips();
}

function addReferenceImageFile(file) {
  if (!file || !file.type.startsWith("image/")) {
    showToast(t("imageOnly", "请提供图片文件"));
    return;
  }
  state.referenceImageFiles.push(file);
  renderReferenceChips();
}

function handlePasteImage(event) {
  const items = event.clipboardData && event.clipboardData.items;
  if (!items) return;
  let addedCount = 0;
  for (const item of items) {
    if (item.type && item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (file) {
        event.preventDefault();
        addReferenceImageFile(file);
        addedCount += 1;
      }
    }
  }
  if (addedCount > 1) {
    showToast(
      t("addedReferences", "已添加 {count} 张参考图片").replace(
        "{count}",
        String(addedCount),
      ),
    );
  }
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () =>
      reject(new Error(t("readImageFailed", "参考图读取失败")));
    reader.readAsDataURL(file);
  });
}

function renderResultStatus(message) {
  const resultArea = $("createResultArea");
  resultArea.classList.remove("hidden");

  resultArea.innerHTML = `
    <div class="aurora-loading-container">
      <div class="aurora-ring-wrapper">
        <div class="aurora-glow"></div>
        <div class="aurora-ring">
          <div class="aurora-ring-core"></div>
        </div>
        <svg class="icon aurora-center-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" />
        </svg>
      </div>
      <div class="aurora-status-text">${message}</div>
      <div class="aurora-line-wrapper">
        <div class="aurora-line"></div>
      </div>
    </div>`;
}

function renderResultError(message) {
  const resultArea = $("createResultArea");
  resultArea.classList.remove("hidden");
  resultArea.innerHTML = `
    <div class="result-error-card">
      <div class="result-error-info">
        <div class="result-error-title">${t("taskFailed", "图片处理失败")}</div>
        <div class="result-error-desc">${message || t("taskFailedHint", "请查看错误信息后重试。")}</div>
      </div>
      <button id="retryCreateBtn" class="btn primary" type="button">${t("retry", "重试")}</button>
    </div>`;
  $("retryCreateBtn").addEventListener("click", () => submitCreateTask());
}

function renderCreateResults(images, promptText) {
  const resultArea = $("createResultArea");
  if (!images.length) {
    resultArea.classList.add("hidden");
    return;
  }
  resultArea.classList.remove("hidden");
  resultArea.innerHTML = `
    <div class="result-header">
      <span class="result-tag">${t("latestResult", "本次生成结果")} (${images.length}张)</span>
      <button id="hideResultBtn" class="btn text-btn" type="button">${t("collapse", "收起")}</button>
    </div>
    <div class="result-grid ${images.length === 1 ? "result-single" : "result-multi"}">
      ${images
        .map(
          (image, index) => `
        <div class="result-card">
          <img src="${image.data_url}" alt="${image.name}" data-index="${index}" title="${t("viewOriginal", "查看原图")}" />
        </div>`,
        )
        .join("")}
    </div>`;

  $("hideResultBtn").addEventListener("click", () => {
    resultArea.classList.add("hidden");
  });

  resultArea.querySelectorAll(".result-card img").forEach((imgNode) => {
    imgNode.addEventListener("click", () => {
      const index = Number(imgNode.dataset.index);
      showOriginalImage(images[index]);
    });
  });
}

async function submitCreateTask() {
  const promptInput = $("createPrompt");
  const prompt = promptInput.value.trim();
  if (!prompt) {
    showToast(t("promptRequired", "请先输入提示词"));
    promptInput.focus();
    return;
  }
  if (state.isGenerating) return;

  state.isGenerating = true;
  $("createSubmitBtn").disabled = true;
  $("optimizePromptBtn").disabled = true;

  let sizeValue = state.createSize;
  if (sizeValue === "custom") {
    sizeValue = $("createCustomSize").value.trim() || "auto";
  }

  const quality = $("createQuality").value;
  const moderation = $("createModeration").value;
  const count = state.createCount;
  const hasReferences = state.referenceImageFiles.length > 0;
  const startTime = Date.now();
  let timerInterval = null;

  try {
    const finalPrompt = prompt;

    let slotAspect = "1 / 1";
    if (sizeValue) {
      const [w, h] = sizeValue.split("x").map(Number);
      if (w && h) slotAspect = `${w} / ${h}`;
    }

    const resultArea = $("createResultArea");
    resultArea.classList.remove("hidden");
    resultArea.innerHTML = `
      <div class="result-header">
        <div class="result-header-left">
          <span id="resultHeaderTag" class="result-tag">${t("latestResult", "本次生成结果")} (0/${count})</span>
          <span id="resultTimeBadge" class="result-time-badge">⏱ 0.0s</span>
        </div>
        <button id="hideResultBtn" class="btn text-btn" type="button">${t("collapse", "收起")}</button>
      </div>
      <div class="result-grid ${count === 1 ? "result-single" : "result-multi"}">
        ${Array.from({ length: count }, (_, i) => `
          <div class="result-card slot-loading" data-slot="${i}" style="aspect-ratio: ${slotAspect}">
            <div class="aurora-ring-wrapper">
              <div class="aurora-glow"></div>
              <div class="aurora-ring">
                <div class="aurora-ring-core"></div>
              </div>
              <svg class="icon aurora-center-icon" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" />
              </svg>
            </div>
            <span class="slot-loading-tag">${count > 1 ? `#${i + 1} · ` : ""}${t("generating", "正在生成图片，请稍候...")} <span class="slot-timer">0.0s</span></span>
          </div>
        `).join("")}
      </div>`;

    $("hideResultBtn").addEventListener("click", () => {
      resultArea.classList.add("hidden");
    });

    const updateTimer = () => {
      const elapsedSec = ((Date.now() - startTime) / 1000).toFixed(1);
      const timeStr = `${elapsedSec}s`;
      const badge = $("resultTimeBadge");
      if (badge) {
        badge.textContent = `⏱ ${timeStr}`;
      }
      resultArea.querySelectorAll(".slot-timer").forEach((el) => {
        el.textContent = timeStr;
      });
    };

    timerInterval = window.setInterval(updateTimer, 100);

    let dataUrls = [];
    if (hasReferences) {
      dataUrls = await Promise.all(
        state.referenceImageFiles.map(fileToDataUrl),
      );
    }

    const slots = new Array(count).fill(null);
    let completedCount = 0;

    const updateHeaderTag = () => {
      const tag = $("resultHeaderTag");
      if (tag) {
        tag.textContent = `${t("latestResult", "本次生成结果")} (${completedCount}/${count})`;
      }
    };

    const taskPromises = Array.from({ length: count }, async (_, index) => {
      try {
        const data = await bridge.apiPost(hasReferences ? "edit" : "generate", {
          prompt: finalPrompt,
          size: sizeValue,
          quality,
          moderation,
          ...(hasReferences ? { data_urls: dataUrls } : {}),
        });

        if (!data || !data.image || !data.image.name) {
          throw new Error("服务端未返回图片信息");
        }

        const img = await fetchFullImage(data.image.name);
        slots[index] = img;
        completedCount += 1;
        updateHeaderTag();

        const slotCard = resultArea.querySelector(`.result-card[data-slot="${index}"]`);
        if (slotCard) {
          slotCard.classList.remove("slot-loading");
          slotCard.style.aspectRatio = "";
          slotCard.innerHTML = `<img src="${img.data_url}" alt="${img.name}" data-index="${index}" title="${t("viewOriginal", "查看原图")}" />`;
          const imgNode = slotCard.querySelector("img");
          if (imgNode) {
            imgNode.addEventListener("click", () => showOriginalImage(img));
          }
        }
      } catch (err) {
        const slotCard = resultArea.querySelector(`.result-card[data-slot="${index}"]`);
        if (slotCard) {
          slotCard.classList.remove("slot-loading");
          slotCard.classList.add("slot-error");
          slotCard.innerHTML = `
            <svg class="icon slot-error-icon" viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="12" cy="12" r="10"/>
              <line x1="12" y1="8" x2="12" y2="12"/>
              <line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            <span class="slot-error-tag">#${index + 1} 失败</span>
          `;
          slotCard.title = err.message || "生成失败";
        }
        slots[index] = { error: err.message };
      }
    });

    await Promise.allSettled(taskPromises);

    if (timerInterval) {
      window.clearInterval(timerInterval);
      timerInterval = null;
    }

    const finalSec = ((Date.now() - startTime) / 1000).toFixed(1);
    const timeBadge = $("resultTimeBadge");
    if (timeBadge) {
      timeBadge.textContent = `⏱ ${finalSec}s`;
      timeBadge.classList.add("is-finished");
    }

    const validImages = slots.filter((item) => item && item.data_url);
    state.lastGeneratedImages = validImages;

    if (validImages.length > 0) {
      const tag = $("resultHeaderTag");
      if (tag) {
        tag.textContent = `${t("latestResult", "本次生成结果")} (${validImages.length}张)`;
      }
      showToast(t("taskDone", "图片处理完成"));
      loadGallery().catch(() => {});
    } else {
      const firstError = slots.find((item) => item && item.error)?.error;
      renderResultError(firstError || t("taskFailedHint", "请查看错误信息后重试。"));
    }
  } catch (error) {
    renderResultError(error.message);
    showToast(error.message);
  } finally {
    if (timerInterval) {
      window.clearInterval(timerInterval);
      timerInterval = null;
    }
    state.isGenerating = false;
    $("createSubmitBtn").disabled = !promptInput.value.trim();
    $("optimizePromptBtn").disabled = false;
  }
}

$("refreshBtn").addEventListener("click", () => {
  loadGallery().catch((error) => showToast(error.message));
});

// 搜索走后端分页查询，因此需要防抖，否则每次按键都会打一次 bridge 请求。
let searchDebounceTimer = null;
$("searchInput").addEventListener("input", () => {
  window.clearTimeout(searchDebounceTimer);
  searchDebounceTimer = window.setTimeout(() => {
    state.galleryPage = 1;
    loadGallery().catch((error) => showToast(error.message));
  }, 300);
});

["typeFilter", "sortFilter"].forEach((id) => {
  $(id).addEventListener("change", () => {
    state.galleryPage = 1;
    loadGallery().catch((error) => showToast(error.message));
  });
});

$("prevPageBtn").addEventListener("click", () => {
  state.galleryPage = Math.max(1, state.galleryPage - 1);
  loadGallery().catch((error) => showToast(error.message));
});

$("nextPageBtn").addEventListener("click", () => {
  state.galleryPage += 1;
  loadGallery().catch((error) => showToast(error.message));
});

$("savePromptOptimizerSettingsBtn").addEventListener("click", () => {
  savePromptOptimizerSettings();
});

function closeAllDropdowns() {
  $("sizeMenu").classList.add("hidden");
  $("countMenu").classList.add("hidden");
  $("advancedMenu").classList.add("hidden");
  $("sizeDropdownBtn").setAttribute("aria-expanded", "false");
  $("countDropdownBtn").setAttribute("aria-expanded", "false");
}

$("referenceUploadBtn").addEventListener("click", () => {
  $("referenceFileInput").click();
});

$("referenceFileInput").addEventListener("change", () => {
  Array.from($("referenceFileInput").files || []).forEach(addReferenceImageFile);
  $("referenceFileInput").value = "";
});

$("createPrompt").addEventListener("input", () => {
  resizePromptTextarea($("createPrompt"));
  $("createSubmitBtn").disabled =
    !$("createPrompt").value.trim() || state.isGenerating;
});

$("createPrompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submitCreateTask();
  }
});

$("createSubmitBtn").addEventListener("click", () => {
  submitCreateTask();
});

$("optimizePromptBtn").addEventListener("click", () => {
  optimizePrompt($("optimizePromptBtn"));
});

$("sizeDropdownBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  const willOpen = $("sizeMenu").classList.contains("hidden");
  closeAllDropdowns();
  if (willOpen) {
    $("sizeMenu").classList.remove("hidden");
    $("sizeDropdownBtn").setAttribute("aria-expanded", "true");
  }
});

$("sizeMenu").querySelectorAll(".dropdown-item").forEach((item) => {
  item.addEventListener("click", () => {
    state.createSize = item.dataset.size;
    $("sizeLabel").textContent = item.textContent.split(" ")[0];
    $("sizeMenu").querySelectorAll(".dropdown-item").forEach((el) => {
      el.classList.toggle("active", el === item);
    });
    closeAllDropdowns();
  });
});

$("createCustomSize").addEventListener("input", () => {
  const value = $("createCustomSize").value.trim();
  if (value) {
    state.createSize = "custom";
    $("sizeLabel").textContent = value;
    $("sizeMenu").querySelectorAll(".dropdown-item").forEach((el) => {
      el.classList.remove("active");
    });
  }
});

$("countDropdownBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  const willOpen = $("countMenu").classList.contains("hidden");
  closeAllDropdowns();
  if (willOpen) {
    $("countMenu").classList.remove("hidden");
    $("countDropdownBtn").setAttribute("aria-expanded", "true");
  }
});

$("countMenu").querySelectorAll(".dropdown-item").forEach((item) => {
  item.addEventListener("click", () => {
    state.createCount = Number(item.dataset.count);
    $("countLabel").textContent = `${state.createCount}张`;
    $("countMenu").querySelectorAll(".dropdown-item").forEach((el) => {
      el.classList.toggle("active", el === item);
    });
    closeAllDropdowns();
  });
});

$("advancedBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  const willOpen = $("advancedMenu").classList.contains("hidden");
  closeAllDropdowns();
  if (willOpen) {
    $("advancedMenu").classList.remove("hidden");
  }
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".dropdown-wrapper")) {
    closeAllDropdowns();
  }
});

document.addEventListener("paste", (event) => {
  if (state.activeTab === "create") handlePasteImage(event);
});

document.querySelectorAll("[data-tab]").forEach((tab) => {
  tab.addEventListener("click", () => showTab(tab.dataset.tab));
});

$("previewBox").addEventListener("dblclick", () =>
  showOriginalImage(state.selected),
);
$("viewOriginalBtn").addEventListener("click", () =>
  showOriginalImage(state.selected),
);
function zoomLightboxBy(factor, centerX, centerY) {
  const stage = $("lightboxStage");
  const rect = stage.getBoundingClientRect();
  const cursorX = centerX !== undefined ? centerX : rect.width / 2;
  const cursorY = centerY !== undefined ? centerY : rect.height / 2;
  const anchorX = (stage.scrollLeft + cursorX) / Math.max(1, stage.scrollWidth);
  const anchorY = (stage.scrollTop + cursorY) / Math.max(1, stage.scrollHeight);

  lightbox.zoom = Math.min(
    LIGHTBOX_MAX_ZOOM,
    Math.max(LIGHTBOX_MIN_ZOOM, lightbox.zoom * factor),
  );
  applyLightboxZoom();

  stage.scrollLeft = anchorX * stage.scrollWidth - cursorX;
  stage.scrollTop = anchorY * stage.scrollHeight - cursorY;
}

// 拖拽结束后浏览器仍会派发 click，必须吞掉这一次，否则拖完图就被误关。
$("lightbox").addEventListener("click", (event) => {
  if (lightbox.suppressClick) {
    lightbox.suppressClick = false;
    return;
  }
  if (
    event.target.closest("#lightboxToolbar") ||
    event.target.closest(".lightbox-toolbar")
  ) {
    return;
  }
  // 放大状态下点击图片本身不应误关闭浮层
  if (lightbox.zoom > LIGHTBOX_MIN_ZOOM && event.target === $("lightboxImage")) {
    return;
  }
  hideLightbox();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideLightbox();
});

// 禁止图片原生 HTML5 拖拽，防止冲突打断平移手势
$("lightboxImage").addEventListener("dragstart", (event) => {
  event.preventDefault();
});

// 双击图片：未放大时快速放大至 2.5x，已放大时重置回 1x
$("lightboxImage").addEventListener("dblclick", (event) => {
  event.stopPropagation();
  const stage = $("lightboxStage");
  if (lightbox.zoom <= LIGHTBOX_MIN_ZOOM) {
    const rect = stage.getBoundingClientRect();
    const cursorX = event.clientX - rect.left;
    const cursorY = event.clientY - rect.top;
    const anchorX = (stage.scrollLeft + cursorX) / Math.max(1, stage.scrollWidth);
    const anchorY = (stage.scrollTop + cursorY) / Math.max(1, stage.scrollHeight);
    lightbox.zoom = 2.5;
    applyLightboxZoom();
    stage.scrollLeft = anchorX * stage.scrollWidth - cursorX;
    stage.scrollTop = anchorY * stage.scrollHeight - cursorY;
  } else {
    lightbox.zoom = LIGHTBOX_MIN_ZOOM;
    applyLightboxZoom();
    stage.scrollTo(0, 0);
  }
});

// 缩放工具栏操作
$("lightboxZoomInBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  zoomLightboxBy(LIGHTBOX_WHEEL_FACTOR);
});
$("lightboxZoomOutBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  zoomLightboxBy(1 / LIGHTBOX_WHEEL_FACTOR);
});
$("lightboxZoomResetBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  lightbox.zoom = LIGHTBOX_MIN_ZOOM;
  applyLightboxZoom();
  $("lightboxStage").scrollTo(0, 0);
});

// 滚轮缩放必须注册为非 passive，否则 event.preventDefault() 会被浏览器忽略，
// 结果是浮层跟着页面一起滚动而不是缩放图片。
$("lightboxStage").addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const stage = $("lightboxStage");
    const rect = stage.getBoundingClientRect();
    const cursorX = event.clientX - rect.left;
    const cursorY = event.clientY - rect.top;
    // 先记下光标在可滚动内容里的相对位置，改完尺寸再按新内容尺寸还原，
    // 这样光标下的像素保持不动。
    const anchorX = (stage.scrollLeft + cursorX) / Math.max(1, stage.scrollWidth);
    const anchorY = (stage.scrollTop + cursorY) / Math.max(1, stage.scrollHeight);

    lightbox.zoom = Math.min(
      LIGHTBOX_MAX_ZOOM,
      Math.max(
        LIGHTBOX_MIN_ZOOM,
        lightbox.zoom *
          (event.deltaY < 0 ? LIGHTBOX_WHEEL_FACTOR : 1 / LIGHTBOX_WHEEL_FACTOR),
      ),
    );
    applyLightboxZoom();

    stage.scrollLeft = anchorX * stage.scrollWidth - cursorX;
    stage.scrollTop = anchorY * stage.scrollHeight - cursorY;
  },
  { passive: false },
);

// 未放大时不启动拖拽，否则会挡住点击遮罩关闭。
$("lightboxStage").addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return; // 仅限鼠标左键拖拽
  if (lightbox.zoom <= LIGHTBOX_MIN_ZOOM) return;
  event.preventDefault(); // 阻止浏览器原生拖拽和文字选中
  const stage = $("lightboxStage");
  lightbox.drag = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    scrollLeft: stage.scrollLeft,
    scrollTop: stage.scrollTop,
  };
  try {
    stage.setPointerCapture(event.pointerId);
  } catch {}
  $("lightboxImage").classList.add("is-dragging");
});

$("lightboxStage").addEventListener("pointermove", (event) => {
  if (!lightbox.drag || lightbox.drag.pointerId !== event.pointerId) return;
  event.preventDefault();
  const deltaX = event.clientX - lightbox.drag.startX;
  const deltaY = event.clientY - lightbox.drag.startY;
  if (
    !lightbox.suppressClick &&
    Math.hypot(deltaX, deltaY) > LIGHTBOX_DRAG_THRESHOLD
  ) {
    lightbox.suppressClick = true;
  }
  const stage = $("lightboxStage");
  stage.scrollLeft = lightbox.drag.scrollLeft - deltaX;
  stage.scrollTop = lightbox.drag.scrollTop - deltaY;
});

const endLightboxDrag = (event) => {
  if (!lightbox.drag || lightbox.drag.pointerId !== event.pointerId) return;
  try {
    $("lightboxStage").releasePointerCapture(event.pointerId);
  } catch {}
  lightbox.drag = null;
  $("lightboxImage").classList.remove("is-dragging");
};

["pointerup", "pointercancel"].forEach((eventName) => {
  $("lightboxStage").addEventListener(eventName, endLightboxDrag);
});
window.addEventListener("pointerup", (event) => {
  if (lightbox.drag && lightbox.drag.pointerId === event.pointerId) {
    endLightboxDrag(event);
  }
});

$("deleteImageBtn").addEventListener("click", () => {
  if (state.selected) {
    deleteImageByName(state.selected.name).catch((error) =>
      showToast(error.message),
    );
  }
});

window.addEventListener("resize", () => {
  if (state.activeTab === "gallery") renderGallery();
  resizePromptTextareas();
  // 浮层开着时窗口尺寸变了要重算适应屏幕的基准倍率。
  if (!$("lightbox").classList.contains("hidden")) layoutLightbox();
});


await bridge.ready();
applyI18n();
bridge.onContext(applyI18n);
resizePromptTextareas();
try {
  await loadPromptOptimizerSettings();
  await loadGallery();
} catch (error) {
  showToast(error.message);
}
