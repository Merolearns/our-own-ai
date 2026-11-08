const csrf = document.querySelector('meta[name="csrf-token"]').content;
// The chat section carries the agent's display name (Nova / Scoopy Doog),
// so waiting and error text names the agent this host serves.
const agentName =
  document.querySelector(".chat")?.dataset.agent || "Nova";
const log = document.getElementById("log");
const form = document.getElementById("composer");
const input = document.getElementById("message");
const send = document.getElementById("send");
const starters = document.getElementById("starters");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function post(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  return { status: response.status, ok: response.ok, data };
}

function scrollDown() {
  log.scrollTop = log.scrollHeight;
}

function addUser(text) {
  log.append(el("div", "bubble user", text));
  scrollDown();
}

function feedbackButtons(traceId) {
  const box = el("span", "feedback");
  for (const [rating, label] of [
    ["up", "Helpful"],
    ["down", "Not helpful"],
  ]) {
    const button = el("button", "quiet small", label);
    button.type = "button";
    button.addEventListener("click", async () => {
      const result = await post("/api/feedback", { trace_id: traceId, rating });
      box.textContent = result.ok
        ? "Thanks for the feedback."
        : "Feedback didn't save. Try again.";
    });
    box.append(button);
  }
  return box;
}

function confirmBox(confirm) {
  const box = el("div", "confirm");
  box.append(
    el("p", null, `Refund ${confirm.display} on order #${confirm.order_id}?`),
  );
  const button = el("button", "primary", `Confirm ${confirm.display} refund`);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    const result = await post("/api/refund/confirm", { token: confirm.token });
    box.replaceChildren(
      el(
        "p",
        result.ok ? "confirm-done" : "confirm-failed",
        result.data.message ||
          result.data.error ||
          `That didn't work. Ask ${agentName} again.`,
      ),
    );
  });
  box.append(button);
  return box;
}

function addNova(turn) {
  const slip = el("article", "slip reply");
  if (turn.blocked) {
    slip.classList.add("blocked");
    slip.append(el("span", "stamp", "Blocked"));
  }
  slip.append(el("p", "answer", turn.answer));
  if (turn.proposal)
    slip.append(el("p", "pending", "Waiting for staff approval"));
  if (turn.confirm) slip.append(confirmBox(turn.confirm));

  if (turn.sources && turn.sources.length) {
    const list = el("ol", "sources");
    list.setAttribute("aria-label", "Sources");
    for (const source of turn.sources) {
      const item = el("li", null, `${source.title}: ${source.section}`);
      item.value = source.n;
      list.append(item);
    }
    slip.append(el("h2", "sources-title", "Sources"), list);
  }

  const meta = el("footer", "meta");
  if (turn.demo)
    meta.append(el("span", "demo-tag", "demo mode — no live model"));
  if (turn.cache && turn.cache !== "miss" && turn.cache !== "skip")
    meta.append(el("span", null, `Answered from cache (${turn.cache})`));
  const trace = el("a", null, "View trace");
  trace.href = `/traces/${turn.trace_id}`;
  meta.append(trace, feedbackButtons(turn.trace_id));
  slip.append(meta);
  log.append(slip);
  scrollDown();
}

async function submit(text) {
  starters.hidden = true;
  addUser(text);
  input.value = "";
  send.disabled = true;
  const waiting = el("p", "waiting", `${agentName} is checking`);
  log.append(waiting);
  scrollDown();
  try {
    const result = await post("/api/chat", { message: text });
    waiting.remove();
    if (result.ok) addNova(result.data);
    else
      log.append(
        el(
          "p",
          "flash error",
          result.data.error || "That message didn't send. Try again.",
        ),
      );
  } catch {
    waiting.remove();
    log.append(
      el(
        "p",
        "flash error",
        "The connection dropped. Check your network and send again.",
      ),
    );
  }
  send.disabled = false;
  input.focus();
  scrollDown();
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (text) submit(text);
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

for (const button of starters.querySelectorAll("button")) {
  button.addEventListener("click", () => submit(button.dataset.prompt));
}

const history = JSON.parse(document.getElementById("history").textContent);
for (const turn of history) {
  addUser(turn.user);
  addNova(turn);
}
starters.hidden = history.length > 0;
