# Web Admin Layout And Gallery Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the plugin web admin into the confirmed balanced workspace layout, add four-state result rendering with incremental image insertion, and convert the history gallery to paginated lazy loading with local page-size preferences.

**Architecture:** Keep the current single-file web admin implementation pattern so the plugin stays easy to ship, but expand `ImageLibrary` and `/api/images` into a paginated metadata service and refactor the inline HTML/CSS/JS inside `core/web_admin.py` around explicit result-state and gallery-state helpers. Drive every behavior change from `tests/test_web_admin.py` first, then update `metadata.yaml` and run plugin-local formatting, linting, and verification from the AstrBot root virtual environment.

**Tech Stack:** Python 3, `aiohttp`, inline HTML/CSS/JavaScript in `core/web_admin.py`, `pytest`, `ruff`, `Pillow`

---

## Implementation File Map

- Modify: `core/web_admin.py`
  - Add gallery pagination query handling in `_handle_list_images`.
  - Add image width/height/aspect metadata in `ImageLibrary`.
  - Replace the current result-box markup, CSS, and JavaScript with the confirmed workspace layout and explicit result-state helpers.
  - Replace the current all-at-once gallery loader with paginated lazy loading, local page-size settings, and page-window pruning.
- Modify: `tests/test_web_admin.py`
  - Add backend API regression tests for pagination, cursor handling, and image dimensions.
  - Add static HTML regression tests for layout structure, result state helpers, page-size settings, and gallery loading behavior.
- Modify: `requirements.txt`
  - Add `Pillow` so the backend can read real image dimensions without writing fragile format parsers.
- Modify: `metadata.yaml`
  - Bump the plugin version after the feature work is complete.
- Do not stage: `__pycache__/` or any `.pyc` files already dirty in the plugin repo.

## Preconditions

- Work from plugin repo: `D:\Programs\git_repos\AstrBot\data\plugins\astrbot_plugin_openai_image`
- Use AstrBot root Python: `D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe`
- When committing, stage only the files listed in each task. Do not use `git add .` because the repo already has unrelated `.pyc` changes.

---

### Task 1: Add Backend Pagination And Dimension Metadata

**Files:**
- Modify: `requirements.txt`
- Modify: `core/web_admin.py`
- Test: `tests/test_web_admin.py`

- [ ] **Step 1: Write the failing backend tests**

```python
import struct
import zlib


def _write_test_image(path: Path, size: tuple[int, int]) -> None:
    width, height = size
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = (
        struct.pack(">I", len(ihdr_data))
        + b"IHDR"
        + ihdr_data
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF)
    )
    scanline = b"\x00" + (b"\x30\x60\x90" * width)
    compressed = zlib.compress(scanline * height, level=9)
    idat = (
        struct.pack(">I", len(compressed))
        + b"IDAT"
        + compressed
        + struct.pack(">I", zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF)
    )
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    path.write_bytes(signature + ihdr + idat + iend)


def test_image_library_list_images_page_returns_cursor_and_dimensions(tmp_path: Path):
    module = _load_module()
    _write_test_image(tmp_path / "20260511_120001_landscape.png", (1536, 1024))
    _write_test_image(tmp_path / "20260511_120002_square.png", (1024, 1024))
    _write_test_image(tmp_path / "20260511_120003_portrait.png", (1024, 1536))
    library = module.ImageLibrary(tmp_path)

    first_page = library.list_images_page(limit=2, cursor="", keyword="", type_filter="", sort="latest")

    assert [item["name"] for item in first_page["images"]] == [
        "20260511_120003_portrait.png",
        "20260511_120002_square.png",
    ]
    assert first_page["images"][0]["width"] == 1024
    assert first_page["images"][0]["height"] == 1536
    assert first_page["images"][0]["aspect_ratio"] == pytest.approx(1024 / 1536, rel=1e-3)
    assert first_page["has_more"] is True
    assert first_page["next_cursor"] == "20260511_120002_square.png"


@pytest.mark.asyncio
async def test_list_images_handler_respects_limit_cursor_and_sort(tmp_path: Path):
    module = _load_module()
    _write_test_image(tmp_path / "a_landscape.png", (1536, 1024))
    _write_test_image(tmp_path / "b_square.png", (1024, 1024))
    _write_test_image(tmp_path / "c_portrait.png", (1024, 1536))
    server = module.WebAdminServer(
        plugin=SimpleNamespace(),
        settings=module.WebAdminSettings(
            enabled=True,
            host="127.0.0.1",
            port=7865,
            password="secret",
        ),
        cache_dir=tmp_path,
    )
    server._tokens.add("token-1")
    app = server._create_app()
    runner = module.web.AppRunner(app)
    await runner.setup()
    site = module.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/api/images?limit=2&sort=name",
                headers={"Authorization": "Bearer token-1"},
            ) as response:
                assert response.status == 200
                payload = await response.json()
    finally:
        await runner.cleanup()

    assert [item["name"] for item in payload["images"]] == ["a_landscape.png", "b_square.png"]
    assert payload["has_more"] is True
    assert payload["next_cursor"] == "b_square.png"
```

