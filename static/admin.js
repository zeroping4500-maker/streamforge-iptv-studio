/* ZEMYIPTV admin panel — Neon Auth sign-in + admin API client */
(() => {
  "use strict";

  const state = {
    cfg: null,
    admin: null,
    view: "overview",
    channels: { q: "", status: "", offset: 0, total: 0 },
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function el(tag, attrs = {}, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }

  let toastTimer = 0;
  function toast(message, bad = false) {
    const node = $("#toast");
    node.textContent = message;
    node.classList.toggle("bad", !!bad);
    node.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("show"), 3200);
  }

  async function jsonFetch(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: Object.assign({ Accept: "application/json" }, options.headers || {}),
      ...options,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (err) {
      payload = null;
    }
    return { ok: response.ok, status: response.status, payload };
  }

  function apiGet(path) {
    return jsonFetch(path);
  }

  function apiPost(path, body) {
    return jsonFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "zemy-admin" },
      body: JSON.stringify(body || {}),
    });
  }

  function showBanner(message) {
    const banner = $("#viewError");
    if (!message) {
      banner.hidden = true;
      banner.textContent = "";
      return;
    }
    banner.textContent = message;
    banner.hidden = false;
  }

  function skeleton(host, rows = 4) {
    host.replaceChildren();
    for (let i = 0; i < rows; i += 1) host.append(el("div", { class: "skeleton row-sk" }));
  }

  function empty(host, title, hint) {
    host.replaceChildren(
      el("div", { class: "empty" }, el("strong", { text: title }), el("span", { text: hint || "" }))
    );
  }

  // ---------------------------------------------------------------- auth view
  function showAuth() {
    $("#authView").hidden = false;
    $("#appView").hidden = true;
    $("#emailInput").focus();
  }

  function showApp() {
    $("#authView").hidden = true;
    $("#appView").hidden = false;
    $("#whoami").textContent = state.admin
      ? `${state.admin.email} · ${state.admin.role}`
      : "";
  }

  function setNote(message, kind = "") {
    const note = $("#authNote");
    note.textContent = message;
    note.className = "note" + (kind ? " " + kind : "");
  }

  function busy(button, isBusy, label) {
    if (!button) return;
    if (label) button.textContent = isBusy ? label : button.dataset.label || button.textContent;
    button.disabled = isBusy;
  }

  async function exchangeToken(token, options = {}) {
    if (!token) throw new Error("Neon Auth did not return a session token");
    const { ok, status, payload } = await apiPost("/api/admin/session", { token });
    if (!ok) {
      const message =
        payload && payload.code === "not_admin"
          ? "That account signed in but has no admin permission for ZEMYIPTV."
          : (payload && payload.error) || `Sign-in failed (HTTP ${status})`;
      throw new Error(message);
    }
    state.admin = payload.admin;
    sessionStorage.removeItem("zemy.pendingAuth");
    showApp();
    await switchView(state.view || "overview");
    toast(`Signed in as ${state.admin.email}`);
  }

  async function sendCode(email) {
    const { ok, status, payload } = await apiPost("/api/auth/otp/send", { email });
    if (!ok) {
      throw new Error((payload && payload.error) || `Could not send the code (HTTP ${status})`);
    }
  }

  async function verifyCode(email, otp) {
    const { ok, status, payload } = await apiPost("/api/auth/otp/verify", { email, otp });
    if (!ok) {
      throw new Error((payload && payload.error) || "That code was not accepted");
    }
    const token = payload && payload.token;
    if (!token) throw new Error("Sign-in service returned no session token");
    return token;
  }

  async function googleStart() {
    const { ok, status, payload } = await apiPost("/api/auth/google/start", {});
    if (!ok || !payload || !payload.url) {
      throw new Error((payload && payload.error) || `Google sign-in is unavailable (HTTP ${status})`);
    }
    // Keep the address that can tell us who came back (hosted flow only).
    if (payload.pickup) sessionStorage.setItem("zemy.authPickup", payload.pickup);
    else sessionStorage.removeItem("zemy.authPickup");
    sessionStorage.setItem("zemy.pendingAuth", "google");
    return payload.url;
  }

  function bindAuth() {
    $$(".switch-btn").forEach((button) => {
      button.addEventListener("click", () => {
        const method = button.dataset.method;
        $$(".switch-btn").forEach((other) => {
          const active = other === button;
          other.classList.toggle("is-active", active);
          other.setAttribute("aria-selected", String(active));
        });
        $("#otpForm").hidden = method !== "otp";
        $("#googlePanel").hidden = method !== "google";
        setNote("");
      });
    });

    $("#otpForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const email = $("#emailInput").value.trim();
      if (!email) return;
      busy($("#sendBtn"), true);
      setNote("Sending a one-time code…");
      try {
        await sendCode(email);
        $("#codeRow").hidden = false;
        $("#sendBtn").hidden = true;
        $("#verifyBtn").hidden = false;
        setNote(`Code sent to ${email}. It expires in a few minutes.`, "ok");
        $("#codeInput").focus();
      } catch (err) {
        setNote(err.message, "bad");
        busy($("#sendBtn"), false);
      }
    });

    $("#verifyBtn").addEventListener("click", async () => {
      const email = $("#emailInput").value.trim();
      const otp = $("#codeInput").value.trim();
      if (!otp) {
        setNote("Enter the 6-digit code from the email.", "bad");
        return;
      }
      busy($("#verifyBtn"), true, "Verifying…");
      try {
        const token = await verifyCode(email, otp);
        await exchangeToken(token);
      } catch (err) {
        setNote(err.message, "bad");
        busy($("#verifyBtn"), false);
      }
    });

    $("#googleBtn").addEventListener("click", async () => {
      busy($("#googleBtn"), true, "Opening Google…");
      try {
        const url = await googleStart();
        sessionStorage.setItem("zemy.pendingAuth", "google");
        location.href = url;
      } catch (err) {
        setNote(err.message, "bad");
        busy($("#googleBtn"), false);
      }
    });
  }

  async function resumeGoogle() {
    // Google's callback sets its cookie on the auth host, not on this origin.
    // Ask that host who the visitor is, then trade the token for our own session.
    const pickup = sessionStorage.getItem("zemy.authPickup");
    if (!pickup) return false;
    let token = null;
    try {
      const response = await fetch(pickup, { credentials: "include", mode: "cors", cache: "no-store" });
      if (response.ok) {
        const data = await response.json().catch(() => null);
        token = (data && (data.token || (data.session && data.session.token))) || null;
      }
    } catch (err) {
      token = null;
    }
    if (!token) {
      setNote("Google finished but the panel got no session. Use the email code — it signs in on this site.", "bad");
      return false;
    }
    try {
      await exchangeToken(token);
      sessionStorage.removeItem("zemy.authPickup");
      sessionStorage.removeItem("zemy.pendingAuth");
      return true;
    } catch (err) {
      setNote(friendlyAuthError(err), "bad");
      sessionStorage.removeItem("zemy.authPickup");
      return false;
    }
  }

  function authErrorFromQuery() {
    const params = new URLSearchParams(location.search);
    const code = params.get("error");
    if (!code) return;
    setNote(friendlyAuthError({ message: code }), "bad");
    params.delete("error");
    const rest = params.toString();
    history.replaceState(null, "", location.pathname + (rest ? "?" + rest : ""));
  }

  function friendlyAuthError(err) {
    const raw = String((err && err.message) || err || "");
    if (raw.includes("account_not_linked")) {
      return "This Google email already has an email-code account that Neon Auth will not attach Google to. Sign in with the email code — that is the same account and has admin access.";
    }
    if (raw.includes("not_admin")) return "That account is not on the ZEMYIPTV admin list.";
    if (raw.includes("google_not_configured")) return "Google sign-in is not configured on this server. Use the email code.";
    if (raw.includes("sign_in_expired")) return "That Google sign-in took too long. Try again.";
    if (raw.includes("google_")) return "Google sign-in failed. Try again, or use the email code.";
    if (/expired|invalid/i.test(raw)) return "Your session expired — sign in again.";
    return raw || "Sign-in failed";
  }

  // ------------------------------------------------------------- panel views
  async function switchView(view) {
    if (!state.admin) return;
    state.view = view;
    $$(".tab").forEach((tab) => tab.classList.toggle("is-active", tab.dataset.view === view));
    $$(".view").forEach((section) => (section.hidden = section.id !== "view-" + view));
    showBanner("");
    try {
      if (view === "overview") await loadOverview();
      if (view === "channels") await loadChannels(true);
      if (view === "playlists") await loadPlaylists();
      if (view === "providers") await loadProviders();
      if (view === "admins") await loadAdmins();
      if (view === "audit") await loadAudit();
    } catch (err) {
      if (err && err.status === 401) return signedOut();
      showBanner(err.message || "Could not load this section");
    }
  }

  function guard(result) {
    if (result.status === 401) {
      signedOut();
      throw Object.assign(new Error("Your admin session expired"), { status: 401 });
    }
    if (!result.ok) {
      const message = (result.payload && result.payload.error) || `Request failed (HTTP ${result.status})`;
      throw new Error(message);
    }
    return result.payload;
  }

  async function loadOverview() {
    const stats = $("#stats");
    const health = $("#healthBlock");
    const storage = $("#storageBlock");
    const recent = $("#recentChannels");
    stats.replaceChildren(...Array.from({ length: 4 }, () => el("div", { class: "stat skeleton", style: "height:96px" })));
    skeleton(health, 2);
    skeleton(storage, 3);
    skeleton(recent, 3);

    const data = guard(await apiGet("/api/admin/overview"));

    const cards = [
      { k: "Channels", v: data.channels, s: `${data.channels_enabled} enabled` },
      { k: "Playlists", v: data.playlists, s: "in the library" },
      { k: "Providers", v: data.providers, s: `${data.providers_enabled} enabled` },
      { k: "Admin sessions", v: data.sessions, s: `${data.audit_events} audit events` },
    ];
    stats.replaceChildren(
      ...cards.map((card) =>
        el("div", { class: "stat" }, el("div", { class: "k", text: card.k }), el("div", { class: "v", text: card.v }), el("div", { class: "s", text: card.s }))
      )
    );

    const h = data.last_health;
    if (!h) {
      empty(health, "No health run yet", "Run a check from the main studio to see stream health here.");
    } else {
      const total = Math.max(1, h.total_checked || 0);
      const online = h.online_count || 0;
      const offline = h.offline_count || 0;
      const pct = Math.round((online / total) * 100);
      health.replaceChildren(
        el("div", { class: "pulse" },
          el("div", { class: "on", style: `width:${pct}%` }),
          el("div", { class: "off", style: `width:${100 - pct}%` })
        ),
        el("div", { class: "legend" },
          el("span", {}, "Online ", el("b", { text: online })),
          el("span", {}, "Offline ", el("b", { text: offline })),
          el("span", {}, "Checked ", el("b", { text: total })),
          el("span", {}, "Last run ", el("b", { text: minutesAgo(h.ran_at) }))
        )
      );
    }

    const store = data.storage || {};
    storage.replaceChildren(
      kv("Engine", store.storage || "unknown"),
      kv("Database", store.database || "—"),
      kv("Version", store.version || "—"),
      kv("Sessions active", data.sessions)
    );

    if (!data.recent_channels.length) {
      empty(recent, "No channels yet", "Import a playlist to see it here.");
    } else {
      recent.replaceChildren(
        ...data.recent_channels.map((channel) =>
          el("li", {}, el("span", { text: channel.name }), el("span", { class: "muted", text: channel.category || "—" }))
        )
      );
    }
  }

  function kv(label, value) {
    return el("div", {}, el("span", { text: label }), el("span", { text: value }));
  }

  function minutesAgo(epochSeconds) {
    if (!epochSeconds) return "never";
    const diff = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
    return `${Math.floor(diff / 86400)} d ago`;
  }

  async function loadChannels(reset) {
    const list = $("#channelList");
    if (reset) {
      state.channels.offset = 0;
      skeleton(list, 5);
    }
    const params = new URLSearchParams({
      q: state.channels.q,
      status: state.channels.status,
      limit: "50",
      offset: String(state.channels.offset),
    });
    const data = guard(await apiGet("/api/admin/channels?" + params.toString()));
    state.channels.total = data.total;
    if (reset) list.replaceChildren();
    if (!data.items.length && state.channels.offset === 0) {
      const filtered = state.channels.q || state.channels.status;
      empty(list, filtered ? "Nothing matches that filter" : "No channels yet", filtered ? "Try another name or clear the filter." : "Import a playlist from the studio.");
    } else {
      data.items.forEach((channel) => list.append(channelRow(channel)));
    }
    $("#channelSummary").replaceChildren(
      el("span", { text: `${data.total} channel${data.total === 1 ? "" : "s"}` }),
      el("span", { text: state.channels.q ? `matching “${state.channels.q}”` : "" })
    );
    state.channels.offset += data.items.length;
    $("#loadMore").hidden = state.channels.offset >= data.total || !data.items.length;
  }

  function statusBadge(status, enabled) {
    if (!enabled) return el("span", { class: "badge disabled" }, el("span", { class: "dot" }), "disabled");
    if (status === "online") return el("span", { class: "badge online" }, el("span", { class: "dot" }), "online");
    if (status === "offline") return el("span", { class: "badge offline" }, el("span", { class: "dot" }), "offline");
    return el("span", { class: "badge" }, el("span", { class: "dot" }), status || "unknown");
  }

  function channelRow(channel) {
    let badge = statusBadge(channel.status, channel.enabled);
    const toggle = el("button", {
      class: "switch",
      type: "button",
      role: "switch",
      "aria-checked": String(!!channel.enabled),
      "aria-label": `Enable ${channel.name}`,
    });
    toggle.addEventListener("click", async () => {
      const next = toggle.getAttribute("aria-checked") !== "true";
      toggle.disabled = true;
      const result = await apiPost(`/api/admin/channels/${encodeURIComponent(channel.id)}/enabled`, { enabled: next });
      toggle.disabled = false;
      if (!result.ok) {
        toast((result.payload && result.payload.error) || "Could not update the channel", true);
        return;
      }
      toggle.setAttribute("aria-checked", String(next));
      toggle.setAttribute("aria-label", `${next ? "Disable" : "Enable"} ${channel.name}`);
      channel.enabled = next ? 1 : 0;
      const updated = statusBadge(channel.status, channel.enabled);
      badge.replaceWith(updated);
      badge = updated;
      toast(next ? `Enabled ${channel.name}` : `Disabled ${channel.name}`);
    });

    const remove = el("button", { class: "icon-btn", type: "button", "aria-label": `Delete ${channel.name}`, title: "Delete channel" }, "✕");
    remove.addEventListener("click", async () => {
      if (!confirm(`Delete “${channel.name}” from the library? This cannot be undone.`)) return;
      remove.disabled = true;
      const result = await apiPost(`/api/admin/channels/${encodeURIComponent(channel.id)}/delete`, {});
      if (!result.ok) {
        remove.disabled = false;
        toast((result.payload && result.payload.error) || "Could not delete the channel", true);
        return;
      }
      const row = remove.closest(".row");
      if (row) row.remove();
      state.channels.total = Math.max(0, state.channels.total - 1);
      toast(`Deleted ${channel.name}`);
    });

    return el("div", { class: "row" },
      el("div", { class: "cell-main" },
        el("div", { class: "name", text: channel.name }),
        el("div", { class: "meta", text: `${channel.category || "Uncategorized"} · ${channel.playlist_name || "no playlist"}` })
      ),
      el("div", { class: "meta secondary" },
        statusBadge(channel.status, channel.enabled),
        channel.status_note ? el("span", { class: "note" }, " " + channel.status_note) : ""
      ),
      el("div", { class: "actions" }, toggle, remove)
    );
  }

  async function loadPlaylists() {
    const host = $("#playlistList");
    skeleton(host, 3);
    const data = guard(await apiGet("/api/admin/playlists"));
    if (!data.playlists.length) {
      empty(host, "No playlists yet", "Create one in the studio, then manage it here.");
      return;
    }
    host.replaceChildren(
      ...data.playlists.map((playlist) => {
        const remove = el("button", { class: "btn ghost small", type: "button", text: "Delete" });
        remove.addEventListener("click", async () => {
          if (!confirm(`Delete “${playlist.name}” and its ${playlist.channel_count} channels?`)) return;
          remove.disabled = true;
          const result = await apiPost(`/api/admin/playlists/${encodeURIComponent(playlist.id)}/delete`, {});
          if (!result.ok) {
            remove.disabled = false;
            toast((result.payload && result.payload.error) || "Could not delete the playlist", true);
            return;
          }
          toast(`Deleted ${playlist.name}`);
          await loadPlaylists();
        });
        return el("article", { class: "card" },
          el("h3", { text: playlist.name }),
          el("div", { class: "meta", text: playlist.description || "No description" }),
          el("div", { class: "meta", text: `${playlist.channel_count} channels · ${playlist.enabled_count} enabled` }),
          el("div", { class: "foot" },
            el("span", { class: "badge", text: new Date(playlist.created_at * 1000).toLocaleDateString() }),
            remove
          )
        );
      })
    );
  }

  async function loadProviders() {
    const host = $("#providerList");
    skeleton(host, 5);
    const data = guard(await apiGet("/api/admin/providers"));
    if (!data.providers.length) {
      empty(host, "No providers configured", "Providers are seeded on first boot.");
      return;
    }
    host.replaceChildren(
      ...data.providers.map((provider) => {
        const toggle = el("button", {
          class: "switch",
          type: "button",
          role: "switch",
          "aria-checked": String(!!provider.enabled),
          "aria-label": `Enable ${provider.name}`,
        });
        toggle.addEventListener("click", async () => {
          const next = toggle.getAttribute("aria-checked") !== "true";
          toggle.disabled = true;
          const result = await apiPost("/api/admin/providers/toggle", { id: provider.id, enabled: next });
          toggle.disabled = false;
          if (!result.ok) {
            toast((result.payload && result.payload.error) || "Could not update the provider", true);
            return;
          }
          toggle.setAttribute("aria-checked", String(next));
          toast(next ? `Enabled ${provider.name}` : `Disabled ${provider.name}`);
        });
        return el("div", { class: "row" },
          el("div", {},
            el("div", { class: "name", text: provider.name }),
            el("div", { class: "meta", text: provider.badge || provider.provider_type })
          ),
          el("div", { class: "meta secondary", text: `${provider.channel_count || 0} channels` }),
          el("div", { class: "actions" }, toggle)
        );
      })
    );
  }

  async function loadAdmins() {
    const host = $("#adminList");
    const users = $("#userList");
    skeleton(host, 3);
    skeleton(users, 2);
    const data = guard(await apiGet("/api/admin/admins"));
    if (!data.admins.length) {
      empty(host, "No explicit grants", "Accounts with the Neon Auth admin role can still sign in.");
    } else {
      host.replaceChildren(
        ...data.admins.map((admin) => {
          const revoke = el("button", { class: "btn ghost small", type: "button", text: "Revoke" });
          revoke.addEventListener("click", async () => {
            if (!confirm(`Revoke admin access for ${admin.email}?`)) return;
            revoke.disabled = true;
            const result = await apiPost("/api/admin/admins/revoke", { user_id: admin.user_id });
            if (!result.ok) {
              revoke.disabled = false;
              toast((result.payload && result.payload.error) || "Could not revoke", true);
              return;
            }
            toast(`Revoked ${admin.email}`);
            await loadAdmins();
          });
          return el("div", { class: "row" },
            el("div", {},
              el("div", { class: "name", text: admin.email }),
              el("div", { class: "meta", text: `last seen ${admin.last_seen_at ? minutesAgo(admin.last_seen_at) : "never"}` })
            ),
            el("div", { class: "meta secondary" }, el("span", { class: "badge role", text: admin.role })),
            el("div", { class: "actions" }, revoke)
          );
        })
      );
    }

    const userData = guard(await apiGet("/api/admin/users"));
    if (!userData.users.length) {
      empty(users, "No Neon Auth accounts", "Accounts appear here after sign-up.");
      return;
    }
    users.replaceChildren(
      ...userData.users.map((user) => {
        const already = user.is_admin || ["admin", "owner"].includes((user.role || "").toLowerCase());
        const button = el("button", {
          class: "btn ghost small",
          type: "button",
          text: already ? "Has access" : "Grant admin",
        });
        if (already) button.disabled = true;
        button.addEventListener("click", async () => {
          button.disabled = true;
          const result = await apiPost("/api/admin/admins/grant", { email: user.email, role: "admin" });
          if (!result.ok) {
            button.disabled = false;
            toast((result.payload && result.payload.error) || "Could not grant", true);
            return;
          }
          toast(`Granted admin to ${user.email}`);
          await loadAdmins();
        });
        return el("div", { class: "row" },
          el("div", {},
            el("div", { class: "name", text: user.email }),
            el("div", { class: "meta", text: `${user.name || "no name"} · ${user.verified ? "verified" : "unverified"}` })
          ),
          el("div", { class: "meta secondary", text: user.role || "user" }),
          el("div", { class: "actions" }, button)
        );
      })
    );
  }

  async function loadAudit() {
    const host = $("#auditList");
    skeleton(host, 6);
    const data = guard(await apiGet("/api/admin/audit"));
    if (!data.events.length) {
      empty(host, "No admin activity yet", "Sign-ins and changes are recorded here.");
      return;
    }
    host.replaceChildren(
      ...data.events.map((event) =>
        el("div", { class: "row" },
          el("div", {},
            el("div", { class: "name", text: event.action }),
            el("div", { class: "meta", text: event.detail || "—" })
          ),
          el("div", { class: "meta secondary", text: event.actor_email || "system" }),
          el("div", { class: "meta", text: minutesAgo(event.at) })
        )
      )
    );
  }

  function signedOut() {
    state.admin = null;
    showAuth();
    setNote("Your admin session ended. Sign in again to continue.");
  }

  function bindPanel() {
    $$(".tab").forEach((tab) => tab.addEventListener("click", () => switchView(tab.dataset.view)));

    let searchTimer = 0;
    $("#channelSearch").addEventListener("input", (event) => {
      state.channels.q = event.target.value.trim();
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => loadChannels(true).catch(() => {}), 280);
    });
    $("#channelStatus").addEventListener("change", (event) => {
      state.channels.status = event.target.value;
      loadChannels(true).catch(() => {});
    });
    $("#loadMore").addEventListener("click", () => loadChannels(false).catch(() => {}));

    $("#grantForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const email = $("#grantEmail").value.trim();
      const role = $("#grantRole").value;
      const note = $("#grantNote");
      if (!email) return;
      note.className = "note";
      note.textContent = "Granting…";
      const result = await apiPost("/api/admin/admins/grant", { email, role });
      if (!result.ok) {
        note.className = "note bad";
        note.textContent = (result.payload && result.payload.error) || "Could not grant access";
        return;
      }
      note.className = "note ok";
      note.textContent = `${email} can now sign in as ${role}.`;
      $("#grantEmail").value = "";
      await loadAdmins();
    });

    $("#signOut").addEventListener("click", async () => {
      await apiPost("/api/admin/logout", {});
      state.admin = null;
      showAuth();
      toast("Signed out");
    });
  }

  async function boot() {
    bindAuth();
    bindPanel();
    const config = await jsonFetch("/api/auth/config");
    state.cfg = config.payload || {};
    if (!state.cfg.enabled) {
      showAuth();
      setNote("Neon Auth is not configured on this server.", "bad");
      return;
    }
    $("#authHost").textContent = state.cfg.appName || "ZEMYIPTV sign-in";
    const me = await apiGet("/api/admin/session");
    if (me.ok && me.payload && me.payload.authenticated) {
      state.admin = me.payload.admin;
      showApp();
      await switchView(state.view || "overview");
      return;
    }
    if (await resumeGoogle()) return;
    showAuth();
    authErrorFromQuery();
    if (sessionStorage.getItem("zemy.pendingAuth") === "google") {
      sessionStorage.removeItem("zemy.pendingAuth");
      setNote("Google returned without a session. Try the email code, or configure the app's own Google client.", "bad");
    }
  }

  boot().catch((err) => {
    showAuth();
    setNote(err.message || "Could not start the admin panel", "bad");
  });
})();
