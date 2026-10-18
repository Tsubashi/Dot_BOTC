import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import nextcord

# from datetime import timedelta
# Load DISCORD_TOKEN etc from .env if it exists.
# Alternatively, these can be put in environment variables.
from dotenv import load_dotenv
from nextcord.ext import commands, tasks

load_dotenv()

intents = nextcord.Intents.default()
intents.message_content = True
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    default_guild_ids=[int(id) for id in os.environ["GUILDS"].split(",")],
)

# File paths in the same directory as the script
livequeue_file_path = os.path.join(os.path.dirname(__file__), "Livequeue.json")
cooldowns_file_path = os.path.join(os.path.dirname(__file__), "Cooldowns.json")
active_st_file_path = os.path.join(os.path.dirname(__file__), "ActiveStorytellers.json")
New_ST_Exceptions_path = os.path.join(os.path.dirname(__file__), "NewSTExceptions.json")

# Define global cooldown durations (18 hours in seconds)
BEGINNER_COOLDOWN_DURATION = int(os.environ["BEGINNER_COOLDOWN_DURATION"])
PICKUP_COOLDOWN_DURATION = int(os.environ["PICKUP_COOLDOWN_DURATION"])
ANY_COOLDOWN_DURATION = int(os.environ["ANY_COOLDOWN_DURATION"])
REMOVECOOLDOWN_COOLDOWN_DURATION = int(os.environ["REMOVECOOLDOWN_COOLDOWN_DURATION"])
RE_RACK_TIMER_DURATION = int(os.environ["RE_RACK_TIMER_DURATION"])

# Define cooldown duration for leaving the queue (1 hour in seconds)
LEAVE_COOLDOWN_DURATION = int(os.environ["LEAVE_COOLDOWN_DURATION"])

# Define global merged status, games running status, and timer
MERGED = True
GAMES_RUNNING = False
TIMEOUT_TIMER = int(os.environ["TIMEOUT_TIMER"])

# Channel IDs
BEGINNER_CHANNEL_ID = int(os.environ["BEGINNER_CHANNEL_ID"])
PICKUP_CHANNEL_ID = int(os.environ["PICKUP_CHANNEL_ID"])
MERGED_CHANNEL_ID = PICKUP_CHANNEL_ID

# Role pinged when a storyteller finishes and nobody is waiting in the queue
LFST_ROLE_ID = int(os.environ["LFST_ROLE_ID"])

# Flags for managing the state of the LFST cycle and queue status
# These are keyed by queue ("Merged", or "Beginner"/"Pickup" when split).

# lfst_armed holds queues where a game finished and the slot hasn't been claimed
# yet. This will prompt check_queue to start a ping cycle.
lfst_armed = set()
# lfst_state will be one of the following for each queue:
# None (idle), {"phase": "counting", "deadline": ts}, or {"phase": "open"}.
# When a queue is "open", /join ignores cooldowns and allows anyone to /join.
lfst_state = {}

# Ensure the files exist
for file_path in [livequeue_file_path, cooldowns_file_path, active_st_file_path]:
    if not os.path.exists(file_path):
        with open(file_path, "w") as file:
            json.dump({}, file)


def load_json(file_path):
    try:
        with open(file_path, "r") as file:
            return json.load(file)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def save_json(file_path, data):
    with open(file_path, "w") as file:
        json.dump(data, file, indent=4)


# Initialize in-memory queue, cooldowns, and active storytellers
queue = load_json(livequeue_file_path)
cooldowns = load_json(cooldowns_file_path)
active_storytellers = load_json(active_st_file_path)
New_ST_Exceptions = load_json(New_ST_Exceptions_path)


def is_active_storyteller(user_id):
    # Return false if all active STs are of queue type "Extra"
    if all(st["QueueType"] == "Extra" for st in active_storytellers.values()):
        return False
    return str(user_id) in active_storytellers


def add_active_storyteller(user, queue_type):
    if queue_type == "Any":
        queue_type = queue[str(user.id)]["QueueType"]
    lfst_armed.discard(lfst_key_for(queue_type))
    active_storytellers[str(user.id)] = {
        "DisplayName": user.display_name,
        "Discord_ID": user.id,
        "User_Image_URL": str(user.display_avatar.url),
        "QueueType": queue_type,
    }
    save_json(active_st_file_path, active_storytellers)


def remove_active_storyteller(user_id):
    if str(user_id) in active_storytellers:
        del active_storytellers[str(user_id)]
        save_json(active_st_file_path, active_storytellers)


def update_queue_positions():
    sorted_queue = sorted(queue.values(), key=lambda x: x["Merged_Queue_Position"])
    for idx, entry in enumerate(sorted_queue):
        entry["Merged_Queue_Position"] = idx + 1
    save_json(livequeue_file_path, queue)


async def remove_queue(user_id):
    current_time = int(time.time())

    if str(user_id) in queue:
        if str(user_id) not in cooldowns:
            cooldowns[str(user_id)] = {
                "DisplayName": queue[str(user_id)]["DisplayName"],
                "Discord_ID": user_id,
                "User_Image_URL": queue[str(user_id)]["User_Image_URL"],
                "Cooldown": 0,
                "removeCooldown_Cooldown": 0,
            }
        cooldowns[str(user_id)]["Cooldown"] = current_time + BEGINNER_COOLDOWN_DURATION
        del queue[str(user_id)]

        update_queue_positions()
        save_json(cooldowns_file_path, cooldowns)
        save_json(livequeue_file_path, queue)


