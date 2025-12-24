const statusEl = document.getElementById("status");
const errorEl = document.getElementById("error");
const successEl = document.getElementById("success");
const uploadCard = document.getElementById("upload-card");
const fileInput = document.getElementById("file-input");
const fileList = document.getElementById("file-list");
const uploadButton = document.getElementById("upload-button");
const signOutButton = document.getElementById("sign-out");

const MAX_FILE_MB = 50;
const ALLOWED_TYPES = new Set([
  "application/pdf",
  "text/csv",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]);
let sessionToken = "";
let files = [];
const fileRowMap = new Map();

function setStatus(message) {
  statusEl.textContent = message;
}

function setError(message) {
  if (!message) {
    errorEl.hidden = true;
    errorEl.textContent = "";
    return;
  }
  errorEl.hidden = false;
  errorEl.textContent = message;
}

function setSuccess(message) {
  if (!message) {
    successEl.hidden = true;
    successEl.textContent = "";
    return;
  }
  successEl.hidden = false;
  successEl.textContent = message;
}

function fileKey(file) {
  return `${file.name}-${file.size}-${file.lastModified}`;
}

function parseToken() {
  const params = new URLSearchParams(window.location.search);
  return params.get("token") || "";
}

function clearTokenFromUrl() {
  if (window.location.search) {
    window.history.replaceState({}, document.title, window.location.pathname);
  }
}

async function postJson(path, payload, token) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Request failed.");
  }
  return data.data || {};
}

function renderFileList() {
  fileList.innerHTML = "";
  fileRowMap.clear();
  if (!files.length) {
    uploadButton.disabled = true;
    return;
  }
  files.forEach((file) => {
    const item = document.createElement("div");
    item.className = "file-item";

    const row = document.createElement("div");
    row.className = "file-row";

    const name = document.createElement("div");
    name.className = "file-name";
    name.textContent = file.name;

    const status = document.createElement("div");
    status.className = "file-status";
    status.textContent = "Ready";

    row.appendChild(name);
    row.appendChild(status);

    const progress = document.createElement("div");
    progress.className = "progress";
    const bar = document.createElement("div");
    bar.className = "progress-bar";
    progress.appendChild(bar);

    item.appendChild(row);
    item.appendChild(progress);
    fileList.appendChild(item);

    fileRowMap.set(fileKey(file), { status, bar });
  });
  uploadButton.disabled = false;
}

function updateFileStatus(file, statusText, progressPercent) {
  const entry = fileRowMap.get(fileKey(file));
  if (!entry) {
    return;
  }
  entry.status.textContent = statusText;
  if (typeof progressPercent === "number") {
    entry.bar.style.width = `${progressPercent}%`;
  }
}

function uploadWithProgress(file, signedUrl) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", signedUrl, true);
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        const percent = Math.round((event.loaded / event.total) * 100);
        updateFileStatus(file, `Uploading ${percent}%`, percent);
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        updateFileStatus(file, "Uploaded", 100);
        resolve();
      } else {
        reject(new Error("Upload failed."));
      }
    };

    xhr.onerror = () => reject(new Error("Upload failed."));
    xhr.send(file);
  });
}

async function handleUpload() {
  setError("");
  setSuccess("");
  uploadButton.disabled = true;

  let hadFailure = false;
  for (const file of files) {
    if (file.size > MAX_FILE_MB * 1024 * 1024) {
      updateFileStatus(file, "Too large", 0);
      setError(`File exceeds ${MAX_FILE_MB}MB limit.`);
      hadFailure = true;
      continue;
    }
    if (!ALLOWED_TYPES.has((file.type || "").toLowerCase())) {
      updateFileStatus(file, "Unsupported type", 0);
      setError("Unsupported file type. Please upload a PDF, CSV, or Excel file.");
      hadFailure = true;
      continue;
    }
    try {
      updateFileStatus(file, "Preparing", 5);
      const signed = await postJson(
        "/api/upload-portal/signed-upload-url",
        {
          filename: file.name,
          content_type: file.type,
          byte_size: file.size,
        },
        sessionToken
      );
      await uploadWithProgress(file, signed.signed_url);
      await postJson("/api/upload-portal/complete", { upload_id: signed.upload_id }, sessionToken);
    } catch (error) {
      updateFileStatus(file, "Failed", 0);
      setError("One or more uploads failed. Please retry or contact support.");
      hadFailure = true;
    }
  }

  uploadButton.disabled = false;
  if (!hadFailure) {
    setSuccess("Upload complete. Thank you!");
  }
}

async function initPortal() {
  const token = parseToken();
  if (!token) {
    setError("Missing upload token. Please use the secure link in your email.");
    return;
  }

  try {
    setStatus("Validating your link…");
    const response = await postJson("/api/upload-portal/verify", { token });
    sessionToken = response.session_token || "";
    if (!sessionToken) {
      throw new Error("Session unavailable.");
    }
    clearTokenFromUrl();
    uploadCard.hidden = false;
    setStatus("Link verified. You may upload your documents.");
  } catch (error) {
    setError(error.message || "Unable to validate the link.");
  }
}

fileInput.addEventListener("change", () => {
  files = Array.from(fileInput.files || []);
  renderFileList();
});

uploadButton.addEventListener("click", handleUpload);

signOutButton.addEventListener("click", () => {
  sessionToken = "";
  window.location.reload();
});

initPortal();
