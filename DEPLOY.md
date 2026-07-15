# Deployment Guide (MongoDB Atlas + Render / Vercel)

The app stores its data in **MongoDB Atlas** and reads all connection settings
from environment variables, so deploying just means pointing the app at the
Atlas cluster. No code changes are needed.

---

## 1. MongoDB Atlas

- Data lives in an Atlas cluster, database **`shopping_ai`**, collections:
  `users`, `products`, `orders`, `order_items` (the last is denormalized so
  natural-language questions need no joins).
- **Network Access:** add `0.0.0.0/0` (Allow Access From Anywhere) so Render
  and Vercel can connect. Without this, cloud deploys time out.

## 2. Environment variables

Set these in **both** Render and Vercel dashboards:

| Key | Value |
|-----|-------|
| `mongoDB_URL` | your Atlas connection string (`mongodb+srv://...`) |
| `GEMINI_API_KEY` | your Google Gemini API key |
| `MONGO_DB_NAME` | *(optional)* database name, defaults to `shopping_ai` |

## 3. Render

- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT`
- Add the environment variables above, then deploy.

## 4. Vercel

- `vercel.json` configures the Python serverless build automatically.
- Add the environment variables above in Project Settings, then deploy.

---

## Viewing the data

- Live: open **`/data`** on the app (tabs for each collection).
- Locally: run `python app.py` and browse `http://127.0.0.1:5000/data`.
- Atlas UI: cloud.mongodb.com -> Browse Collections.

## Error -> fix reference

| Symptom | Cause / Fix |
|---------|-------------|
| "Sorry, I couldn't understand that" | Set `DEBUG_ERRORS=true` to see the real error |
| Connection timeout | Atlas Network Access missing `0.0.0.0/0`, or bad `mongoDB_URL` |
| "MongoDB URL is not set" | `mongoDB_URL` env var not set on that platform |
| No open ports / deploy timed out (Render) | Start command must be the gunicorn line above |

## Security note

Rotate the `GEMINI_API_KEY` and the MongoDB password if they have been exposed
(logs, chats, screenshots). Never commit real secrets — set them in the
platform dashboards. `.env` is gitignored.
