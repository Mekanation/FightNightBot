"""
Fight Night Bot for AOE4 Discord Server
----------------------------------------
Commands:
  !join          - Add yourself to the queue
  !leave         - Remove yourself from the queue
  !win @player   - Report the winner of the current game
  !queue         - Show the current queue and active tables
  !hof           - Show the all-time Hall of Fame
  !fn reset      - (Admin) Fully reset all games and queue
  !fn removetable <1|2> - (Admin) Remove a stalled table
"""

import discord
import json
import os
from discord.ext import commands
from collections import deque

# ──────────────────────────────────────────────
# Config — edit these before running
# ──────────────────────────────────────────────
BOT_PREFIX = "!"
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "YOUR_TOKEN_HERE")

# The channel where the bot posts Fight Night updates.
# Set to None to let it respond wherever commands are used.
FIGHT_NIGHT_CHANNEL_ID = None  # e.g. 1234567890

# How many people in queue triggers a second table
SECOND_TABLE_THRESHOLD = 12

# How many consecutive wins = a streak announcement + HOF entry
WIN_STREAK_TARGET = 3

# Role or user IDs allowed to use admin commands (besides server admins)
ADMIN_ROLE_NAME = "Moderator"  # set to None to disable role check

# ──────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────
HOF_FILE = "hall_of_fame.json"

def load_hof() -> dict:
    if os.path.exists(HOF_FILE):
        with open(HOF_FILE, "r") as f:
            return json.load(f)
    return {}

def save_hof(hof: dict):
    with open(HOF_FILE, "w") as f:
        json.dump(hof, f, indent=2)

# ──────────────────────────────────────────────
# Game State
# ──────────────────────────────────────────────

class Table:
    """
    Represents one active game between a champion and a challenger.
    The champion holds a win streak; the challenger is the next person from the queue.
    """
    def __init__(self, number: int, champion_id: int, challenger_id: int):
        self.number = number          # 1 or 2
        self.champion_id = champion_id
        self.challenger_id = challenger_id
        self.streak = 0               # champion's current consecutive wins (0 before first win)

    def players(self):
        return {self.champion_id, self.challenger_id}

    def __repr__(self):
        return f"Table(number={self.number}, champion={self.champion_id}, challenger={self.challenger_id}, streak={self.streak})"


# Global state — all in memory, HOF is persisted to disk
queue: deque[int] = deque()   # user IDs in order
tables: dict[int, Table] = {} # table_number -> Table (max 2 tables)

# ──────────────────────────────────────────────
# Bot setup
# ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def get_mention(user_id: int) -> str:
    return f"<@{user_id}>"

def next_free_table_number() -> int | None:
    for n in [1, 2]:
        if n not in tables:
            return n
    return None

def all_active_player_ids() -> set[int]:
    ids = set()
    for t in tables.values():
        ids.update(t.players())
    return ids

async def get_fn_channel(ctx) -> discord.TextChannel:
    """Returns the designated fight night channel, or falls back to ctx.channel."""
    if FIGHT_NIGHT_CHANNEL_ID:
        ch = bot.get_channel(FIGHT_NIGHT_CHANNEL_ID)
        return ch if ch else ctx.channel
    return ctx.channel

def is_admin(ctx) -> bool:
    if ctx.author.guild_permissions.administrator:
        return True
    if ADMIN_ROLE_NAME:
        return any(r.name == ADMIN_ROLE_NAME for r in ctx.author.roles)
    return False

def queue_embed(guild: discord.Guild) -> discord.Embed:
    """Builds a neat status embed showing tables and queue."""
    embed = discord.Embed(title="⚔️ Fight Night Status", color=0xE67E22)

    if not tables:
        embed.add_field(name="Active Games", value="No games running yet.", inline=False)
    else:
        for num, table in sorted(tables.items()):
            champ = guild.get_member(table.champion_id)
            chal = guild.get_member(table.challenger_id)
            champ_str = champ.display_name if champ else str(table.champion_id)
            chal_str = chal.display_name if chal else str(table.challenger_id)
            streak_bar = "🔥" * table.streak if table.streak > 0 else "—"
            embed.add_field(
                name=f"Table {num}",
                value=f"**Champion:** {champ_str} {streak_bar}\n**Challenger:** {chal_str}",
                inline=True
            )

    if not queue:
        embed.add_field(name="Queue", value="Empty — join with `!join`", inline=False)
    else:
        lines = []
        for i, uid in enumerate(queue, start=1):
            member = guild.get_member(uid)
            name = member.display_name if member else str(uid)
            lines.append(f"`{i}.` {name}")
        embed.add_field(name=f"Queue ({len(queue)})", value="\n".join(lines), inline=False)

    return embed