def lfst_queue_keys():
    return ["Merged"] if MERGED else ["Beginner", "Pickup"]


def lfst_key_for(queue_type):
    return "Merged" if MERGED else queue_type


def lfst_channel_id(queue_key):
    return {
        "Merged": MERGED_CHANNEL_ID,
        "Beginner": BEGINNER_CHANNEL_ID,
        "Pickup": PICKUP_CHANNEL_ID,
    }[queue_key]


def slot_unclaimed(queue_key):
    """True when nobody is running a game for this queue and nobody is waiting to."""
    if queue_key == "Merged":
        active = any(is_active_storyteller(uid) for uid in active_storytellers)
        waiting = bool(queue)
    else:
        types = [queue_key, "Any"]
        active = any(st["QueueType"] in types for st in active_storytellers.values())
        waiting = any(entry["QueueType"] in types for entry in queue.values())
    return not active and not waiting


def lfst_window_open(queue_type):
    """True if someone joining this queue type may bypass their cooldown."""
    if MERGED:
        keys = ["Merged"]
    elif queue_type == "Any":
        keys = ["Beginner", "Pickup"]
    else:
        keys = [queue_type]
    return any(
        (lfst_state.get(key) or {}).get("phase") == "open" and slot_unclaimed(key)
        for key in keys
    )


async def lfst_tick():
    """Advance the LFST ping cycle for each queue. Called from check_queue every tick."""
    now = int(time.time())
    current_keys = lfst_queue_keys()

    # Forget state for queues that don't exist in the current merge mode
    for key in list(lfst_state):
        if key not in current_keys:
            lfst_state[key] = None

    for key in current_keys:
        if not slot_unclaimed(key):
            # Someone is waiting or running a game, so any cycle in progress is over.
            # The queue stays armed, so if they drop out later a fresh ping goes out.
            lfst_state[key] = None
            continue

        state = lfst_state.get(key)
        if state is None:
            if key not in lfst_armed:
                continue
            deadline = now + TIMEOUT_TIMER
            lfst_state[key] = {"phase": "counting", "deadline": deadline}
            await bot.get_channel(lfst_channel_id(key)).send(
                f"<@&{LFST_ROLE_ID}> Queue is empty, anyone can storytell <t:{deadline}:R>."
            )
        elif state["phase"] == "counting" and now >= state["deadline"]:
            lfst_state[key] = {"phase": "open"}
            await bot.get_channel(lfst_channel_id(key)).send(
                "LFST cooldown expired. If the queue is still empty, anyone can now storytell."
            )


@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user}")
    check_queue.start()


@bot.slash_command(name="join", description="Join the Live Queue")
async def join(
    interaction: nextcord.Interaction,
    queue_type: str = nextcord.SlashOption(
        name="queue_type",
        description="Type of queue",
        choices={"Beginner": "Beginner", "Pickup": "Pickup", "Any": "Any"},
        required=True,
    ),
    notes: str = nextcord.SlashOption(
        name="notes",
        description="Additional notes",
        required=True,
    ),
):
    user = interaction.user
    current_time = time.time()
    join_threshold = datetime.now(timezone.utc) - timedelta(weeks=2)

    if user.joined_at > join_threshold and str(user.id) not in New_ST_Exceptions.keys():
        await interaction.response.send_message(
            f"{user.mention} You must be on the server for more than 2 weeks to Storytell on the server."
        )
        return

    # Check if the user is on cooldown, unless the LFST window is open and the slot is unclaimed
    on_cooldown = (
        str(user.id) in cooldowns and cooldowns[str(user.id)]["Cooldown"] > current_time
    )
    if on_cooldown and not lfst_window_open(queue_type):
        await interaction.response.send_message(
            "You are currently on cooldown and cannot join the queue."
        )
        return

    # Check if the user is already in the queue
    if str(user.id) in queue:
        await interaction.response.send_message(
            "You are currently in the queue and cannot re-join the queue."
        )
        return

    merged_queue_position = len(queue) + 1

    new_entry = {
        "DisplayName": user.display_name,
        "Discord_ID": user.id,
        "User_Image_URL": str(user.display_avatar.url),
        "QueueType": queue_type,
        "Merged_Queue_Position": merged_queue_position,
        "Notes": notes,
    }

    cooldown_entry = {
        "DisplayName": user.display_name,
        "Discord_ID": user.id,
        "User_Image_URL": str(user.display_avatar.url),
        "Cooldown": 0,
        "removeCooldown_Cooldown": 0,
    }

    queue[str(user.id)] = new_entry
    cooldowns[str(user.id)] = cooldown_entry

    await interaction.response.send_message(
        f"{user.display_name} has been added to the queue."
    )


