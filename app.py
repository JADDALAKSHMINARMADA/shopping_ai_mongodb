import os
import re
import json
import base64
import difflib
from flask import Flask, request, jsonify, render_template_string, send_file
from llm import (
    generate_query, generate_answer, format_full_list, answer_from_profile,
    MODEL_NAME,
)
from database import (
    run_query, find_all, find_similar_names, is_empty_result, get_profile,
)
from export_utils import rows_to_excel, rows_to_pdf

app = Flask(__name__)

# When a result has MORE than this many rows, don't dump it as text — offer
# Excel/PDF downloads instead (and refuse a plain-text version).
EXPORT_THRESHOLD = 15

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

# --- Profile Q&A: questions about the developer's bio are answered from the
# stored profile text, not the shopping database. ---
PROFILE_RE = re.compile(
    r"\b(narmada|jadda|lakshmi|resume|bio|cgpa|internship|certification|"
    r"skills|projects?|education|qualification|developer|"
    r"who\s+(made|created|built|developed|designed))\b",
    re.IGNORECASE,
)

# --- Full-list intent: "all / every / each" (but NOT "summary") means the user
# wants every row listed, not an AI summary. ---
FULL_LIST_RE = re.compile(r"\b(all|every|each|entire|complete|full)\b", re.IGNORECASE)
SUMMARY_RE = re.compile(r"\b(summar\w+|overview|brief|highlight)\b", re.IGNORECASE)

# The user explicitly wants a text answer ("in text", "not pdf", ...).
TEXT_REQUEST_RE = re.compile(
    r"\b(in text|as text|text version|text only|plain text|not\s+(a\s+)?(pdf|excel))\b",
    re.IGNORECASE,
)

# "give me all raw data / everything" -> export the full denormalized dataset.
RAW_DATA_RE = re.compile(
    r"\b(raw data|all data|full data|everything|entire data|complete data|all raw)\b",
    re.IGNORECASE,
)

# Format words ("in text", "as pdf", "download"...) are about HOW to answer, not
# WHAT to fetch — strip them before asking the LLM for a query so they don't
# pollute the search.
FORMAT_NOISE_RE = re.compile(
    r"\b(in|as)\s+(text|pdf|excel|xlsx|a\s+file)\b|"
    r"\bnot\s+(a\s+)?(pdf|excel|text)\b|"
    r"\b(text|pdf|excel)\s+version\b|\bplain text\b|\btext only\b|"
    r"\bdownload(ed|able)?\b|\bexport\b",
    re.IGNORECASE,
)


def clean_for_query(question):
    """Remove format words so the DB query reflects only what to fetch."""

    cleaned = FORMAT_NOISE_RE.sub(" ", question)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,")
    return cleaned or question


def wants_full_list(question):
    return bool(FULL_LIST_RE.search(question)) and not SUMMARY_RE.search(question)


