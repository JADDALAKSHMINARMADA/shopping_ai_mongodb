import os
import re
import difflib
from flask import Flask, request, jsonify, render_template_string
from llm import generate_query, generate_answer, format_full_list, MODEL_NAME
from database import run_query, find_all, find_similar_names, is_empty_result

app = Flask(__name__)

# Remembers the last "Did you mean X?" so a "yes" reply can re-run the query.
# `pairs` is a list of (typed, real) tuples so multiple names are handled.
last_suggestion = {"question": None, "pairs": []}

YES_WORDS = ["yes", "y", "yeah", "yep", "yup", "yea", "sure", "ok", "okay", "correct"]
NO_WORDS = ["no", "n", "nope", "nah", "cancel"]

# --- Identity: answer "which model are you?" directly, never via the LLM
# (which otherwise hallucinates GPT-4o). ---
IDENTITY_RE = re.compile(
    r"(which|what)\b.*\b(model|ai|llm|engine)\b|who\s+are\s+you|"
    r"are\s+you\s+(a\s+)?(gpt|chatgpt|openai|gemini|bot|ai|human)",
    re.IGNORECASE,
)
IDENTITY_ANSWER = (
    "I'm the Shopping Assistant, powered by Google's Gemini AI "
    f"(model {MODEL_NAME})."
)

# --- Full-list intent: "all / every / each" (but NOT "summary") means the user
# wants every row listed, not an AI summary. ---
FULL_LIST_RE = re.compile(r"\b(all|every|each|entire|complete|full)\b", re.IGNORECASE)
SUMMARY_RE = re.compile(r"\b(summar\w+|overview|brief|highlight)\b", re.IGNORECASE)


def wants_full_list(question):
    return bool(FULL_LIST_RE.search(question)) and not SUMMARY_RE.search(question)


def classify_reply(text):
    """Return 'yes'/'no' for a short confirmation, even if slightly misspelled
    (e.g. "yesw" -> yes, "noo" -> no). Returns None for anything else."""

    t = text.lower().strip()

    if t in YES_WORDS:
        return "yes"
    if t in NO_WORDS:
        return "no"

    # only auto-correct a single short word, so real questions aren't hijacked
    if " " not in t and len(t) <= 6:
        if difflib.get_close_matches(t, YES_WORDS, n=1, cutoff=0.7):
            return "yes"
        if difflib.get_close_matches(t, NO_WORDS, n=1, cutoff=0.7):
            return "no"

    return None

PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Shopping AI Chatbot</title>
  <style>
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    :root {
      --bg: #f4f6f9;
      --panel: #ffffff;
      --border: #e5e8ec;
      --text: #1f2933;
      --muted: #6b7280;
      --accent: #2563eb;
      --accent-dark: #1d4ed8;
      --bot-bg: #f1f4f8;
    }
    body {
      font-family: 'Segoe UI', system-ui, -apple-system, Arial, sans-serif;
      margin: 0; color: var(--text); background: var(--bg);
      display: flex; flex-direction: column; height: 100vh;
    }

    .app {
      display: flex; flex-direction: column; height: 100vh;
      width: 100%; max-width: 760px; margin: 0 auto;
      background: var(--panel);
      border-left: 1px solid var(--border); border-right: 1px solid var(--border);
    }

    header {
      padding: 18px 24px; border-bottom: 1px solid var(--border);
      display: flex; align-items: center; gap: 12px; background: var(--panel);
    }
    header .logo {
      width: 38px; height: 38px; border-radius: 8px; flex: none;
      background: var(--accent); color: #fff;
      display: flex; align-items: center; justify-content: center; font-size: 18px;
    }
    header h1 { margin: 0; font-size: 17px; font-weight: 600; }
    header p  { margin: 2px 0 0; font-size: 12.5px; color: var(--muted); }

    #chat {
      flex: 1; overflow-y: auto; padding: 24px;
      display: flex; flex-direction: column; gap: 14px;
    }
    .msg {
      max-width: 76%; padding: 12px 16px; border-radius: 12px;
      line-height: 1.5; white-space: pre-wrap; font-size: 14.5px;
    }
    .user {
      align-self: flex-end; background: var(--accent); color: #fff;
      border-bottom-right-radius: 3px;
    }
    .bot  {
      align-self: flex-start; background: var(--bot-bg); color: var(--text);
      border: 1px solid var(--border); border-bottom-left-radius: 3px;
    }

    form {
      display: flex; padding: 16px 20px; gap: 10px;
      border-top: 1px solid var(--border); background: var(--panel);
    }
    input {
      flex: 1; padding: 12px 15px; border-radius: 8px;
      border: 1px solid var(--border); background: #fff; color: var(--text);
      font-size: 14.5px; outline: none;
    }
    input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37,99,235,.12); }
    button {
      padding: 12px 22px; border: none; border-radius: 8px; font-weight: 600;
      background: var(--accent); color: #fff; font-size: 14.5px; cursor: pointer;
      transition: background .15s ease;
    }
    button:hover:not(:disabled) { background: var(--accent-dark); }
    button:disabled { opacity: .55; cursor: default; }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="logo">AI</div>
      <div>
        <h1>Shopping Assistant</h1>
        <p>Ask about customers, orders and products</p>
      </div>
    </header>
    <div id="chat">
      <div class="msg bot">Hello. How can I help you? You can ask about customers, orders, or products.</div>
    </div>
    <form id="form">
      <input id="q" placeholder="e.g. What did Ravi purchase?" autocomplete="off" autofocus>
      <button id="send" type="submit">Send</button>
    </form>
  </div>

  <script>
    const chat = document.getElementById("chat");
    const form = document.getElementById("form");
    const q = document.getElementById("q");
    const send = document.getElementById("send");

    function add(text, cls) {
      const div = document.createElement("div");
      div.className = "msg " + cls;
      div.textContent = text;
      chat.appendChild(div);
      chat.scrollTop = chat.scrollHeight;
      return div;
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const question = q.value.trim();
      if (!question) return;
      add(question, "user");
      q.value = "";
      send.disabled = true;
      const thinking = add("Thinking…", "bot");

      try {
        const res = await fetch("/ask", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question })
        });
        const data = await res.json();
        thinking.textContent = data.answer || data.error || "Sorry, something went wrong.";
      } catch (err) {
        thinking.textContent = "Error: " + err.message;
      } finally {
        send.disabled = false;
        q.focus();
      }
    });
  </script>
</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(PAGE)


@app.route("/ask", methods=["POST"])
def ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()

    if not question:
        return jsonify({"error": "Please ask a question."}), 400

    try:
        # Identity questions ("which model are you?") are answered directly so
        # the LLM can't hallucinate a wrong model (e.g. GPT-4o).
        if IDENTITY_RE.search(question):
            return jsonify({"answer": IDENTITY_ANSWER})

        # If there's a pending "Did you mean X?", interpret short yes/no replies
        # (auto-correcting typos like "yesw"/"noo").
        if last_suggestion["question"]:
            reply = classify_reply(question)
            if reply == "yes":
                question = last_suggestion["question"]
                for typed, real in last_suggestion["pairs"]:
                    question = re.sub(
                        re.escape(typed), real, question, flags=re.IGNORECASE
                    )
                last_suggestion["question"] = None  # consume it
            elif reply == "no":
                last_suggestion["question"] = None
                return jsonify({"answer": "No problem! What would you like to know?"})

        collection, pipeline = generate_query(question)
        result = run_query(collection, pipeline)

        # If nothing matched, check for close (misspelled) names and ask
        # for confirmation instantly — no extra LLM call, so it's fast.
        if is_empty_result(result):
            suggestions = find_similar_names(question)
            if suggestions:
                last_suggestion.update(question=question, pairs=suggestions)

                typed_list = ", ".join(f'"{t}"' for t, _ in suggestions)
                real_list = " and ".join(f'"{r}"' for _, r in suggestions)
                answer = (
                    f"I couldn't find {typed_list}. "
                    f"Did you mean {real_list}? Reply \"yes\" and I'll look it up."
                )
                return jsonify({"answer": answer})

        # "all / every / each" (not "summary") -> list EVERY row, no AI summary.
        if wants_full_list(question) and not is_empty_result(result):
            return jsonify({"answer": format_full_list(result)})

        answer = generate_answer(question, result)
        return jsonify({"answer": answer})
    except Exception as e:
        # Log the real error for developers, but never show it to the user.
        app.logger.error("Error handling question %r: %s", question, e)
        if os.environ.get("DEBUG_ERRORS", "").lower() in ("1", "true", "yes"):
            return jsonify({"answer": f"[DEBUG] {type(e).__name__}: {e}"})
        return jsonify({
            "answer": "Sorry, I couldn't understand that. "
                      "Could you please rephrase your question?"
        })