@bot.slash_command(name="list", description="List the current queue(s)")
async def list_queue(interaction: nextcord.Interaction):
    sorted_queue = sorted(queue.values(), key=lambda x: x["Merged_Queue_Position"])
    channel_to_send = interaction.channel
    if not GAMES_RUNNING:
        embed = nextcord.Embed(title="Games Currently Paused")
        embed.add_field(
            name="Games are currently paused, please use `/resume` to restart the queue",
            value="----------",
            inline=False,
        )
        t = "\n".join(
            [
                f"[{str(entry['QueueType'])[0]}] {entry['DisplayName']} | {str(entry['Notes'])[:100] if entry['Notes'] else 'None'}"
                for entry in sorted_queue
            ]
        )
        embed.add_field(name="Current Queue", value=t, inline=False)
        await interaction.response.send_message(embed=embed)

    elif MERGED:
        embed = nextcord.Embed(title="Merged Queue List")
        t = "\n".join(
            [
                f"{entry['DisplayName']} | {str(entry['Notes'])[:100] if entry['Notes'] else 'None'}"
                for entry in sorted_queue
            ]
        )
        embed.add_field(name="Current Queue", value=t, inline=False)
        await interaction.response.send_message(embed=embed)
    else:
        beginner_any_queue = [
            entry for entry in sorted_queue if entry["QueueType"] in ["Beginner", "Any"]
        ]
        pickup_any_queue = [
            entry for entry in sorted_queue if entry["QueueType"] in ["Pickup", "Any"]
        ]

        embed = nextcord.Embed(title="Beginner/Any Queue List")
        t = "\n".join(
            [
                f"{entry['DisplayName']} | {str(entry['Notes'])[:100] if entry['Notes'] else 'None'}"
                for entry in beginner_any_queue
            ]
        )
        embed.add_field(name="Current Queue", value=t, inline=False)
        await interaction.response.send_message(embed=embed)

        embed = nextcord.Embed(title="Pickup/Any Queue List")
        t = "\n".join(
            [
                f"{entry['DisplayName']} | {str(entry['Notes'])[:100] if entry['Notes'] else 'None'}"
                for entry in pickup_any_queue
            ]
        )
        embed.add_field(name="Current Queue", value=t, inline=False)
        await channel_to_send.send(embed=embed)


@bot.slash_command(name="leave", description="Leave the queue if you're signed up")
async def leave(interaction: nextcord.Interaction):
    await leave_queue(interaction)


async def leave_queue(interaction: nextcord.Interaction, user_id=None):
    if user_id is not None:
        user = await bot.fetch_user(user_id)
    else:
        user = interaction.user

    current_time = int(time.time())

    if str(user.id) in queue:
        queue_type = queue[str(user.id)]["QueueType"]
        cooldowns[str(user.id)]["Cooldown"] = current_time + LEAVE_COOLDOWN_DURATION
        del queue[str(user.id)]

        update_queue_positions()

        if queue_type == "Beginner":
            channel = bot.get_channel(BEGINNER_CHANNEL_ID)
        elif queue_type == "Pickup":
            channel = bot.get_channel(PICKUP_CHANNEL_ID)
        else:
            channel = bot.get_channel(MERGED_CHANNEL_ID)

        await channel.send(
            f"{user.display_name} has been removed from the queue and is on cooldown until <t:{current_time + LEAVE_COOLDOWN_DURATION}:f>."
        )
        await interaction.response.send_message(
            "You have left the queue.", ephemeral=True
        )
    else:
        await interaction.response.send_message("You are not in the queue.")


@bot.slash_command(name="removefromqueue", description="Removes player from queue")
async def removefromqueue(interaction: nextcord.Interaction, user: nextcord.Member):
    current_time = int(time.time())

    if str(user.id) in queue:
        queue_type = queue[str(user.id)]["QueueType"]
        cooldowns[str(user.id)]["Cooldown"] = current_time + LEAVE_COOLDOWN_DURATION
        del queue[str(user.id)]

        update_queue_positions()

        if queue_type == "Beginner":
            channel = bot.get_channel(BEGINNER_CHANNEL_ID)
        elif queue_type == "Pickup":
            channel = bot.get_channel(PICKUP_CHANNEL_ID)
        else:
            channel = bot.get_channel(MERGED_CHANNEL_ID)

        await channel.send(
            f"{user.display_name} has been removed from the queue and is on cooldown until <t:{current_time + LEAVE_COOLDOWN_DURATION}:f>."
        )
        await interaction.response.send_message(
            f"You removed {user.display_name} the queue.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"{user.display_name} is not in the queue."
        )


@bot.slash_command(name="debug", description="Used for testing purposes")
async def debug(interaction: nextcord.Interaction, member: nextcord.Member):
    await interaction.response.send_message(
        f"B_Queue ID: {BEGINNER_CHANNEL_ID}, P_Queue ID: {PICKUP_CHANNEL_ID}, A_Queue ID: {MERGED_CHANNEL_ID}"
    )


@bot.slash_command(name="check", description="Check your cooldown status")
async def check_cooldown(interaction: nextcord.Interaction):
    user = interaction.user
    current_time = int(time.time())

    if str(user.id) in cooldowns and cooldowns[str(user.id)]["Cooldown"] > current_time:
        timestamp = cooldowns[str(user.id)]["Cooldown"]
        await interaction.response.send_message(
            f"You are still on cooldown until <t:{timestamp}:f>"
        )
    else:
        await interaction.response.send_message("You are not on cooldown.")


