import difflib
import os
import re

from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

# Load variables from a local .env file when present. On hosting platforms
# (Render, Vercel, etc.) the variables come from the dashboard's environment
# settings instead, and load_dotenv() simply does nothing there.
load_dotenv()

# Ordinary question words that should never be treated as (misspelled) names.
COMMON_WORDS = {
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "did", "does", "do", "has", "have", "had", "was", "were", "are", "is",
    "the", "and", "for", "from", "with", "about", "into", "that", "this",
    "purchase", "purchased", "purchases", "buy", "bought", "order", "orders",
    "ordered", "price", "prices", "cost", "costs", "product", "products",
    "item", "items", "money", "much", "many", "total", "amount", "spend",
    "spent", "get", "got", "show", "list", "all", "any", "name", "names",
    "customer", "customers", "user", "users", "quantity", "number",
}

# ===========================
# Database Connection (MongoDB)
# ===========================

MONGO_URL = (
    os.getenv("mongoDB_URL")
    or os.getenv("MONGODB_URL")
    or os.getenv("MONGO_URL")
)
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "shopping_ai")

# The collections this app knows about.
COLLECTIONS = ["users", "products", "orders", "order_items"]

# Connect lazily (on first query) rather than at import time, so a bad/unset
# URL doesn't crash the whole app on startup.
_client = None


def get_db():
    """Return the MongoDB database handle, connecting on first use."""

    global _client

    if _client is None:
        if not MONGO_URL:
            raise RuntimeError(
                "MongoDB URL is not set. Add mongoDB_URL to the environment "
                "(the Atlas connection string)."
            )
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=10000)

    return _client[MONGO_DB_NAME]


def _clean(docs):
    """Drop the noisy ObjectId `_id` from result docs (keep meaningful
    $group _id values, which are not ObjectIds)."""

    for d in docs:
        if isinstance(d.get("_id"), ObjectId):
            d.pop("_id", None)
    return docs


# ===========================
# Describe the data (for the LLM)
# ===========================

def get_schema():
    """Describe each collection and its fields from a sample document,
    so the LLM knows what it can query."""

    db = get_db()
    schema = ""

    for coll in COLLECTIONS:
        doc = db[coll].find_one()
        schema += f"\nCollection: {coll}\n"
        if doc:
            for key, value in doc.items():
                if key == "_id":
                    continue
                schema += f"- {key} ({type(value).__name__})\n"

    return schema


# ===========================
# Run a MongoDB aggregation query
# ===========================

def run_query(collection, pipeline):
    """Execute an aggregation pipeline against a collection and return the
    result documents (as plain dicts)."""

    if collection not in COLLECTIONS:
        raise ValueError(f"Unknown collection: {collection}")

    db = get_db()
    docs = list(db[collection].aggregate(pipeline or []))
    return _clean(docs)


def find_all(collection):
    """Return every document in a collection (used by the /data viewer)."""

    if collection not in COLLECTIONS:
        raise ValueError(f"Unknown collection: {collection}")

    db = get_db()
    return list(db[collection].find({}, {"_id": 0}))


# ===========================
# Name lists (for "did you mean?")
# ===========================

def get_customer_names():
    db = get_db()
    return [d["name"] for d in db.users.find({}, {"name": 1, "_id": 0}) if d.get("name")]


def get_product_names():
    db = get_db()
    return [
        d["product_name"]
        for d in db.products.find({}, {"product_name": 1, "_id": 0})
        if d.get("product_name")
    ]


# ===========================
# Empty-result check
# ===========================

def is_empty_result(result):
    """
    True if the query returned nothing meaningful.

    An aggregate like $sum/$count still returns ONE doc even when no rows
    match (e.g. [{'total': None}]), so a plain `not result` check misses it.
    Treat a result as empty when every value is None / 0 / "".
    """

    if not result:
        return True

    for row in result:
        for value in row.values():
            if value not in (None, 0, 0.0, ""):
                return False

    return True


# ===========================
# Fuzzy "Did you mean?" Suggestions
# ===========================

def find_similar_names(question):
    """
    Look at each word in the user's question and, if it looks like a
    misspelled customer or product name, return close real matches.

    Returns a list of (typed_word, suggested_name) tuples.
    """

    known = get_customer_names() + get_product_names()

    # map lowercase -> original spelling so matching is case-insensitive
    lookup = {name.lower(): name for name in known}
    lower_names = list(lookup.keys())

    # split the question into candidate words (letters only, 3+ chars)
    words = re.findall(r"[A-Za-z]{3,}", question)

    suggestions = []
    seen = set()

    for word in words:

        w = word.lower()

        # skip ordinary question words so e.g. "price" isn't matched to "Priya"
        if w in COMMON_WORDS:
            continue

        # skip words that already exactly match a known name
        if w in lookup:
            continue

        # 0.75 cutoff: real typos (sneh->sneha, ravii->ravi) score ~0.88,
        # while unrelated words (price->priya ~0.6) stay below the bar.
        matches = difflib.get_close_matches(w, lower_names, n=1, cutoff=0.75)

        if matches and matches[0] not in seen:
            suggestions.append((word, lookup[matches[0]]))
            seen.add(matches[0])

    return suggestions
