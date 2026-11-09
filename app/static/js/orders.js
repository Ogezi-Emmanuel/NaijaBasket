/* Live order tracker: polls the ownership-scoped status endpoint. */
(function () {
  "use strict";

  const root = document.querySelector("[data-order-tracker]");
  if (!root) return;

  const reference = root.dataset.orderTracker;
  const terminal = ["delivered", "cancelled", "refunded"];
  const POLL_MS = 20000;
  let timer = null;

  function apply(payload) {
    const statusNode = document.querySelector("[data-status-label]");
    if (statusNode) statusNode.textContent = payload.status_label;

    const escortNode = document.querySelector("[data-escrow-label]");
    if (escortNode && payload.escrow_status) {
      escortNode.textContent = payload.escrow_status.replace(/_/g, " ");
    }

    document.querySelectorAll("[data-tracker-step]").forEach((step, index) => {
      const reached = payload.tracker[index] && payload.tracker[index].reached;
      step.classList.toggle("is-reached", Boolean(reached));
    });

    const etaNode = document.querySelector("[data-transit-eta]");
    if (etaNode && payload.transit && payload.transit.eta_minutes) {
      etaNode.textContent = `${payload.transit.eta_minutes} min`;
    }
    const riderNode = document.querySelector("[data-transit-rider]");
    if (riderNode && payload.transit && payload.transit.rider_name) {
      riderNode.textContent = `${payload.transit.rider_name}${
        payload.transit.vehicle ? " · " + payload.transit.vehicle : ""
      }`;
    }
    const slaNode = document.querySelector("[data-transit-deadline]");
    if (slaNode && payload.sla && payload.sla.state) {
      slaNode.dataset.state = payload.sla.state;
    }

    if (terminal.indexOf(payload.status) !== -1) {
      if (timer) window.clearInterval(timer);
      if (payload.status === "delivered") {
        const banner = document.querySelector("[data-delivered-banner]");
        if (banner && banner.hidden) {
          banner.hidden = false;
          window.setTimeout(() => window.location.reload(), 2600);
        }
      }
    }
  }

  async function poll() {
    try {
      const response = await fetch(`/api/orders/${reference}/status`, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!response.ok) return;
      apply(await response.json());
    } catch (error) {
      /* offline: the service worker keeps the shell, we simply retry later */
    }
  }

  timer = window.setInterval(poll, POLL_MS);
  poll();
})();
