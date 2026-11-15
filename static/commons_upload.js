const { photoCount, qualityImageDisabled } = JSON.parse(
  document.getElementById("review-config").textContent
);
const skippedPhotoIndexes = new Set();
const completedUploads = new Map();
const photoFields = document.getElementById("photo-fields");
const spinner = document.getElementById("spinner");
const status = document.getElementById("status");
const hintStage = document.getElementById("hint-stage");
const reviewStage = document.getElementById("review-stage");
const categoryTemplate = document.getElementById("category-row-template");
const editorTemplate = document.getElementById("photo-editor-template");
let pendingButtons = [];

function addCategory(container, value = "") {
  const row = categoryTemplate.content.firstElementChild.cloneNode(true);
  row.querySelector(".category-input").value = value;
  row.querySelector(".remove-btn").onclick = () => row.remove();
  container.appendChild(row);
}

function buildPhotoEditor(photo, position) {
  const editor = editorTemplate.content.firstElementChild.cloneNode(true);
  editor.dataset.photoIndex = String(photo.index);
  const heading = photoCount > 1 ? `Photo ${position + 1}` : "Photo";
  editor.querySelector("h2").textContent = heading;
  editor.querySelector(".filename-input").value = photo.filename;
  editor.querySelector(".caption-input").value = photo.caption;
  const categoryContainer = editor.querySelector(".photo-categories");
  photo.categories.forEach((category) => addCategory(categoryContainer, category));
  editor.querySelector(".add-category").onclick = () =>
    addCategory(categoryContainer, "");
  editor.querySelector(".quality-image-input").disabled = qualityImageDisabled;
  if (skippedPhotoIndexes.has(Number(photo.index))) {
    editor.classList.add("skipped");
  }
  return editor;
}

