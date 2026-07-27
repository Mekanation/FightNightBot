"""
Fight Night Bot for AOE4 Discord Server
----------------------------------------
Commands:
  !join          - Add yourself to the queue
  !leave         - Remove yourself from the queue
  !win @player   - Report the winner of the current game
  !queue         - Show the current queue and active tables
  !hof           - Show the all-time Hall of Fame
  !reportwin @winner @loser - Report a custom game result (civ picked via dropdown, needs confirmation)
  !leaderboard   - Show all-time win/loss rankings
  !elo [@player] - Show a player's rough hidden elo (yourself by default)
  !customs       - Ping for a custom game, showing your rough hidden elo
  !fn reset      - (Admin) Fully reset all games and queue
  !fn removetable <1|2> - (Admin) Remove a stalled table
  !fn resetstats - (Admin) Wipe all win/loss records and elo
"""

import discord
import json
import os
import asyncio
from discord.ext import commands
from collections import deque

# ──────────────────────────────────────────────
# Config — edit these before running
# ──────────────────────────────────────────────
BOT_PREFIX = "!"
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# The channel where the bot posts Fight Night updates.
# Set to None to let it respond wherever commands are used.
FIGHT_NIGHT_CHANNEL_ID = 1478507531243487253  # e.g. 1234567890

# How many people in queue triggers a second table
SECOND_TABLE_THRESHOLD = 12

# How many consecutive wins = a streak announcement + HOF entry
WIN_STREAK_TARGET = 3

# Role or user IDs allowed to use admin commands (besides server admins)
ADMIN_ROLE_NAME = "Lord Mod"  # set to None to disable role check

# Starting elo for players with no recorded games, and the K-factor used
# when adjusting elo after each confirmed !reportwin result.
DEFAULT_ELO = 1000
ELO_K_FACTOR = 32

# How long a !reportwin confirmation request waits for the other player to react
REPORT_CONFIRM_TIMEOUT = 300  # seconds

# All currently released AOE4 civs (base game + free updates + expansions), used to
# populate the !reportwin civ dropdowns. Update this list when new civs release —
# Vikings and Scots (Raiders of the North) are not yet out and are intentionally omitted.
CIV_LIST = [
    "Abbasid Dynasty", "Ayyubids", "Byzantines", "Chinese", "Delhi Sultanate",
    "English", "French", "Golden Horde", "Holy Roman Empire", "House of Lancaster",
    "Japanese", "Jeanne d'Arc", "Jin Dynasty", "Knights Templar", "Macedonian Dynasty",
    "Malians", "Mongols", "Order of the Dragon", "Ottomans", "Rus",
    "Sengoku Daimyo", "Tughlaq Dynasty", "Zhu Xi's Legacy",
]

# ──────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────
HOF_FILE = "hall_of_fame.json"
STATS_FILE = "player_stats.json"

def load_hof() -> dict:
    if os.path.exists(HOF_FILE):
        with open(HOF_FILE, "r") as f:
            return json.load(f)
    return {}

def save_hof(hof: dict):
    with open(HOF_FILE, "w") as f:
        json.dump(hof, f, indent=2)

def load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r") as f:
            return json.load(f)
    return {"players": {}, "matches": []}

def save_stats(stats: dict):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)

def get_player_record(stats: dict, user_id: int) -> dict:
    """Returns the stats entry for a player, creating a default one if needed."""
    key = str(user_id)
    if key not in stats["players"]:
        stats["players"][key] = {"wins": 0, "losses": 0, "elo": DEFAULT_ELO}
    return stats["players"][key]

def calc_new_elo(winner_elo: float, loser_elo: float) -> tuple[float, float]:
    """Standard elo update: winner and loser move toward the result they 'should' have had."""
    expected_winner = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    new_winner_elo = winner_elo + ELO_K_FACTOR * (1 - expected_winner)
    new_loser_elo = loser_elo - ELO_K_FACTOR * (1 - expected_winner)
    return new_winner_elo, new_loser_elo

# ──────────────────────────────────────────────
# Game State
# ──────────────────────────────────────────────

class Table:
    """
    Represents one active game between a champion and a challenger.
    challenger_id is None when the table is paused, waiting for someone to !join and challenge the champion.
    """
    def __init__(self, number: int, champion_id: int, challenger_id: int | None = None):
        self.number = number          # 1 or 2
        self.champion_id = champion_id
        self.challenger_id = challenger_id

    def players(self):
        ids = {self.champion_id}
        if self.challenger_id is not None:
            ids.add(self.challenger_id)
        return ids

    def __repr__(self):
        return f"Table(number={self.number}, champion={self.champion_id}, challenger={self.challenger_id})"