async def try_start_second_table(channel: discord.TextChannel, guild: discord.Guild):
    """If queue is large enough and table 2 is free, spin up a second game."""
    if 2 in tables:
        return  # already running
    if len(queue) < SECOND_TABLE_THRESHOLD:
        return
    if len(queue) < 2:
        return

    table_num = next_free_table_number()
    if table_num is None:
        return

    p1 = queue.popleft()
    p2 = queue.popleft()
    tables[table_num] = Table(table_num, p1, p2)

    await channel.send(
        f"📣 **Queue hit {SECOND_TABLE_THRESHOLD} players — Table {table_num} is now open!**\n"
        f"{get_mention(p1)} vs {get_mention(p2)} — good luck! 🗡️"
    )

async def advance_table(table: Table, winner_id: int, loser_id: int,
                        channel: discord.TextChannel, guild: discord.Guild):
    """
    Called after a win is reported. Updates streak, checks for 3-in-a-row,
    pulls next challenger from queue or closes the table.
    """
    hof = load_hof()
    table.streak += 1

    # ── 3-in-a-row ──────────────────────────────
    if table.streak >= WIN_STREAK_TARGET:
        winner = guild.get_member(winner_id)
        winner_name = winner.display_name if winner else str(winner_id)

        # Update HOF
        hof_key = str(winner_id)
        hof[hof_key] = {"name": winner_name, "count": hof.get(hof_key, {}).get("count", 0) + 1}
        save_hof(hof)

        await channel.send(
            f"🏆 **{get_mention(winner_id)} WON 3 IN A ROW ON TABLE {table.number}!** 🏆\n"
            f"That's {hof[hof_key]['count']} time(s) in the Hall of Fame. Absolutely dominant. 👑"
        )

        # Remove table, start fresh with next two in queue
        del tables[table.number]
        if len(queue) >= 2:
            p1 = queue.popleft()
            p2 = queue.popleft()
            tables[table.number] = Table(table.number, p1, p2)
            await channel.send(
                f"🎮 **Table {table.number} resets!** {get_mention(p1)} vs {get_mention(p2)} — you're up!"
            )
        elif len(queue) == 1:
            p1 = queue.popleft()
            # Need one more person — leave table closed but hold player
            queue.appendleft(p1)
            await channel.send(
                f"⏳ Table {table.number} needs one more player to restart. Join with `!join`!"
            )
        else:
            await channel.send(
                f"⏳ Table {table.number} is waiting for players. Join with `!join`!"
            )
        return

    # ── Normal win — champion stays, pull next challenger ──
    if queue:
        next_challenger = queue.popleft()
        table.champion_id = winner_id
        table.challenger_id = next_challenger
        streak_str = f"({table.streak} in a row 🔥)" if table.streak > 1 else ""
        await channel.send(
            f"✅ **Game over on Table {table.number}!** {get_mention(winner_id)} wins {streak_str}\n"
            f"⚔️ Next up: {get_mention(winner_id)} vs {get_mention(next_challenger)} — let's go!"
        )
    else:
        # No one in queue — pause the table
        del tables[table.number]
        await channel.send(
            f"✅ **Game over on Table {table.number}!** {get_mention(winner_id)} wins — "
            f"but the queue is empty. Someone `!join` to keep it going!"
        )

# ──────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────

@bot.command(name="join")
async def join_queue(ctx):
    """Add yourself to the Fight Night queue."""
    uid = ctx.author.id
    channel = await get_fn_channel(ctx)

    # Already playing at a table
    if uid in all_active_player_ids():
        await ctx.message.add_reaction("❌")
        await channel.send(f"{ctx.author.mention} you're already in an active game!")
        return

    # Already in queue
    if uid in queue:
        await ctx.message.add_reaction("❌")
        await channel.send(f"{ctx.author.mention} you're already in the queue.")
        return

    queue.append(uid)
    position = list(queue).index(uid) + 1

    # If no tables exist yet and we have 2 players, start table 1
    if not tables and len(queue) >= 2:
        p1 = queue.popleft()
        p2 = queue.popleft()
        tables[1] = Table(1, p1, p2)
        await channel.send(
            f"⚔️ **Fight Night is starting!** {get_mention(p1)} vs {get_mention(p2)} — Table 1 is live!"
        )
    else:
        await channel.send(
            f"✅ {ctx.author.mention} joined the queue at position **#{position}**."
        )
        # Check if second table should open
        await try_start_second_table(channel, ctx.guild)


@bot.command(name="leave")
async def leave_queue(ctx):
    """Remove yourself from the queue."""
    uid = ctx.author.id
    channel = await get_fn_channel(ctx)

    if uid in all_active_player_ids():
        await channel.send(
            f"{ctx.author.mention} you're in an active game — use `!win` to report the result first."
        )
        return

    if uid not in queue:
        await channel.send(f"{ctx.author.mention} you're not in the queue.")
        return

    queue.remove(uid)
    await channel.send(f"👋 {ctx.author.mention} has left the queue.")


