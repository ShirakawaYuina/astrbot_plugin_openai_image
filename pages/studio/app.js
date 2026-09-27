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

const state = {
  images: [],
  selected: null,
  activeTab: "gallery",
  galleryPage: 1,
  totalPages: 1,
  total: 0,
  referenceImageFiles: [],
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
  } else {
    resizePromptTextareas();
  }
}

function resizePromptTextarea(textarea) {
  if (!textarea) return;
  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight}px`;
}

function resizePromptTextareas() {
  ["generatePrompt", "editPrompt"].forEach((id) => resizePromptTextarea($(id)));
}

function syncCustomSize(presetId, customId) {
  $(customId).classList.toggle("hidden", $(presetId).value !== "custom");
}

function resolveSizeValue(presetId, customId) {
  const preset = $(presetId).value;
  return preset === "custom" ? $(customId).value.trim() : preset;
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
function showOriginalImage(image) {
  if (!image || !image.data_url) return;
  $("lightboxImage").src = image.data_url;
  $("lightboxImage").alt = image.name;
  $("lightbox").classList.remove("hidden");
}

function hideLightbox() {
  $("lightbox").classList.add("hidden");
  $("lightboxImage").removeAttribute("src");
  $("lightboxImage").alt = "";
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

function setResultState(targetId, stateName, message = "") {
  const box = $(targetId);
  box.classList.remove(
    "result-state-empty",
    "result-state-loading",
    "result-state-success",
    "result-state-error",
  );
  box.classList.add(`result-state-${stateName}`);
  if (stateName === "empty") {
    box.textContent = message || t("noResult", "暂无结果");
    return;
  }
  if (stateName === "loading") {
    box.innerHTML = `
      <div class="result-status">
        <span class="result-spinner" aria-hidden="true"></span>
        <strong>${message || t("generating", "正在生成图片，请稍候...")}</strong>
      </div>`;
    return;
  }
  if (stateName === "error") {
    box.innerHTML = `
      <div class="result-status" role="alert">
        <strong>${t("taskFailed", "图片处理失败")}</strong>
        <span>${message || t("taskFailedHint", "请查看错误信息后重试。")}</span>
      </div>`;
  }
}

function renderResultImages(targetId, images) {
  // 生成/编辑完成后把本次结果留在大预览区，减少用户在图库和表单之间来回找图。
  if (!images.length) {
    setResultState(targetId, "empty");
    return;
  }
  const box = $(targetId);
  setResultState(targetId, "success");
  box.innerHTML = images
    .map(
      (image) =>
        `<img src="${image.data_url}" alt="${image.name}" title="${t("doubleClickOriginal", "双击查看原图")}">`,
    )
    .join("");
  box.querySelectorAll("img").forEach((node, index) => {
    node.addEventListener("dblclick", () => showOriginalImage(images[index]));
  });
}

function renderReferenceThumbnails() {
  // 多参考图只在编辑页显示缩略图，提交时按文件对象逐张转成 data URL。
  $("referenceThumbs").innerHTML = state.referenceImageFiles
    .map((file, index) => {
      if (!REFERENCE_IMAGE_PREVIEW_URLS.has(file)) {
        REFERENCE_IMAGE_PREVIEW_URLS.set(file, URL.createObjectURL(file));
      }
      return `
        <div class="reference-thumb">
          <img src="${REFERENCE_IMAGE_PREVIEW_URLS.get(file)}" alt="参考图片 ${index + 1}" />
          <button class="reference-thumb-remove" type="button" data-index="${index}" title="移除参考图片 ${index + 1}">
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M18 6L6 18" />
              <path d="M6 6l12 12" />
            </svg>
          </button>
        </div>`;
    })
    .join("");
  $("referenceThumbs")
    .querySelectorAll(".reference-thumb-remove")
    .forEach((button) => {
      button.addEventListener("click", () =>
        removeReferenceImageFile(Number(button.dataset.index)),
      );
    });
}

function removeReferenceImageFile(index) {
  const file = state.referenceImageFiles[index];
  if (!file) return;
  const previewUrl = REFERENCE_IMAGE_PREVIEW_URLS.get(file);
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  REFERENCE_IMAGE_PREVIEW_URLS.delete(file);
  state.referenceImageFiles.splice(index, 1);
  renderReferenceThumbnails();
}

function addReferenceImageFile(file) {
  if (!file || !file.type.startsWith("image/")) {
    showToast(t("imageOnly", "请提供图片文件"));
    return;
  }
  state.referenceImageFiles.push(file);
  renderReferenceThumbnails();
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

async function runTask(endpoint, body, count, targetId) {
  // 逐张串行提交，避免一次性并发多张触发上游限流。
  const resultNames = [];
  for (let index = 0; index < count; index += 1) {
    const data = await bridge.apiPost(endpoint, body);
    if (data.image && data.image.name) {
      resultNames.push(data.image.name);
    }
  }

  showToast(t("taskDone", "图片处理完成"));
  await loadGallery();
  renderResultImages(
    targetId,
    await Promise.all(resultNames.map((name) => fetchFullImage(name))),
  );
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

$("generateOptimizePrompt").addEventListener("click", () =>
  optimizePrompt($("generateOptimizePrompt")),
);
$("editOptimizePrompt").addEventListener("click", () =>
  optimizePrompt($("editOptimizePrompt")),
);

$("generateSizePreset").addEventListener("change", () =>
  syncCustomSize("generateSizePreset", "generateCustomSize"),
);
$("editSizePreset").addEventListener("change", () =>
  syncCustomSize("editSizePreset", "editCustomSize"),
);

["generatePrompt", "editPrompt"].forEach((id) => {
  $(id).addEventListener("input", () => resizePromptTextarea($(id)));
});

$("editImage").addEventListener("change", () => {
  Array.from($("editImage").files || []).forEach(addReferenceImageFile);
  $("editImage").value = "";
});

document.addEventListener("paste", (event) => {
  if (state.activeTab === "edit") handlePasteImage(event);
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
$("lightbox").addEventListener("click", hideLightbox);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideLightbox();
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
});

$("generateForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("generateSubmit");
  submit.disabled = true;
  setResultState("generateResultBox", "loading");
  try {
    await runTask(
      "generate",
      {
        prompt: $("generatePrompt").value,
        size: resolveSizeValue("generateSizePreset", "generateCustomSize"),
        quality: $("generateQuality").value,
        moderation: $("generateModeration").value,
      },
      Math.max(1, Math.min(4, Number($("generateCount").value || 1))),
      "generateResultBox",
    );
  } catch (error) {
    setResultState("generateResultBox", "error", error.message);
    showToast(error.message);
  } finally {
    submit.disabled = false;
  }
});

$("editForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("editSubmit");
  submit.disabled = true;
  try {
    const files = state.referenceImageFiles;
    if (!files.length) {
      const message = t("referenceRequired", "请先上传或粘贴参考图片");
      setResultState("editResultBox", "error", message);
      showToast(message);
      return;
    }
    setResultState("editResultBox", "loading");
    await runTask(
      "edit",
      {
        prompt: $("editPrompt").value,
        size: resolveSizeValue("editSizePreset", "editCustomSize"),
        quality: $("editQuality").value,
        moderation: $("editModeration").value,
        data_urls: await Promise.all(files.map(fileToDataUrl)),
      },
      Math.max(1, Math.min(4, Number($("editCount").value || 1))),
      "editResultBox",
    );
  } catch (error) {
    setResultState("editResultBox", "error", error.message);
    showToast(error.message);
  } finally {
    submit.disabled = false;
  }
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
