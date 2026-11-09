/* NaijaBasket PWA client: basket mutations, toasts, offline awareness. */
(function () {
  "use strict";

  const config = window.NAIJA || {};
  const csrfToken = config.csrfToken || "";
  const toastHost = document.getElementById("toast-host");

  function toast(message, kind) {
    if (!toastHost || !message) return;
    const node = document.createElement("div");
    node.className = "toast" + (kind ? ` is-${kind}` : "");
    node.textContent = message;
    toastHost.appendChild(node);
    setTimeout(() => node.remove(), 4200);
  }

  async function api(path, options) {
    const opts = options || {};
    const headers = { Accept: "application/json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (csrfToken) headers["X-CSRFToken"] = csrfToken;

    const response = await fetch(path, {
      method: opts.method || "GET",
      headers,
      credentials: "same-origin",
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    });

    let payload = {};
    try {
      payload = await response.json();
    } catch (err) {
      payload = {};
    }
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `Request failed (${response.status})`);
    }
    return payload;
  }

  function setCartBadges(count) {
    document.querySelectorAll("[data-cart-badge]").forEach((node) => {
      node.textContent = String(count);
      node.hidden = count <= 0;
    });
  }

  function busy(button, state) {
    if (!button) return;
    if (state) {
      button.dataset.label = button.textContent;
      button.textContent = "Adding…";
      button.disabled = true;
    } else {
      button.textContent = button.dataset.label || button.textContent;
      button.disabled = false;
    }
  }

  document.addEventListener("click", async (event) => {
    const addButton = event.target.closest("[data-add-to-cart]");
    if (addButton) {
      event.preventDefault();
      busy(addButton, true);
      try {
        const result = await api("/api/cart/items", {
          method: "POST",
          body: { product_id: Number(addButton.dataset.addToCart), quantity: 1 },
        });
        setCartBadges(result.cart.item_count);
        const minimum = result.cart.minimum;
        toast(
          minimum.met
            ? `${result.message} · basket ready for checkout`
            : `${result.message} · ${minimum.shortfall.toLocaleString("en-NG")} NGN to the minimum`,
          minimum.met ? "accent" : ""
        );
      } catch (error) {
        if (/sign in/i.test(error.message) || /401/.test(error.message)) {
          window.location.href = "/auth/login?next=" + encodeURIComponent(window.location.pathname);
          return;
        }
        toast(error.message, "error");
      } finally {
        busy(addButton, false);
      }
      return;
    }

    const qtyButton = event.target.closest("[data-qty-delta]");
    if (qtyButton) {
      event.preventDefault();
      const productId = Number(qtyButton.dataset.qtyDelta);
      const delta = Number(qtyButton.dataset.delta);
      const valueNode = document.querySelector(`[data-qty-value="${productId}"]`);
      const next = Math.max(0, Number(valueNode ? valueNode.textContent : 1) + delta);
      busy(qtyButton, true);
      try {
        await api(`/api/cart/items/${productId}`, {
          method: "PATCH",
          body: { quantity: next },
        });
        window.location.reload();
      } catch (error) {
        toast(error.message, "error");
        busy(qtyButton, false);
      }
      return;
    }

    const removeButton = event.target.closest("[data-remove-product]");
    if (removeButton) {
      event.preventDefault();
      busy(removeButton, true);
      try {
        await api(`/api/cart/items/${Number(removeButton.dataset.removeProduct)}`, {
          method: "DELETE",
        });
        window.location.reload();
      } catch (error) {
        toast(error.message, "error");
        busy(removeButton, false);
      }
    }
  });

  /* Checkout guard: ask the server whether the minimum order rule is satisfied. */
  const checkoutForm = document.querySelector("[data-checkout-form]");
  if (checkoutForm) {
    checkoutForm.addEventListener("submit", async (event) => {
      const submit = checkoutForm.querySelector('button[type="submit"]');
      if (checkoutForm.dataset.validated === "1") return;
      event.preventDefault();
      busy(submit, true);
      try {
        await api("/api/cart/validate", { method: "POST", body: {} });
        checkoutForm.dataset.validated = "1";
        checkoutForm.submit();
      } catch (error) {
        toast(error.message, "error");
        busy(submit, false);
      }
    });
  }

  /* Copy-to-clipboard affordances (delivery PIN, recovery links). */
  document.addEventListener("click", async (event) => {
    const copyButton = event.target.closest("[data-copy]");
    if (!copyButton) return;
    event.preventDefault();
    try {
      await navigator.clipboard.writeText(copyButton.dataset.copy);
      toast("Copied to clipboard", "accent");
    } catch (error) {
      toast("Copy failed — select the text manually", "error");
    }
  });

  /* Offline awareness */
  const offlineBar = document.getElementById("offline-bar");
  function syncConnection() {
    if (!offlineBar) return;
    offlineBar.classList.toggle("is-visible", !navigator.onLine);
  }
  window.addEventListener("online", syncConnection);
  window.addEventListener("offline", syncConnection);
  syncConnection();

  /* Service worker */
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/static/sw.js").catch(() => undefined);
    });
  }

  window.NB = { api, toast, setCartBadges };
})();