@bot.slash_command(name="save", description="Save the queue to the JSON file")
async def save(interaction: nextcord.Interaction):
    save_json(livequeue_file_path, queue)
    save_json(cooldowns_file_path, cooldowns)
    save_json(active_st_file_path, active_storytellers)
    save_json(New_ST_Exceptions_path, New_ST_Exceptions)
    await interaction.response.send_message(
        "The queue, cooldowns, and active storytellers have been saved to the JSON files."
    )


@bot.slash_command(name="load", description="Load the queue from the JSON file")
async def load(interaction: nextcord.Interaction):
    global queue
    global cooldowns
    global active_storytellers
    global New_ST_Exceptions
    queue = load_json(livequeue_file_path)
    cooldowns = load_json(cooldowns_file_path)
    active_storytellers = load_json(active_st_file_path)
    New_ST_Exceptions = load_json(New_ST_Exceptions_path)
    await interaction.response.send_message(
        "The queue, cooldowns, and active storytellers have been loaded from the JSON files."
    )


@bot.slash_command(
    name="split", description="Split the merged queue into Beginner / Pickup Games"
)
async def split(interaction: nextcord.Interaction):
    global MERGED
    MERGED = False
    await interaction.response.send_message(
        "The queue has been split into Beginner / Pickup Games."
    )


@bot.slash_command(
    name="merge", description="Merge Beginner / Pickup Games into one Queue"
)
async def merge(interaction: nextcord.Interaction):
    global MERGED
    MERGED = True
    await interaction.response.send_message("The queue has been merged into one Queue.")


@bot.slash_command(
    name="pause", description="Pause the queue if there aren't enough players"
)
async def pause(interaction: nextcord.Interaction):
    global GAMES_RUNNING
    GAMES_RUNNING = False
    await interaction.response.send_message("The games have been paused.")


@bot.slash_command(
    name="resume", description="Resume the queue when enough players are around"
)
async def resume(interaction: nextcord.Interaction):
    global GAMES_RUNNING
    GAMES_RUNNING = True
    await interaction.response.send_message("The games have been resumed.")


@bot.slash_command(name="finish", description="Finish your turn and leave the queue")
async def finish(interaction: nextcord.Interaction):
    global BEGINNER_CHANNEL_ID
    global PICKUP_CHANNEL_ID
    global MERGED_CHANNEL_ID
    user = interaction.user
    if is_active_storyteller(user.id):
        QueueType = active_storytellers[str(user.id)]["QueueType"]
        remove_active_storyteller(user.id)
        await interaction.response.send_message(
            f"{user.display_name} has finished their game, please wait whilst the next ST is alerted. To submit feedback, use </feedback:1555321074931998721>"
        )
        update_queue_positions()
        if QueueType == "Beginner":
            BEGINNER_CHANNEL_ID = interaction.channel.id
            MERGED_CHANNEL_ID = interaction.channel.id
        elif QueueType == "Pickup":
            PICKUP_CHANNEL_ID = interaction.channel.id
            MERGED_CHANNEL_ID = interaction.channel.id
        lfst_armed.add(lfst_key_for(QueueType))
    else:
        await interaction.response.send_message("You are not active in the queue.")


@bot.slash_command(name="forcefinish", description="Force finish a user's turn")
async def forcefinish(interaction: nextcord.Interaction, player: nextcord.Member):
    global BEGINNER_CHANNEL_ID
    global PICKUP_CHANNEL_ID
    global MERGED_CHANNEL_ID
    if is_active_storyteller(player.id):
        QueueType = active_storytellers[str(player.id)]["QueueType"]
        remove_active_storyteller(player.id)
        await interaction.response.send_message(
            f"{player.display_name} has been force finished and removed from the queue. To submit feedback, use </feedback:1555321074931998721>"
        )
        update_queue_positions()
        if QueueType == "Beginner":
            BEGINNER_CHANNEL_ID = interaction.channel.id
            MERGED_CHANNEL_ID = interaction.channel.id
        elif QueueType == "Pickup":
            PICKUP_CHANNEL_ID = interaction.channel.id
            MERGED_CHANNEL_ID = interaction.channel.id
        lfst_armed.add(lfst_key_for(QueueType))
    else:
        await interaction.response.send_message(
            f"{player.display_name} is not active in the queue."
        )


@bot.slash_command(name="activests", description="List current Storytellers")
async def active_sts(interaction: nextcord.Interaction):
    embed = nextcord.Embed(title="Active Storytellers")
    for st in active_storytellers.values():
        embed.add_field(
            name=st["DisplayName"], value=f"Queue Type: {st['QueueType']}", inline=False
        )
    await interaction.response.send_message(embed=embed)


