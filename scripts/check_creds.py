"""Verify that the credentials in .env work. Prints no secret values."""
import json
import os
import urllib.error
import urllib.request

import anthropic
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
tok = os.environ["DISCORD_TOKEN"]
g = os.environ["DISCORD_GUILD_ID"]
c = os.environ["DISCORD_CHANNEL_ID"]


def get(path):
    req = urllib.request.Request(
        "https://discord.com/api/v10" + path,
        headers={"Authorization": "Bot " + tok, "User-Agent": "DiscordBot (https://example.com, 0.1)"},
    )
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        return {"HTTP": e.code, "body": e.read().decode()[:200]}


me = get("/users/@me")
print("bot:", me.get("username"), me.get("id"), me.get("HTTP"))
gd = get(f"/guilds/{g}")
print("guild:", gd.get("name"), gd.get("HTTP"), gd.get("body"))
ch = get(f"/channels/{c}")
print("channel:", ch.get("name"), "| in guild:", ch.get("guild_id") == g, ch.get("HTTP"), ch.get("body"))
cm = get(f"/applications/{me.get('id')}/guilds/{g}/commands")
print("slash command access:", cm if isinstance(cm, dict) else f"ok ({len(cm)} existing)")
try:
    r = anthropic.Anthropic().messages.create(
        model="claude-opus-5-5", max_tokens=200, messages=[{"role": "user", "content": "Reply with: ok"}]
    )
    print("anthropic:", r.stop_reason, [b.text for b in r.content if b.type == "text"])
except Exception as e:  # noqa: BLE001 - diagnostic script
    print("anthropic error:", type(e).__name__, str(e)[:300])
print("gmail app password set:", bool(os.environ.get("GMAIL_APP_PASSWORD")))
print("service account file set:", bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")))
