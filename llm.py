import json

from google import genai
from config import GEMINI_API_KEY
from database import get_schema

client = genai.Client(api_key=GEMINI_API_KEY)

MODEL_NAME = "gemini-3.1-flash-lite"


def generate_query(question):
    """Ask the LLM for a MongoDB read query as JSON:
        {"collection": "<name>", "pipeline": [ ...aggregation stages... ]}
    Returns (collection, pipeline).
    """

    schema = get_schema()

    prompt = f"""
You are a MongoDB expert. Convert the user's question into a MongoDB read query.

Return ONLY valid JSON in EXACTLY this shape (no explanation, no markdown):
{{"collection": "<collection_name>", "pipeline": [ <aggregation stages> ]}}

Rules:
- Use ONLY read stages: $match, $group, $project, $sort, $limit, $count.
- The `order_items` collection is DENORMALIZED — each document already contains
  user_name, city, product_name, brand, category, price, quantity, order_date
  and line_total. So most "who bought / spent / purchased" questions need ONLY
  the order_items collection (NO joins).
- For matching a person or product name, ALWAYS use a case-insensitive regex:
  {{"user_name": {{"$regex": "^Ravi$", "$options": "i"}}}}
- To total spending use line_total, e.g.
  {{"$group": {{"_id": null, "total_spent": {{"$sum": "$line_total"}}}}}}
- Exclude _id from plain lists with a $project like {{"$project": {{"_id": 0}}}}.

Collections and their fields:
{schema}

Examples:
Q: What did Ravi purchase?
{{"collection": "order_items", "pipeline": [{{"$match": {{"user_name": {{"$regex": "^Ravi$", "$options": "i"}}}}}}, {{"$project": {{"_id": 0, "product_name": 1, "quantity": 1}}}}]}}

Q: How much did Ravi spend in total?
{{"collection": "order_items", "pipeline": [{{"$match": {{"user_name": {{"$regex": "^Ravi$", "$options": "i"}}}}}}, {{"$group": {{"_id": null, "total_spent": {{"$sum": "$line_total"}}}}}}]}}

Q: List all products
{{"collection": "products", "pipeline": [{{"$project": {{"_id": 0}}}}]}}

Question: {question}
"""

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
    )

    text = response.text.strip()

    # Strip markdown code fences (```json ... ```) the model may add
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]

    data = json.loads(text.strip())
    return data["collection"], data.get("pipeline", [])


def generate_answer(question, result, suggestions=None):
    """Turn a raw database result into a friendly natural-language answer."""

    suggestion_text = ""
    if not result and suggestions:
        pairs = "; ".join(f'"{typed}" -> "{real}"' for typed, real in suggestions)
        suggestion_text = f"""
No records matched. However, these words in the question look like
misspelled names that DO exist in the store: {pairs}.
Gently point this out and ASK the user to confirm, e.g.
"I couldn't find 'Rahu'. Did you mean 'Rahul'?"
"""

    # A big raw dump of rows overwhelms the model and makes it hallucinate
    # "no information found" even when rows exist. Cap how many rows we send
    # and tell the model the true total so it can summarise instead.
    MAX_ROWS = 40
    total = len(result) if result else 0
    shown = result[:MAX_ROWS] if result else result

    truncated_note = ""
    if total > MAX_ROWS:
        truncated_note = (
            f"\n(Only the first {MAX_ROWS} of {total} matching rows are shown "
            f"above; summarise them and mention there are {total} in total.)"
        )

    prompt = f"""
You are a helpful shopping assistant chatbot.

The user asked:
{question}

Here is the data retrieved for this question ({total} matching row(s)):
{shown}{truncated_note}
{suggestion_text}
IMPORTANT: If the data above is non-empty, it DOES contain the matching
records for the question — use it to write the answer, summarising when the
list is long. Only say that no matching information was found when the data
is genuinely empty (0 rows) and no suggestions are given.

Write a short, natural-language answer to the user's question based only on
this data. Do not mention databases, queries, or technical details.

Do NOT begin with a greeting (no "Hi", "Hello", "Hey") — the conversation is
already in progress, so answer directly.

If the user asks which AI, model, or engine powers you, say you are powered by
Google's Gemini AI, model {MODEL_NAME}. Never claim to be GPT, GPT-4o, ChatGPT,
or made by OpenAI.
"""

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt
    )

    return response.text.strip()


def answer_from_profile(question, profile_text):
    """Answer a question using ONLY the stored profile/bio text."""

    prompt = f"""
Answer the user's question using ONLY the profile text below. Be concise,
factual, and friendly. If the answer is not in the profile, say you don't have
that information. Do not begin with a greeting.

Profile:
{profile_text}

Question: {question}
"""

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
    )

    return response.text.strip()


def format_full_list(result):
    """Return EVERY row as a plain numbered list (no AI summary).

    Used when the user explicitly asks for "all"/"every" records, so nothing
    is condensed. Works for any query — columns are taken from the result.
    """

    if not result:
        return "No matching records were found."

    total = len(result)
    cols = list(result[0].keys())

    lines = [f"Showing all {total} result(s):", " | ".join(cols)]
    for i, row in enumerate(result, 1):
        lines.append(f"{i}. " + " | ".join(str(row[c]) for c in cols))

    return "\n".join(lines)