# ===========================
# Database Viewer  (/data)
# ===========================

DATA_TABLES = ["users", "products", "orders", "order_items"]

# union of all field names seen across a collection's documents
def _collect_columns(rows):
    cols = []
    for r in rows:
        for k in r.keys():
            if k not in cols:
                cols.append(k)
    return cols

DATA_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Database Viewer</title>
  <style>
    body { font-family: 'Segoe UI', system-ui, Arial, sans-serif; margin: 0;
           background: #f4f6f9; color: #1f2933; }
    header { padding: 16px 24px; background: #2563eb; color: #fff; }
    header h1 { margin: 0; font-size: 18px; }
    .tabs { padding: 14px 24px; display: flex; gap: 8px; flex-wrap: wrap;
            border-bottom: 1px solid #e5e8ec; background: #fff; }
    .tab { padding: 8px 16px; border-radius: 8px; text-decoration: none;
           border: 1px solid #d5d9df; color: #1f2933; font-size: 14px; }
    .tab.active { background: #2563eb; color: #fff; border-color: #2563eb; }
    .wrap { padding: 20px 24px; }
    .count { margin: 0 0 12px; color: #6b7280; font-size: 14px; }
    .scroll { overflow: auto; max-height: 78vh; border: 1px solid #e5e8ec;
              border-radius: 8px; background: #fff; }
    table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
    th, td { padding: 8px 12px; border-bottom: 1px solid #eef1f4;
             text-align: left; white-space: nowrap; }
    th { position: sticky; top: 0; background: #f1f4f8; z-index: 1; }
    tr:hover td { background: #f9fbff; }
  </style>
</head>
<body>
  <header><h1>Database Viewer</h1></header>
  <div class="tabs">
    {% for t in tables %}
      <a class="tab {{ 'active' if t == current else '' }}" href="/data?table={{ t }}">{{ t }}</a>
    {% endfor %}
    <a class="tab" href="/">&larr; Back to chat</a>
  </div>
  <div class="wrap">
    <p class="count">Table <b>{{ current }}</b> — {{ rows|length }} rows</p>
    <div class="scroll">
      <table>
        <thead><tr>{% for c in cols %}<th>{{ c }}</th>{% endfor %}</tr></thead>
        <tbody>
          {% for r in rows %}
            <tr>{% for c in cols %}<td>{{ r[c] }}</td>{% endfor %}</tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


@app.route("/data")
def data_viewer():
    """A viewable HTML table of the database contents (works on Render/Vercel)."""

    current = request.args.get("table", "products")
    if current not in DATA_TABLES:
        current = "products"

    rows = find_all(current)
    cols = _collect_columns(rows)

    return render_template_string(
        DATA_PAGE, tables=DATA_TABLES, current=current, rows=rows, cols=cols
    )


if __name__ == "__main__":
    # Bind to 0.0.0.0 and the port the host assigns (Render/Railway set $PORT).
    # Without this the server only listens on localhost and the platform's
    # health check can't reach it, so the deploy times out.
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug)