- [ ] **Step 2: Run the backend tests and verify they fail**

Run:

```powershell
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m pytest tests/test_web_admin.py::test_image_library_list_images_page_returns_cursor_and_dimensions tests/test_web_admin.py::test_list_images_handler_respects_limit_cursor_and_sort -v
```

Expected: `FAIL` because `ImageLibrary` does not expose `list_images_page`, image metadata does not contain `width` / `height` / `aspect_ratio`, and `/api/images` still returns the legacy `{"images": ...}` shape.

- [ ] **Step 3: Write the minimal backend implementation**

```text
requirements.txt
aiohttp
Pillow
```

```python
from PIL import Image


def _normalize_limit(value: Any, default: int = 40, minimum: int = 1, maximum: int = 120) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


class ImageLibrary:
    def list_images_page(
        self,
        *,
        limit: int,
        cursor: str,
        keyword: str,
        type_filter: str,
        sort: str,
    ) -> dict[str, Any]:
        images = self._collect_images(keyword=keyword, type_filter=type_filter, sort=sort)
        start_index = 0
        if cursor:
            for index, item in enumerate(images):
                if item["name"] == cursor:
                    start_index = index + 1
                    break
        page_items = images[start_index : start_index + limit]
        next_cursor = page_items[-1]["name"] if start_index + limit < len(images) and page_items else ""
        return {
            "images": page_items,
            "next_cursor": next_cursor,
            "has_more": bool(next_cursor),
        }

    def _collect_images(
        self, *, keyword: str, type_filter: str, sort: str
    ) -> list[dict[str, Any]]:
        keyword_text = keyword.strip().lower()
        suffix_filter = type_filter.strip().lower()
        image_items = []
        for image_path in self.cache_dir.iterdir():
            if not self._is_supported_image(image_path):
                continue
            try:
                metadata = self._build_image_metadata(image_path)
            except FileNotFoundError:
                continue
            if keyword_text and keyword_text not in metadata["name"].lower():
                continue
            if suffix_filter and not metadata["name"].lower().endswith(suffix_filter):
                continue
            image_items.append(metadata)
        if sort == "oldest":
            image_items.sort(key=lambda item: (int(item["modified_at"]), str(item["name"])))
        elif sort == "name":
            image_items.sort(key=lambda item: str(item["name"]))
        else:
            image_items.sort(
                key=lambda item: (int(item["modified_at"]), str(item["name"])),
                reverse=True,
            )
        return image_items

    @classmethod
    def _build_image_metadata(cls, image_path: Path) -> dict[str, Any]:
        stat_result = image_path.stat()
        metadata = cls._read_image_sidecar_metadata(image_path)
        width, height = cls._read_image_dimensions(image_path)
        aspect_ratio = round(width / height, 6) if width and height else None
        return {
            "name": image_path.name,
            "url": f"/api/images/{image_path.name}",
            "mime_type": _guess_mime_type(image_path),
            "size_bytes": stat_result.st_size,
            "modified_at": int(stat_result.st_mtime),
            "prompt": str(metadata.get("prompt", "") or ""),
            "generation_size": str(metadata.get("size", "") or ""),
            "mode": str(metadata.get("mode", "") or ""),
            "width": width,
            "height": height,
            "aspect_ratio": aspect_ratio,
        }

    @staticmethod
    def _read_image_dimensions(image_path: Path) -> tuple[int | None, int | None]:
        try:
            with Image.open(image_path) as image:
                return int(image.width), int(image.height)
        except (OSError, ValueError):
            return None, None
```