@bot.command(name="win")
async def report_win(ctx, winner: discord.Member = None):
    """
    Report the winner of a game. Mention the winner: !win @player
    Can be called by either player at the table.
    """
    channel = await get_fn_channel(ctx)

    if winner is None:
        await channel.send(f"❓ Please mention the winner, e.g. `!win @player`")
        return

    caller_id = ctx.author.id
    winner_id = winner.id

    # Find the table this caller belongs to
    caller_table = None
    for t in tables.values():
        if caller_id in t.players():
            caller_table = t
            break

    if caller_table is None:
        await channel.send(f"{ctx.author.mention} you're not in an active game.")
        return

    if winner_id not in caller_table.players():
        await channel.send(
            f"❌ {winner.mention} isn't playing on your table. "
            f"Only {get_mention(caller_table.champion_id)} and {get_mention(caller_table.challenger_id)} are."
        )
        return

    loser_id = (caller_table.players() - {winner_id}).pop()
    await advance_table(caller_table, winner_id, loser_id, channel, ctx.guild)

    # After resolving, check if second table should open
    await try_start_second_table(channel, ctx.guild)


@bot.command(name="queue")
async def show_queue(ctx):
    """Show the current queue and active tables."""
    channel = await get_fn_channel(ctx)
    await channel.send(embed=queue_embed(ctx.guild))


@bot.command(name="hof")
async def hall_of_fame(ctx):
    """Show the all-time Hall of Fame for 3-in-a-row wins."""
    channel = await get_fn_channel(ctx)
    hof = load_hof()

    if not hof:
        await channel.send("🏆 The Hall of Fame is empty — be the first to win 3 in a row!")
        return

    sorted_hof = sorted(hof.values(), key=lambda x: x["count"], reverse=True)
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, entry in enumerate(sorted_hof):
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        times = "time" if entry["count"] == 1 else "times"
        lines.append(f"{medal} **{entry['name']}** — {entry['count']} {times}")

    embed = discord.Embed(
        title="🏆 Fight Night Hall of Fame",
        description="\n".join(lines),
        color=0xF1C40F
    )
    embed.set_footer(text="Awarded for winning 3 games in a row")
    await channel.send(embed=embed)


@bot.command(name="fn")
async def fn_admin(ctx, subcommand: str = None, *args):
    """Admin commands: !fn reset | !fn removetable <1|2>"""
    channel = await get_fn_channel(ctx)

    if not is_admin(ctx):
        await channel.send("❌ You don't have permission to use admin commands.")
        return

    if subcommand == "reset":
        queue.clear()
        tables.clear()
        await channel.send("🔄 Fight Night has been fully reset. Queue and tables cleared.")

    elif subcommand == "removetable":
        if not args or not args[0].isdigit():
            await channel.send("Usage: `!fn removetable <1|2>`")
            return
        num = int(args[0])
        if num not in tables:
            await channel.send(f"Table {num} doesn't exist.")
            return
        del tables[num]
        await channel.send(f"🗑️ Table {num} has been removed.")

    else:
        await channel.send(
            "**Admin commands:**\n"
            "`!fn reset` — clear all tables and queue\n"
            "`!fn removetable <1|2>` — remove a stalled table"
        )


@bot.command(name="help")
async def help_cmd(ctx):
    """Show all Fight Night commands."""
    embed = discord.Embed(title="⚔️ Fight Night Bot Commands", color=0x3498DB)
    embed.add_field(name="`!join`", value="Add yourself to the queue", inline=False)
    embed.add_field(name="`!leave`", value="Remove yourself from the queue", inline=False)
    embed.add_field(name="`!win @player`", value="Report the winner of your current game", inline=False)
    embed.add_field(name="`!queue`", value="Show active tables and the queue", inline=False)
    embed.add_field(name="`!hof`", value="Show the all-time Hall of Fame", inline=False)
    embed.add_field(name="`!fn reset` *(admin)*", value="Reset all tables and queue", inline=False)
    embed.add_field(name="`!fn removetable <1|2>` *(admin)*", value="Remove a stalled table", inline=False)
    await ctx.send(embed=embed)


# ──────────────────────────────────────────────
# Events
# ──────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Fight Night Bot is online as {bot.user}")
    print(f"   Prefix: {BOT_PREFIX}")
    print(f"   Second table threshold: {SECOND_TABLE_THRESHOLD} players")
    print(f"   Win streak target: {WIN_STREAK_TARGET}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Couldn't find that member. Make sure to @mention them.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument. Try `!help` for usage.")
    else:
        print(f"Error: {error}")

# ──────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
