/* Merchant OMS: 15-minute SLA countdowns and pick-list ergonomics. */
(function () {
  "use strict";

  /* Countdown: every [data-countdown-seconds] ticks down locally, flipping to
     the danger state under the warning threshold; the server remains the
     authority for breach auditing (flask sla-check). */
  const timers = Array.from(document.querySelectorAll("[data-countdown-seconds]"));
  if (timers.length) {
    const warningSeconds = Number(
      document.body.dataset.slaWarningSeconds || 180
    );

    function render() {
      timers.forEach((node) => {
        let remaining = Number(node.dataset.countdownSeconds || 0);
        if (remaining > 0) {
          remaining -= 1;
          node.dataset.countdownSeconds = String(remaining);
        }
        const minutes = Math.floor(Math.max(0, remaining) / 60);
        const seconds = Math.max(0, remaining) % 60;
        node.textContent = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;

        let state = "success";
        if (remaining <= 0 || remaining <= warningSeconds) state = "danger";
        else if (remaining <= warningSeconds * 2) state = "warning";
        node.dataset.state = state;

        if (remaining <= 0) {
          const card = node.closest("[data-sla-card]");
          if (card && card.dataset.escalated !== "1") {
            card.dataset.escalated = "1";
            const flag = card.querySelector("[data-sla-flag]");
            if (flag) {
              flag.textContent = "Picking window exceeded — dispatch now";
              flag.className = "pill pill-danger";
            }
          }
        }
      });
    }

    render();
    window.setInterval(render, 1000);
  }

  /* Pick & pack checklist: strikethrough + progress + pack-gate. */
  const checklist = document.querySelector("[data-picklist]");
  if (checklist) {
    const boxes = Array.from(checklist.querySelectorAll('input[type="checkbox"]'));
    const progress = document.querySelector("[data-pick-progress]");
    const progressLabel = document.querySelector("[data-pick-progress-label]");
    const packButton = document.querySelector("[data-pack-button]");

    function sync() {
      const done = boxes.filter((box) => box.checked).length;
      boxes.forEach((box) => box.closest(".pick-item").classList.toggle("is-picked", box.checked));
      if (progress) progress.style.width = `${boxes.length ? (done / boxes.length) * 100 : 0}%`;
      if (progressLabel) progressLabel.textContent = `${done} of ${boxes.length} picked`;
      if (packButton) {
        const ready = done === boxes.length && boxes.length > 0;
        packButton.disabled = !ready;
        packButton.title = ready ? "" : "Tick every item before sealing the order";
      }
    }

    boxes.forEach((box) => box.addEventListener("change", sync));
    sync();
  }
})();