def encode_query(collection, pipeline):
    """Pack a (collection, pipeline) into a URL-safe token for the /export link,
    so the download re-runs the exact same query (no extra LLM call)."""

    raw = json.dumps({"collection": collection, "pipeline": pipeline}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_query(token):
    raw = base64.urlsafe_b64decode(token.encode())
    data = json.loads(raw)
    return data["collection"], data.get("pipeline", [])


def download_links(collection, pipeline):
    """The Excel + PDF download buttons for a given query."""

    token = encode_query(collection, pipeline)
    return [
        {"label": "⬇ Excel (.xlsx)", "url": f"/export?fmt=xlsx&q={token}"},
        {"label": "⬇ PDF", "url": f"/export?fmt=pdf&q={token}"},
    ]


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
      --panel: #ffffff;
      --border: #e6e9ef;
      --text: #111827;
      --muted: #6b7280;
      --accent: #4f46e5;
      --accent-dark: #4338ca;
      --bot-bg: #f5f7fb;
      --shadow: 0 24px 60px -12px rgba(15, 23, 42, .28);
    }
    body {
      font-family: 'Segoe UI', system-ui, -apple-system, Arial, sans-serif;
      margin: 0; color: var(--text);
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh; padding: 28px 20px;
      background: #eef1f8;
      background-image:
        radial-gradient(900px 500px at 12% 0%, rgba(99,102,241,.30), transparent 60%),
        radial-gradient(800px 500px at 88% 100%, rgba(14,165,233,.26), transparent 60%),
        linear-gradient(135deg, #f6f8fd 0%, #e8edf9 100%);
      background-attachment: fixed;
    }

    .app {
      display: flex; flex-direction: column;
      height: min(880px, calc(100vh - 56px));
      width: 100%; max-width: 820px;
      background: var(--panel);
      border: 1px solid rgba(255,255,255,.7);
      border-radius: 20px; box-shadow: var(--shadow); overflow: hidden;
    }

    header {
      padding: 18px 24px; border-bottom: 1px solid var(--border);
      display: flex; align-items: center; gap: 14px;
      background: linear-gradient(135deg, #4f46e5 0%, #6366f1 55%, #0ea5e9 100%);
      color: #fff;
    }
    header .logo {
      width: 42px; height: 42px; border-radius: 12px; flex: none;
      background: rgba(255,255,255,.18); color: #fff;
      border: 1px solid rgba(255,255,255,.3);
      display: flex; align-items: center; justify-content: center;
      font-size: 16px; font-weight: 700; letter-spacing: .5px;
    }
    header h1 { margin: 0; font-size: 17px; font-weight: 600; }
    header p  { margin: 3px 0 0; font-size: 12.5px; color: rgba(255,255,255,.82); }
    header .status {
      margin-left: auto; display: flex; align-items: center; gap: 7px;
      font-size: 12px; color: rgba(255,255,255,.9);
      background: rgba(255,255,255,.15); border: 1px solid rgba(255,255,255,.25);
      padding: 6px 12px; border-radius: 999px;
    }
    header .dot {
      width: 7px; height: 7px; border-radius: 50%; background: #4ade80;
      box-shadow: 0 0 0 0 rgba(74,222,128,.7); animation: pulse 2s infinite;
    }
    @keyframes pulse {
      70%  { box-shadow: 0 0 0 7px rgba(74,222,128,0); }
      100% { box-shadow: 0 0 0 0 rgba(74,222,128,0); }
    }

    #chat {
      flex: 1; overflow-y: auto; padding: 26px 24px;
      display: flex; flex-direction: column; gap: 16px;
      background:
        radial-gradient(circle at 1px 1px, rgba(79,70,229,.09) 1px, transparent 0) 0 0 / 22px 22px,
        linear-gradient(180deg, #fbfcfe 0%, #f7f9fc 100%);
      scrollbar-width: thin; scrollbar-color: #cbd5e1 transparent;
    }
    #chat::-webkit-scrollbar { width: 8px; }
    #chat::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 8px; }

    .msg {
      max-width: 76%; padding: 12px 16px; border-radius: 16px;
      line-height: 1.55; white-space: pre-wrap; font-size: 14.5px;
      animation: rise .22s ease-out;
    }
    @keyframes rise {
      from { opacity: 0; transform: translateY(6px); }
      to   { opacity: 1; transform: none; }
    }
    .user {
      align-self: flex-end; color: #fff;
      background: linear-gradient(135deg, var(--accent) 0%, #6366f1 100%);
      border-bottom-right-radius: 4px;
      box-shadow: 0 6px 16px -6px rgba(79,70,229,.55);
    }
    .bot  {
      align-self: flex-start; background: var(--panel); color: var(--text);
      border: 1px solid var(--border); border-bottom-left-radius: 4px;
      box-shadow: 0 2px 10px -4px rgba(15,23,42,.14);
    }

    form {
      display: flex; padding: 16px 20px; gap: 10px;
      border-top: 1px solid var(--border); background: var(--panel);
    }
    input {
      flex: 1; padding: 13px 16px; border-radius: 12px;
      border: 1px solid var(--border); background: #f8fafc; color: var(--text);
      font-size: 14.5px; outline: none;
      transition: border-color .15s ease, box-shadow .15s ease, background .15s ease;
    }
    input:focus {
      border-color: var(--accent); background: #fff;
      box-shadow: 0 0 0 4px rgba(79,70,229,.13);
    }
    button {
      padding: 13px 24px; border: none; border-radius: 12px; font-weight: 600;
      background: linear-gradient(135deg, var(--accent) 0%, #6366f1 100%);
      color: #fff; font-size: 14.5px; cursor: pointer;
      box-shadow: 0 8px 18px -8px rgba(79,70,229,.7);
      transition: transform .12s ease, box-shadow .15s ease, opacity .15s ease;
    }
    button:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 12px 22px -8px rgba(79,70,229,.8); }
    button:active:not(:disabled) { transform: translateY(0); }
    button:disabled { opacity: .5; cursor: default; box-shadow: none; }

    .downloads { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
    .dl {
      display: inline-block; padding: 9px 15px; border-radius: 10px;
      background: #eef2ff; color: var(--accent-dark); border: 1px solid #c7d2fe;
      font-size: 13.5px; font-weight: 600; text-decoration: none;
      transition: background .15s ease, transform .12s ease;
    }
    .dl:hover { background: #e0e7ff; transform: translateY(-1px); }

    @media (max-width: 640px) {
      body { padding: 0; }
      .app { height: 100vh; border-radius: 0; border: none; max-width: 100%; }
      .msg { max-width: 88%; }
      header .status { display: none; }
    }
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
      <div class="status"><span class="dot"></span>Online</div>
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

        // If the server offered file downloads (large result), show buttons.
        if (data.downloads && data.downloads.length) {
          const box = document.createElement("div");
          box.className = "downloads";
          data.downloads.forEach(d => {
            const a = document.createElement("a");
            a.className = "dl";
            a.href = d.url;
            a.textContent = d.label;
            a.setAttribute("download", "");
            box.appendChild(a);
          });
          thinking.appendChild(box);
        }
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

        # Questions about the developer's profile/bio are answered from the
        # stored profile text, not the shopping database.
        if PROFILE_RE.search(question):
            profile = get_profile()
            if profile:
                return jsonify({"answer": answer_from_profile(question, profile)})

        # If there's a pending "Did you mean X?", interpret short yes/no replies
        # (auto-correcting typos like "yesw"/"noo").
        just_corrected = False
        if last_suggestion["question"]:
            reply = classify_reply(question)
            if reply == "yes":
                question = last_suggestion["question"]
                for typed, real in last_suggestion["pairs"]:
                    question = re.sub(
                        re.escape(typed), real, question, flags=re.IGNORECASE
                    )
                last_suggestion["question"] = None  # consume it
                just_corrected = True
            elif reply == "no":
                last_suggestion["question"] = None
                return jsonify({"answer": "No problem! What would you like to know?"})

        # A bare "yes"/"no" with nothing pending is not a question. Passing it to
        # the LLM turns it into a query that matches everything and dumps the
        # whole database back at the user.
        if not just_corrected and classify_reply(question):
            return jsonify({
                "answer": "There's nothing for me to confirm right now — "
                          "what would you like to know?"
            })

        # "give me all raw data / everything" -> the full denormalized dataset
        # (users, products, quantity, price, city, order_date...). Skip the LLM.
        if RAW_DATA_RE.search(question):
            collection, pipeline = "order_items", [{"$project": {"_id": 0}}]
        else:
            # Strip format words ("in text", "as pdf") so they don't skew the query.
            collection, pipeline = generate_query(clean_for_query(question))

        result = run_query(collection, pipeline)

        # If nothing matched, check for close (misspelled) names and ask
        # for confirmation instantly — no extra LLM call, so it's fast.
        if is_empty_result(result):
            # Never suggest again right after applying a correction: the name is
            # already the real one, so re-suggesting it would loop on "yes".
            suggestions = [] if just_corrected else find_similar_names(question)

            if just_corrected:
                return jsonify({
                    "answer": "I looked that up, but there are no records for it. "
                              "Try another customer or product name."
                })

            if suggestions:
                last_suggestion.update(question=question, pairs=suggestions)

                typed_list = ", ".join(f'"{t}"' for t, _ in suggestions)
                real_list = " and ".join(f'"{r}"' for _, r in suggestions)
                answer = (
                    f"I couldn't find {typed_list}. "
                    f"Did you mean {real_list}? Reply \"yes\" and I'll look it up."
                )
                return jsonify({"answer": answer})

        # A "summary" request stays as short text even for big results.
        if SUMMARY_RE.search(question):
            return jsonify({"answer": generate_answer(question, result)})

        # Large result (> 15 rows): don't dump as text — offer Excel/PDF only.
        n = len(result)
        if n > EXPORT_THRESHOLD:
            links = download_links(collection, pipeline)
            if TEXT_REQUEST_RE.search(question):
                answer = (
                    f"I'm sorry — there are {n} records, which is too many to "
                    f"show as text. Please download them as Excel or PDF below."
                )
            else:
                answer = (
                    f"I found {n} records — too many to display here. "
                    f"Download the full list as Excel or PDF:"
                )
            return jsonify({"answer": answer, "downloads": links})

        # Small result: "all/every" -> list each row; otherwise a friendly answer.
        if wants_full_list(question):
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
# Export  (/export)  — Excel / PDF download
# ===========================

# union of all field names seen across a collection's documents
def _collect_columns(rows):
    cols = []
    for r in rows:
        for k in r.keys():
            if k not in cols:
                cols.append(k)
    return cols


@app.route("/export")
def export():
    """Stream the result of a saved query as an .xlsx or .pdf download."""

    token = request.args.get("q", "")
    fmt = request.args.get("fmt", "xlsx").lower()

    try:
        collection, pipeline = decode_query(token)
        rows = run_query(collection, pipeline)
    except Exception as e:
        app.logger.error("Export failed for token %r: %s", token, e)
        return "Invalid or expired download link.", 400

    cols = _collect_columns(rows)

    if fmt == "pdf":
        buf = rows_to_pdf(rows, cols, title=collection)
        return send_file(
            buf, mimetype="application/pdf",
            as_attachment=True, download_name=f"{collection}.pdf",
        )

    buf = rows_to_excel(rows, cols)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=f"{collection}.xlsx",
    )


# ===========================
# Database Viewer  (/data)
# ===========================

DATA_TABLES = ["users", "products", "orders", "order_items"]

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