```python
async def _handle_list_images(self, request: web.Request) -> web.Response:
    auth_response = self._require_auth(request)
    if auth_response is not None:
        return auth_response
    payload = self.library.list_images_page(
        limit=_normalize_limit(request.query.get("limit")),
        cursor=str(request.query.get("cursor", "") or ""),
        keyword=str(request.query.get("keyword", "") or ""),
        type_filter=str(request.query.get("type", "") or ""),
        sort=str(request.query.get("sort", "latest") or "latest"),
    )
    return web.json_response(payload)
```

- [ ] **Step 4: Run the backend tests and verify they pass**

Run:

```powershell
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m pytest tests/test_web_admin.py::test_image_library_list_images_page_returns_cursor_and_dimensions tests/test_web_admin.py::test_list_images_handler_respects_limit_cursor_and_sort -v
```

Expected: `PASS` for both tests.

- [ ] **Step 5: Commit the backend pagination contract**

```powershell
git add requirements.txt core/web_admin.py tests/test_web_admin.py
git commit -m "feat: add paginated web gallery metadata"
```

---

### Task 2: Rebuild The Workspace Layout Shell

**Files:**
- Modify: `core/web_admin.py`
- Test: `tests/test_web_admin.py`

- [ ] **Step 1: Write the failing layout structure tests**

```python
def test_admin_html_moves_submit_button_to_prompt_row_and_removes_heavy_result_labels():
    module = _load_module()

    assert "class=\"prompt-action-row\"" in module.ADMIN_HTML
    assert "class=\"prompt-action-submit\"" in module.ADMIN_HTML
    assert "class=\"prompt-action-input\"" in module.ADMIN_HTML
    assert "<label>生成结果</label>" not in module.ADMIN_HTML
    assert "<label>编辑结果</label>" not in module.ADMIN_HTML
    assert "style=\"width:100%; margin-top:16px;\"" not in module.ADMIN_HTML


def test_admin_html_defines_result_state_shell_for_generate_and_edit_panels():
    module = _load_module()

    assert "class=\"result-status\"" in module.ADMIN_HTML
    assert "class=\"result-grid\"" in module.ADMIN_HTML
    assert "data-result-mode=\"generate\"" in module.ADMIN_HTML
    assert "data-result-mode=\"edit\"" in module.ADMIN_HTML
    assert "function setResultState(" in module.ADMIN_HTML
    assert "function createResultCard(" in module.ADMIN_HTML
```

- [ ] **Step 2: Run the layout tests and verify they fail**

Run:

```powershell
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m pytest tests/test_web_admin.py::test_admin_html_moves_submit_button_to_prompt_row_and_removes_heavy_result_labels tests/test_web_admin.py::test_admin_html_defines_result_state_shell_for_generate_and_edit_panels -v
```

Expected: `FAIL` because the current markup still renders standalone result labels, full-width bottom buttons, and has no `setResultState` helper.

- [ ] **Step 3: Write the minimal layout shell implementation**

```html
<div class="workspace-result-panel" data-result-mode="generate">
  <div class="result-status" id="generateResultStatus">暂无结果</div>
  <div id="generateResultBox" class="result-box state-empty">
    <div class="result-empty-copy">暂无结果，输入提示词后开始生成。</div>
    <div class="result-grid" id="generateResultGrid"></div>
  </div>
</div>

<div class="prompt-action-row">
  <button id="generateSubmit" class="btn primary prompt-action-submit" type="submit">
    生成图片
  </button>
  <div class="prompt-action-input">
    <label for="generatePrompt">提示词</label>
    <textarea id="generatePrompt" placeholder="例如：浅色自然光下的现代别墅，干净构图，高细节。"></textarea>
  </div>
</div>
```

```css
.prompt-action-row {
  display: grid;
  grid-template-columns: 148px minmax(0, 1fr);
  gap: 12px;
  align-items: stretch;
}

.prompt-action-submit {
  width: 100%;
  height: 100%;
  min-height: 120px;
}

.prompt-action-input {
  min-width: 0;
}

.result-status {
  width: fit-content;
  min-height: 28px;
  padding: 0 12px;
  border-radius: 999px;
  background: rgba(49, 64, 89, 0.08);
  color: var(--muted);
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  font-weight: 700;
}

.result-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  align-items: start;
}
```