document.querySelectorAll(".overwrite-btn").forEach((button) => {
  button.onclick = async () => {
    const title = button.dataset.fileTitle;
    const photoIndex = Number(button.dataset.photoIndex);
    setBusy(`Overwriting ${title}...`);
    try {
      const data = await postJson("/overwrite", { title, photo_index: photoIndex }, "overwrite");
      if (photoCount === 1) {
        clearBusy("Overwrite complete. Redirecting...");
        window.location.href = data.redirect_url;
        return;
      }
      skippedPhotoIndexes.add(photoIndex);
      button.disabled = true;
      button.textContent = "Overwritten; omitted from batch";
      const editor = document.querySelector(
        `.photo-editor[data-photo-index="${photoIndex}"]`
      );
      if (editor) editor.classList.add("skipped");
      window.open(data.redirect_url, "_blank", "noopener");
      clearBusy("Overwrite complete. That photo will be omitted from the new uploads.");
    } catch (error) {
      clearBusy(error.message || "Overwrite failed", true);
      return;
    }
  };
});
function setBusy(message) {
  pendingButtons = Array.from(document.querySelectorAll("button:not(:disabled)"));
  pendingButtons.forEach((button) => { button.disabled = true; });
  document.body.setAttribute("aria-busy", "true");
  spinner.classList.add("visible");
  status.classList.remove("error");
  status.textContent = message;
}
function clearBusy(message = "", isError = false) {
  pendingButtons.forEach((button) => {
    button.disabled = button.matches(".overwrite-btn") &&
      skippedPhotoIndexes.has(Number(button.dataset.photoIndex));
  });
  pendingButtons = [];
  document.body.setAttribute("aria-busy", "false");
  spinner.classList.remove("visible");
  status.classList.toggle("error", isError);
  status.textContent = message;
}
function serverUnavailableMessage(action) {
  return (
    "The local upload server is no longer responding while trying to " +
    action +
    ". Restart upload_commons.py and reload this page to continue."
  );
}
async function postJson(path, payload, action) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (error) {
    throw new Error(serverUnavailableMessage(action));
  }

  let data;
  try {
    data = await response.json();
  } catch (error) {
    if (!response.ok) {
      throw new Error(
        action.charAt(0).toUpperCase() +
          action.slice(1) +
          " failed: HTTP " +
          response.status
      );
    }
    throw new Error(
      "The local upload server returned an invalid response while trying to " +
        action +
        "."
    );
  }

  if (!response.ok) {
    const requestError = new Error(
      data.error || action.charAt(0).toUpperCase() + action.slice(1) + " failed"
    );
    requestError.responseData = data;
    throw requestError;
  }
  return data;
}
function populateReview(data) {
  document.getElementById("review-hint").value = data.hint || "";
  const proposedPhotos = data.photos || [{
    index: 0,
    filename: data.filename,
    caption: data.caption,
    categories: data.categories,
  }];
  photoFields.replaceChildren();
  proposedPhotos.forEach((photo, position) =>
    photoFields.appendChild(buildPhotoEditor(photo, position))
  );
  hintStage.hidden = true;
  reviewStage.hidden = false;
}
async function analyzeWithHint(hintValue) {
  setBusy("Generating proposal...");
  let data;
  try {
    data = await postJson("/analyze", { hint: hintValue }, "generate a proposal");
  } catch (error) {
    clearBusy(error.message || "Analysis failed", true);
    return null;
  }
  populateReview(data);
  clearBusy("Proposal ready.");
  return data;
}
document.getElementById("analyze").onclick = async () => {
  const hintValue = document.getElementById("hint").value.trim();
  await analyzeWithHint(hintValue);
};
document.getElementById("redo").onclick = async () => {
  const hintValue = document.getElementById("review-hint").value.trim();
  await analyzeWithHint(hintValue);
};
document.getElementById("submit").onclick = async () => {
  const photos = Array.from(document.querySelectorAll(".photo-editor"))
    .filter((editor) => !skippedPhotoIndexes.has(Number(editor.dataset.photoIndex)))
    .map((editor) => ({
      index: Number(editor.dataset.photoIndex),
      filename: editor.querySelector(".filename-input").value,
      caption: editor.querySelector(".caption-input").value,
      categories: Array.from(editor.querySelectorAll(".category-input"))
        .map((node) => node.value.trim())
        .filter(Boolean),
      nominate_quality_image: editor.querySelector(".quality-image-input").checked,
    }));
  if (!photos.length) {
    clearBusy("There are no photos left to upload in this session.", true);
    return;
  }
  setBusy(photos.length === 1 ? "Uploading..." : `Uploading ${photos.length} photos...`);
  const payload = {
    photos,
  };
  try {
    const data = await postJson("/submit", payload, "upload");
    data.uploaded_files.forEach((file) => completedUploads.set(file.index, file));
    const allUploadedFiles = Array.from(completedUploads.values())
      .sort((left, right) => left.index - right.index);
    if (photoCount === 1) {
      clearBusy("Upload complete. Redirecting...");
      window.location.href = data.redirect_url;
      return;
    }
    const heading = document.createElement("h1");
    heading.textContent = "Upload complete";
    reviewStage.replaceChildren(heading);
    const message = document.createElement("p");
    message.textContent = `${allUploadedFiles.length} photos uploaded successfully.`;
    reviewStage.appendChild(message);
    const list = document.createElement("ol");
    list.className = "completion-list";
    allUploadedFiles.forEach((file) => {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = file.url;
      link.textContent = file.filename;
      link.target = "_blank";
      link.rel = "noreferrer";
      item.appendChild(link);
      list.appendChild(item);
    });
    reviewStage.appendChild(list);
    clearBusy("Batch upload complete.");
  } catch (error) {
    const uploadedBeforeError = error.responseData?.uploaded_files || [];
    uploadedBeforeError.forEach((file) => {
      completedUploads.set(file.index, file);
      skippedPhotoIndexes.add(file.index);
      const editor = document.querySelector(
        `.photo-editor[data-photo-index="${file.index}"]`
      );
      if (editor) editor.classList.add("skipped");
    });
    const retryNote = uploadedBeforeError.length
      ? " Successfully uploaded items were hidden; after addressing the error, retry to upload only the remaining items."
      : "";
    clearBusy((error.message || "Upload failed") + retryNote, true);
    return;
  }
};