@bot.slash_command(name="adminremovecooldown", description="Remove a user's cooldown")
async def removecooldown(
    interaction: nextcord.Interaction, player: nextcord.Member = None
):
    current_time = int(time.time())
    if player is None:
        player = interaction.user

    if str(player.id) in cooldowns:
        cooldowns[str(player.id)]["Cooldown"] = current_time
        await interaction.response.send_message(
            f"{player.display_name}'s cooldown has been removed."
        )
    else:
        await interaction.response.send_message(
            f"{player.display_name} does not have a cooldown."
        )


@bot.slash_command(
    name="removecooldown",
    description="Remove a your cooldown if you missed your turn (Usable once per 60 days)",
)
async def removecooldown(interaction: nextcord.Interaction):
    current_time = int(time.time())

    player = interaction.user

    if cooldowns[str(player.id)]["removeCooldown_Cooldown"] > current_time:
        await interaction.response.send_message(
            f"{player.display_name} cannot remove their cooldown until <t:{cooldowns[str(player.id)]['removeCooldown_Cooldown']}>."
        )
    elif str(player.id) in cooldowns:
        cooldowns[str(player.id)]["Cooldown"] = current_time
        cooldowns[str(player.id)]["removeCooldown_Cooldown"] = (
            current_time + REMOVECOOLDOWN_COOLDOWN_DURATION
        )
        await interaction.response.send_message(
            f"{player.display_name}'s cooldown has been removed and they cannot remove their cooldown again until <t:{current_time + REMOVECOOLDOWN_COOLDOWN_DURATION}:f>."
        )
    else:
        await interaction.response.send_message(
            f"{player.display_name} does not have a cooldown."
        )


@bot.slash_command(name="addcooldown", description="Add a cooldown to a user")
async def addcooldown(
    interaction: nextcord.Interaction, player: nextcord.Member, hours: int
):
    current_time = int(time.time())

    if str(player.id) in cooldowns:
        cooldowns[str(player.id)]["Cooldown"] = current_time + hours * 3600
    else:
        cooldown_entry = {
            "DisplayName": player.display_name,
            "Discord_ID": player.id,
            "User_Image_URL": str(player.display_avatar.url),
            "Cooldown": current_time + hours * 3600,
            "removeCooldown_Cooldown": 0,
        }

        cooldowns[str(player.id)] = cooldown_entry
    await interaction.response.send_message(
        f"{player.display_name}'s cooldown has been set to <t:{current_time + hours * 3600}:f>."
    )


@bot.slash_command(
    name="allow", description="Bypasses the 2 week waiting period for new STs"
)
async def addcooldown(interaction: nextcord.Interaction, player: nextcord.Member):
    entry = {
        "DisplayName": player.display_name,
        "Discord_ID": player.id,
        "User_Image_URL": str(player.display_avatar.url),
        "Added By Name": interaction.user.display_name,
        "Added By ID": interaction.user.id,
    }

    New_ST_Exceptions[str(player.id)] = entry
    await interaction.response.send_message(
        f"{player.display_name} may now join the Queue."
    )


@bot.slash_command(
    name="removeremovecooldowncooldown",
    description="Remove a user's removeCooldown_Cooldown",
)
async def removeremovecooldowncooldown(
    interaction: nextcord.Interaction, player: nextcord.Member
):
    current_time = int(time.time())

    if str(player.id) in cooldowns:
        cooldowns[str(player.id)]["removeCooldown_Cooldown"] = current_time
        await interaction.response.send_message(
            f"{player.display_name}'s removeCooldown_Cooldown has been removed."
        )
    else:
        await interaction.response.send_message(
            f"{player.display_name} is not in the queue."
        )


@bot.slash_command(
    name="startextra", description="Starts an extra game if next in queue"
)
async def startextra(interaction: nextcord.Interaction):
    user = interaction.user
    eligible = False

    if MERGED:
        # Find the lowest merged queue position that is not Active_ST
        sorted_queue = sorted(queue.values(), key=lambda x: x["Merged_Queue_Position"])
        if (
            sorted_queue
            and sorted_queue[0]["Discord_ID"] == user.id
            and not is_active_storyteller(user.id)
        ):
            eligible = True
    else:
        # Find the lowest queue position for beginner, pickup, or any that is not Active_ST
        beginner_queue = [
            entry
            for entry in queue.values()
            if entry["QueueType"] in ["Beginner", "Any"]
        ]
        pickup_queue = [
            entry for entry in queue.values() if entry["QueueType"] in ["Pickup", "Any"]
        ]
        sorted_beginner_queue = sorted(
            beginner_queue, key=lambda x: x["Merged_Queue_Position"]
        )
        sorted_pickup_queue = sorted(
            pickup_queue, key=lambda x: x["Merged_Queue_Position"]
        )

        if (
            sorted_beginner_queue
            and sorted_beginner_queue[0]["Discord_ID"] == user.id
            and not is_active_storyteller(user.id)
        ) or (
            sorted_pickup_queue
            and sorted_pickup_queue[0]["Discord_ID"] == user.id
            and not is_active_storyteller(user.id)
        ):
            eligible = True

    if eligible:
        add_active_storyteller(user, "Extra")
        await remove_queue(user.id)
        await interaction.response.send_message(
            f"{user.display_name} is now active and has been removed from the queue."
        )

        # Notify user after 40 minutes
        await asyncio.sleep(RE_RACK_TIMER_DURATION)
        await interaction.channel.send(
            f"{user.mention}, the Re-rack timer has expired."
        )
    else:
        await interaction.response.send_message("You are not eligible to start extra.")


