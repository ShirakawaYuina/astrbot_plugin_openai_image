/**
 * 图片工作台页面逻辑。
 *
 * 页面运行在 AstrBot WebUI 的受限 iframe 中，所有后端调用都必须经过
 * window.AstrBotPluginPage bridge。受限环境拿不到 Dashboard 的登录态和浏览器存储，
 * 因此图库分页、筛选等状态只保存在页面内存中，刷新即重置。
 */
const bridge = window.AstrBotPluginPage;
const PAGE_I18N_KEY = "pages.studio";
const PROMPT_OPTIMIZER_API_KEY_MASK = "********";
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
  $("promptOptimizerModel").value = settings.model || "";
  $("promptOptimizerBaseUrl").value = settings.base_url || "";
  $("promptOptimizerApiKey").value = settings.has_api_key
    ? PROMPT_OPTIMIZER_API_KEY_MASK
    : "";
  $("promptOptimizerKeyState").textContent = settings.has_api_key
    ? t("keySaved", "API Key 已保存")
    : t("keyMissing", "API Key 未保存");
}

async function loadPromptOptimizerSettings() {
  renderPromptOptimizerSettings(await bridge.apiGet("prompt-optimizer-settings"));
}

async function savePromptOptimizerSettings() {
  const button = $("savePromptOptimizerSettingsBtn");
  button.disabled = true;
  try {
    const apiKeyInput = $("promptOptimizerApiKey").value;
    const settings = await bridge.apiPost("prompt-optimizer-settings", {
      model: $("promptOptimizerModel").value,
      base_url: $("promptOptimizerBaseUrl").value,
      api_key: apiKeyInput === PROMPT_OPTIMIZER_API_KEY_MASK ? "" : apiKeyInput,
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
  const textarea = $(button.dataset.promptTarget);
  const prompt = textarea.value.trim();
  if (!prompt) {
    showToast(t("promptRequired", "请先输入提示词"));
    textarea.focus();
    return;
  }

  const label = button.querySelector("span");
  const originalLabel = label.textContent;
  button.disabled = true;
  textarea.disabled = true;
  button.classList.add("is-loading");
  label.textContent = t("optimizing", "优化中");
  try {
    const data = await bridge.apiPost("optimize-prompt", {
      prompt,
      mode: button.dataset.promptMode || "generate",
    });
    textarea.value = data.prompt || "";
    resizePromptTextarea(textarea);
    showToast(t("promptOptimized", "提示词已优化"));
  } catch (error) {
    showToast(error.message);
  } finally {
    label.textContent = originalLabel;
    button.classList.remove("is-loading");
    textarea.disabled = false;
    button.disabled = false;
    textarea.focus();
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

  const count = state.createCount || 1;
  const size = state.createSize || "auto";

  let aspectClass = "aspect-square";
  if (size === "1024x1536" || size === "1440x2560") {
    aspectClass = "aspect-portrait";
  } else if (size === "1536x1024" || size === "2560x1440") {
    aspectClass = "aspect-landscape";
  }

  const skeletons = Array.from({ length: count })
    .map(
      (_, index) => `
      <div class="skeleton-card ${aspectClass}">
        <div class="skeleton-card-inner">
          <div class="skeleton-glow-orb"></div>
          <svg class="icon skeleton-sparkle" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" />
          </svg>
          ${count > 1 ? `<div class="skeleton-card-tag">#${index + 1}</div>` : ""}
        </div>
      </div>`,
    )
    .join("");

  resultArea.innerHTML = `
    <div class="skeleton-loading-container">
      <div class="skeleton-grid ${count === 1 ? "skeleton-single" : ""}">
        ${skeletons}
      </div>
      <div class="skeleton-status-bar">
        <div class="skeleton-status-pulse"></div>
        <span class="skeleton-status-text">${message}</span>
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
    <div class="result-grid">
      ${images
        .map(
          (image, index) => `
        <div class="result-card">
          <img src="${image.data_url}" alt="${image.name}" data-index="${index}" title="${t("doubleClickOriginal", "双击查看原图")}" />
          <div class="result-card-actions">
            <button class="result-action-btn action-zoom" type="button" data-index="${index}" title="${t("viewOriginal", "查看原图")}">
              <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg>
              <span>${t("preview", "原图")}</span>
            </button>
            <button class="result-action-btn action-download" type="button" data-index="${index}" title="${t("download", "下载")}">
              <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              <span>${t("download", "下载")}</span>
            </button>
            <button class="result-action-btn action-use-ref" type="button" data-index="${index}" title="${t("useAsReference", "作为参考图")}">
              <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
              <span>${t("useAsReference", "作为参考图")}</span>
            </button>
            <button class="result-action-btn action-copy-prompt" type="button" title="${t("copyPrompt", "复制提示词")}">
              <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              <span>${t("copyPrompt", "复制")}</span>
            </button>
          </div>
        </div>`,
        )
        .join("")}
    </div>`;

  $("hideResultBtn").addEventListener("click", () => {
    resultArea.classList.add("hidden");
  });

  resultArea.querySelectorAll(".result-card img").forEach((imgNode) => {
    imgNode.addEventListener("dblclick", () => {
      const index = Number(imgNode.dataset.index);
      showOriginalImage(images[index]);
    });
  });

  resultArea.querySelectorAll(".action-zoom").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.index);
      showOriginalImage(images[index]);
    });
  });

  resultArea.querySelectorAll(".action-download").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.index);
      const img = images[index];
      const link = document.createElement("a");
      link.href = img.data_url;
      link.download = img.name;
      link.click();
    });
  });

  resultArea.querySelectorAll(".action-use-ref").forEach((button) => {
    button.addEventListener("click", async () => {
      const index = Number(button.dataset.index);
      const img = images[index];
      try {
        const response = await fetch(img.data_url);
        const blob = await response.blob();
        const file = new File([blob], img.name, {
          type: blob.type || "image/png",
        });
        addReferenceImageFile(file);
        showToast(t("addedReferences", "已添加参考图片").replace("{count}", "1"));
      } catch (error) {
        showToast(`转换参考图失败: ${error.message}`);
      }
    });
  });

  resultArea.querySelectorAll(".action-copy-prompt").forEach((button) => {
    button.addEventListener("click", () => {
      if (promptText) {
        navigator.clipboard.writeText(promptText).then(() => {
          showToast(t("promptCopied", "提示词已复制"));
        });
      }
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

  let sizeValue = state.createSize;
  if (sizeValue === "custom") {
    sizeValue = $("createCustomSize").value.trim() || "auto";
  }

  const quality = $("createQuality").value;
  const moderation = $("createModeration").value;
  const count = state.createCount;
  const hasReferences = state.referenceImageFiles.length > 0;

  try {
    let finalPrompt = prompt;

    if ($("autoOptimizeToggle").checked) {
      renderResultStatus(t("optimizing", "正在优化提示词..."));
      try {
        const optimizeRes = await bridge.apiPost("optimize-prompt", {
          prompt,
          mode: hasReferences ? "edit" : "generate",
        });
        if (optimizeRes && optimizeRes.prompt) {
          finalPrompt = optimizeRes.prompt;
          promptInput.value = finalPrompt;
          resizePromptTextarea(promptInput);
        }
      } catch (optErr) {
        showToast(`提示词优化异常: ${optErr.message}，将使用原提示词`);
      }
    }

    renderResultStatus(
      `${t("generating", "正在生成图片...")} (${hasReferences ? "图片编辑" : "文生图"} · ${count}张)`,
    );

    const resultNames = [];
    if (hasReferences) {
      const dataUrls = await Promise.all(
        state.referenceImageFiles.map(fileToDataUrl),
      );
      for (let i = 0; i < count; i += 1) {
        const data = await bridge.apiPost("edit", {
          prompt: finalPrompt,
          size: sizeValue,
          quality,
          moderation,
          data_urls: dataUrls,
        });
        if (data.image && data.image.name) {
          resultNames.push(data.image.name);
        }
      }
    } else {
      for (let i = 0; i < count; i += 1) {
        const data = await bridge.apiPost("generate", {
          prompt: finalPrompt,
          size: sizeValue,
          quality,
          moderation,
        });
        if (data.image && data.image.name) {
          resultNames.push(data.image.name);
        }
      }
    }

    showToast(t("taskDone", "图片处理完成"));
    await loadGallery();

    const fullImages = await Promise.all(
      resultNames.map((name) => fetchFullImage(name)),
    );
    state.lastGeneratedImages = fullImages;
    renderCreateResults(fullImages, finalPrompt);
  } catch (error) {
    renderResultError(error.message);
    showToast(error.message);
  } finally {
    state.isGenerating = false;
    $("createSubmitBtn").disabled = !promptInput.value.trim();
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
// 拖拽结束后浏览器仍会派发 click，必须吞掉这一次，否则拖完图就被误关。
$("lightbox").addEventListener("click", () => {
  if (lightbox.suppressClick) {
    lightbox.suppressClick = false;
    return;
  }
  hideLightbox();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideLightbox();
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
  if (lightbox.zoom <= LIGHTBOX_MIN_ZOOM) return;
  const stage = $("lightboxStage");
  lightbox.drag = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    scrollLeft: stage.scrollLeft,
    scrollTop: stage.scrollTop,
  };
  stage.setPointerCapture(event.pointerId);
  $("lightboxImage").classList.add("is-dragging");
});

$("lightboxStage").addEventListener("pointermove", (event) => {
  if (!lightbox.drag || lightbox.drag.pointerId !== event.pointerId) return;
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

["pointerup", "pointercancel"].forEach((eventName) => {
  $("lightboxStage").addEventListener(eventName, (event) => {
    if (!lightbox.drag || lightbox.drag.pointerId !== event.pointerId) return;
    lightbox.drag = null;
    $("lightboxImage").classList.remove("is-dragging");
  });
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
