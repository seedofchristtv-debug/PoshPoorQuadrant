(function () {
  "use strict";

  var DB_NAME = "seed-church-offline";
  var DB_VERSION = 1;
  var SYNC_TAG = "seed-church-sync";

  function requestResult(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function openDb() {
    return new Promise(function (resolve, reject) {
      var request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = function () {
        var db = request.result;
        if (!db.objectStoreNames.contains("queue")) {
          db.createObjectStore("queue", { keyPath: "id" });
        }
        if (!db.objectStoreNames.contains("members")) {
          db.createObjectStore("members", { keyPath: "client_id" });
        }
        if (!db.objectStoreNames.contains("meta")) {
          db.createObjectStore("meta", { keyPath: "key" });
        }
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  async function getAll(storeName) {
    var db = await openDb();
    return requestResult(db.transaction(storeName, "readonly").objectStore(storeName).getAll());
  }

  async function put(storeName, value) {
    var db = await openDb();
    return requestResult(db.transaction(storeName, "readwrite").objectStore(storeName).put(value));
  }

  async function remove(storeName, key) {
    var db = await openDb();
    return requestResult(db.transaction(storeName, "readwrite").objectStore(storeName).delete(key));
  }

  async function clear(storeName) {
    var db = await openDb();
    return requestResult(db.transaction(storeName, "readwrite").objectStore(storeName).clear());
  }

  function makeId() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return "offline-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, function (character) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"}[character];
    });
  }

  function setNetworkState(isOnline, message) {
    var indicator = document.getElementById("network-status");
    if (!indicator) return;
    indicator.classList.toggle("offline", !isOnline);
    indicator.innerHTML = '<span class="network-dot"></span> ' + escapeHtml(message || (isOnline ? "Online" : "Offline — saving locally"));
  }

  function showMessage(message, kind, scope) {
    var messageBox = scope && scope.parentElement
      ? scope.parentElement.querySelector("[data-offline-message]")
      : document.querySelector("[data-offline-message]");
    if (!messageBox) return;
    messageBox.textContent = message;
    messageBox.className = "offline-form-note " + (kind || "success");
  }

  function fileToDataUrl(file) {
    return new Promise(function (resolve, reject) {
      if (!file || !file.size) return resolve(null);
      var reader = new FileReader();
      reader.onload = function () { resolve(reader.result); };
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  async function registerBackgroundSync() {
    if (!("serviceWorker" in navigator)) return;
    try {
      var registration = await navigator.serviceWorker.ready;
      if (registration.sync) await registration.sync.register(SYNC_TAG);
    } catch (_) {
      // The online event below remains the fallback for browsers without Background Sync.
    }
  }

  async function queueMember(form) {
    var formData = new FormData(form);
    var photo = formData.get("photo");
    if (photo && photo.size > 5 * 1024 * 1024) {
      throw new Error("Please choose an image under 5 MB.");
    }
    var clientId = makeId();
    var data = {
      first_name: formData.get("first_name") || "",
      last_name: formData.get("last_name") || "",
      phone: formData.get("phone") || "",
      email: formData.get("email") || "",
      address: formData.get("address") || "",
      group_name: formData.get("group_name") || "",
      join_date: formData.get("join_date") || "",
      birth_date: formData.get("birth_date") || "",
      photo_data: await fileToDataUrl(photo)
    };
    if (!data.first_name || !data.last_name) {
      throw new Error("First name and last name are required.");
    }
    await put("members", {
      client_id: clientId,
      server_id: null,
      first_name: data.first_name,
      last_name: data.last_name,
      group_name: data.group_name,
      photo_data: data.photo_data,
      local_only: true
    });
    await put("queue", {
      id: makeId(),
      kind: "member",
      client_id: clientId,
      data: data,
      created_at: new Date().toISOString()
    });
    await registerBackgroundSync();
    form.reset();
    if (document.querySelector("[data-offline-member-list]")) {
      await renderOfflineMembers();
    }
    showMessage("Saved on this device. It will sync automatically when the connection returns.", "success", form);
  }

  function isSunday(value) {
    if (!value) return false;
    return new Date(value + "T12:00:00").getDay() === 0;
  }

  async function queueAttendance(form) {
    var dateInput = form.querySelector("[name=attendance_date]");
    var attendanceDate = dateInput && dateInput.value;
    if (!isSunday(attendanceDate)) {
      throw new Error("Attendance can only be recorded for a Sunday service.");
    }
    var rows = form.querySelectorAll("[data-attendance-row]");
    var queued = 0;
    for (var index = 0; index < rows.length; index += 1) {
      var row = rows[index];
      var selected = row.querySelector("input[type=radio]:checked");
      if (!selected) continue;
      var note = row.querySelector(".notes-input, .offline-notes");
      await put("queue", {
        id: makeId(),
        kind: "attendance",
        sync_id: makeId(),
        member_id: row.getAttribute("data-member-id") || null,
        member_client_id: row.getAttribute("data-client-id") || null,
        attendance_date: attendanceDate,
        status: selected.value,
        notes: note ? note.value : "",
        created_at: new Date().toISOString()
      });
      queued += 1;
    }
    await registerBackgroundSync();
    showMessage(
      queued
        ? "Sunday attendance saved on this device. It will sync automatically when the connection returns."
        : "There were no attendance rows to save.",
      queued ? "success" : "error",
      form
    );
  }

  async function sendFormOrQueue(form, queueFunction) {
    var submitButton = form.querySelector('button[type="submit"]');
    var saveConfirmed = false;
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.setAttribute("aria-busy", "true");
    }
    try {
      if (!navigator.onLine) {
        await queueFunction(form);
        setNetworkState(false);
        return;
      }

      var response;
      try {
        response = await fetch(form.action || window.location.href, {
          method: "POST",
          body: new FormData(form),
          credentials: "same-origin",
          headers: {"X-Requested-With": "XMLHttpRequest"}
        });
      } catch (_) {
        await queueFunction(form);
        setNetworkState(false, "Connection lost — saved locally");
        return;
      }

      var responseUrl = new URL(response.url || window.location.href, window.location.href);
      if (response.redirected && (responseUrl.pathname === "/login" || responseUrl.pathname === "/setup")) {
        showMessage(
          "Your admin sign-in has expired. Your entries are still on this form. Sign in, then return and submit again.",
          "error",
          form
        );
        return;
      }

      var contentType = response.headers.get("Content-Type") || "";
      if (contentType.indexOf("application/json") !== -1) {
        var result = await response.json();
        if (!response.ok || !result.ok) {
          showMessage(
            result.error || "The server could not save this member. Your entries are still on this form.",
            "error",
            form
          );
          return;
        }
        showMessage(result.message || "Member saved successfully.", "success", form);
        saveConfirmed = true;
        window.setTimeout(function () {
          window.location.assign(result.redirect_url || window.location.href);
        }, 1000);
        return;
      }

      var responseHtml = await response.text();
      var responseDocument = new DOMParser().parseFromString(responseHtml, "text/html");
      var serverMessage = responseDocument.querySelector(".flash");
      if (!response.ok) {
        showMessage(
          serverMessage
            ? serverMessage.textContent.replace(/^[✓!]\s*/, "").trim()
            : "The server could not confirm the save (HTTP " + response.status + "). Your entries are still on this form.",
          "error",
          form
        );
        return;
      }
      if (response.redirected && responseUrl.pathname !== new URL(window.location.href).pathname) {
        window.location.assign(responseUrl.href);
        return;
      }
      if (serverMessage) {
        showMessage(
          serverMessage.textContent.replace(/^[✓!]\s*/, "").trim(),
          serverMessage.classList.contains("error") ? "error" : "success",
          form
        );
      } else {
        showMessage(
          "The server returned the form without confirming the member was saved. Your entries are still here; check the member list before retrying.",
          "error",
          form
        );
      }
    } catch (error) {
      showMessage(error.message || "The member could not be saved.", "error", form);
    } finally {
      if (submitButton && !saveConfirmed) {
        submitButton.disabled = false;
        submitButton.removeAttribute("aria-busy");
      }
    }
  }

  function splitSyncBatches(operations) {
    var batches = [];
    var current = [];
    operations.forEach(function (operation) {
      var next = current.concat([operation]);
      var tooLarge = new Blob([JSON.stringify({operations: next})]).size > 7 * 1024 * 1024;
      if (current.length && (next.length > 100 || tooLarge)) {
        batches.push(current);
        current = [operation];
      } else {
        current = next;
      }
      if (new Blob([JSON.stringify({operations: current})]).size > 7 * 1024 * 1024) {
        throw new Error("One offline record is too large to sync.");
      }
    });
    if (current.length) batches.push(current);
    return batches;
  }

  async function syncPending() {
    if (!navigator.onLine) return;
    var queued = await getAll("queue");
    if (!queued.length) {
      setNetworkState(true);
      return;
    }
    queued.sort(function (a, b) {
      return (a.kind === "member" ? 0 : 1) - (b.kind === "member" ? 0 : 1);
    });
    try {
      var batches = splitSyncBatches(queued);
      for (var batchIndex = 0; batchIndex < batches.length; batchIndex += 1) {
        var batch = batches[batchIndex];
        var response = await fetch("/api/offline/sync", {
          method: "POST",
          credentials: "same-origin",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({operations: batch})
        });
        if (!response.ok) throw new Error("Sync was rejected.");
        var result = await response.json();
        var memberMap = result.member_map || {};
        var members = await getAll("members");
        for (var index = 0; index < members.length; index += 1) {
          var localMember = members[index];
          if (Object.prototype.hasOwnProperty.call(memberMap, localMember.client_id)) {
            localMember.server_id = memberMap[localMember.client_id];
            localMember.local_only = false;
            await put("members", localMember);
          }
        }
        for (var queueIndex = 0; queueIndex < batch.length; queueIndex += 1) {
          await remove("queue", batch[queueIndex].id);
        }
      }
      setNetworkState(true, "Online · synced");
      showMessage("Offline records synced successfully.", "success");
      await refreshLocalMembers();
    } catch (_) {
      setNetworkState(false, "Waiting to sync");
    }
  }

  async function refreshLocalMembers() {
    if (!navigator.onLine) return;
    try {
      var response = await fetch("/api/offline/bootstrap", {credentials: "same-origin"});
      if (!response.ok) return;
      var result = await response.json();
      for (var index = 0; index < result.members.length; index += 1) {
        var member = result.members[index];
        var clientId = member.client_id || "server-" + member.server_id;
        var current = (await getAll("members")).filter(function (item) {
          return item.client_id === clientId;
        })[0] || {};
        await put("members", Object.assign(current, member, {
          client_id: clientId,
          local_only: false
        }));
      }
      if (document.querySelector("[data-offline-member-list]")) renderOfflineMembers();
    } catch (_) {
      // Bootstrap is best-effort; the local copy remains available offline.
    }
  }

  async function renderOfflineMembers() {
    var list = document.querySelector("[data-offline-member-list]");
    if (!list) return;
    var members = await getAll("members");
    members.sort(function (a, b) {
      return (a.first_name || "").localeCompare(b.first_name || "");
    });
    if (!members.length) {
      list.innerHTML = '<div class="offline-empty">No members are stored on this device yet. Add one above first.</div>';
      return;
    }
    list.innerHTML = members.map(function (member, index) {
      var initials = escapeHtml((member.first_name || "").slice(0, 1) + (member.last_name || "").slice(0, 1));
      var image = member.photo_data
        ? '<img class="offline-avatar" src="' + member.photo_data + '" alt="">'
        : '<span class="offline-avatar">' + initials + "</span>";
      return '<div class="offline-attendance-row" data-attendance-row data-member-id="' +
        escapeHtml(member.server_id || "") + '" data-client-id="' + escapeHtml(member.client_id || "") + '">' +
        '<div class="offline-member-name">' + image + '<span><strong>' +
        escapeHtml(member.first_name + " " + member.last_name) + '</strong><small>' +
        escapeHtml(member.group_name || "General member") + "</small></span></div>" +
        '<div class="offline-status-options">' +
        ["present", "absent", "excused"].map(function (status) {
          return '<label><input type="radio" name="offline-status-' + index + '" value="' + status + '"' +
            (status === "absent" ? " checked" : "") + "><span>" + status[0].toUpperCase() + status.slice(1) + "</span></label>";
        }).join("") +
        '</div><input class="offline-notes" name="offline-notes-' + index + '" placeholder="Optional note"></div>';
    }).join("");
  }

  function attachFormHandlers() {
    var memberForm = document.querySelector('[data-offline-member-form="true"]');
    if (memberForm) {
      memberForm.addEventListener("submit", function (event) {
        event.preventDefault();
        sendFormOrQueue(memberForm, queueMember).catch(function (error) {
        showMessage(error.message, "error", memberForm);
        });
      });
    }
    var attendanceForm = document.querySelector("[data-offline-attendance-form]");
    if (attendanceForm) {
      attendanceForm.addEventListener("submit", function (event) {
        if (event.submitter && event.submitter.name === "load_date") {
          if (!navigator.onLine) {
            event.preventDefault();
            showMessage("Reconnect to load a different Sunday service.", "error", attendanceForm);
          }
          return;
        }
        event.preventDefault();
        sendFormOrQueue(attendanceForm, queueAttendance).catch(function (error) {
          showMessage(error.message, "error", attendanceForm);
        });
      });
    }
    var offlineMemberForm = document.querySelector("[data-offline-page-member-form]");
    if (offlineMemberForm) {
      offlineMemberForm.addEventListener("submit", function (event) {
        event.preventDefault();
        queueMember(offlineMemberForm).catch(function (error) {
          showMessage(error.message, "error", offlineMemberForm);
        });
      });
    }
    var offlineAttendanceForm = document.querySelector("[data-offline-page-attendance-form]");
    if (offlineAttendanceForm) {
      offlineAttendanceForm.addEventListener("submit", function (event) {
        event.preventDefault();
        queueAttendance(offlineAttendanceForm).catch(function (error) {
          showMessage(error.message, "error", offlineAttendanceForm);
        });
      });
    }
  }

  function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("/sw.js").catch(function () {});
    navigator.serviceWorker.addEventListener("message", function (event) {
      if (event.data && event.data.type === "SYNC_NOW") syncPending();
    });
  }

  async function init() {
    setNetworkState(navigator.onLine);
    registerServiceWorker();
    attachFormHandlers();
    if (!document.body.classList.contains("offline-page")) {
      await refreshLocalMembers();
    }
    await renderOfflineMembers();
    await syncPending();
    window.addEventListener("online", function () {
      setNetworkState(true);
      refreshLocalMembers();
      syncPending();
    });
    window.addEventListener("offline", function () {
      setNetworkState(false);
    });
    window.setInterval(syncPending, 60000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();