@bot.slash_command(
    name="setposition", description="Move a player to the top of the merged queue"
)
async def setposition(
    interaction: nextcord.Interaction, player: nextcord.Member, position: int
):
    if str(player.id) in queue:
        queue[str(player.id)]["Merged_Queue_Position"] = position - 0.5
        update_queue_positions()
        await interaction.response.send_message(
            f"{player.display_name} has been moved to position {position} of the merged queue."
        )
    else:
        await interaction.response.send_message(
            f"{player.display_name} is not in the queue."
        )


@bot.slash_command(name="addplayer", description="Force add a player to the queue")
async def addplayer(
    interaction: nextcord.Interaction,
    player: nextcord.Member,
    queue_type: str = nextcord.SlashOption(
        name="queue_type",
        description="Type of queue",
        choices={"Beginner": "Beginner", "Pickup": "Pickup", "Any": "Any"},
        required=True,
    ),
    notes: str = "Mod Added to Queue",
):
    merged_queue_position = len(queue) + 1

    new_entry = {
        "DisplayName": player.display_name,
        "Discord_ID": player.id,
        "User_Image_URL": str(player.display_avatar.url),
        "QueueType": queue_type,
        "Merged_Queue_Position": merged_queue_position,
        "Notes": notes[:128],
    }

    cooldown_entry = {
        "DisplayName": player.display_name,
        "Discord_ID": player.id,
        "User_Image_URL": str(player.display_avatar.url),
        "Cooldown": 0,
        "removeCooldown_Cooldown": 0,
    }

    queue[str(player.id)] = new_entry
    cooldowns[str(player.id)] = cooldown_entry

    update_queue_positions()
    await interaction.response.send_message(
        f"{player.display_name} has been added to the queue."
    )


@bot.slash_command(name="setqueue", description="Set the queue for a specific type")
async def setqueue(
    interaction: nextcord.Interaction,
    queue_type: str = nextcord.SlashOption(
        name="queue_type",
        description="Type of queue",
        choices={"Beginner": "Beginner", "Pickup": "Pickup", "Any": "Any"},
        required=True,
    ),
    player1: nextcord.Member = None,
    player2: nextcord.Member = None,
    player3: nextcord.Member = None,
    player4: nextcord.Member = None,
    player5: nextcord.Member = None,
    player6: nextcord.Member = None,
    player7: nextcord.Member = None,
    player8: nextcord.Member = None,
    player9: nextcord.Member = None,
    player10: nextcord.Member = None,
):
    players = [
        player
        for player in [
            player1,
            player2,
            player3,
            player4,
            player5,
            player6,
            player7,
            player8,
            player9,
            player10,
        ]
        if player is not None
    ]

    # Remove all players from the current queue type
    for user_id in list(queue.keys()):
        if queue[user_id]["QueueType"] == queue_type:
            del queue[user_id]

    # Add mentioned users to the queue
    for idx, player in enumerate(players):
        merged_queue_position = len(queue) + 1
        new_entry = {
            "DisplayName": player.display_name,
            "Discord_ID": player.id,
            "User_Image_URL": str(player.display_avatar.url),
            "QueueType": queue_type,
            "Merged_Queue_Position": merged_queue_position,
            "Notes": "Mod Added to Queue",
        }

        cooldown_entry = {
            "DisplayName": player.display_name,
            "Discord_ID": player.id,
            "User_Image_URL": str(player.display_avatar.url),
            "Cooldown": 0,
            "removeCooldown_Cooldown": 0,
        }

        cooldowns[str(player.id)] = cooldown_entry
        queue[str(player.id)] = new_entry

    update_queue_positions()
    await interaction.response.send_message(
        f"The queue for {queue_type} has been updated with the mentioned players."
    )


async def start_user(interaction, user):
    add_active_storyteller(user, queue[str(user.id)]["QueueType"])
    await remove_queue(user.id)
    await interaction.response.send_message(
        f"{user.display_name} is now active.", ephemeral=False
    )
    # await check_queue()

    # Notify user after 40 minutes
    await asyncio.sleep(RE_RACK_TIMER_DURATION)
    await interaction.channel.send(f"{user.mention}, the Re-rack timer has expired.")


@bot.slash_command(name="start", description="Start a game if you're next to ST")
async def start(interaction: nextcord.Interaction):
    user = interaction.user
    await start_user(interaction, user)


@bot.slash_command(name="forcestart", description="Force start a user")
async def forcestart(interaction: nextcord.Interaction, player: nextcord.Member):
    await start_user(interaction, player)