# Global state — all in memory, HOF is persisted to disk
queue: deque[int] = deque()   # user IDs in order
tables: dict[int, Table] = {} # table_number -> Table (max 2 tables)
win_streaks: dict[int, int] = {}  # user ID -> current consecutive wins (per player, not per table)

# ──────────────────────────────────────────────
# Bot setup
# ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

class CivSelectView(discord.ui.View):
    """A single-use dropdown for picking one civ from CIV_LIST. Only `author_id` may use it."""
    def __init__(self, author_id: int, placeholder: str):
        super().__init__(timeout=REPORT_CONFIRM_TIMEOUT)
        self.author_id = author_id
        self.chosen_civ: str | None = None

        select = discord.ui.Select(
            placeholder=placeholder,
            options=[discord.SelectOption(label=civ) for civ in CIV_LIST]
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `!reportwin` can make this selection.", ephemeral=True
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        self.chosen_civ = interaction.data["values"][0]
        await interaction.response.defer()
        self.stop()


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

def seat_challenger(table: Table) -> bool:
    """
    Seat the next queued player as the table's challenger, if the seat is open.
    Returns True if someone was seated.
    """
    if table.challenger_id is None and queue:
        table.challenger_id = queue.popleft()
        return True
    return False

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
            champ_str = champ.display_name if champ else str(table.champion_id)
            champ_streak = win_streaks.get(table.champion_id, 0)
            streak_bar = "🔥" * champ_streak if champ_streak > 0 else "—"
            if table.challenger_id is not None:
                chal = guild.get_member(table.challenger_id)
                chal_str = chal.display_name if chal else str(table.challenger_id)
            else:
                chal_str = "_Waiting for a challenger — `!join`_"
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

async def try_seat_players(channel: discord.TextChannel, guild: discord.Guild):
    """Fill any table that's paused waiting for a challenger, start table 1 if none exist yet,
    and open table 2 if the queue is large enough."""
    for num in sorted(tables.keys()):
        table = tables[num]
        if seat_challenger(table):
            await channel.send(
                f"⚔️ **Table {num} is back on!** "
                f"{get_mention(table.champion_id)} vs {get_mention(table.challenger_id)} — let's go!"
            )

    if not tables and len(queue) >= 2:
        p1 = queue.popleft()
        p2 = queue.popleft()
        tables[1] = Table(1, p1, p2)
        await channel.send(
            f"⚔️ **Fight Night is starting!** {get_mention(p1)} vs {get_mention(p2)} — Table 1 is live!"
        )

    await try_start_second_table(channel, guild)

async def advance_table(table: Table, winner_id: int, loser_id: int,
                        channel: discord.TextChannel, guild: discord.Guild):
    """
    Called after a win is reported. Updates the winner's personal streak,
    checks for 3-in-a-row, sends the loser to the back of the queue, and
    pulls the next challenger from queue or closes the table.
    """
    hof = load_hof()

    # Win/loss streaks are tracked per player, not per table seat
    win_streaks.pop(loser_id, None)
    streak = win_streaks.get(winner_id, 0) + 1
    win_streaks[winner_id] = streak

    # ── 3-in-a-row ──────────────────────────────
    if streak >= WIN_STREAK_TARGET:
        winner = guild.get_member(winner_id)
        winner_name = winner.display_name if winner else str(winner_id)

        # Update HOF
        hof_key = str(winner_id)
        hof[hof_key] = {"name": winner_name, "count": hof.get(hof_key, {}).get("count", 0) + 1}
        save_hof(hof)

        # Winner leaves the rotation entirely — streak is cleared
        win_streaks.pop(winner_id, None)

        await channel.send(
            f"🏆 **{get_mention(winner_id)} WON 3 IN A ROW ON TABLE {table.number}!** 🏆\n"
            f"That's {hof[hof_key]['count']} time(s) in the Hall of Fame. Absolutely dominant. 👑"
        )

        # Remove table, start fresh from whoever is already queued —
        # seat from the existing queue BEFORE the loser rejoins it, so the
        # loser can never instantly refill the seat they just lost.
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
            tables[table.number] = Table(table.number, p1)
            await channel.send(
                f"⏳ {get_mention(p1)} is holding Table {table.number} — `!join` to challenge them!"
            )
        else:
            await channel.send(
                f"⏳ Table {table.number} is waiting for players. Join with `!join`!"
            )

        # Loser rejoins the back of the queue for another shot
        queue.append(loser_id)
        return

    # ── Normal win — champion stays, pull next challenger ──
    # Seat from the existing queue BEFORE the loser rejoins it, so the loser
    # can't immediately refill the seat they just vacated.
    table.champion_id = winner_id
    table.challenger_id = None
    seated = seat_challenger(table)
    queue.append(loser_id)

    if seated:
        streak_str = f"({streak} in a row 🔥)" if streak > 1 else ""
        await channel.send(
            f"✅ **Game over on Table {table.number}!** {get_mention(winner_id)} wins {streak_str}\n"
            f"⚔️ Next up: {get_mention(winner_id)} vs {get_mention(table.challenger_id)} — let's go!"
        )
    else:
        await channel.send(
            f"✅ **Game over on Table {table.number}!** {get_mention(winner_id)} wins — "
            f"queue is empty. {get_mention(winner_id)} holds the table — `!join` to challenge them!"
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
    await try_seat_players(channel, ctx.guild)

    # Only announce a queue position if they weren't immediately seated
    if uid in queue:
        position = list(queue).index(uid) + 1
        await channel.send(
            f"✅ {ctx.author.mention} joined the queue at position **#{position}**."
        )


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
    win_streaks.pop(uid, None)
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

    if caller_table.challenger_id is None:
        await channel.send(
            f"⏳ Table {caller_table.number} is still waiting for a challenger — nothing to report yet."
        )
        return

    if winner_id not in caller_table.players():
        await channel.send(
            f"❌ {winner.mention} isn't playing on your table. "
            f"Only {get_mention(caller_table.champion_id)} and {get_mention(caller_table.challenger_id)} are."
        )
        return

    loser_id = (caller_table.players() - {winner_id}).pop()
    await advance_table(caller_table, winner_id, loser_id, channel, ctx.guild)

    # Open a second table if the queue is large enough. Do NOT run the general
    # try_seat_players sweep here — it would immediately refill the table we
    # just paused using the loser who was just appended to the queue.
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
        win_streaks.clear()
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

    elif subcommand == "resetstats":
        save_stats({"players": {}, "matches": []})
        await channel.send("🔄 Win/loss records and elo have been reset for all players.")

    else:
        await channel.send(
            "**Admin commands:**\n"
            "`!fn reset` — clear all tables and queue\n"
            "`!fn removetable <1|2>` — remove a stalled table\n"
            "`!fn resetstats` — wipe all win/loss records and elo"
        )


@bot.command(name="fnhelp")
async def help_cmd(ctx):
    """Show all Fight Night commands."""
    embed = discord.Embed(title="⚔️ Fight Night Bot Commands", color=0x3498DB)
    embed.add_field(name="`!join`", value="Add yourself to the queue", inline=False)
    embed.add_field(name="`!leave`", value="Remove yourself from the queue", inline=False)
    embed.add_field(name="`!win @player`", value="Report the winner of your current game", inline=False)
    embed.add_field(name="`!queue`", value="Show active tables and the queue", inline=False)
    embed.add_field(name="`!hof`", value="Show the all-time Hall of Fame", inline=False)
    embed.add_field(
        name="`!reportwin @winner @loser`",
        value="Report a custom game result — pick each player's civ from a dropdown, then the other player confirms with ✅",
        inline=False
    )
    embed.add_field(name="`!leaderboard`", value="Show all-time custom game win/loss rankings", inline=False)
    embed.add_field(name="`!elo [@player]`", value="Show a player's rough hidden elo (yourself by default)", inline=False)
    embed.add_field(name="`!customs`", value="Ping for a custom game, showing your rough elo", inline=False)
    embed.add_field(name="`!fn reset` *(admin)*", value="Reset all tables and queue", inline=False)
    embed.add_field(name="`!fn removetable <1|2>` *(admin)*", value="Remove a stalled table", inline=False)
    embed.add_field(name="`!fn resetstats` *(admin)*", value="Wipe all win/loss records and elo", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="reportwin")
async def report_win_custom(ctx, winner: discord.Member = None, loser: discord.Member = None):
    """
    Report a custom game result: !reportwin @winner @loser
    You'll be prompted to pick each player's civ from a dropdown. Either player can
    report, but the OTHER player must confirm with a ✅ reaction before it's recorded.
    """
    channel = await get_fn_channel(ctx)

    if winner is None or loser is None:
        await channel.send('❓ Usage: `!reportwin @winner @loser`')
        return

    if winner.id == loser.id:
        await channel.send("❌ Winner and loser can't be the same person.")
        return

    reporter_id = ctx.author.id
    if reporter_id == winner.id:
        other_player = loser
    elif reporter_id == loser.id:
        other_player = winner
    else:
        await channel.send("❌ Only one of the two players in the match can report the result.")
        return

    winner_view = CivSelectView(reporter_id, f"Select {winner.display_name}'s civ")
    winner_prompt = await channel.send(
        f"🎮 {ctx.author.mention}, select **{winner.display_name}**'s civ:", view=winner_view
    )
    if await winner_view.wait() or winner_view.chosen_civ is None:
        await winner_prompt.edit(content="⌛ Civ selection timed out — run `!reportwin` again.", view=None)
        return
    winner_civ = winner_view.chosen_civ
    await winner_prompt.edit(content=f"✅ **{winner.display_name}**'s civ: **{winner_civ}**", view=None)

    loser_view = CivSelectView(reporter_id, f"Select {loser.display_name}'s civ")
    loser_prompt = await channel.send(
        f"🎮 {ctx.author.mention}, select **{loser.display_name}**'s civ:", view=loser_view
    )
    if await loser_view.wait() or loser_view.chosen_civ is None:
        await loser_prompt.edit(content="⌛ Civ selection timed out — run `!reportwin` again.", view=None)
        return
    loser_civ = loser_view.chosen_civ
    await loser_prompt.edit(content=f"✅ **{loser.display_name}**'s civ: **{loser_civ}**", view=None)

    confirm_msg = await channel.send(
        f"📋 **Match report:** {winner.mention} defeated {loser.mention} "
        f"({winner_civ} vs {loser_civ}).\n"
        f"{other_player.mention} react ✅ to confirm or ❌ to dispute — expires in "
        f"{REPORT_CONFIRM_TIMEOUT // 60} minutes."
    )
    await confirm_msg.add_reaction("✅")
    await confirm_msg.add_reaction("❌")

    def check(reaction: discord.Reaction, user: discord.User) -> bool:
        return (
            reaction.message.id == confirm_msg.id
            and user.id == other_player.id
            and str(reaction.emoji) in ("✅", "❌")
        )

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=REPORT_CONFIRM_TIMEOUT, check=check)
    except asyncio.TimeoutError:
        await channel.send(f"⌛ Match report expired — {other_player.mention} never confirmed.")
        return

    if str(reaction.emoji) == "❌":
        await channel.send(f"🚫 {other_player.mention} disputed the report. No changes made.")
        return

    stats = load_stats()
    winner_record = get_player_record(stats, winner.id)
    loser_record = get_player_record(stats, loser.id)

    new_winner_elo, new_loser_elo = calc_new_elo(winner_record["elo"], loser_record["elo"])
    winner_record["elo"] = new_winner_elo
    loser_record["elo"] = new_loser_elo
    winner_record["wins"] += 1
    loser_record["losses"] += 1

    stats["matches"].append({
        "winner_id": winner.id,
        "loser_id": loser.id,
        "winner_civ": winner_civ,
        "loser_civ": loser_civ,
    })
    save_stats(stats)

    await channel.send(
        f"✅ **Confirmed!** {winner.mention} defeated {loser.mention} "
        f"({winner_civ} vs {loser_civ}). Records updated."
    )


@bot.command(name="leaderboard")
async def leaderboard(ctx):
    """Show all-time win/loss rankings for custom games."""
    channel = await get_fn_channel(ctx)
    stats = load_stats()
    players = stats.get("players", {})

    ranked = [
        (uid, record) for uid, record in players.items()
        if record["wins"] + record["losses"] > 0
    ]
    if not ranked:
        await channel.send("📊 No custom games have been reported yet — use `!reportwin` after your next match!")
        return

    ranked.sort(key=lambda item: (
        item[1]["wins"] / (item[1]["wins"] + item[1]["losses"]),
        item[1]["wins"]
    ), reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, record) in enumerate(ranked):
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else uid
        wins, losses = record["wins"], record["losses"]
        winrate = wins / (wins + losses) * 100
        medal = medals[i] if i < 3 else f"`{i + 1}.`"
        lines.append(f"{medal} **{name}** — {wins}-{losses} ({winrate:.0f}%)")

    embed = discord.Embed(
        title="📊 Custom Games Leaderboard",
        description="\n".join(lines),
        color=0x2ECC71
    )
    await channel.send(embed=embed)


@bot.command(name="elo")
async def show_elo(ctx, player: discord.Member = None):
    """Show a player's rough hidden elo (yourself by default): !elo [@player]"""
    channel = await get_fn_channel(ctx)
    target = player or ctx.author

    stats = load_stats()
    record = stats.get("players", {}).get(str(target.id))

    if record is None or record["wins"] + record["losses"] == 0:
        await channel.send(f"📈 {target.display_name} hasn't had any `!reportwin` results recorded yet.")
        return

    elo_display = round(record["elo"])
    await channel.send(
        f"📈 **{target.display_name}** — ~{elo_display} elo ({record['wins']}-{record['losses']})"
    )


@bot.command(name="customs")
async def customs(ctx):
    customGames = discord.utils.get(ctx.guild.roles, id=1478504818027794543)
    stats = load_stats()
    record = stats.get("players", {}).get(str(ctx.author.id), {"elo": DEFAULT_ELO})
    elo_display = round(record["elo"])
    await ctx.send(
        f'{customGames.mention} {ctx.author.mention} (~{elo_display} elo) is looking for a custom game'
    )



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