```javascript
function createResultCard(image) {
  return `
    <article class="result-card ${resultCardSpanClass(image)}" data-image-name="${escapeAttribute(image.name)}">
      <img src="${escapeAttribute(imageUrl(image))}" alt="${escapeAttribute(image.name)}" title="双击查看原图">
    </article>
  `;
}

function resultCardSpanClass(image) {
  const width = Number(image?.width || 0);
  const height = Number(image?.height || 0);
  if (width > 0 && height > 0 && width >= height * 1.25) return "span-2";
  if (width > 0 && height > 0 && height >= width * 1.25) return "is-tall";
  return "is-square";
}

function setResultState(targetId, status, message = "") {
  const box = $(targetId);
  box.classList.remove("state-empty", "state-loading", "state-success", "state-error", "state-streaming");
  box.classList.add(`state-${status}`);
  const statusNode = targetId === "generateResultBox" ? $("generateResultStatus") : $("editResultStatus");
  statusNode.textContent = message || {
    empty: "暂无结果",
    loading: "生成中",
    streaming: "正在追加结果",
    success: "生成完成",
    error: "生成失败",
  }[status];
}
```

- [ ] **Step 4: Run the layout tests and verify they pass**

Run:

```powershell
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m pytest tests/test_web_admin.py::test_admin_html_moves_submit_button_to_prompt_row_and_removes_heavy_result_labels tests/test_web_admin.py::test_admin_html_defines_result_state_shell_for_generate_and_edit_panels -v
```

Expected: `PASS` for both tests.

- [ ] **Step 5: Commit the workspace shell changes**

```powershell
git add core/web_admin.py tests/test_web_admin.py
git commit -m "feat: redesign web admin workspace layout"
```

---

### Task 3: Stream Generate And Edit Results Incrementally

**Files:**
- Modify: `core/web_admin.py`
- Test: `tests/test_web_admin.py`

- [ ] **Step 1: Write the failing incremental result tests**

```python
def test_admin_html_streams_generate_results_before_loop_completion():
    module = _load_module()
    submit_section = module.ADMIN_HTML.split("$(\"generateForm\").addEventListener(\"submit\"", 1)[1]

    assert "setResultState(\"generateResultBox\", \"loading\"" in submit_section
    assert "appendResultImage(\"generateResultGrid\", data.image);" in submit_section
    assert "setResultState(\"generateResultBox\", \"streaming\"" in submit_section
    assert "setResultState(\"generateResultBox\", \"success\"" in submit_section
    assert "renderResultImages(\"generateResultBox\", resultImages);" not in submit_section


def test_admin_html_sets_error_state_when_generate_or_edit_request_fails():
    module = _load_module()

    assert "setResultState(\"generateResultBox\", \"error\", error.message);" in module.ADMIN_HTML
    assert "setResultState(\"editResultBox\", \"error\", error.message);" in module.ADMIN_HTML
```

- [ ] **Step 2: Run the incremental result tests and verify they fail**

Run:

```powershell
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m pytest tests/test_web_admin.py::test_admin_html_streams_generate_results_before_loop_completion tests/test_web_admin.py::test_admin_html_sets_error_state_when_generate_or_edit_request_fails -v
```

Expected: `FAIL` because the current submit handlers wait for the full loop to finish and only then call `renderResultImages(...)`.

- [ ] **Step 3: Write the minimal incremental rendering implementation**

```javascript
function appendResultImage(targetGridId, image) {
  const grid = $(targetGridId);
  if (!image || !image.name) return;
  grid.insertAdjacentHTML("beforeend", createResultCard(image));
  grid.querySelectorAll("img").forEach((imageNode) => {
    imageNode.addEventListener("dblclick", () => {
      const imageName = imageNode.closest(".result-card")?.dataset.imageName || "";
      const match = state.images.find((item) => item.name === imageName);
      if (match) window.open(imageUrl(match), "_blank", "noopener");
    });
  });
}

$("generateForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("generateSubmit");
  submit.disabled = true;
  $("generateResultGrid").innerHTML = "";
  setResultState("generateResultBox", "loading", "生成中");
  try {
    const resultImages = [];
    const count = Math.max(1, Math.min(4, Number($("generateCount").value || 1)));
    for (let index = 0; index < count; index += 1) {
      const data = await apiFetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: $("generatePrompt").value,
          size: resolveSizeValue("generateSizePreset", "generateCustomSize"),
          quality: $("generateQuality").value,
          moderation: $("generateModeration").value,
        }),
      });
      if (data.image && data.image.name) {
        resultImages.push(data.image.name);
        state.images.unshift(data.image);
        appendResultImage("generateResultGrid", data.image);
        setResultState("generateResultBox", "streaming", `已生成 ${resultImages.length} 张`);
      }
    }
    setResultState("generateResultBox", "success", "生成完成");
    await loadImages(resultImages[resultImages.length - 1] || "");
  } catch (error) {
    setResultState("generateResultBox", "error", error.message);
    showToast(error.message);
  } finally {
    submit.disabled = false;
  }
});
```