@tasks.loop(seconds=10)  # Adjust the interval as needed
async def check_queue():
    if not GAMES_RUNNING:
        return

    await lfst_tick()

    if MERGED:
        # Check for Active_ST in the merged queue
        for entry in active_storytellers.values():
            if is_active_storyteller(entry["Discord_ID"]):
                return

        # Find the first user in the merged queue
        sorted_queue = sorted(queue.values(), key=lambda x: x["Merged_Queue_Position"])
        if sorted_queue:
            user_id = sorted_queue[0]["Discord_ID"]

            if str(user_id) in queue:
                user = await bot.fetch_user(user_id)
                channel = bot.get_channel(MERGED_CHANNEL_ID)
                initial_merged_state = MERGED

                queue[str(user.id)]["QueueType"] = "Pickup"

                embed = nextcord.Embed(
                    title=f"Game Notification for {queue[str(user_id)]['QueueType']} Queue",
                    description=f"{user.mention}, it's your turn!",
                )
                embed.set_thumbnail(url=user.display_avatar.url)
                timeout_timestamp = int(time.time()) + TIMEOUT_TIMER
                embed.add_field(
                    name="Action Required",
                    value=f"Please choose to start or leave the queue. Timeout <t:{timeout_timestamp}:R>",
                    inline=False,
                )

                view = nextcord.ui.View()
                start_button = nextcord.ui.Button(
                    label="Start", style=nextcord.ButtonStyle.green
                )
                leave_button = nextcord.ui.Button(
                    label="Leave", style=nextcord.ButtonStyle.red
                )

                async def start_callback(interaction: nextcord.Interaction):
                    if interaction.user.id == user.id and str(user.id) in queue:
                        await start_user(interaction, user)
                        # await interaction.message.edit(view=None)
                    else:
                        await interaction.response.send_message(
                            "You are not authorized to use this button.", ephemeral=True
                        )

                async def leave_callback(interaction: nextcord.Interaction):
                    if interaction.user.id == user.id and str(user.id) in queue:
                        await leave_queue(interaction, user_id=user.id)
                        # await check_queue()
                        await interaction.message.edit(view=None)
                    else:
                        await interaction.response.send_message(
                            "You are not authorized to use this button.", ephemeral=True
                        )

                start_button.callback = start_callback
                leave_button.callback = leave_callback

                view.add_item(start_button)
                view.add_item(leave_button)

                await channel.send(embed=embed, view=view)
                await channel.send(f"{user.mention}, it's your turn!")
                try:
                    next_user_id = sorted_queue[1]["Discord_ID"]
                    next_user = await bot.fetch_user(next_user_id)
                    await channel.send(
                        f"{next_user.mention}, You are 2nd in the queue!"
                    )
                except:
                    await channel.send("Queue is empty after you.")

                for i in range(100):
                    if str(user.id) in queue and not is_active_storyteller(user.id):
                        await asyncio.sleep(TIMEOUT_TIMER / 100)  # Wait for 5 minutes
                    else:
                        return

                # Check if the user is still first in queue and the merge state has not changed
                if (
                    MERGED == initial_merged_state
                    and str(user.id) in queue
                    and not is_active_storyteller(user.id)
                    and GAMES_RUNNING is True
                ):
                    await channel.send(
                        f"{user.mention}, You did not reply in time, your space has been skipped"
                    )
                    await remove_queue(user_id=user.id)
                    # await check_queue()
    else:
        # Check for Active_ST in the beginner queue
        beginner_active_sts = [
            st
            for st in active_storytellers.values()
            if st["QueueType"] in ["Beginner", "Any"]
        ]
        if not beginner_active_sts:
            beginner_queue = [
                entry
                for entry in queue.values()
                if entry["QueueType"] in ["Beginner", "Any"]
            ]
            sorted_beginner_queue = sorted(
                beginner_queue, key=lambda x: x["Merged_Queue_Position"]
            )
            if sorted_beginner_queue:
                user_id = sorted_beginner_queue[0]["Discord_ID"]
                if str(user_id) in queue:
                    user = await bot.fetch_user(user_id)
                    channel = bot.get_channel(BEGINNER_CHANNEL_ID)
                    initial_merged_state = MERGED

                    queue[str(user.id)]["QueueType"] = "Beginner"

                    embed = nextcord.Embed(
                        title=f"Game Notification for {queue[str(user_id)]['QueueType']} Queue",
                        description=f"{user.mention}, it's your turn!",
                    )
                    embed.set_thumbnail(url=user.display_avatar.url)
                    timeout_timestamp = int(time.time()) + TIMEOUT_TIMER
                    embed.add_field(
                        name="Action Required",
                        value=f"Please choose to start or leave the queue. Timeout at <t:{timeout_timestamp}:R>",
                        inline=False,
                    )

                    view = nextcord.ui.View()
                    start_button = nextcord.ui.Button(
                        label="Start", style=nextcord.ButtonStyle.green
                    )
                    leave_button = nextcord.ui.Button(
                        label="Leave", style=nextcord.ButtonStyle.red
                    )

                    async def start_callback(interaction: nextcord.Interaction):
                        if interaction.user.id == user.id and str(user.id) in queue:
                            await start_user(interaction, user)
                            # await interaction.message.edit(view=None)
                        else:
                            await interaction.response.send_message(
                                "You are not authorized to use this button.",
                                ephemeral=True,
                            )

                    async def leave_callback(interaction: nextcord.Interaction):
                        if interaction.user.id == user.id and str(user.id) in queue:
                            await leave_queue(interaction, user_id=user.id)
                            # await interaction.message.edit(view=None)
                            # await check_queue()
                        else:
                            await interaction.response.send_message(
                                "You are not authorized to use this button.",
                                ephemeral=True,
                            )

                    start_button.callback = start_callback
                    leave_button.callback = leave_callback

                    view.add_item(start_button)
                    view.add_item(leave_button)

                    await channel.send(embed=embed, view=view)
                    await channel.send(f"{user.mention}, it's your turn!")

                    try:
                        next_user_id = sorted_beginner_queue[1]["Discord_ID"]
                        next_user = await bot.fetch_user(next_user_id)
                        await channel.send(
                            f"{next_user.mention}, You are 2nd in the queue!"
                        )
                    except:
                        await channel.send("Queue is empty after you.")

                    for i in range(100):
                        if str(user.id) in queue and not is_active_storyteller(user.id):
                            await asyncio.sleep(
                                TIMEOUT_TIMER / 100
                            )  # Wait for 5 minutes
                        else:
                            return

                    # Check if the user is still first in queue and the merge state has not changed
                    if (
                        MERGED == initial_merged_state
                        and str(user.id) in queue
                        and not is_active_storyteller(user.id)
                        and GAMES_RUNNING is True
                    ):
                        await channel.send(
                            f"{user.mention}, You did not reply in time, your space has been skipped"
                        )
                        await remove_queue(user_id=user.id)
                        # await check_queue()

        # Check for Active_ST in the pickup queue
        pickup_active_sts = [
            st
            for st in active_storytellers.values()
            if st["QueueType"] in ["Pickup", "Any"]
        ]
        if not pickup_active_sts:
            pickup_queue = [
                entry
                for entry in queue.values()
                if entry["QueueType"] in ["Pickup", "Any"]
            ]
            sorted_pickup_queue = sorted(
                pickup_queue, key=lambda x: x["Merged_Queue_Position"]
            )
            if sorted_pickup_queue:
                user_id = sorted_pickup_queue[0]["Discord_ID"]
                if str(user_id) in queue:
                    user = await bot.fetch_user(user_id)
                    channel = bot.get_channel(PICKUP_CHANNEL_ID)
                    initial_merged_state = MERGED

                    queue[str(user.id)]["QueueType"] = "Pickup"

                    embed = nextcord.Embed(
                        title=f"Game Notification for {queue[str(user_id)]['QueueType']} Queue",
                        description=f"{user.mention}, it's your turn!",
                    )
                    embed.set_thumbnail(url=user.display_avatar.url)
                    timeout_timestamp = int(time.time()) + TIMEOUT_TIMER
                    embed.add_field(
                        name="Action Required",
                        value=f"Please choose to start or leave the queue. Timeout at <t:{timeout_timestamp}:R>",
                        inline=False,
                    )

                    view = nextcord.ui.View()
                    start_button = nextcord.ui.Button(
                        label="Start", style=nextcord.ButtonStyle.green
                    )
                    leave_button = nextcord.ui.Button(
                        label="Leave", style=nextcord.ButtonStyle.red
                    )

                    async def start_callback(interaction: nextcord.Interaction):
                        if interaction.user.id == user.id and str(user.id) in queue:
                            await start_user(interaction, user)
                            # await interaction.message.edit(view=None)
                        else:
                            await interaction.response.send_message(
                                "You are not authorized to use this button.",
                                ephemeral=True,
                            )

                    async def leave_callback(interaction: nextcord.Interaction):
                        if interaction.user.id == user.id and str(user.id) in queue:
                            await leave_queue(interaction, user_id=user.id)
                            # await interaction.message.edit(view=None)
                            # await check_queue()
                        else:
                            await interaction.response.send_message(
                                "You are not authorized to use this button.",
                                ephemeral=True,
                            )

                    start_button.callback = start_callback
                    leave_button.callback = leave_callback

                    view.add_item(start_button)
                    view.add_item(leave_button)

                    await channel.send(embed=embed, view=view)
                    await channel.send(f"{user.mention}, it's your turn!")

                    try:
                        next_user_id = sorted_pickup_queue[1]["Discord_ID"]
                        next_user = await bot.fetch_user(next_user_id)
                        await channel.send(
                            f"{next_user.mention}, You are 2nd in the queue!"
                        )
                    except:
                        await channel.send("Queue is empty after you.")

                    for i in range(100):
                        if str(user.id) in queue and not is_active_storyteller(user.id):
                            await asyncio.sleep(
                                TIMEOUT_TIMER / 100
                            )  # Wait for 5 minutes
                        else:
                            return

                    # Check if the user is still first in queue and the merge state has not changed
                    if (
                        MERGED == initial_merged_state
                        and str(user.id) in queue
                        and not is_active_storyteller(user.id)
                        and GAMES_RUNNING is True
                    ):
                        await channel.send(
                            f"{user.mention}, You did not reply in time, your space has been skipped"
                        )
                        await remove_queue(user_id=user.id)
                        # await check_queue()


bot.load_extension("townsquare_spy.discord", extras=dict(db_path="townsquare.db"))

# Add other necessary commands and functionality as needed
bot.run(os.environ["DISCORD_TOKEN"])