```javascript
$("editForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("editSubmit");
  submit.disabled = true;
  $("editResultGrid").innerHTML = "";
  setResultState("editResultBox", "loading", "编辑中");
  try {
    const body = new FormData();
    body.append("prompt", $("editPrompt").value);
    body.append("size", resolveSizeValue("editSizePreset", "editCustomSize"));
    body.append("quality", $("editQuality").value);
    body.append("moderation", "low");
    state.referenceImageFiles.forEach((file) => {
      body.append("image", file);
    });
    const data = await apiFetch("/api/edit", { method: "POST", body });
    if (data.image && data.image.name) {
      state.images.unshift(data.image);
      appendResultImage("editResultGrid", data.image);
    }
    setResultState("editResultBox", "success", "编辑完成");
    await loadImages(data.image?.name || "");
  } catch (error) {
    setResultState("editResultBox", "error", error.message);
    showToast(error.message);
  } finally {
    submit.disabled = false;
  }
});
```

- [ ] **Step 4: Run the incremental result tests and verify they pass**

Run:

```powershell
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m pytest tests/test_web_admin.py::test_admin_html_streams_generate_results_before_loop_completion tests/test_web_admin.py::test_admin_html_sets_error_state_when_generate_or_edit_request_fails -v
```

Expected: `PASS` for both tests.

- [ ] **Step 5: Commit the incremental result flow**

```powershell
git add core/web_admin.py tests/test_web_admin.py
git commit -m "feat: stream web admin result rendering"
```

---

### Task 4: Add Paginated Lazy Gallery Browsing

**Files:**
- Modify: `core/web_admin.py`
- Test: `tests/test_web_admin.py`

- [ ] **Step 1: Write the failing gallery behavior tests**

```python
def test_admin_html_adds_local_gallery_page_size_setting_and_auto_paging():
    module = _load_module()

    assert "id=\"galleryPageSize\"" in module.ADMIN_HTML
    assert "openaiImageGalleryPageSize" in module.ADMIN_HTML
    assert "function getGalleryPageSize()" in module.ADMIN_HTML
    assert "function loadNextGalleryPage(" in module.ADMIN_HTML
    assert "new IntersectionObserver(" in module.ADMIN_HTML


def test_admin_html_uses_dense_gallery_layout_and_virtual_page_pruning():
    module = _load_module()
    gallery_rule = re.search(r"\\.gallery\\s*\\{(?P<body>[^}]+)\\}", module.ADMIN_HTML)

    assert gallery_rule is not None
    assert "grid-auto-flow: dense;" in gallery_rule.group("body")
    assert "function pruneFarGalleryPages()" in module.ADMIN_HTML
    assert "function hydrateVisibleGalleryImages()" in module.ADMIN_HTML
    assert "function resetGalleryState()" in module.ADMIN_HTML
```

- [ ] **Step 2: Run the gallery behavior tests and verify they fail**

Run:

```powershell
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m pytest tests/test_web_admin.py::test_admin_html_adds_local_gallery_page_size_setting_and_auto_paging tests/test_web_admin.py::test_admin_html_uses_dense_gallery_layout_and_virtual_page_pruning -v
```

Expected: `FAIL` because the current gallery has no page-size setting, no intersection observers, no dense flow, and no page-pruning helpers.

- [ ] **Step 3: Write the minimal paginated gallery implementation**

```html
<div class="settings-actions">
  <label for="galleryPageSize">历史图库每页数量</label>
  <select id="galleryPageSize" class="control">
    <option value="20">20</option>
    <option value="40" selected>40</option>
    <option value="60">60</option>
    <option value="80">80</option>
  </select>
</div>
```

```css
.gallery {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  grid-auto-flow: dense;
  align-items: start;
  gap: 14px;
  min-height: 300px;
}

.image-card.span-2 {
  grid-column: span 2;
}

.image-card.is-tall .thumb {
  min-height: 260px;
}

.gallery-sentinel {
  grid-column: 1 / -1;
  min-height: 1px;
}
```

```javascript
const GALLERY_PAGE_SIZE_KEY = "openaiImageGalleryPageSize";

state.imagesByName = new Map();
state.gallery = {
  orderedNames: [],
  nextCursor: "",
  hasMore: true,
  loading: false,
  pageWindowStart: 0,
  pageWindowEnd: 0,
  sentinelObserver: null,
};

function getGalleryPageSize() {
  const stored = Number(localStorage.getItem(GALLERY_PAGE_SIZE_KEY) || "40");
  return Math.max(20, Math.min(80, stored || 40));
}

function resetGalleryState() {
  state.gallery.orderedNames = [];
  state.gallery.nextCursor = "";
  state.gallery.hasMore = true;
  state.gallery.loading = false;
  state.gallery.pageWindowStart = 0;
  state.gallery.pageWindowEnd = 0;
  $("gallery").innerHTML = '<div class="gallery-sentinel" id="gallerySentinel"></div>';
}

function bindGalleryCardEvents() {
  $("gallery").querySelectorAll(".image-card").forEach((card) => {
    card.addEventListener("click", () => selectImage(card.dataset.name).catch((error) => showToast(error.message)));
  });
  $("gallery").querySelectorAll(".delete-image-action").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteImageByName(button.dataset.name).catch((error) => showToast(error.message));
    });
  });
}

function renderGalleryWindow() {
  pruneFarGalleryPages();
  const names = state.gallery.orderedNames.slice(
    state.gallery.pageWindowStart,
    state.gallery.pageWindowEnd || state.gallery.orderedNames.length,
  );
  const cards = names.map((name) => {
    const image = state.imagesByName.get(name);
    if (!image) return "";
    const safeName = escapeHtml(image.name);
    const safeAttrName = escapeAttribute(image.name);
    return `
      <button class="image-card ${resultCardSpanClass(image)}" type="button" data-name="${safeAttrName}">
        <span class="image-card-actions">
          <span class="image-card-action delete-image-action" data-name="${safeAttrName}" title="删除图片" aria-label="删除图片"></span>
        </span>
        <img class="thumb" data-name="${safeAttrName}" alt="${safeAttrName}" loading="lazy">
        <div class="card-meta">
          <div class="card-name" title="${safeAttrName}">${safeName}</div>
          <div class="card-sub"><span>${formatBytes(image.size_bytes)}</span><span>${new Date(image.modified_at * 1000).toLocaleString()}</span></div>
        </div>
      </button>
    `;
  }).join("");
  $("gallery").innerHTML = `${cards}<div class="gallery-sentinel" id="gallerySentinel"></div>`;
  bindGalleryCardEvents();
  ensureGalleryObservers();
  hydrateVisibleGalleryImages().catch((error) => showToast(error.message));
}

async function loadNextGalleryPage({ reset = false } = {}) {
  if (state.gallery.loading) return;
  if (reset) resetGalleryState();
  if (!state.gallery.hasMore && !reset) return;
  state.gallery.loading = true;
  const params = new URLSearchParams({
    limit: String(getGalleryPageSize()),
    cursor: state.gallery.nextCursor,
    keyword: $("searchInput").value.trim(),
    type: $("typeFilter").value,
    sort: $("sortFilter").value,
  });
  try {
    const payload = await apiFetch(`/api/images?${params.toString()}`);
    payload.images.forEach((image) => {
      state.imagesByName.set(image.name, image);
      state.gallery.orderedNames.push(image.name);
    });
    state.images = state.gallery.orderedNames
      .map((name) => state.imagesByName.get(name))
      .filter(Boolean);
    state.gallery.nextCursor = payload.next_cursor || "";
    state.gallery.hasMore = Boolean(payload.has_more);
    renderGalleryWindow();
  } finally {
    state.gallery.loading = false;
  }
}

function pruneFarGalleryPages() {
  const pageSize = getGalleryPageSize();
  const maxVisible = pageSize * 3;
  state.gallery.pageWindowEnd = state.gallery.orderedNames.length;
  state.gallery.pageWindowStart = Math.max(0, state.gallery.pageWindowEnd - maxVisible);
}

async function hydrateVisibleGalleryImages() {
  const cards = Array.from($("gallery").querySelectorAll(".image-card img.thumb[data-name]"));
  for (const imageNode of cards) {
    const image = state.imagesByName.get(imageNode.dataset.name);
    if (!image) continue;
    if (!imageNode.dataset.hydrated) {
      imageNode.src = await getCachedThumbnailUrl(image);
      imageNode.dataset.hydrated = "true";
    }
  }
}

function ensureGalleryObservers() {
  state.gallery.sentinelObserver?.disconnect();
  state.gallery.sentinelObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        loadNextGalleryPage().catch((error) => showToast(error.message));
      }
    });
  }, { rootMargin: "0px 0px 320px 0px" });
  const sentinel = $("gallerySentinel");
  if (sentinel) state.gallery.sentinelObserver.observe(sentinel);
}

$("galleryPageSize").value = String(getGalleryPageSize());
$("galleryPageSize").addEventListener("change", () => {
  localStorage.setItem(GALLERY_PAGE_SIZE_KEY, $("galleryPageSize").value);
  loadNextGalleryPage({ reset: true }).catch((error) => showToast(error.message));
});
```

- [ ] **Step 4: Run the gallery behavior tests and verify they pass**

Run:

```powershell
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m pytest tests/test_web_admin.py::test_admin_html_adds_local_gallery_page_size_setting_and_auto_paging tests/test_web_admin.py::test_admin_html_uses_dense_gallery_layout_and_virtual_page_pruning -v
```

Expected: `PASS` for both tests.

- [ ] **Step 5: Commit the gallery loading behavior**

```powershell
git add core/web_admin.py tests/test_web_admin.py
git commit -m "feat: add lazy paginated web gallery"
```

---

### Task 5: Final Verification, Version Bump, And Release Hygiene

**Files:**
- Modify: `metadata.yaml`
- Modify: `core/web_admin.py`
- Modify: `tests/test_web_admin.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Bump the plugin version after the feature set is complete**

```yaml
name: astrbot_plugin_openai_image
display_name: OpenAI 图片生成
author: AsryMiu
version: 0.6.19
description: 基于 OpenAI 兼容图片接口的 AstrBot 图片生成与图片编辑插件，支持缓存、并发限制、OneBot v11 回传与网页后台。
repo: https://github.com/ShirakawaYuina/astrbot_plugin_openai_image
```

- [ ] **Step 2: Run the focused web admin test file**

Run:

```powershell
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m pytest tests/test_web_admin.py -v
```

Expected: `PASS` for all `test_web_admin.py` cases, including the new pagination and layout regressions.

- [ ] **Step 3: Run the full plugin test suite**

Run:

```powershell
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m pytest -v
```

Expected: `PASS` for the full plugin suite with no regressions outside the web admin area.

- [ ] **Step 4: Run formatting and linting from the plugin repo**

Run:

```powershell
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m ruff format .
D:\Programs\git_repos\AstrBot\.venv\Scripts\python.exe -m ruff check .
```

Expected: `ruff format` completes cleanly and `ruff check` reports no violations.

- [ ] **Step 5: Commit only the intended source files**

```powershell
git add metadata.yaml requirements.txt core/web_admin.py tests/test_web_admin.py
git commit -m "feat: improve web admin gallery loading and layout"
```

---

## Self-Review Notes

- Spec coverage check:
  - Workspace layout, button placement, weak result labels, four-state result area, incremental result insertion, paginated gallery API, dense masonry gallery, lazy loading, virtual page pruning, local page-size setting, version bump, and verification are all mapped to Tasks 1-5.
- Placeholder scan:
  - No `TODO`, `TBD`, or “similar to previous task” instructions remain.
- Type consistency:
  - The plan consistently uses `list_images_page`, `setResultState`, `appendResultImage`, `loadNextGalleryPage`, `resetGalleryState`, and `pruneFarGalleryPages`.
